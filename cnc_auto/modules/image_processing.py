"""
Image Processing Module - Prepares captured images for vectorization.

Pipeline:
1. Convert to grayscale
2. Contrast/brightness adjustment
3. Denoising
4. Gaussian blur
5. Adaptive thresholding
6. Edge detection (Canny)
7. Morphological cleanup
8. Final binary output ready for tracing

All parameters are configurable via config.json.
"""

import os
import numpy as np
from typing import Dict, Any, Optional

try:
    import cv2
except ImportError:
    raise ImportError("OpenCV is required. Install with: pip install opencv-python")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.logger import setup_logger
from utils.helpers import ensure_dir


class ImageProcessor:
    """Processes captured images for vector tracing."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.img_cfg = config.get("image_processing", {})
        self.logger = setup_logger("image_processing", config)
        self.pipeline_cfg = config.get("pipeline", {})
        self.intermediate_dir = ensure_dir(
            self.pipeline_cfg.get("intermediate_dir", "intermediate")
        )
        self.save_intermediates = self.pipeline_cfg.get("save_intermediates", True)

    def _save_intermediate(self, image: np.ndarray, stage_name: str) -> None:
        """Save intermediate processing result for debugging."""
        if self.save_intermediates:
            path = os.path.join(self.intermediate_dir, f"{stage_name}.png")
            cv2.imwrite(path, image)
            self.logger.debug(f"Saved intermediate: {path}")

    def adjust_contrast_brightness(self, image: np.ndarray) -> np.ndarray:
        """Apply contrast (alpha) and brightness (beta) adjustment."""
        alpha = self.img_cfg.get("contrast_alpha", 1.5)
        beta = self.img_cfg.get("brightness_beta", 0)

        adjusted = cv2.convertScaleAbs(image, alpha=alpha, beta=beta)
        self.logger.debug(f"Contrast/brightness adjusted: alpha={alpha}, beta={beta}")
        self._save_intermediate(adjusted, "01_contrast")
        return adjusted

    def to_grayscale(self, image: np.ndarray) -> np.ndarray:
        """Convert image to grayscale."""
        if len(image.shape) == 2:
            self.logger.debug("Image already grayscale")
            return image

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        self.logger.debug("Converted to grayscale")
        self._save_intermediate(gray, "02_grayscale")
        return gray

    def denoise(self, image: np.ndarray) -> np.ndarray:
        """Apply non-local means denoising."""
        strength = self.img_cfg.get("denoise_strength", 10)
        if strength <= 0:
            return image

        denoised = cv2.fastNlMeansDenoising(image, None, strength, 7, 21)
        self.logger.debug(f"Denoised with strength={strength}")
        self._save_intermediate(denoised, "03_denoised")
        return denoised

    def blur(self, image: np.ndarray) -> np.ndarray:
        """Apply Gaussian blur for smoothing."""
        ksize = self.img_cfg.get("blur_kernel_size", 5)
        # Kernel size must be odd
        if ksize % 2 == 0:
            ksize += 1

        blurred = cv2.GaussianBlur(image, (ksize, ksize), 0)
        self.logger.debug(f"Gaussian blur applied: kernel={ksize}")
        self._save_intermediate(blurred, "04_blurred")
        return blurred

    def adaptive_threshold(self, image: np.ndarray) -> np.ndarray:
        """Apply adaptive thresholding for binarization."""
        block_size = self.img_cfg.get("adaptive_threshold_block_size", 11)
        constant = self.img_cfg.get("adaptive_threshold_constant", 2)

        # Block size must be odd and > 1
        if block_size % 2 == 0:
            block_size += 1
        if block_size < 3:
            block_size = 3

        thresh = cv2.adaptiveThreshold(
            image, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            block_size, constant
        )
        self.logger.debug(f"Adaptive threshold: block={block_size}, C={constant}")
        self._save_intermediate(thresh, "05_threshold")
        return thresh

    def edge_detect(self, image: np.ndarray) -> np.ndarray:
        """Apply Canny edge detection."""
        low = self.img_cfg.get("canny_low", 50)
        high = self.img_cfg.get("canny_high", 150)

        edges = cv2.Canny(image, low, high)
        self.logger.debug(f"Canny edge detection: low={low}, high={high}")
        self._save_intermediate(edges, "06_edges")
        return edges

    def morphological_cleanup(self, image: np.ndarray) -> np.ndarray:
        """Apply morphological operations to clean up noise."""
        ksize = self.img_cfg.get("morph_kernel_size", 3)
        iterations = self.img_cfg.get("morph_iterations", 1)

        kernel = np.ones((ksize, ksize), np.uint8)

        # Close small gaps
        closed = cv2.morphologyEx(image, cv2.MORPH_CLOSE, kernel, iterations=iterations)
        # Remove small noise
        opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, kernel, iterations=iterations)

        self.logger.debug(f"Morphological cleanup: kernel={ksize}, iter={iterations}")
        self._save_intermediate(opened, "07_morphology")
        return opened

    def invert_if_needed(self, image: np.ndarray) -> np.ndarray:
        """Invert image if configured (for dark-on-light vs light-on-dark)."""
        if self.img_cfg.get("invert_image", False):
            inverted = cv2.bitwise_not(image)
            self.logger.debug("Image inverted")
            self._save_intermediate(inverted, "08_inverted")
            return inverted
        return image

    def process(self, image: np.ndarray, mode: str = "threshold") -> np.ndarray:
        """
        Run the full image processing pipeline.

        Args:
            image: Input image (BGR or grayscale)
            mode: Processing mode:
                  'threshold' - adaptive threshold (best for text/line art)
                  'edge' - Canny edge detection (best for outlines)
                  'combined' - threshold + edge detection

        Returns:
            Processed binary image ready for vectorization
        """
        self.logger.info(f"Starting image processing pipeline (mode={mode})...")
        self._save_intermediate(image, "00_original")

        # Step 1: Contrast/brightness
        result = self.adjust_contrast_brightness(image)

        # Step 2: Grayscale
        result = self.to_grayscale(result)

        # Step 3: Denoise
        result = self.denoise(result)

        # Step 4: Blur
        result = self.blur(result)

        if mode == "threshold":
            # Step 5a: Adaptive threshold
            result = self.adaptive_threshold(result)
        elif mode == "edge":
            # Step 5b: Edge detection
            result = self.edge_detect(result)
        elif mode == "combined":
            # Step 5c: Both - combine results
            thresh = self.adaptive_threshold(result.copy())
            edges = self.edge_detect(result.copy())
            result = cv2.bitwise_or(thresh, edges)
            self._save_intermediate(result, "05c_combined")
        else:
            self.logger.warning(f"Unknown mode '{mode}', defaulting to threshold")
            result = self.adaptive_threshold(result)

        # Step 6: Morphological cleanup
        result = self.morphological_cleanup(result)

        # Step 7: Invert if needed
        result = self.invert_if_needed(result)

        # Save final result
        final_path = os.path.join(self.intermediate_dir, "final_processed.png")
        cv2.imwrite(final_path, result)
        self.logger.info(f"Image processing complete. Output: {final_path}")

        return result

    def resize_to_bed(
        self,
        image: np.ndarray,
        bed_width_mm: float,
        bed_height_mm: float,
        dpi: float = 96.0,
        margin_mm: float = 5.0,
        maintain_aspect: bool = True
    ) -> np.ndarray:
        """
        Resize image to fit within CNC bed dimensions.

        Args:
            image: Input image
            bed_width_mm: Bed width in mm
            bed_height_mm: Bed height in mm
            dpi: Dots per inch for mm-to-pixel conversion
            margin_mm: Margin on each side in mm
            maintain_aspect: Whether to maintain aspect ratio

        Returns:
            Resized image
        """
        mm_per_pixel = 25.4 / dpi
        target_w_px = int((bed_width_mm - 2 * margin_mm) / mm_per_pixel)
        target_h_px = int((bed_height_mm - 2 * margin_mm) / mm_per_pixel)

        h, w = image.shape[:2]

        if maintain_aspect:
            scale = min(target_w_px / w, target_h_px / h)
            new_w = int(w * scale)
            new_h = int(h * scale)
        else:
            new_w = target_w_px
            new_h = target_h_px

        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
        self.logger.info(f"Resized image: {w}x{h} -> {new_w}x{new_h}")
        return resized

