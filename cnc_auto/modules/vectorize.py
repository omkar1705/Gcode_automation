"""
Vectorization Module - Converts processed bitmap images to SVG.

Supports multiple backends:
- Potrace (via pypotrace or subprocess)
- OpenCV contour-based fallback

Produces clean SVG output ready for G-code generation.
"""

import os
import subprocess
import tempfile
import numpy as np
from typing import Dict, Any, List, Tuple, Optional

try:
    import cv2
except ImportError:
    raise ImportError("OpenCV is required. Install with: pip install opencv-python")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.logger import setup_logger
from utils.helpers import ensure_dir


class Vectorizer:
    """Converts processed binary images to SVG vector paths."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.vec_cfg = config.get("vectorization", {})
        self.logger = setup_logger("vectorize", config)
        self.output_dir = ensure_dir(self.vec_cfg.get("output_dir", "svg_output"))
        self.method = self.vec_cfg.get("method", "potrace")
        self.min_path_length = self.vec_cfg.get("min_path_length", 5)
        self.simplify_tolerance = self.vec_cfg.get("simplify_tolerance", 0.5)

    def vectorize(self, image: np.ndarray, output_name: str = "output.svg") -> str:
        """
        Convert a binary image to SVG.

        Args:
            image: Binary (black/white) image as numpy array
            output_name: Output SVG filename

        Returns:
            Path to the generated SVG file
        """
        output_path = os.path.join(self.output_dir, output_name)

        if self.method == "potrace":
            result = self._vectorize_potrace(image, output_path)
        elif self.method == "contour":
            result = self._vectorize_contours(image, output_path)
        else:
            self.logger.warning(f"Unknown method '{self.method}', falling back to contour")
            result = self._vectorize_contours(image, output_path)

        if result:
            self.logger.info(f"SVG generated: {result}")
        return result

    def _vectorize_potrace(self, image: np.ndarray, output_path: str) -> Optional[str]:
        """
        Vectorize using Potrace (command-line tool).
        Falls back to contour method if Potrace is not available.
        """
        self.logger.info("Attempting Potrace vectorization...")

        # Ensure binary image (Potrace needs BMP input)
        if len(image.shape) > 2:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        _, binary = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY)

        # Save as BMP for Potrace input
        with tempfile.NamedTemporaryFile(suffix=".bmp", delete=False) as tmp:
            tmp_bmp = tmp.name
            cv2.imwrite(tmp_bmp, binary)

        try:
            # Try running potrace
            turdsize = self.vec_cfg.get("turdsize", 2)
            alphamax = self.vec_cfg.get("alphamax", 1.0)
            opttolerance = self.vec_cfg.get("opttolerance", 0.2)

            cmd = [
                "potrace",
                tmp_bmp,
                "-s",  # SVG output
                "-o", output_path,
                "-t", str(turdsize),
                "-a", str(alphamax),
                "-O", str(opttolerance),
                "--flat",
            ]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )

            if result.returncode == 0 and os.path.exists(output_path):
                self.logger.info("Potrace vectorization successful")
                return output_path
            else:
                self.logger.warning(f"Potrace failed: {result.stderr}")
                self.logger.info("Falling back to contour vectorization...")
                return self._vectorize_contours(image, output_path)

        except FileNotFoundError:
            self.logger.warning("Potrace not found. Install it or use 'contour' method.")
            self.logger.info("Falling back to contour vectorization...")
            return self._vectorize_contours(image, output_path)
        except subprocess.TimeoutExpired:
            self.logger.error("Potrace timed out")
            return self._vectorize_contours(image, output_path)
        finally:
            # Cleanup temp file
            if os.path.exists(tmp_bmp):
                os.unlink(tmp_bmp)

    def _vectorize_contours(self, image: np.ndarray, output_path: str) -> Optional[str]:
        """
        Vectorize using OpenCV contour detection.
        Built-in fallback that doesn't require external tools.
        """
        self.logger.info("Using OpenCV contour vectorization...")

        if len(image.shape) > 2:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        _, binary = cv2.threshold(image, 127, 255, cv2.THRESH_BINARY)

        h, w = binary.shape
        contours, hierarchy = cv2.findContours(
            binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
        )

        if not contours:
            self.logger.warning("No contours found in image")
            return None

        self.logger.info(f"Found {len(contours)} raw contours")

        # Filter and simplify contours
        filtered_paths = []
        for contour in contours:
            # Filter by minimum path length
            if len(contour) < self.min_path_length:
                continue

            # Simplify contour (Douglas-Peucker)
            epsilon = self.simplify_tolerance * cv2.arcLength(contour, True) / 100
            simplified = cv2.approxPolyDP(contour, epsilon, True)

            if len(simplified) >= 2:
                filtered_paths.append(simplified)

        self.logger.info(f"Filtered to {len(filtered_paths)} paths")

        # Generate SVG
        svg_content = self._contours_to_svg(filtered_paths, w, h)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(svg_content)

        return output_path

    def _contours_to_svg(
        self, contours: List[np.ndarray], width: int, height: int
    ) -> str:
        """Convert OpenCV contours to SVG markup."""
        svg_lines = [
            f'<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">',
        ]

        for contour in contours:
            points = contour.squeeze()
            if len(points.shape) < 2:
                continue

            # Build SVG path data
            path_data = f"M {points[0][0]},{points[0][1]}"
            for point in points[1:]:
                path_data += f" L {point[0]},{point[1]}"
            path_data += " Z"  # Close path

            svg_lines.append(
                f'  <path d="{path_data}" '
                f'fill="none" stroke="black" stroke-width="1"/>'
            )

        svg_lines.append("</svg>")
        return "\n".join(svg_lines)

    def get_svg_bounds(self, svg_path: str) -> Tuple[float, float, float, float]:
        """
        Parse SVG file and return bounding box (min_x, min_y, max_x, max_y).
        Simple parser for our generated SVGs.
        """
        import re

        with open(svg_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Extract viewBox if present
        vb_match = re.search(r'viewBox="([^"]+)"', content)
        if vb_match:
            parts = vb_match.group(1).split()
            if len(parts) == 4:
                return tuple(float(p) for p in parts)

        # Extract width/height
        w_match = re.search(r'width="(\d+\.?\d*)"', content)
        h_match = re.search(r'height="(\d+\.?\d*)"', content)

        w = float(w_match.group(1)) if w_match else 100
        h = float(h_match.group(1)) if h_match else 100

        return (0, 0, w, h)

