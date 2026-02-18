"""
MQTT G-code Sender - Streams G-code to ESP32 over MQTT.

Protocol:
  - Chunks G-code into small batches (configurable lines per chunk)
  - Publishes each chunk on the command topic
  - Waits for an ACK from the ESP32 on the ack topic before sending next chunk
  - Supports JOB_START / CHUNK / JOB_END message types
  - Supports PAUSE, RESUME, ESTOP control commands
  - Dry-run mode for testing without a broker

Topics:
  cnc/gcode/command  ->  PC publishes TO the ESP32
  cnc/gcode/ack      <-  ESP32 publishes ACKs BACK to the PC
  cnc/control        ->  PC sends control commands (pause, resume, estop)
  cnc/status         <-  ESP32 reports status
"""

import os
import sys
import json
import time
import uuid
import threading
from typing import Dict, Any, Optional, Callable, List

try:
    import paho.mqtt.client as mqtt
except ImportError:
    raise ImportError(
        "paho-mqtt is required. Install with: pip install paho-mqtt"
    )

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.logger import setup_logger


class MQTTSender:
    """
    Streams G-code to an ESP32 over MQTT using chunked,
    ACK-based flow control.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.mqtt_cfg = config.get("mqtt", {})
        self.logger = setup_logger("mqtt_sender", config)

        # Broker settings
        self.broker_host = self.mqtt_cfg.get("broker_host", "mqtt.omkartigade.tech")
        self.broker_port = self.mqtt_cfg.get("broker_port", 443)
        self.transport = self.mqtt_cfg.get("transport", "websockets")
        self.ws_path = self.mqtt_cfg.get("ws_path", "/mqtt")
        self.use_tls = self.mqtt_cfg.get("use_tls", True)
        self.username = self.mqtt_cfg.get("username", "")
        self.password = self.mqtt_cfg.get("password", "")
        self.keepalive = self.mqtt_cfg.get("keepalive", 60)
        self.qos = self.mqtt_cfg.get("qos", 1)
        self.dry_run = self.mqtt_cfg.get("dry_run", False)

        # Topics
        self.topic_command = self.mqtt_cfg.get(
            "topic_command", "cnc/gcode/command"
        )
        self.topic_ack = self.mqtt_cfg.get("topic_ack", "cnc/gcode/ack")
        self.topic_control = self.mqtt_cfg.get(
            "topic_control", "cnc/control"
        )
        self.topic_status = self.mqtt_cfg.get("topic_status", "cnc/status")

        # Chunking settings
        self.lines_per_chunk = self.mqtt_cfg.get("lines_per_chunk", 10)
        self.ack_timeout = self.mqtt_cfg.get("ack_timeout", 30.0)
        self.retry_count = self.mqtt_cfg.get("retry_count", 3)

        # Internal state
        self.client: Optional[mqtt.Client] = None
        self._connected = False
        self._stop_event = threading.Event()
        self._ack_event = threading.Event()
        self._last_ack_seq = -1
        self._last_ack_status = ""
        self._last_ack_error = ""
        self._is_running = False
        self._progress_callback: Optional[Callable] = None
        self._esp32_status = "unknown"

        # Statistics
        self.lines_sent = 0
        self.lines_total = 0
        self.chunks_sent = 0
        self.chunks_total = 0
        self.errors: List[str] = []

    # ------------------------------------------------------------------ #
    #  Connection
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        """Connect to the MQTT broker and subscribe to ack/status topics."""
        if self.dry_run:
            self.logger.info("DRY RUN mode - no MQTT connection")
            self._connected = True
            return True

        try:
            client_id = f"cnc_sender_{uuid.uuid4().hex[:8]}"

            # Support both paho-mqtt v1 and v2
            try:
                self.client = mqtt.Client(
                    client_id=client_id,
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    transport=self.transport,
                )
            except (AttributeError, TypeError):
                self.client = mqtt.Client(
                    client_id=client_id,
                    transport=self.transport,
                )

            # WebSocket path (required for WSS brokers)
            if self.transport == "websockets":
                self.client.ws_set_options(path=self.ws_path)

            # TLS for secure connection
            if self.use_tls:
                import ssl
                self.client.tls_set(
                    cert_reqs=ssl.CERT_NONE,
                    tls_version=ssl.PROTOCOL_TLS,
                )
                self.client.tls_insecure_set(True)

            if self.username:
                self.client.username_pw_set(self.username, self.password)

            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message

            proto = "wss" if self.use_tls else "ws"
            self.logger.info(
                f"Connecting to MQTT broker "
                f"{proto}://{self.broker_host}:{self.broker_port}"
                f"{self.ws_path}..."
            )
            self.client.connect(
                self.broker_host, self.broker_port, self.keepalive
            )
            self.client.loop_start()

            # Wait for connection
            deadline = time.time() + 10.0
            while not self._connected and time.time() < deadline:
                time.sleep(0.1)

            if not self._connected:
                self.logger.error("MQTT connection timed out")
                return False

            self.logger.info("MQTT connected and subscribed")
            return True

        except Exception as e:
            self.logger.error(f"MQTT connection error: {e}")
            return False

    def disconnect(self) -> None:
        """Disconnect from the MQTT broker."""
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self._connected = False
        self.logger.info("MQTT disconnected")

    # ------------------------------------------------------------------ #
    #  MQTT callbacks
    # ------------------------------------------------------------------ #

    def _on_connect(self, client, userdata, flags, *args):
        """Called when connected to the broker."""
        rc = args[0] if args else 0
        if rc == 0:
            self._connected = True
            self.logger.info("MQTT broker connected")
            # Subscribe to ack and status topics
            client.subscribe(self.topic_ack, qos=self.qos)
            client.subscribe(self.topic_status, qos=self.qos)
            self.logger.info(
                f"Subscribed to {self.topic_ack}, {self.topic_status}"
            )
        else:
            self.logger.error(f"MQTT connect failed, rc={rc}")

    def _on_disconnect(self, client, userdata, *args):
        """Called when disconnected from the broker."""
        self._connected = False
        rc = args[-1] if args else 0
        if rc != 0:
            self.logger.warning(f"Unexpected MQTT disconnect, rc={rc}")

    def _on_message(self, client, userdata, msg):
        """Handle incoming ACK and status messages from ESP32."""
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.logger.warning(f"Bad message on {msg.topic}: {msg.payload}")
            return

        if msg.topic == self.topic_ack:
            self._handle_ack(payload)
        elif msg.topic == self.topic_status:
            self._handle_status(payload)

    def _handle_ack(self, payload: dict):
        """Process an ACK from the ESP32."""
        msg_type = payload.get("type", "")
        if msg_type == "ACK":
            self._last_ack_seq = payload.get("seq", -1)
            self._last_ack_status = payload.get("status", "")
            self._last_ack_error = payload.get("error", "")
            self.logger.debug(
                f"ACK seq={self._last_ack_seq} "
                f"status={self._last_ack_status}"
            )
            self._ack_event.set()

    def _handle_status(self, payload: dict):
        """Process a status update from the ESP32."""
        self._esp32_status = payload.get("status", "unknown")
        self.logger.info(f"ESP32 status: {self._esp32_status}")

    # ------------------------------------------------------------------ #
    #  Publishing helpers
    # ------------------------------------------------------------------ #

    def _publish(self, topic: str, payload: dict) -> bool:
        """Publish a JSON payload to a topic."""
        if self.dry_run:
            self.logger.debug(f"[DRY] {topic}: {json.dumps(payload)}")
            return True

        if not self.client or not self._connected:
            self.logger.error("Not connected to MQTT broker")
            return False

        try:
            msg = json.dumps(payload)
            info = self.client.publish(topic, msg, qos=self.qos)
            info.wait_for_publish(timeout=5.0)
            return True
        except Exception as e:
            self.logger.error(f"Publish error: {e}")
            return False

    def _wait_for_ack(self, expected_seq: int) -> bool:
        """
        Block until the ESP32 ACKs the given sequence number,
        or until timeout.
        """
        if self.dry_run:
            return True

        self._ack_event.clear()
        deadline = time.time() + self.ack_timeout

        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            if self._ack_event.wait(timeout=0.5):
                if self._last_ack_seq == expected_seq:
                    if self._last_ack_status == "ok":
                        return True
                    else:
                        self.logger.error(
                            f"ACK seq={expected_seq} returned error: "
                            f"{self._last_ack_error}"
                        )
                        return False
                # ACK for a different seq, keep waiting
                self._ack_event.clear()

        self.logger.error(f"ACK timeout for seq={expected_seq}")
        return False

    # ------------------------------------------------------------------ #
    #  File sending
    # ------------------------------------------------------------------ #

    def send_file(
        self,
        gcode_path: str,
        progress_callback: Optional[Callable] = None,
    ) -> bool:
        """
        Send a G-code file to the ESP32 via MQTT in chunks.

        Args:
            gcode_path: Path to the .gcode file
            progress_callback: Optional callback(lines_sent, total_lines)

        Returns:
            True if all chunks were sent and ACKed successfully
        """
        self._progress_callback = progress_callback
        self._stop_event.clear()
        self.errors = []
        self.lines_sent = 0
        self.chunks_sent = 0

        # ------ Read and clean G-code file ------
        with open(gcode_path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()

        gcode_lines: List[str] = []
        for line in raw_lines:
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            if ";" in line:
                line = line[: line.index(";")].strip()
            if line:
                gcode_lines.append(line)

        self.lines_total = len(gcode_lines)

        # ------ Build chunks ------
        chunks: List[List[str]] = []
        for i in range(0, len(gcode_lines), self.lines_per_chunk):
            chunks.append(gcode_lines[i : i + self.lines_per_chunk])
        self.chunks_total = len(chunks)

        self.logger.info(
            f"Sending {self.lines_total} G-code lines in "
            f"{self.chunks_total} chunks ({self.lines_per_chunk} lines/chunk) "
            f"from {gcode_path}"
        )

        if self.dry_run:
            return self._dry_run_send(chunks)

        self._is_running = True
        job_id = f"job_{uuid.uuid4().hex[:8]}"

        try:
            # ---- JOB_START ----
            if not self._publish(self.topic_command, {
                "type": "JOB_START",
                "job_id": job_id,
                "total_lines": self.lines_total,
                "total_chunks": self.chunks_total,
                "lines_per_chunk": self.lines_per_chunk,
            }):
                return False

            # Wait for ESP32 to acknowledge the job start
            # ESP32 sends ACK with seq=-1 for JOB_START
            if not self._wait_for_ack(-1):
                self.logger.error("ESP32 did not ACK JOB_START")
                return False

            self.logger.info(f"Job {job_id} started on ESP32")

            # ---- CHUNKS ----
            for seq, chunk in enumerate(chunks):
                if self._stop_event.is_set():
                    self.logger.warning("Send stopped by user")
                    return False

                sent_ok = False
                for attempt in range(1, self.retry_count + 1):
                    self.logger.debug(
                        f"Sending chunk {seq}/{self.chunks_total - 1} "
                        f"({len(chunk)} lines, attempt {attempt})"
                    )
                    if not self._publish(self.topic_command, {
                        "type": "CHUNK",
                        "seq": seq,
                        "lines": chunk,
                    }):
                        continue

                    if self._wait_for_ack(seq):
                        sent_ok = True
                        break
                    else:
                        self.logger.warning(
                            f"Retry chunk {seq} "
                            f"(attempt {attempt}/{self.retry_count})"
                        )

                if not sent_ok:
                    self.errors.append(
                        f"Chunk {seq} failed after {self.retry_count} retries"
                    )
                    self.logger.error(f"Chunk {seq} permanently failed")
                    return False

                self.chunks_sent = seq + 1
                self.lines_sent = min(
                    (seq + 1) * self.lines_per_chunk, self.lines_total
                )
                if self._progress_callback:
                    self._progress_callback(self.lines_sent, self.lines_total)

            # ---- JOB_END ----
            self._publish(self.topic_command, {
                "type": "JOB_END",
                "job_id": job_id,
            })

            self.logger.info(
                f"Job {job_id} complete: "
                f"{self.chunks_sent}/{self.chunks_total} chunks sent"
            )
            return True

        except Exception as e:
            self.logger.error(f"MQTT send failed: {e}")
            return False
        finally:
            self._is_running = False

    # ------------------------------------------------------------------ #
    #  Dry-run simulation
    # ------------------------------------------------------------------ #

    def _dry_run_send(self, chunks: List[List[str]]) -> bool:
        """Simulate sending G-code chunks (dry run mode)."""
        self.logger.info("=== DRY RUN - Simulating MQTT G-code send ===")
        self._is_running = True

        self.logger.info(
            f"[DRY] JOB_START total_lines={self.lines_total} "
            f"total_chunks={self.chunks_total}"
        )

        for seq, chunk in enumerate(chunks):
            if self._stop_event.is_set():
                self.logger.warning("Dry run stopped")
                self._is_running = False
                return False

            self.logger.debug(
                f"[DRY] CHUNK seq={seq} lines={len(chunk)}: "
                f"{chunk[0]}...{chunk[-1]}"
            )

            self.chunks_sent = seq + 1
            self.lines_sent = min(
                (seq + 1) * self.lines_per_chunk, self.lines_total
            )
            if self._progress_callback:
                self._progress_callback(self.lines_sent, self.lines_total)

            time.sleep(0.01)  # Simulate network latency

        self.logger.info(
            f"[DRY] JOB_END {self.chunks_sent}/{self.chunks_total} chunks"
        )
        self._is_running = False
        self.logger.info("=== DRY RUN complete ===")
        return True

    # ------------------------------------------------------------------ #
    #  Control commands
    # ------------------------------------------------------------------ #

    def emergency_stop(self) -> None:
        """Send an emergency-stop command to the ESP32."""
        self.logger.warning("!!! MQTT EMERGENCY STOP !!!")
        self._stop_event.set()
        self._publish(self.topic_control, {"type": "ESTOP"})

    def pause(self) -> None:
        """Pause the ESP32 job execution."""
        self.logger.info("Sending PAUSE")
        self._publish(self.topic_control, {"type": "PAUSE"})

    def resume(self) -> None:
        """Resume the ESP32 job execution."""
        self.logger.info("Sending RESUME")
        self._publish(self.topic_control, {"type": "RESUME"})

    # ------------------------------------------------------------------ #
    #  Properties
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def progress(self) -> float:
        if self.lines_total <= 0:
            return 0.0
        return (self.lines_sent / self.lines_total) * 100.0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False
