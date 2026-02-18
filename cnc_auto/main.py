"""
CNC Auto - Main Pipeline Orchestrator

Fully automated workflow:
  Camera → Image Processing → Vectorization → SVG → G-code → CNC

Supports two sender backends:
  - serial : Direct USB serial to GRBL controller
  - mqtt   : Chunked streaming to ESP32 over MQTT (default)

Usage:
  python main.py                        # Full pipeline (camera → CNC)
  python main.py --input image.png      # Skip camera, use existing image
  python main.py --dry-run              # Simulate without sending to CNC
  python main.py --sender serial        # Use serial instead of MQTT
  python main.py --skip-camera --input capture.png  # Process existing image
  python main.py --calibrate            # Run calibration check
  python main.py --list-ports           # List available serial ports
"""

import os
import sys
import argparse
import time
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from utils.helpers import load_json, ensure_dir
from utils.logger import setup_logger
from utils.calibration import CalibrationManager
from modules.camera import CameraCapture
from modules.image_processing import ImageProcessor
from modules.vectorize import Vectorizer
from modules.gcode_generator import GCodeGenerator
from modules.cnc_sender import CNCSender
from modules.mqtt_sender import MQTTSender


def _create_sender(config: dict, sender_mode: str = None):
    """
    Factory: return the appropriate sender based on config or override.

    Args:
        config: Full application config dict
        sender_mode: "serial" or "mqtt" (None = use config value)

    Returns:
        A CNCSender or MQTTSender instance
    """
    mode = sender_mode or config.get("sender_mode", "mqtt")
    if mode == "serial":
        return CNCSender(config)
    return MQTTSender(config)


class CNCAutoPipeline:
    """
    Main pipeline controller.
    Orchestrates the full Camera → Image → SVG → G-code → CNC workflow.
    """

    def __init__(
        self,
        config_path: str = None,
        jscut_path: str = None,
        sender_mode: str = None,
    ):
        # Load configuration
        if config_path is None:
            config_path = os.path.join(PROJECT_ROOT, "config.json")
        if jscut_path is None:
            jscut_path = os.path.join(PROJECT_ROOT, "jscut_settings.json")

        self.config = load_json(config_path)
        self.jscut_settings = load_json(jscut_path)
        self.logger = setup_logger("pipeline", self.config)

        # Resolve sender mode: CLI arg > config > default "mqtt"
        self.sender_mode = (
            sender_mode or self.config.get("sender_mode", "mqtt")
        )

        # Initialize modules
        self.camera = CameraCapture(self.config)
        self.processor = ImageProcessor(self.config)
        self.vectorizer = Vectorizer(self.config)
        self.gcode_gen = GCodeGenerator(self.config, self.jscut_settings)
        self.sender = _create_sender(self.config, self.sender_mode)
        self.calibration = CalibrationManager(self.config)

        # Pipeline state
        self.current_image = None
        self.processed_image = None
        self.svg_path = None
        self.gcode_path = None

        self.logger.info("=" * 60)
        self.logger.info("CNC Auto Pipeline initialized")
        self.logger.info(f"Config: {config_path}")
        self.logger.info(f"JSCut Settings: {jscut_path}")
        self.logger.info(f"Sender mode: {self.sender_mode}")
        self.logger.info("=" * 60)

    def run(
        self,
        input_image: str = None,
        skip_camera: bool = False,
        dry_run: bool = None,
        output_name: str = None
    ) -> bool:
        """
        Execute the full automation pipeline.

        Args:
            input_image: Path to existing image (skips camera capture)
            skip_camera: If True and no input_image, uses last capture
            dry_run: Override dry_run setting (None = use config)
            output_name: Custom output filename base

        Returns:
            True if pipeline completed successfully
        """
        start_time = time.time()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if output_name is None:
            output_name = f"job_{timestamp}"

        if dry_run is not None:
            # Set dry_run on whichever sender backend is active
            if self.sender_mode == "serial":
                self.config["serial"]["dry_run"] = dry_run
            else:
                self.config["mqtt"]["dry_run"] = dry_run
            self.sender = _create_sender(self.config, self.sender_mode)

        self.logger.info("=" * 60)
        self.logger.info(f"PIPELINE START: {output_name}")
        self.logger.info("=" * 60)

        try:
            # ──────────────────────────────────────────
            # STEP 1: Image Capture
            # ──────────────────────────────────────────
            if input_image and os.path.exists(input_image):
                self.logger.info(f"[1/5] Loading input image: {input_image}")
                import cv2
                self.current_image = cv2.imread(input_image)
                if self.current_image is None:
                    self.logger.error(f"Failed to load image: {input_image}")
                    return False
            elif not skip_camera:
                self.logger.info("[1/5] Capturing image from camera...")
                self.current_image = self._step_capture(output_name)
                if self.current_image is None:
                    return False
            else:
                self.logger.error("No input image and camera skipped")
                return False

            h, w = self.current_image.shape[:2]
            self.logger.info(f"Input image: {w}x{h} pixels")

            # ──────────────────────────────────────────
            # STEP 2: Image Processing
            # ──────────────────────────────────────────
            self.logger.info("[2/5] Processing image...")
            self.processed_image = self._step_process(output_name)
            if self.processed_image is None:
                return False

            # ──────────────────────────────────────────
            # STEP 3: Vectorization (Image → SVG)
            # ──────────────────────────────────────────
            self.logger.info("[3/5] Vectorizing to SVG...")
            self.svg_path = self._step_vectorize(output_name)
            if not self.svg_path:
                return False

            # ──────────────────────────────────────────
            # STEP 4: G-code Generation (SVG → G-code)
            # ──────────────────────────────────────────
            self.logger.info("[4/5] Generating G-code...")
            self.gcode_path = self._step_generate_gcode(output_name)
            if not self.gcode_path:
                return False

            # ──────────────────────────────────────────
            # STEP 5: Send to CNC
            # ──────────────────────────────────────────
            self.logger.info("[5/5] Sending to CNC...")
            success = self._step_send_gcode()

            elapsed = time.time() - start_time
            self.logger.info("=" * 60)
            if success:
                self.logger.info(f"PIPELINE COMPLETE in {elapsed:.1f}s")
            else:
                self.logger.error(f"PIPELINE FAILED after {elapsed:.1f}s")
            self.logger.info("=" * 60)

            return success

        except KeyboardInterrupt:
            self.logger.warning("Pipeline interrupted by user")
            self.sender.emergency_stop()
            return False
        except Exception as e:
            self.logger.error(f"Pipeline error: {e}", exc_info=True)
            return False
        finally:
            self.camera.close()

    def _step_capture(self, job_name: str):
        """Step 1: Capture image from camera."""
        import cv2

        try:
            with self.camera as cam:
                image = cam.capture(f"{job_name}_raw.png")
                if image is None:
                    self.logger.error("Camera capture failed")
                    return None

                # Auto-detect and crop work area
                image = cam.crop_to_work_area(image)

                # Save cropped image
                crop_path = os.path.join(
                    self.camera.capture_dir, f"{job_name}_cropped.png"
                )
                cv2.imwrite(crop_path, image)

                return image
        except Exception as e:
            self.logger.error(f"Camera error: {e}")
            return None

    def _step_process(self, job_name: str):
        """Step 2: Process image for vectorization."""
        import cv2

        try:
            # Resize to fit bed dimensions
            bed_w = self.config["machine"]["bed_size_x"]
            bed_h = self.config["machine"]["bed_size_y"]
            margin = self.config["gcode"].get("margin_mm", 5.0)
            px_per_inch = self.jscut_settings.get("svg", {}).get("px_per_inch", 96)

            resized = self.processor.resize_to_bed(
                self.current_image, bed_w, bed_h,
                dpi=px_per_inch, margin_mm=margin
            )

            # Run processing pipeline
            processed = self.processor.process(resized, mode="threshold")

            # Save processed image
            proc_path = os.path.join(
                self.processor.intermediate_dir, f"{job_name}_processed.png"
            )
            cv2.imwrite(proc_path, processed)

            return processed

        except Exception as e:
            self.logger.error(f"Image processing error: {e}")
            return None

    def _step_vectorize(self, job_name: str) -> str:
        """Step 3: Convert processed image to SVG."""
        try:
            svg_path = self.vectorizer.vectorize(
                self.processed_image, f"{job_name}.svg"
            )
            if svg_path and os.path.exists(svg_path):
                size = os.path.getsize(svg_path)
                self.logger.info(f"SVG created: {svg_path} ({size} bytes)")
                return svg_path
            else:
                self.logger.error("Vectorization produced no output")
                return ""
        except Exception as e:
            self.logger.error(f"Vectorization error: {e}")
            return ""

    def _step_generate_gcode(self, job_name: str) -> str:
        """Step 4: Generate G-code from SVG."""
        try:
            gcode_path = self.gcode_gen.generate(
                self.svg_path, f"{job_name}.gcode"
            )
            if gcode_path and os.path.exists(gcode_path):
                size = os.path.getsize(gcode_path)
                with open(gcode_path, "r") as f:
                    line_count = sum(1 for _ in f)
                self.logger.info(
                    f"G-code created: {gcode_path} "
                    f"({line_count} lines, {size} bytes)"
                )
                return gcode_path
            else:
                self.logger.error("G-code generation produced no output")
                return ""
        except Exception as e:
            self.logger.error(f"G-code generation error: {e}")
            return ""

    def _step_send_gcode(self) -> bool:
        """Step 5: Send G-code to CNC controller."""
        try:
            def progress_cb(sent, total):
                pct = (sent / total) * 100 if total > 0 else 0
                if sent % 50 == 0 or sent == total:
                    self.logger.info(f"Progress: {sent}/{total} ({pct:.1f}%)")

            if not self.sender.connect():
                self.logger.error("Failed to connect to CNC")
                return False

            success = self.sender.send_file(self.gcode_path, progress_cb)
            self.sender.disconnect()

            return success

        except Exception as e:
            self.logger.error(f"CNC send error: {e}")
            self.sender.disconnect()
            return False

    def calibrate(self) -> bool:
        """Run calibration validation and report."""
        self.logger.info("=" * 60)
        self.logger.info("CALIBRATION CHECK")
        self.logger.info("=" * 60)

        valid = self.calibration.validate()
        limits = self.calibration.get_bed_limits()
        origin = self.calibration.get_origin_offset()

        self.logger.info(f"Bed limits: {limits}")
        self.logger.info(f"Origin offset: {origin}")
        self.logger.info(f"Origin mode: {self.config['machine'].get('origin_mode')}")

        # JSCut settings summary
        tool = self.jscut_settings.get("tool", {})
        self.logger.info(f"\nJSCut Tool Settings:")
        self.logger.info(f"  Tool diameter: {tool.get('diameter')}mm")
        self.logger.info(f"  Cut rate: {tool.get('cut_rate')}mm/min")
        self.logger.info(f"  Plunge rate: {tool.get('plunge_rate')}mm/min")
        self.logger.info(f"  Rapid rate: {tool.get('rapid_rate')}mm/min")
        self.logger.info(f"  Pass depth: {tool.get('pass_depth')}mm")

        if valid:
            self.logger.info("\n✓ Calibration OK")
        else:
            self.logger.error("\n✗ Calibration has errors - fix config.json")

        return valid


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="CNC Auto - Fully Automated 3-Axis CNC Engraving System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                              Full pipeline (MQTT → ESP32)
  python main.py --input photo.png            Use existing image
  python main.py --sender serial              Use serial USB instead of MQTT
  python main.py --dry-run                    Simulate (no CNC send)
  python main.py --dry-run --input photo.png  Process image, simulate send
  python main.py --calibrate                  Check calibration
  python main.py --list-ports                 Show serial ports
        """
    )

    parser.add_argument(
        "--input", "-i",
        help="Path to input image (skips camera capture)"
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to config.json (default: ./config.json)"
    )
    parser.add_argument(
        "--jscut", "-j",
        default=None,
        help="Path to jscut_settings.json"
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output job name (default: auto-generated timestamp)"
    )
    parser.add_argument(
        "--sender", "-s",
        choices=["serial", "mqtt"],
        default=None,
        help="Sender backend: 'mqtt' (default) or 'serial'"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate G-code sending without CNC connection"
    )
    parser.add_argument(
        "--skip-camera",
        action="store_true",
        help="Skip camera capture step"
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Run calibration validation"
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List available serial ports"
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()

    # Handle --list-ports
    if args.list_ports:
        print("\nAvailable serial ports:")
        ports = CNCSender.list_ports()
        if ports:
            for p in ports:
                print(f"  {p}")
        else:
            print("  No serial ports found")
        return

    # Initialize pipeline
    pipeline = CNCAutoPipeline(
        config_path=args.config,
        jscut_path=args.jscut,
        sender_mode=args.sender,
    )

    # Handle --calibrate
    if args.calibrate:
        pipeline.calibrate()
        return

    # Determine input mode
    skip_camera = args.skip_camera or args.input is not None
    input_image = args.input

    # Handle --input with relative path
    if input_image and not os.path.isabs(input_image):
        input_image = os.path.join(os.getcwd(), input_image)

    # Run pipeline
    success = pipeline.run(
        input_image=input_image,
        skip_camera=skip_camera,
        dry_run=args.dry_run if args.dry_run else None,
        output_name=args.output
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

