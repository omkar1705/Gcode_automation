/*
 * ESP32 G-code Receiver over MQTT (WSS)
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
// The ESP-IDF client takes a full URI: wss://host:port/path
const char* MQTT_URI      = "wss://mqtt.omkartigade.tech:443/mqtt";
const char* MQTT_USER     = "omkar";
const char* MQTT_PASS     = "omkar";

// MQTT topics (must match config.json on the PC side)
const char* TOPIC_COMMAND = "cnc/gcode/command";
const char* TOPIC_ACK     = "cnc/gcode/ack";
const char* TOPIC_CONTROL = "cnc/control";
const char* TOPIC_STATUS  = "cnc/status";

// ═══════════════════════════════════════════════════════════
//  GLOBALS
// ═══════════════════════════════════════════════════════════

esp_mqtt_client_handle_t mqttClient = NULL;
bool    mqttConnected = false;

// Job state
bool    jobActive    = false;
bool    paused       = false;
int     totalLines   = 0;
int     totalChunks  = 0;
int     linesExec    = 0;
int     chunksExec   = 0;
String  currentJobId = "";

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

void mqttPublish(const char* topic, const char* payload) {
    if (mqttClient && mqttConnected) {
        esp_mqtt_client_publish(mqttClient, topic, payload, 0, 1, 0);
    }
}

void sendAck(int seq, const char* status, const char* error = "") {
    JsonDocument doc;
    doc["type"]       = "ACK";
    doc["seq"]        = seq;
    doc["status"]     = status;
    if (strlen(error) > 0) {
        doc["error"] = error;
    }
    doc["lines_exec"] = linesExec;

    char buffer[256];
    serializeJson(doc, buffer, sizeof(buffer));
    mqttPublish(TOPIC_ACK, buffer);

    Serial.print("[ACK] seq=");
    Serial.print(seq);
    Serial.print(" status=");
    Serial.println(status);
}

void sendStatus(const char* status) {
    JsonDocument doc;
    doc["status"]      = status;
    doc["job_id"]      = currentJobId;
    doc["lines_exec"]  = linesExec;
    doc["chunks_exec"] = chunksExec;

    char buffer[256];
    serializeJson(doc, buffer, sizeof(buffer));
    mqttPublish(TOPIC_STATUS, buffer);
}

// ═══════════════════════════════════════════════════════════
//  MESSAGE HANDLERS
// ═══════════════════════════════════════════════════════════

void handleJobStart(JsonDocument& doc) {
    currentJobId = doc["job_id"].as<String>();
    totalLines   = doc["total_lines"] | 0;
    totalChunks  = doc["total_chunks"] | 0;
    linesExec    = 0;
    chunksExec   = 0;
    jobActive    = true;
    paused       = false;

    Serial.println("========================================");
    Serial.print("[JOB START] id=");
    Serial.print(currentJobId);
    Serial.print("  lines=");
    Serial.print(totalLines);
    Serial.print("  chunks=");
    Serial.println(totalChunks);
    Serial.println("========================================");

    sendStatus("running");
    sendAck(-1, "ok");
}

void handleChunk(JsonDocument& doc) {
    if (!jobActive) {
        Serial.println("[WARN] Chunk received but no active job");
        return;
    }

    int seq = doc["seq"] | -1;
    JsonArray lines = doc["lines"].as<JsonArray>();
    int lineCount = lines.size();

    Serial.print("[CHUNK ");
    Serial.print(seq);
    Serial.print("] ");
    Serial.print(lineCount);
    Serial.println(" lines:");

    for (int i = 0; i < lineCount; i++) {
        const char* gcodeLine = lines[i];
        if (gcodeLine) {
            Serial.print("  >> ");
            Serial.println(gcodeLine);

            // FUTURE: Forward to GRBL on Serial2:
            //   Serial2.println(gcodeLine);
            //   // wait for "ok" from GRBL...

            linesExec++;
        }
    }

    chunksExec++;

    Serial.print("[CHUNK ");
    Serial.print(seq);
    Serial.print("] Done. Progress: ");
    Serial.print(linesExec);
    Serial.print("/");
    Serial.println(totalLines);

    sendAck(seq, "ok");
}

void handleJobEnd(JsonDocument& doc) {
    String jobId = doc["job_id"].as<String>();

    Serial.println("========================================");
    Serial.print("[JOB END] id=");
    Serial.print(jobId);
    Serial.print("  lines_executed=");
    Serial.print(linesExec);
    Serial.print("/");
    Serial.println(totalLines);
    Serial.println("========================================");

    jobActive = false;
    sendStatus("idle");
}

void handleControl(JsonDocument& doc) {
    String type = doc["type"].as<String>();

    if (type == "PAUSE") {
        Serial.println("[CONTROL] PAUSE");
        paused = true;
        sendStatus("paused");
    }
    else if (type == "RESUME") {
        Serial.println("[CONTROL] RESUME");
        paused = false;
        sendStatus("running");
    }
    else if (type == "ESTOP") {
        Serial.println("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!");
        Serial.println("[CONTROL] EMERGENCY STOP");
        Serial.println("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!");
        paused    = false;
        jobActive = false;
        linesExec = 0;
        sendStatus("estop");
    }
}

// ═══════════════════════════════════════════════════════════
//  MQTT MESSAGE DISPATCHER
// ═══════════════════════════════════════════════════════════

void processMessage(const char* topic, const char* data, int dataLen) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, data, dataLen);
    if (err) {
        Serial.print("[MQTT] JSON parse error: ");
        Serial.println(err.c_str());
        return;
    }

    String topicStr = String(topic);
    String msgType  = doc["type"].as<String>();

    if (topicStr == TOPIC_COMMAND) {
        if (msgType == "JOB_START") {
            handleJobStart(doc);
        } else if (msgType == "CHUNK") {
            handleChunk(doc);
        } else if (msgType == "JOB_END") {
            handleJobEnd(doc);
        } else {
            Serial.print("[MQTT] Unknown command: ");
            Serial.println(msgType);
        }
    }
    else if (topicStr == TOPIC_CONTROL) {
        handleControl(doc);
    }
}

// ═══════════════════════════════════════════════════════════
//  ESP-IDF MQTT EVENT HANDLER
// ═══════════════════════════════════════════════════════════

// Buffer to reassemble messages that arrive in fragments
static char msgBuffer[4096];
static int  msgBufferLen = 0;
static char msgTopic[128];

static void mqttEventHandler(void* args, esp_event_base_t base,
                             int32_t event_id, void* event_data) {
    esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)event_data;

    switch (event->event_id) {

        case MQTT_EVENT_CONNECTED:
            Serial.println("[MQTT] Connected!");
            mqttConnected = true;

            // Subscribe to command and control topics (QoS 1)
            esp_mqtt_client_subscribe(mqttClient, TOPIC_COMMAND, 1);
            esp_mqtt_client_subscribe(mqttClient, TOPIC_CONTROL, 1);
            Serial.print("[MQTT] Subscribed to: ");
            Serial.print(TOPIC_COMMAND);
            Serial.print(", ");
            Serial.println(TOPIC_CONTROL);

            sendStatus("idle");
            break;

        case MQTT_EVENT_DISCONNECTED:
            Serial.println("[MQTT] Disconnected");
            mqttConnected = false;
            break;

        case MQTT_EVENT_DATA:
            // Topic is only sent in the first fragment of a message
            if (event->topic_len > 0) {
                int tLen = event->topic_len < (int)sizeof(msgTopic) - 1
                           ? event->topic_len : (int)sizeof(msgTopic) - 1;
                memcpy(msgTopic, event->topic, tLen);
                msgTopic[tLen] = '\0';
                msgBufferLen = 0;  // reset buffer for new message
            }

            // Append data fragment to buffer
            if (msgBufferLen + event->data_len < (int)sizeof(msgBuffer) - 1) {
                memcpy(msgBuffer + msgBufferLen, event->data, event->data_len);
                msgBufferLen += event->data_len;
                msgBuffer[msgBufferLen] = '\0';
            }

            // Check if this is the last fragment (current_data_offset + data_len == total_data_len)
            if (event->current_data_offset + event->data_len >= event->total_data_len) {
                processMessage(msgTopic, msgBuffer, msgBufferLen);
                msgBufferLen = 0;
            }
            break;

        case MQTT_EVENT_ERROR:
            Serial.print("[MQTT] Error type: ");
            if (event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
                Serial.print("TCP/TLS, esp-tls err=");
                Serial.print(event->error_handle->esp_tls_last_esp_err);
                Serial.print(" tls_stack=");
                Serial.println(event->error_handle->esp_tls_stack_err);
            } else {
                Serial.println(event->error_handle->error_type);
            }
            break;

        default:
            break;
    }
}

// ═══════════════════════════════════════════════════════════
//  MQTT INIT (WSS via ESP-IDF native client)
// ═══════════════════════════════════════════════════════════

void connectMQTT() {
    Serial.print("[MQTT] Connecting to ");
    Serial.println(MQTT_URI);

    esp_mqtt_client_config_t config = {};
    config.broker.address.uri          = MQTT_URI;
    config.credentials.username        = MQTT_USER;
    config.credentials.authentication.password = MQTT_PASS;
    config.broker.verification.crt_bundle_attach = esp_crt_bundle_attach;
    config.buffer.size                 = 4096;       // RX buffer for chunks
    config.buffer.out_size             = 512;        // TX buffer for ACKs
    config.network.disable_auto_reconnect = false;
    config.session.keepalive           = 60;

    mqttClient = esp_mqtt_client_init(&config);
    esp_mqtt_client_register_event(mqttClient, MQTT_EVENT_ANY,
                                   mqttEventHandler, NULL);
    esp_mqtt_client_start(mqttClient);
}

// ═══════════════════════════════════════════════════════════
//  ARDUINO SETUP & LOOP
// ═══════════════════════════════════════════════════════════

void setup() {
    Serial.begin(115200);
    delay(1000);

    Serial.println();
    Serial.println("========================================");
    Serial.println("  ESP32 G-code Receiver (MQTT over WSS)");
    Serial.println("========================================");
    Serial.println();

    setupWiFi();
    connectMQTT();

    Serial.println();
    Serial.println("[READY] Waiting for G-code...");
    Serial.println();
}

void loop() {
    // ESP-IDF MQTT client runs in its own FreeRTOS task,
    // so loop() just needs to yield.
    delay(10);
}
