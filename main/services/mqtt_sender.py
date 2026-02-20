import json

import time

import uuid

import threading

import ssl

import paho.mqtt.client as mqtt

import os





# -- Load config --

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")

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

MACHINE_ID  = cfg["machine_id"]

LINES_PER_CHUNK = cfg["lines_per_chunk"]

ACK_TIMEOUT     = cfg["ack_timeout"]

RETRY_COUNT     = cfg.get("retry_count", 3)



TOPIC_CMD  = f"cnc/{MACHINE_ID}/cmd"

TOPIC_RESP = f"cnc/{MACHINE_ID}/resp"



# â”€â”€ State â”€â”€

ack_event = threading.Event()

connected_event = threading.Event()

esp_online_event = threading.Event()



last_ack_seq = None

last_ack_status = None





def build_message(msg_type, **kwargs):

    msg = {

        "type": msg_type,

        "sender": "pc",

        "machine_id": MACHINE_ID,

    }

    msg.update(kwargs)

    return json.dumps(msg)





def on_connect(client, userdata, flags, *args):

    rc = args[0] if args else 0

    if rc == 0:

        client.subscribe(TOPIC_RESP, qos=QOS)

        connected_event.set()

    else:

        raise Exception(f"MQTT connection failed rc={rc}")





def on_message(client, userdata, msg):

    global last_ack_seq, last_ack_status



    try:

        payload = json.loads(msg.payload.decode())

    except Exception:

        return



    msg_type = payload.get("type", "")



    if msg_type == "STATUS":

        status = payload.get("status", "")

        if status in ("online", "idle", "running", "reconnected"):

            esp_online_event.set()



    elif msg_type == "ACK":

        last_ack_seq = payload.get("seq")

        last_ack_status = payload.get("status")

        ack_event.set()





def wait_for_ack(expected_seq):

    ack_event.clear()

    deadline = time.time() + ACK_TIMEOUT



    while time.time() < deadline:

        if ack_event.wait(timeout=0.5):

            if last_ack_seq == expected_seq:

                return last_ack_status == "ok"

            ack_event.clear()



    return False





def create_client():

    client_id = f"backend_{uuid.uuid4().hex[:6]}"



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



    if TRANSPORT == "websockets":

        client.ws_set_options(path=WS_PATH)



    if USE_TLS:

        client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS)

        client.tls_insecure_set(True)



    if USER:

        client.username_pw_set(USER, PASS)



    client.on_connect = on_connect

    client.on_message = on_message



    return client





def send_gcode_job(gcode_lines):

    client = create_client()

    client.connect(BROKER, PORT, keepalive=60)

    client.loop_start()



    if not connected_event.wait(timeout=10):

        client.loop_stop()

        raise Exception("MQTT connection timeout")



    # Wait for ESP online

    if not esp_online_event.wait(timeout=15):

        client.loop_stop()

        raise Exception("ESP32 not online")



    job_id = f"job_{uuid.uuid4().hex[:6]}"

    total_lines = len(gcode_lines)



    chunks = [

        gcode_lines[i:i + LINES_PER_CHUNK]

        for i in range(0, total_lines, LINES_PER_CHUNK)

    ]



    # â”€â”€ JOB_START â”€â”€

    job_start_payload = build_message(

        "JOB_START",

        job_id=job_id,

        total_lines=total_lines,

        total_chunks=len(chunks),

        lines_per_chunk=LINES_PER_CHUNK,

    )



    for _ in range(RETRY_COUNT):

        client.publish(TOPIC_CMD, job_start_payload, qos=QOS)

        if wait_for_ack(-1):

            break

    else:

        client.loop_stop()

        raise Exception("JOB_START failed (no ACK)")



    # â”€â”€ CHUNKS â”€â”€

    for seq, chunk in enumerate(chunks):

        chunk_payload = build_message(

            "CHUNK",

            job_id=job_id,

            seq=seq,

            lines=chunk,

        )



        for _ in range(RETRY_COUNT):

            client.publish(TOPIC_CMD, chunk_payload, qos=QOS)

            if wait_for_ack(seq):

                break

        else:

            client.loop_stop()

            raise Exception(f"Chunk {seq} failed")



    # â”€â”€ JOB_END â”€â”€

    job_end_payload = build_message("JOB_END", job_id=job_id)

    client.publish(TOPIC_CMD, job_end_payload, qos=QOS)



    client.loop_stop()

    client.disconnect()



    return job_id