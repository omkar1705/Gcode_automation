"""
Calibration utilities for the CNC automation system.
Provides functions to manage, validate, and apply calibration settings.
"""

import os
import json
from typing import Dict, Any, Optional
from .helpers import load_json, save_json
from .logger import setup_logger


class CalibrationManager:
    """Manages CNC machine calibration parameters."""

    REQUIRED_MACHINE_KEYS = [
        "bed_size_x", "bed_size_y", "bed_size_z",
        "steps_per_mm_x", "steps_per_mm_y", "steps_per_mm_z"
    ]

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.logger = setup_logger("calibration", config)
        self.machine = config.get("machine", {})

    def validate(self) -> bool:
        """Validate that all required calibration parameters are present and valid."""
        valid = True

        for key in self.REQUIRED_MACHINE_KEYS:
            if key not in self.machine:
                self.logger.error(f"Missing calibration parameter: {key}")
                valid = False
            elif not isinstance(self.machine[key], (int, float)):
                self.logger.error(f"Invalid type for {key}: {type(self.machine[key])}")
                valid = False
            elif self.machine[key] <= 0:
                self.logger.error(f"Parameter {key} must be positive, got {self.machine[key]}")
                valid = False

        if valid:
            self.logger.info("Calibration parameters validated successfully")
        return valid

    def get_bed_limits(self) -> Dict[str, float]:
        """Get the machine bed limits in mm."""
        return {
            "x_min": self.machine.get("home_offset_x", 0),
            "y_min": self.machine.get("home_offset_y", 0),
            "z_min": 0,
            "x_max": self.machine.get("bed_size_x", 300),
            "y_max": self.machine.get("bed_size_y", 200),
            "z_max": self.machine.get("bed_size_z", 50),
        }

    def is_within_bounds(self, x: float, y: float, z: Optional[float] = None) -> bool:
        """Check if a coordinate is within machine bed limits."""
        limits = self.get_bed_limits()
        in_bounds = (
            limits["x_min"] <= x <= limits["x_max"] and
            limits["y_min"] <= y <= limits["y_max"]
        )
        if z is not None:
            in_bounds = in_bounds and (limits["z_min"] <= z <= limits["z_max"])
        return in_bounds

    def get_origin_offset(self) -> tuple:
        """Get origin offset based on origin_mode setting."""
        mode = self.machine.get("origin_mode", "bottom-left")
        if mode == "center":
            return (
                self.machine.get("bed_size_x", 300) / 2,
                self.machine.get("bed_size_y", 200) / 2
            )
        else:  # bottom-left
            return (
                self.machine.get("home_offset_x", 0),
                self.machine.get("home_offset_y", 0)
            )

    def apply_steps_per_mm(self) -> str:
        """Generate GRBL configuration commands for steps/mm."""
        commands = []
        commands.append(f"$100={self.machine.get('steps_per_mm_x', 800)}")
        commands.append(f"$101={self.machine.get('steps_per_mm_y', 800)}")
        commands.append(f"$102={self.machine.get('steps_per_mm_z', 800)}")
        return "\n".join(commands)

    def export_calibration(self, filepath: str) -> None:
        """Export current calibration to a standalone file."""
        cal_data = {
            "machine": self.machine,
            "bed_limits": self.get_bed_limits(),
            "origin_offset": self.get_origin_offset()
        }
        save_json(cal_data, filepath)
        self.logger.info(f"Calibration exported to {filepath}")

    def update_parameter(self, key: str, value: Any) -> None:
        """Update a single calibration parameter."""
        if key in self.machine:
            old_val = self.machine[key]
            self.machine[key] = value
            self.logger.info(f"Updated {key}: {old_val} -> {value}")
        else:
            self.logger.warning(f"Unknown calibration parameter: {key}")

