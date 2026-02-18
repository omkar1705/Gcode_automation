"""
Shared helper functions for the CNC automation system.
"""

import os
import json
from typing import Any, Dict, Tuple


def load_json(filepath: str) -> Dict[str, Any]:
    """Load and parse a JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: Dict[str, Any], filepath: str) -> None:
    """Save data as formatted JSON."""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def ensure_dir(path: str) -> str:
    """Create directory if it doesn't exist. Returns the path."""
    os.makedirs(path, exist_ok=True)
    return path


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp a value between min and max."""
    return max(min_val, min(value, max_val))


def compute_scaling(
    source_width: float,
    source_height: float,
    target_width: float,
    target_height: float,
    maintain_aspect: bool = True,
    margin: float = 0.0
) -> Tuple[float, float, float]:
    """
    Compute scaling factors to fit source dimensions into target area.

    Args:
        source_width: Original width
        source_height: Original height
        target_width: Target area width
        target_height: Target area height
        maintain_aspect: Whether to maintain aspect ratio
        margin: Margin to leave on each side

    Returns:
        Tuple of (scale_x, scale_y, uniform_scale)
    """
    usable_width = target_width - 2 * margin
    usable_height = target_height - 2 * margin

    if usable_width <= 0 or usable_height <= 0:
        raise ValueError("Target dimensions too small for given margin")

    scale_x = usable_width / source_width if source_width > 0 else 1.0
    scale_y = usable_height / source_height if source_height > 0 else 1.0

    if maintain_aspect:
        uniform = min(scale_x, scale_y)
        return uniform, uniform, uniform
    else:
        return scale_x, scale_y, min(scale_x, scale_y)


def format_gcode_float(value: float, decimals: int = 4) -> str:
    """Format a float for G-code output (strip trailing zeros)."""
    formatted = f"{value:.{decimals}f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted

