"""
MQTT G-code Test Script

Sends dummy G-code data over MQTT to verify the connection
between this PC and the ESP32.

Usage:
  python test_mqtt.py              # Send dummy gcode, wait for ESP32 ACKs
  python test_mqtt.py --listen     # Just listen (see what ESP32 sends back)

Requirements:
  pip install paho-mqtt
"""

import json
import time
import uuid
import argparse
import threading

import ssl
import paho.mqtt.client as mqtt

# ── Load config from config.json ──
CONFIG_PATH = "cnc_auto/config.json"
with open(CONFIG_PATH, "r") as f:
    cfg = json.load(f)["mqtt"]

BROKER      = cfg["broker_host"]
PORT        = cfg["broker_port"]
TRANSPORT   = cfg.get("transport", "websockets")
WS_PATH     = cfg.get("ws_path", "/mqtt")
USE_TLS     = cfg.get("use_tls", True)
USER        = cfg["username"]
PASS        = cfg["password"]
QOS         = cfg["qos"]
TOPIC_CMD    = cfg["topic_command"]
TOPIC_ACK    = cfg["topic_ack"]
TOPIC_STATUS = cfg["topic_status"]
TOPIC_CTRL   = cfg["topic_control"]
LINES_PER_CHUNK = cfg["lines_per_chunk"]
ACK_TIMEOUT     = cfg["ack_timeout"]
RETRY_COUNT     = cfg.get("retry_count", 3)

# ── Dummy G-code (a small engraving job) ──
DUMMY_GCODE = [
    "G90",                     # Absolute positioning
    "G21",                     # Millimeters
    "G17",                     # XY plane
    "G0 Z5.000",              # Raise to safe Z
    "G0 X0.000 Y0.000",      # Go to origin
    "G0 X10.000 Y10.000",    # Rapid to start
    "G1 Z-1.000 F100",       # Plunge
    "G1 X20.000 Y10.000 F500",  # Cut line 1
    "G1 X20.000 Y20.000 F500",  # Cut line 2
    "G1 X10.000 Y20.000 F500",  # Cut line 3
    "G1 X10.000 Y10.000 F500",  # Cut line 4 (close square)
    "G0 Z5.000",              # Retract
    "G0 X30.000 Y10.000",    # Move to next shape
    "G1 Z-1.000 F100",       # Plunge
    "G1 X40.000 Y10.000 F500",
    "G1 X35.000 Y20.000 F500",  # Triangle point
    "G1 X30.000 Y10.000 F500",  # Close triangle
    "G0 Z5.000",              # Retract
    "G0 X0.000 Y0.000",      # Return home
    "M5",                      # Spindle off
    "M2",                      # Program end
]

# ── State ──
ack_event = threading.Event()
last_ack_seq = -999
last_ack_status = ""
connected_event = threading.Event()
esp32_online_event = threading.Event()


def on_connect(client, userdata, flags, *args):
    # Compatible with paho-mqtt v1 (rc) and v2 (rc, properties)
    rc = args[0] if args else 0
    if rc == 0:
        print(f"[OK] Connected to {BROKER}:{PORT}")
        client.subscribe(TOPIC_ACK, qos=QOS)
        client.subscribe(TOPIC_STATUS, qos=QOS)
        print(f"[OK] Subscribed to {TOPIC_ACK}, {TOPIC_STATUS}")
        connected_event.set()
    else:
        print(f"[FAIL] Connection refused, rc={rc}")


def on_message(client, userdata, msg):
    global last_ack_seq, last_ack_status

    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        print(f"[RECV] {topic}: {msg.payload}")
        return

    if topic == TOPIC_ACK:
        seq = payload.get("seq", "?")
        status = payload.get("status", "?")
        lines_exec = payload.get("lines_exec", "?")
        print(f"  [ACK] seq={seq}  status={status}  lines_exec={lines_exec}")
        last_ack_seq = seq
        last_ack_status = status
        ack_event.set()

    elif topic == TOPIC_STATUS:
        print(f"  [STATUS] {json.dumps(payload)}")
        esp32_online_event.set()


def wait_for_ack(expected_seq, timeout=None):
    """Block until we get an ACK for the expected sequence number."""
    if timeout is None:
        timeout = ACK_TIMEOUT
    ack_event.clear()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ack_event.wait(timeout=0.5):
            if last_ack_seq == expected_seq:
                return last_ack_status == "ok"
            ack_event.clear()
    return False


def send_dummy_gcode(client):
    """Send the dummy G-code in chunks, waiting for ACK after each."""
    job_id = f"test_{uuid.uuid4().hex[:6]}"
    total_lines = len(DUMMY_GCODE)

    # Build chunks
    chunks = []
    for i in range(0, total_lines, LINES_PER_CHUNK):
        chunks.append(DUMMY_GCODE[i : i + LINES_PER_CHUNK])
    total_chunks = len(chunks)

    print()
    print("=" * 55)
    print(f"  SENDING DUMMY G-CODE")
    print(f"  Job ID       : {job_id}")
    print(f"  Total lines  : {total_lines}")
    print(f"  Chunk size   : {LINES_PER_CHUNK} lines")
    print(f"  Total chunks : {total_chunks}")
    print(f"  Broker       : {BROKER}:{PORT}")
    print(f"  Command topic: {TOPIC_CMD}")
    print("=" * 55)
    print()

    # ── JOB_START (with retry) ──
    job_start = {
        "type": "JOB_START",
        "job_id": job_id,
        "total_lines": total_lines,
        "total_chunks": total_chunks,
        "lines_per_chunk": LINES_PER_CHUNK,
    }

    job_started = False
    for attempt in range(1, RETRY_COUNT + 1):
        print(f"[SEND] JOB_START  (attempt {attempt}/{RETRY_COUNT}, waiting for ACK...)")
        client.publish(TOPIC_CMD, json.dumps(job_start), qos=QOS)

        if wait_for_ack(-1):
            job_started = True
            break
        print(f"[WARN] No ACK for JOB_START (attempt {attempt}/{RETRY_COUNT})")

    if not job_started:
        print("[FAIL] No ACK for JOB_START after all retries. Is the ESP32 running?")
        return False

    print(f"[OK] ESP32 acknowledged JOB_START")
    print()

    # ── CHUNKS (with retry) ──
    for seq, chunk in enumerate(chunks):
        chunk_msg = {
            "type": "CHUNK",
            "seq": seq,
            "lines": chunk,
        }
        lines_preview = chunk[0] if chunk else "?"

        chunk_ok = False
        for attempt in range(1, RETRY_COUNT + 1):
            print(f"[SEND] CHUNK seq={seq}  ({len(chunk)} lines, first: {lines_preview}, attempt {attempt}/{RETRY_COUNT})")
            client.publish(TOPIC_CMD, json.dumps(chunk_msg), qos=QOS)

            if wait_for_ack(seq):
                chunk_ok = True
                break
            print(f"[WARN] No ACK for chunk {seq} (attempt {attempt}/{RETRY_COUNT})")

        if not chunk_ok:
            print(f"[FAIL] Chunk {seq} failed after {RETRY_COUNT} retries. Aborting.")
            return False

        print(f"[OK] Chunk {seq} acknowledged")

    print()

    # ── JOB_END ──
    job_end = {"type": "JOB_END", "job_id": job_id}
    print(f"[SEND] JOB_END")
    client.publish(TOPIC_CMD, json.dumps(job_end), qos=QOS)

    print()
    print("=" * 55)
    print(f"  TEST PASSED — all {total_lines} lines sent & ACKed")
    print("=" * 55)
    return True


def listen_only(client):
    """Just subscribe and print everything — useful for debugging."""
    print()
    print("=" * 55)
    print("  LISTEN MODE — printing all messages from ESP32")
    print(f"  Subscribed to: {TOPIC_ACK}, {TOPIC_STATUS}")
    print("  Press Ctrl+C to stop")
    print("=" * 55)
    print()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")


def main():
    parser = argparse.ArgumentParser(description="Test MQTT G-code sending")
    parser.add_argument(
        "--listen", action="store_true",
        help="Listen-only mode (don't send, just print ESP32 messages)"
    )
    args = parser.parse_args()

    # ── Connect ──
    client_id = f"test_pc_{uuid.uuid4().hex[:6]}"

    # Support both paho-mqtt v1 and v2
    try:
        client = mqtt.Client(
            client_id=client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            transport=TRANSPORT,
        )
    except (AttributeError, TypeError):
        client = mqtt.Client(
            client_id=client_id,
            transport=TRANSPORT,
        )

    # WebSocket path
    if TRANSPORT == "websockets":
        client.ws_set_options(path=WS_PATH)

    # TLS for WSS
    if USE_TLS:
        client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS)
        client.tls_insecure_set(True)

    if USER:
        client.username_pw_set(USER, PASS)

    client.on_connect = on_connect
    client.on_message = on_message

    proto = "wss" if USE_TLS else "ws"
    print(f"[....] Connecting to {proto}://{BROKER}:{PORT}{WS_PATH} as {client_id}...")
    try:
        client.connect(BROKER, PORT, keepalive=60)
    except Exception as e:
        print(f"[FAIL] Could not connect: {e}")
        return

    client.loop_start()

    # Wait for connection
    if not connected_event.wait(timeout=10):
        print("[FAIL] Connection timed out")
        client.loop_stop()
        return

    # ── Run mode ──
    if args.listen:
        listen_only(client)
    else:
        # Wait for the ESP32 to be online (it sends a status on connect)
        print("[....] Waiting for ESP32 to be online...")
        if esp32_online_event.wait(timeout=15):
            print("[OK] ESP32 is online")
            time.sleep(0.5)  # let subscriptions settle
            send_dummy_gcode(client)
        else:
            print("[WARN] No ESP32 status received yet, sending anyway...")
            send_dummy_gcode(client)

    client.loop_stop()
    client.disconnect()
    print("[DONE] Disconnected.")


if __name__ == "__main__":
    main()
