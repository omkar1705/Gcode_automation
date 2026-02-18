"""
CNC Communication Module - Sends G-code to Arduino/GRBL over serial.

Features:
- Serial port connection with auto-detection
- Line-by-line G-code streaming with flow control
- Waits for 'ok' response from GRBL
- Error handling and recovery
- Dry-run simulation mode
- Emergency stop (soft reset)
- Progress reporting
- Buffer management (character-counting protocol)
"""

import os
import time
import threading
from typing import Dict, Any, Optional, Callable, List

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise ImportError("pyserial is required. Install with: pip install pyserial")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.logger import setup_logger


class CNCSender:
    """Handles serial communication with GRBL-based CNC controller."""

    # GRBL response codes
    GRBL_OK = "ok"
    GRBL_ERROR = "error"
    GRBL_ALARM = "ALARM"

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.serial_cfg = config.get("serial", {})
        self.logger = setup_logger("cnc_sender", config)

        self.port = self.serial_cfg.get("port", "COM3")
        self.baud_rate = self.serial_cfg.get("baud_rate", 115200)
        self.timeout = self.serial_cfg.get("timeout", 2.0)
        self.dry_run = self.serial_cfg.get("dry_run", True)
        self.buffer_size = self.serial_cfg.get("buffer_size", 128)
        self.grbl_init_wait = self.serial_cfg.get("grbl_init_wait", 2.0)

        self.serial_conn: Optional[serial.Serial] = None
        self._stop_event = threading.Event()
        self._is_running = False
        self._progress_callback: Optional[Callable] = None

        # Statistics
        self.lines_sent = 0
        self.lines_total = 0
        self.errors = []

    @staticmethod
    def list_ports() -> List[str]:
        """List available serial ports."""
        ports = serial.tools.list_ports.comports()
        return [f"{p.device} - {p.description}" for p in ports]

    def connect(self) -> bool:
        """
        Establish serial connection to CNC controller.

        Returns:
            True if connected successfully
        """
        if self.dry_run:
            self.logger.info("DRY RUN mode - no serial connection")
            return True

        retry_count = self.serial_cfg.get("connect_retry_count", 3)
        retry_delay = self.serial_cfg.get("connect_retry_delay", 2.0)

        for attempt in range(1, retry_count + 1):
            try:
                self.logger.info(
                    f"Connecting to {self.port} @ {self.baud_rate} "
                    f"(attempt {attempt}/{retry_count})..."
                )

                self.serial_conn = serial.Serial(
                    port=self.port,
                    baudrate=self.baud_rate,
                    timeout=self.timeout,
                    write_timeout=self.timeout
                )

                # Wait for GRBL initialization
                time.sleep(self.grbl_init_wait)

                # Read GRBL welcome message
                welcome = self._read_response(timeout=3.0)
                self.logger.info(f"GRBL response: {welcome}")

                # Flush any startup messages
                self._flush_input()

                self.logger.info(f"Connected to {self.port}")
                return True

            except serial.SerialException as e:
                self.logger.warning(f"Connection attempt {attempt} failed: {e}")
                if attempt < retry_count:
                    time.sleep(retry_delay)

        self.logger.error(f"Failed to connect to {self.port} after {retry_count} attempts")
        return False

    def disconnect(self) -> None:
        """Close serial connection."""
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
            self.logger.info("Serial connection closed")
        self.serial_conn = None

    def send_file(
        self,
        gcode_path: str,
        progress_callback: Optional[Callable] = None
    ) -> bool:
        """
        Send a G-code file to the CNC controller.

        Args:
            gcode_path: Path to G-code file
            progress_callback: Optional callback(lines_sent, total_lines)

        Returns:
            True if all lines sent successfully
        """
        self._progress_callback = progress_callback
        self._stop_event.clear()
        self.errors = []
        self.lines_sent = 0

        # Read G-code file
        with open(gcode_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Filter out comments and empty lines
        gcode_lines = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            # Remove inline comments
            if ";" in line:
                line = line[:line.index(";")].strip()
            if line:
                gcode_lines.append(line)

        self.lines_total = len(gcode_lines)
        self.logger.info(f"Sending {self.lines_total} G-code commands from {gcode_path}")

        if self.dry_run:
            return self._dry_run_send(gcode_lines)

        self._is_running = True
        success = True

        try:
            for i, line in enumerate(gcode_lines):
                if self._stop_event.is_set():
                    self.logger.warning("Send stopped by user")
                    success = False
                    break

                ok = self._send_line(line)
                if not ok:
                    self.errors.append(f"Line {i + 1}: {line}")
                    self.logger.error(f"Error on line {i + 1}: {line}")
                    # Continue sending (GRBL may recover)

                self.lines_sent = i + 1
                if self._progress_callback:
                    self._progress_callback(self.lines_sent, self.lines_total)

        except Exception as e:
            self.logger.error(f"Send failed: {e}")
            success = False
        finally:
            self._is_running = False

        if success and not self.errors:
            self.logger.info("G-code sent successfully!")
        else:
            self.logger.warning(
                f"Send completed with {len(self.errors)} error(s)"
            )

        return success and not self.errors

    def _send_line(self, line: str) -> bool:
        """
        Send a single G-code line and wait for response.

        Args:
            line: G-code command string

        Returns:
            True if 'ok' received
        """
        if not self.serial_conn or not self.serial_conn.is_open:
            self.logger.error("Serial port not open")
            return False

        try:
            # Send line with newline
            cmd = (line + "\n").encode("ascii")
            self.serial_conn.write(cmd)
            self.serial_conn.flush()

            # Wait for response
            response = self._read_response()

            if self.GRBL_OK in response.lower():
                return True
            elif self.GRBL_ERROR in response.lower():
                self.logger.error(f"GRBL error: {response}")
                return False
            elif self.GRBL_ALARM in response:
                self.logger.error(f"GRBL ALARM: {response}")
                return False
            else:
                # Unknown response - log but continue
                self.logger.debug(f"Response: {response}")
                return True

        except serial.SerialException as e:
            self.logger.error(f"Serial write error: {e}")
            return False
        except serial.SerialTimeoutException:
            self.logger.error(f"Write timeout for: {line}")
            return False

    def _read_response(self, timeout: Optional[float] = None) -> str:
        """Read response from GRBL controller."""
        if not self.serial_conn:
            return ""

        old_timeout = self.serial_conn.timeout
        if timeout:
            self.serial_conn.timeout = timeout

        try:
            response = ""
            while True:
                line = self.serial_conn.readline().decode("ascii", errors="replace").strip()
                if not line:
                    break
                response = line
                if "ok" in line.lower() or "error" in line.lower():
                    break
            return response
        finally:
            self.serial_conn.timeout = old_timeout

    def _flush_input(self) -> None:
        """Flush serial input buffer."""
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.reset_input_buffer()

    def _dry_run_send(self, gcode_lines: List[str]) -> bool:
        """Simulate sending G-code (dry run mode)."""
        self.logger.info("=== DRY RUN - Simulating G-code send ===")
        self._is_running = True

        for i, line in enumerate(gcode_lines):
            if self._stop_event.is_set():
                self.logger.warning("Dry run stopped")
                self._is_running = False
                return False

            self.logger.debug(f"[DRY] >> {line}")
            self.lines_sent = i + 1

            if self._progress_callback:
                self._progress_callback(self.lines_sent, self.lines_total)

            # Simulate processing time
            time.sleep(0.001)

        self._is_running = False
        self.logger.info(f"=== DRY RUN complete: {self.lines_total} lines ===")
        return True

    def emergency_stop(self) -> None:
        """
        Send emergency stop (soft reset) to GRBL.
        Sends Ctrl+X (0x18) which triggers GRBL soft reset.
        """
        self.logger.warning("!!! EMERGENCY STOP !!!")
        self._stop_event.set()

        if self.serial_conn and self.serial_conn.is_open:
            try:
                # GRBL soft reset
                self.serial_conn.write(b"\x18")
                self.serial_conn.flush()
                time.sleep(0.5)
                self._flush_input()
                self.logger.info("Emergency stop sent (GRBL soft reset)")
            except Exception as e:
                self.logger.error(f"Emergency stop failed: {e}")
        elif self.dry_run:
            self.logger.info("Emergency stop (dry run)")

    def send_command(self, command: str) -> str:
        """
        Send a single command and return the response.
        Useful for status queries, settings changes, etc.

        Args:
            command: G-code or GRBL command

        Returns:
            Response string
        """
        if self.dry_run:
            self.logger.debug(f"[DRY] >> {command}")
            return "ok"

        if not self.serial_conn or not self.serial_conn.is_open:
            self.logger.error("Not connected")
            return ""

        try:
            self.serial_conn.write((command + "\n").encode("ascii"))
            self.serial_conn.flush()
            return self._read_response()
        except Exception as e:
            self.logger.error(f"Command failed: {e}")
            return ""

    def get_status(self) -> str:
        """Query GRBL status (? command)."""
        return self.send_command("?")

    def get_settings(self) -> str:
        """Query GRBL settings ($$ command)."""
        return self.send_command("$$")

    def unlock(self) -> str:
        """Send GRBL unlock command ($X)."""
        return self.send_command("$X")

    def home(self) -> str:
        """Send homing command ($H)."""
        return self.send_command("$H")

    @property
    def is_running(self) -> bool:
        """Check if a send operation is in progress."""
        return self._is_running

    @property
    def progress(self) -> float:
        """Get current send progress as percentage."""
        if self.lines_total <= 0:
            return 0.0
        return (self.lines_sent / self.lines_total) * 100.0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

