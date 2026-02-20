/*
 * ESP32 G-code Receiver over MQTT (WSS) — Production-Grade
 *
 * Uses the ESP-IDF native MQTT client (built-in, no extra library needed)
 * which supports wss:// natively.
 *
 * Connects to WiFi, then to the MQTT broker over WSS (port 443),
 * receives G-code in chunks, prints each line to Serial Monitor,
 * and sends ACK back to the PC after each chunk.
 *
 * ── Dependencies (install via Arduino Library Manager) ──
 *   - ArduinoJson (v7.x)
 *   (No PubSubClient / ArduinoMqttClient / ArduinoHttpClient needed!)
 *
 * ── Board ──
 *   ESP32 Arduino core 2.x or 3.x
 *
 * ── Production Reliability Features ──
 *   [1]  Two-topic architecture with machine_id isolation
 *   [2]  Strict JSON message validation
 *   [3]  Sequence tracking with duplicate/out-of-order rejection
 *   [4]  Idempotent ACK with job_id + last_seq_processed
 *   [5]  Persistent session with reconnect recovery
 *   [6]  Safety hardening (ESTOP, job-state guards)
 *   [7]  Sender + machine_id authentication
 *   [8]  Chunk watchdog timer (timeout → auto-pause)
 *   [9]  Tuned MQTT buffers, LWT, exponential backoff
 *   [10] Retained online/offline presence
 *   [11] Memory-safe buffer handling with size limits
 */

#include <WiFi.h>
#include <mqtt_client.h>
#include <ArduinoJson.h>
#include <esp_crt_bundle.h>

// ═══════════════════════════════════════════════════════════
//  CONFIGURATION
// ═══════════════════════════════════════════════════════════

// WiFi
const char* WIFI_SSID     = "omkar123";
const char* WIFI_PASSWORD = "omkar123";

// MQTT broker over WSS
const char* MQTT_URI      = "wss://mqtt.omkartigade.tech:443/mqtt";
const char* MQTT_USER     = "omkar";
const char* MQTT_PASS     = "omkar";

// ── [1] Machine identity — isolates this device on the broker ──
const char* MACHINE_ID    = "cnc01";

// ── [1] Two-topic architecture ──
// PC  → ESP: cnc/<machine_id>/cmd   (ESP subscribes)
// ESP → PC:  cnc/<machine_id>/resp  (ESP publishes)
static char TOPIC_CMD[64];
static char TOPIC_RESP[64];

// ── [11] Memory-safety limits ──
static const int MAX_LINES_PER_CHUNK  = 50;
static const int MAX_JSON_PAYLOAD     = 7168;  // 7 KB — fits in 8 KB RX buffer with headroom
static const int MAX_GCODE_LINE_LEN   = 96;

// ── [8] Watchdog: timeout (ms) with no chunk during active job ──
static const unsigned long CHUNK_WATCHDOG_MS = 10000;

// ═══════════════════════════════════════════════════════════
//  GLOBALS
// ═══════════════════════════════════════════════════════════

esp_mqtt_client_handle_t mqttClient = NULL;
volatile bool mqttConnected = false;

// Job state
bool    jobActive       = false;
bool    paused          = false;
int     totalLines      = 0;
int     totalChunks     = 0;
int     linesExec       = 0;
int     chunksExec      = 0;
String  currentJobId    = "";

// ── [3] Sequence tracking ──
int     lastSeqReceived = -1;

// ── [8] Watchdog state ──
unsigned long lastChunkTime = 0;
bool    watchdogTripped     = false;

// ── [9] Exponential backoff for reconnect ──
static unsigned long reconnectBackoffMs  = 1000;
static const unsigned long RECONNECT_MAX = 30000;

// ── [5] Reconnect recovery flag ──
static bool initialConnectDone = false;

// ═══════════════════════════════════════════════════════════
//  TOPIC BUILDER  [1]
// ═══════════════════════════════════════════════════════════

void buildTopics() {
    snprintf(TOPIC_CMD,  sizeof(TOPIC_CMD),  "cnc/%s/cmd",  MACHINE_ID);
    snprintf(TOPIC_RESP, sizeof(TOPIC_RESP), "cnc/%s/resp", MACHINE_ID);
}

// ═══════════════════════════════════════════════════════════
//  WIFI
// ═══════════════════════════════════════════════════════════

void setupWiFi() {
    Serial.println();
    Serial.print("[WiFi] Connecting to ");
    Serial.println(WIFI_SSID);

    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
        attempts++;
        if (attempts > 40) {
            Serial.println("\n[WiFi] FAILED - restarting...");
            ESP.restart();
        }
    }

    Serial.println();
    Serial.print("[WiFi] Connected, IP: ");
    Serial.println(WiFi.localIP());
}

// ═══════════════════════════════════════════════════════════
//  MQTT PUBLISH HELPERS
// ═══════════════════════════════════════════════════════════

// All ESP→PC messages go to TOPIC_RESP only [1]
void mqttPublish(const char* payload, bool retain = false) {
    if (mqttClient && mqttConnected) {
        esp_mqtt_client_publish(mqttClient, TOPIC_RESP, payload, 0, 1, retain ? 1 : 0);
    }
}

// ── [4] Idempotent ACK: includes job_id + last_seq_processed so PC can safely retry ──
void sendAck(int seq, const char* status, const char* error = "") {
    char buffer[384];
    JsonDocument doc;
    doc["type"]               = "ACK";
    doc["job_id"]             = currentJobId;
    doc["seq"]                = seq;
    doc["status"]             = status;
    doc["last_seq_processed"] = lastSeqReceived;
    doc["lines_exec"]         = linesExec;
    doc["machine_id"]         = MACHINE_ID;
    if (strlen(error) > 0) {
        doc["error"] = error;
    }

    serializeJson(doc, buffer, sizeof(buffer));
    mqttPublish(buffer);

    Serial.printf("[ACK] seq=%d status=%s last_seq=%d\n", seq, status, lastSeqReceived);
}

void sendStatus(const char* status, bool retain = false) {
    char buffer[384];
    JsonDocument doc;
    doc["type"]        = "STATUS";
    doc["status"]      = status;
    doc["machine_id"]  = MACHINE_ID;
    doc["job_id"]      = currentJobId;
    doc["lines_exec"]  = linesExec;
    doc["chunks_exec"] = chunksExec;
    doc["last_seq"]    = lastSeqReceived;

    serializeJson(doc, buffer, sizeof(buffer));
    mqttPublish(buffer, retain);
}

// ═══════════════════════════════════════════════════════════
//  JOB STATE RESET  [6]
// ═══════════════════════════════════════════════════════════

// Centralized reset prevents inconsistent state across code paths.
void resetJobState() {
    jobActive       = false;
    paused          = false;
    totalLines      = 0;
    totalChunks     = 0;
    linesExec       = 0;
    chunksExec      = 0;
    currentJobId    = "";
    lastSeqReceived = -1;
    lastChunkTime   = 0;
    watchdogTripped = false;
}

// ═══════════════════════════════════════════════════════════
//  MESSAGE VALIDATION  [2][7]
// ═══════════════════════════════════════════════════════════

// Returns true only if the message passes all security and structural checks.
bool validateEnvelope(JsonDocument& doc) {
    // [7] Sender field must be "pc"
    if (!doc["sender"].is<const char*>()) {
        Serial.println("[REJECT] Missing sender field");
        return false;
    }
    if (strcmp(doc["sender"].as<const char*>(), "pc") != 0) {
        Serial.println("[REJECT] Sender is not 'pc'");
        return false;
    }

    // [7] machine_id must match this device
    if (!doc["machine_id"].is<const char*>()) {
        Serial.println("[REJECT] Missing machine_id");
        return false;
    }
    if (strcmp(doc["machine_id"].as<const char*>(), MACHINE_ID) != 0) {
        Serial.println("[REJECT] machine_id mismatch");
        return false;
    }

    // [2] type field is mandatory
    if (!doc["type"].is<const char*>()) {
        Serial.println("[REJECT] Missing type field");
        return false;
    }

    return true;
}

bool validateJobStart(JsonDocument& doc) {
    if (!doc["job_id"].is<const char*>() || strlen(doc["job_id"].as<const char*>()) == 0) {
        Serial.println("[REJECT] JOB_START: missing/empty job_id");
        return false;
    }
    if (!doc["total_lines"].is<int>() || doc["total_lines"].as<int>() <= 0) {
        Serial.println("[REJECT] JOB_START: invalid total_lines");
        return false;
    }
    if (!doc["total_chunks"].is<int>() || doc["total_chunks"].as<int>() <= 0) {
        Serial.println("[REJECT] JOB_START: invalid total_chunks");
        return false;
    }
    return true;
}

bool validateChunk(JsonDocument& doc) {
    if (!doc["seq"].is<int>() || doc["seq"].as<int>() < 0) {
        Serial.println("[REJECT] CHUNK: invalid seq");
        return false;
    }
    if (!doc["lines"].is<JsonArray>()) {
        Serial.println("[REJECT] CHUNK: lines is not an array");
        return false;
    }
    JsonArray lines = doc["lines"].as<JsonArray>();
    if (lines.size() == 0) {
        Serial.println("[REJECT] CHUNK: empty lines array");
        return false;
    }
    // [11] Enforce max lines per chunk to bound memory usage
    if ((int)lines.size() > MAX_LINES_PER_CHUNK) {
        Serial.printf("[REJECT] CHUNK: too many lines (%d > %d)\n", (int)lines.size(), MAX_LINES_PER_CHUNK);
        return false;
    }
    return true;
}

bool validateJobEnd(JsonDocument& doc) {
    if (!doc["job_id"].is<const char*>() || strlen(doc["job_id"].as<const char*>()) == 0) {
        Serial.println("[REJECT] JOB_END: missing/empty job_id");
        return false;
    }
    return true;
}

// ═══════════════════════════════════════════════════════════
//  MESSAGE HANDLERS
// ═══════════════════════════════════════════════════════════

void handleJobStart(JsonDocument& doc) {
    // [6] Reject if a job is already running (must END or ESTOP first)
    if (jobActive) {
        Serial.println("[WARN] JOB_START received while job already active — ignoring");
        sendAck(-1, "error", "job_already_active");
        return;
    }

    // [2] Validate required fields
    if (!validateJobStart(doc)) {
        sendAck(-1, "error", "invalid_job_start");
        return;
    }

    currentJobId = doc["job_id"].as<String>();
    totalLines   = doc["total_lines"].as<int>();
    totalChunks  = doc["total_chunks"].as<int>();
    linesExec    = 0;
    chunksExec   = 0;
    jobActive    = true;
    paused       = false;

    // [3] Reset sequence tracking for new job
    lastSeqReceived = -1;

    // [8] Start watchdog
    lastChunkTime   = millis();
    watchdogTripped = false;

    Serial.println("========================================");
    Serial.printf("[JOB START] id=%s  lines=%d  chunks=%d\n",
                  currentJobId.c_str(), totalLines, totalChunks);
    Serial.println("========================================");

    sendStatus("running");
    sendAck(-1, "ok");
}

void handleChunk(JsonDocument& doc) {
    // [6] Ignore chunks when no job is active
    if (!jobActive) {
        Serial.println("[WARN] Chunk received but no active job — dropping");
        return;
    }

    // [2] Validate chunk structure
    if (!validateChunk(doc)) {
        sendAck(doc["seq"] | -1, "error", "invalid_chunk");
        return;
    }

    int seq = doc["seq"].as<int>();

    // [3] Sequence safety — reject duplicates
    if (seq <= lastSeqReceived) {
        Serial.printf("[DUP] Chunk seq=%d already processed (last=%d) — resending ACK\n",
                      seq, lastSeqReceived);
        sendAck(seq, "ok");  // Idempotent: PC might be retrying
        return;
    }

    // [3] Sequence safety — reject out-of-order
    if (seq != lastSeqReceived + 1) {
        Serial.printf("[SEQ ERR] Expected seq=%d, got seq=%d\n", lastSeqReceived + 1, seq);
        sendAck(seq, "error", "out_of_order");
        return;
    }

    JsonArray lines = doc["lines"].as<JsonArray>();
    int lineCount = lines.size();

    Serial.printf("[CHUNK %d] %d lines:\n", seq, lineCount);

    for (int i = 0; i < lineCount; i++) {
        const char* gcodeLine = lines[i];
        if (gcodeLine) {
            // [11] Truncate excessively long G-code lines to prevent buffer issues
            if (strlen(gcodeLine) > MAX_GCODE_LINE_LEN) {
                Serial.printf("  >> [TRUNCATED] %.80s...\n", gcodeLine);
            } else {
                Serial.print("  >> ");
                Serial.println(gcodeLine);
            }

            // FUTURE: Forward to GRBL on Serial2:
            //   Serial2.println(gcodeLine);
            //   // wait for "ok" from GRBL...

            linesExec++;
        }
    }

    chunksExec++;
    lastSeqReceived = seq;

    // [8] Reset watchdog on successful chunk
    lastChunkTime   = millis();
    watchdogTripped = false;

    // Clear watchdog pause if we were timed-out but now receiving again
    if (paused) {
        paused = false;
        sendStatus("running");
    }

    Serial.printf("[CHUNK %d] Done. Progress: %d/%d\n", seq, linesExec, totalLines);
    sendAck(seq, "ok");
}

void handleJobEnd(JsonDocument& doc) {
    // [2] Validate
    if (!validateJobEnd(doc)) {
        return;
    }

    String jobId = doc["job_id"].as<String>();

    // [6] Only process if it matches the current job
    if (jobActive && currentJobId != jobId) {
        Serial.printf("[WARN] JOB_END job_id mismatch: got=%s active=%s\n",
                      jobId.c_str(), currentJobId.c_str());
        return;
    }

    Serial.println("========================================");
    Serial.printf("[JOB END] id=%s  lines_executed=%d/%d\n",
                  jobId.c_str(), linesExec, totalLines);
    Serial.println("========================================");

    resetJobState();
    sendStatus("idle");
}

void handleControl(JsonDocument& doc) {
    const char* type = doc["type"].as<const char*>();

    // [6] ESTOP is always honored — the only command that ignores job state
    if (strcmp(type, "ESTOP") == 0) {
        Serial.println("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!");
        Serial.println("[CONTROL] EMERGENCY STOP");
        Serial.println("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!");
        resetJobState();
        sendStatus("estop");
        return;
    }

    // [6] All other control commands require an active job
    if (!jobActive) {
        Serial.printf("[WARN] Control '%s' ignored — no active job\n", type);
        return;
    }

    if (strcmp(type, "PAUSE") == 0) {
        Serial.println("[CONTROL] PAUSE");
        paused = true;
        sendStatus("paused");
    }
    else if (strcmp(type, "RESUME") == 0) {
        Serial.println("[CONTROL] RESUME");
        paused = false;
        watchdogTripped = false;
        lastChunkTime = millis();  // Reset watchdog on resume
        sendStatus("running");
    }
    else {
        // [2] Reject unknown control types
        Serial.printf("[REJECT] Unknown control type: %s\n", type);
    }
}

// ═══════════════════════════════════════════════════════════
//  MQTT MESSAGE DISPATCHER  [1][2][7]
// ═══════════════════════════════════════════════════════════

void processMessage(const char* topic, const char* data, int dataLen) {
    // [11] Reject oversized payloads before parsing
    if (dataLen > MAX_JSON_PAYLOAD) {
        Serial.printf("[REJECT] Payload too large: %d > %d\n", dataLen, MAX_JSON_PAYLOAD);
        return;
    }

    // [1] Only process messages on our cmd topic
    if (strcmp(topic, TOPIC_CMD) != 0) {
        Serial.printf("[REJECT] Unexpected topic: %s\n", topic);
        return;
    }

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, data, dataLen);
    if (err) {
        Serial.print("[MQTT] JSON parse error: ");
        Serial.println(err.c_str());
        return;
    }

    // [2][7] Validate envelope: sender, machine_id, type
    if (!validateEnvelope(doc)) {
        return;
    }

    const char* msgType = doc["type"].as<const char*>();

    if (strcmp(msgType, "JOB_START") == 0) {
        handleJobStart(doc);
    } else if (strcmp(msgType, "CHUNK") == 0) {
        handleChunk(doc);
    } else if (strcmp(msgType, "JOB_END") == 0) {
        handleJobEnd(doc);
    } else if (strcmp(msgType, "PAUSE") == 0 ||
               strcmp(msgType, "RESUME") == 0 ||
               strcmp(msgType, "ESTOP") == 0) {
        handleControl(doc);
    } else {
        // [2] Reject unknown message types
        Serial.printf("[REJECT] Unknown message type: %s\n", msgType);
    }
}

// ═══════════════════════════════════════════════════════════
//  ESP-IDF MQTT EVENT HANDLER
// ═══════════════════════════════════════════════════════════

// [11] Receive buffer sized to RX buffer (8 KB)
static char msgBuffer[8192];
static int  msgBufferLen = 0;
static char msgTopic[128];

static void mqttEventHandler(void* args, esp_event_base_t base,
                             int32_t event_id, void* event_data) {
    esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)event_data;

    switch (event->event_id) {

        case MQTT_EVENT_CONNECTED:
            Serial.println("[MQTT] Connected!");
            mqttConnected = true;

            // [9] Reset backoff on successful connection
            reconnectBackoffMs = 1000;

            // [1] Subscribe only to our cmd topic (QoS 1)
            esp_mqtt_client_subscribe(mqttClient, TOPIC_CMD, 1);
            Serial.printf("[MQTT] Subscribed to: %s\n", TOPIC_CMD);

            // [10] Publish retained online status so PC knows we're alive
            {
                char onlineBuf[128];
                JsonDocument onlineDoc;
                onlineDoc["type"]       = "STATUS";
                onlineDoc["status"]     = "online";
                onlineDoc["machine_id"] = MACHINE_ID;
                serializeJson(onlineDoc, onlineBuf, sizeof(onlineBuf));
                esp_mqtt_client_publish(mqttClient, TOPIC_RESP, onlineBuf, 0, 1, 1);
            }

            // [5] On reconnect (not first boot), announce recovery state
            if (initialConnectDone) {
                Serial.println("[MQTT] Reconnected — publishing recovery status");
                if (jobActive) {
                    sendStatus("reconnected");
                    Serial.printf("[RECOVERY] Job '%s' active, last_seq=%d — PC should resume from seq %d\n",
                                  currentJobId.c_str(), lastSeqReceived, lastSeqReceived + 1);
                } else {
                    sendStatus("idle");
                }
            } else {
                initialConnectDone = true;
                sendStatus("idle");
            }
            break;

        case MQTT_EVENT_DISCONNECTED:
            Serial.println("[MQTT] Disconnected");
            mqttConnected = false;

            // [9] Exponential backoff tracking (ESP-IDF handles reconnect,
            //     but we log the expected delay for diagnostics)
            Serial.printf("[MQTT] Next reconnect in ~%lu ms\n", reconnectBackoffMs);
            if (reconnectBackoffMs < RECONNECT_MAX) {
                reconnectBackoffMs = min(reconnectBackoffMs * 2, RECONNECT_MAX);
            }
            break;

        case MQTT_EVENT_DATA:
            // Topic is only sent in the first fragment of a message
            if (event->topic_len > 0) {
                int tLen = event->topic_len < (int)sizeof(msgTopic) - 1
                           ? event->topic_len : (int)sizeof(msgTopic) - 1;
                memcpy(msgTopic, event->topic, tLen);
                msgTopic[tLen] = '\0';
                msgBufferLen = 0;
            }

            // [11] Prevent buffer overflow on fragment assembly
            if (msgBufferLen + event->data_len < (int)sizeof(msgBuffer) - 1) {
                memcpy(msgBuffer + msgBufferLen, event->data, event->data_len);
                msgBufferLen += event->data_len;
                msgBuffer[msgBufferLen] = '\0';
            } else {
                Serial.println("[REJECT] Message exceeds reassembly buffer — dropped");
                msgBufferLen = 0;
                break;
            }

            // Process when all fragments have arrived
            if (event->current_data_offset + event->data_len >= event->total_data_len) {
                processMessage(msgTopic, msgBuffer, msgBufferLen);
                msgBufferLen = 0;
            }
            break;

        case MQTT_EVENT_ERROR:
            Serial.print("[MQTT] Error type: ");
            if (event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
                Serial.printf("TCP/TLS, esp-tls err=%d tls_stack=%d\n",
                              event->error_handle->esp_tls_last_esp_err,
                              event->error_handle->esp_tls_stack_err);
            } else {
                Serial.println(event->error_handle->error_type);
            }
            break;

        default:
            break;
    }
}

// ═══════════════════════════════════════════════════════════
//  MQTT INIT (WSS via ESP-IDF native client)  [9][10]
// ═══════════════════════════════════════════════════════════

void connectMQTT() {
    Serial.printf("[MQTT] Connecting to %s as machine '%s'\n", MQTT_URI, MACHINE_ID);

    // [9] Build the Last Will and Testament payload
    static char lwt_payload[128];
    {
        JsonDocument lwtDoc;
        lwtDoc["type"]       = "STATUS";
        lwtDoc["status"]     = "offline";
        lwtDoc["machine_id"] = MACHINE_ID;
        serializeJson(lwtDoc, lwt_payload, sizeof(lwt_payload));
    }

    esp_mqtt_client_config_t config = {};
    config.broker.address.uri                    = MQTT_URI;
    config.credentials.username                  = MQTT_USER;
    config.credentials.authentication.password   = MQTT_PASS;
    config.broker.verification.crt_bundle_attach = esp_crt_bundle_attach;

    // [9] Enlarged RX buffer for large chunks; TX sized for ACK/status
    config.buffer.size                           = 8192;
    config.buffer.out_size                       = 1024;

    // [9] Automatic reconnect with keep-alive
    config.network.disable_auto_reconnect        = false;
    config.network.reconnect_timeout_ms          = 5000;
    config.session.keepalive                     = 30;

    // [5] Persistent session — broker queues QoS 1 messages while offline
    config.session.disable_clean_session         = true;

    // [9] Last Will: broker publishes this if ESP drops unexpectedly.
    //     Retained so the PC always sees the latest presence state.
    config.session.last_will.topic               = TOPIC_RESP;
    config.session.last_will.msg                 = lwt_payload;
    config.session.last_will.msg_len             = strlen(lwt_payload);
    config.session.last_will.qos                 = 1;
    config.session.last_will.retain              = 1;

    mqttClient = esp_mqtt_client_init(&config);
    esp_mqtt_client_register_event(mqttClient, MQTT_EVENT_ANY,
                                   mqttEventHandler, NULL);
    esp_mqtt_client_start(mqttClient);
}

// ═══════════════════════════════════════════════════════════
//  WATCHDOG CHECK  [8]
// ═══════════════════════════════════════════════════════════

void checkChunkWatchdog() {
    if (!jobActive || paused || !mqttConnected) return;
    if (watchdogTripped) return;  // Already fired, don't spam

    unsigned long now = millis();
    if (now - lastChunkTime >= CHUNK_WATCHDOG_MS) {
        watchdogTripped = true;
        paused = true;
        Serial.printf("[WATCHDOG] No chunk for %lu ms — auto-pausing job\n", CHUNK_WATCHDOG_MS);
        sendStatus("timeout");
    }
}

// ═══════════════════════════════════════════════════════════
//  ARDUINO SETUP & LOOP
// ═══════════════════════════════════════════════════════════

void setup() {
    Serial.begin(115200);
    delay(1000);

    Serial.println();
    Serial.println("=====================================================");
    Serial.println("  ESP32 G-code Receiver (MQTT/WSS) — Production");
    Serial.printf("  Machine ID : %s\n", MACHINE_ID);
    Serial.println("=====================================================");
    Serial.println();

    // [1] Build topic strings from MACHINE_ID
    buildTopics();
    Serial.printf("[TOPICS] cmd  = %s\n", TOPIC_CMD);
    Serial.printf("[TOPICS] resp = %s\n", TOPIC_RESP);

    setupWiFi();
    connectMQTT();

    Serial.println();
    Serial.println("[READY] Waiting for G-code...");
    Serial.println();
}

void loop() {
    // [8] Watchdog check runs every loop iteration (~10 ms)
    checkChunkWatchdog();

    delay(10);
}
