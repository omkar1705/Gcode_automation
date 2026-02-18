"""
Camera Module - Captures images from USB camera for CNC engraving.

Features:
- USB camera capture via OpenCV
- Auto work-area detection
- Configurable resolution
- Image saving with timestamps
"""

import os
import time
import numpy as np
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

try:
    import cv2
except ImportError:
    raise ImportError("OpenCV is required. Install with: pip install opencv-python")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.logger import setup_logger
from utils.helpers import ensure_dir


class CameraCapture:
    """Handles USB camera image capture for CNC workflow."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.cam_cfg = config.get("camera", {})
        self.logger = setup_logger("camera", config)

        self.device_index = self.cam_cfg.get("device_index", 0)
        self.resolution = (
            self.cam_cfg.get("resolution_width", 1920),
            self.cam_cfg.get("resolution_height", 1080)
        )
        self.warmup_frames = self.cam_cfg.get("warmup_frames", 30)
        self.capture_dir = ensure_dir(self.cam_cfg.get("capture_dir", "captures"))
        self.cap = None

    def open(self) -> bool:
        """Open the camera device."""
        self.logger.info(f"Opening camera device {self.device_index}...")
        self.cap = cv2.VideoCapture(self.device_index, cv2.CAP_DSHOW)

        if not self.cap.isOpened():
            # Fallback without DirectShow
            self.cap = cv2.VideoCapture(self.device_index)

        if not self.cap.isOpened():
            self.logger.error(f"Failed to open camera device {self.device_index}")
            return False

        # Set resolution
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])

        # Warm up camera (auto-exposure adjustment)
        self.logger.info(f"Warming up camera ({self.warmup_frames} frames)...")
        for _ in range(self.warmup_frames):
            self.cap.read()

        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.logger.info(f"Camera ready at {actual_w}x{actual_h}")
        return True

    def capture(self, filename: Optional[str] = None) -> Optional[np.ndarray]:
        """
        Capture a single frame from the camera.

        Args:
            filename: Optional filename to save. Auto-generated if None.

        Returns:
            Captured image as numpy array, or None on failure.
        """
        if self.cap is None or not self.cap.isOpened():
            if not self.open():
                return None

        ret, frame = self.cap.read()
        if not ret or frame is None:
            self.logger.error("Failed to capture frame")
            return None

        # Save the captured image
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"capture_{timestamp}.png"

        filepath = os.path.join(self.capture_dir, filename)
        cv2.imwrite(filepath, frame)
        self.logger.info(f"Image captured and saved: {filepath}")

        return frame

    def detect_work_area(self, image: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """
        Auto-detect the work area in the captured image.
        Looks for the largest rectangular contour (assumed to be the workpiece).

        Args:
            image: Input image (BGR)

        Returns:
            Tuple (x, y, w, h) of detected work area, or None
        """
        if not self.cam_cfg.get("auto_detect_workarea", True):
            h, w = image.shape[:2]
            return (0, 0, w, h)

        self.logger.info("Detecting work area...")

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)

        # Dilate to close gaps
        kernel = np.ones((5, 5), np.uint8)
        dilated = cv2.dilate(edges, kernel, iterations=2)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            self.logger.warning("No contours found, using full image")
            h, w = image.shape[:2]
            return (0, 0, w, h)

        # Find the largest contour by area
        largest = max(contours, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(largest)

        # Minimum area threshold (at least 10% of image)
        img_area = image.shape[0] * image.shape[1]
        if (w * h) < (img_area * 0.1):
            self.logger.warning("Detected area too small, using full image")
            h_img, w_img = image.shape[:2]
            return (0, 0, w_img, h_img)

        self.logger.info(f"Work area detected: x={x}, y={y}, w={w}, h={h}")
        return (x, y, w, h)

    def crop_to_work_area(self, image: np.ndarray) -> np.ndarray:
        """Crop image to detected work area."""
        roi = self.detect_work_area(image)
        if roi is None:
            return image

        x, y, w, h = roi
        cropped = image[y:y+h, x:x+w]
        self.logger.info(f"Cropped to work area: {w}x{h}")
        return cropped

    def close(self) -> None:
        """Release camera resources."""
        if self.cap is not None and self.cap.isOpened():
            self.cap.release()
            self.logger.info("Camera released")
        self.cap = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

