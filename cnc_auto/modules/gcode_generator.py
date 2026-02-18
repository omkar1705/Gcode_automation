"""
G-code Generation Module - Converts SVG paths to CNC G-code.

Features:
- Applies JSCut motion parameters (feed rate, plunge rate, safe Z, etc.)
- Configurable header/footer
- Homing sequence
- Spindle control (optional)
- Bed limit enforcement
- Path ordering optimization (nearest-neighbor)
- Multi-pass depth support
- Scaling to fit within bed dimensions
"""

import os
import re
import math
from typing import Dict, Any, List, Tuple, Optional

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils.logger import setup_logger
from utils.helpers import (
    ensure_dir, load_json, compute_scaling, format_gcode_float, clamp
)
from utils.calibration import CalibrationManager


class GCodeGenerator:
    """Generates G-code from SVG files using JSCut parameters."""

    def __init__(self, config: Dict[str, Any], jscut_settings: Dict[str, Any]):
        self.config = config
        self.jscut = jscut_settings
        self.gcode_cfg = config.get("gcode", {})
        self.logger = setup_logger("gcode_generator", config)
        self.calibration = CalibrationManager(config)
        self.output_dir = ensure_dir(self.gcode_cfg.get("output_dir", "gcode_output"))

        # Extract JSCut parameters
        tool = self.jscut.get("tool", {})
        material = self.jscut.get("material", {})

        self.feed_rate = float(tool.get("cut_rate", 500))
        self.plunge_rate = float(tool.get("plunge_rate", 100))
        self.rapid_rate = float(tool.get("rapid_rate", 2540))
        self.tool_diameter = float(tool.get("diameter", 0.5))
        self.pass_depth = float(tool.get("pass_depth", 0.5))
        self.stepover = float(tool.get("stepover", 0.1))

        self.material_thickness = float(material.get("thickness", 0.5))
        self.safe_z = float(material.get("clearance", 5))
        self.z_origin = material.get("z_origin", "Top")

        self.px_per_inch = float(self.jscut.get("svg", {}).get("px_per_inch", 96))

        # Apply feed rate override from config
        override = self.gcode_cfg.get("feed_rate_override", 1.0)
        self.feed_rate *= override
        self.plunge_rate *= override

        self.logger.info(
            f"G-code generator initialized: feed={self.feed_rate}, "
            f"plunge={self.plunge_rate}, safe_z={self.safe_z}, "
            f"tool_dia={self.tool_diameter}, pass_depth={self.pass_depth}"
        )

    def generate(self, svg_path: str, output_name: str = "output.gcode") -> str:
        """
        Generate G-code from an SVG file.

        Args:
            svg_path: Path to input SVG file
            output_name: Output G-code filename

        Returns:
            Path to generated G-code file
        """
        self.logger.info(f"Generating G-code from: {svg_path}")

        # Parse SVG paths
        paths = self._parse_svg_paths(svg_path)
        if not paths:
            self.logger.error("No paths found in SVG")
            return ""

        self.logger.info(f"Parsed {len(paths)} paths from SVG")

        # Convert pixel coordinates to mm
        paths_mm = self._pixels_to_mm(paths)

        # Scale to fit bed if configured
        if self.gcode_cfg.get("fit_to_bed", True):
            paths_mm = self._scale_to_bed(paths_mm)

        # Optimize path ordering
        if self.gcode_cfg.get("optimize_path_order", True):
            paths_mm = self._optimize_path_order(paths_mm)

        # Enforce bed limits
        paths_mm = self._enforce_bed_limits(paths_mm)

        # Generate G-code lines
        gcode_lines = []
        gcode_lines.extend(self._generate_header())
        gcode_lines.extend(self._generate_homing())
        gcode_lines.extend(self._generate_spindle_on())

        # Calculate number of passes
        num_passes = max(1, math.ceil(self.material_thickness / self.pass_depth))
        self.logger.info(
            f"Material thickness={self.material_thickness}mm, "
            f"pass_depth={self.pass_depth}mm, passes={num_passes}"
        )

        for pass_num in range(num_passes):
            cut_depth = -min(
                (pass_num + 1) * self.pass_depth,
                self.material_thickness
            )
            if self.z_origin != "Top":
                cut_depth = self.material_thickness + cut_depth

            self.logger.info(f"Pass {pass_num + 1}/{num_passes}: depth={cut_depth}mm")
            gcode_lines.append(f"\n; --- Pass {pass_num + 1}/{num_passes} "
                               f"(depth: {format_gcode_float(cut_depth)}mm) ---")

            for path_idx, path in enumerate(paths_mm):
                gcode_lines.extend(
                    self._generate_path_gcode(path, cut_depth, path_idx)
                )

        gcode_lines.extend(self._generate_spindle_off())
        gcode_lines.extend(self._generate_footer())

        # Write output
        output_path = os.path.join(self.output_dir, output_name)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(gcode_lines))

        self.logger.info(f"G-code generated: {output_path} ({len(gcode_lines)} lines)")
        return output_path

    def _parse_svg_paths(self, svg_path: str) -> List[List[Tuple[float, float]]]:
        """
        Parse SVG file and extract path coordinates.
        Supports M (moveto), L (lineto), Z (close) commands.
        """
        with open(svg_path, "r", encoding="utf-8") as f:
            content = f.read()

        paths = []

        # Find all <path d="..."> elements
        path_pattern = re.compile(r'd="([^"]+)"', re.DOTALL)
        matches = path_pattern.findall(content)

        for path_data in matches:
            points = self._parse_path_data(path_data)
            if points and len(points) >= 2:
                paths.append(points)

        # Also look for <line>, <polyline>, <polygon> elements
        paths.extend(self._parse_line_elements(content))
        paths.extend(self._parse_polyline_elements(content))

        return paths

    def _parse_path_data(self, d: str) -> List[Tuple[float, float]]:
        """Parse SVG path 'd' attribute into coordinate list."""
        points = []
        current_x, current_y = 0.0, 0.0
        start_x, start_y = 0.0, 0.0

        # Tokenize path data
        tokens = re.findall(r'[MmLlHhVvZzCcSsQqTtAa]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', d)

        i = 0
        command = ""
        while i < len(tokens):
            token = tokens[i]

            if token.isalpha():
                command = token
                i += 1
                continue

            if command in ("M", "m"):
                x, y = float(token), float(tokens[i + 1])
                if command == "m":
                    x += current_x
                    y += current_y
                current_x, current_y = x, y
                start_x, start_y = x, y
                points.append((x, y))
                # Subsequent coordinate pairs are implicit L/l
                command = "L" if command == "M" else "l"
                i += 2

            elif command in ("L", "l"):
                x, y = float(token), float(tokens[i + 1])
                if command == "l":
                    x += current_x
                    y += current_y
                current_x, current_y = x, y
                points.append((x, y))
                i += 2

            elif command in ("H", "h"):
                x = float(token)
                if command == "h":
                    x += current_x
                current_x = x
                points.append((current_x, current_y))
                i += 1

            elif command in ("V", "v"):
                y = float(token)
                if command == "v":
                    y += current_y
                current_y = y
                points.append((current_x, current_y))
                i += 1

            elif command in ("Z", "z"):
                points.append((start_x, start_y))
                current_x, current_y = start_x, start_y
                i += 1

            elif command in ("C", "c"):
                # Cubic bezier - linearize
                if i + 5 < len(tokens):
                    x1 = float(token)
                    y1 = float(tokens[i + 1])
                    x2 = float(tokens[i + 2])
                    y2 = float(tokens[i + 3])
                    x = float(tokens[i + 4])
                    y = float(tokens[i + 5])
                    if command == "c":
                        x1 += current_x; y1 += current_y
                        x2 += current_x; y2 += current_y
                        x += current_x; y += current_y

                    # Linearize cubic bezier
                    bezier_pts = self._linearize_cubic_bezier(
                        current_x, current_y, x1, y1, x2, y2, x, y
                    )
                    points.extend(bezier_pts)
                    current_x, current_y = x, y
                    i += 6
                else:
                    i += 1

            elif command in ("Q", "q"):
                # Quadratic bezier - linearize
                if i + 3 < len(tokens):
                    x1 = float(token)
                    y1 = float(tokens[i + 1])
                    x = float(tokens[i + 2])
                    y = float(tokens[i + 3])
                    if command == "q":
                        x1 += current_x; y1 += current_y
                        x += current_x; y += current_y

                    bezier_pts = self._linearize_quadratic_bezier(
                        current_x, current_y, x1, y1, x, y
                    )
                    points.extend(bezier_pts)
                    current_x, current_y = x, y
                    i += 4
                else:
                    i += 1
            else:
                i += 1

        return points

    def _linearize_cubic_bezier(
        self, x0, y0, x1, y1, x2, y2, x3, y3, segments: int = 10
    ) -> List[Tuple[float, float]]:
        """Convert cubic bezier curve to line segments."""
        min_seg = self.jscut.get("curve_to_line_conversion", {}).get(
            "min_num_segments", 1
        )
        segments = max(segments, min_seg)

        points = []
        for i in range(1, segments + 1):
            t = i / segments
            t2 = t * t
            t3 = t2 * t
            mt = 1 - t
            mt2 = mt * mt
            mt3 = mt2 * mt

            x = mt3 * x0 + 3 * mt2 * t * x1 + 3 * mt * t2 * x2 + t3 * x3
            y = mt3 * y0 + 3 * mt2 * t * y1 + 3 * mt * t2 * y2 + t3 * y3
            points.append((x, y))

        return points

    def _linearize_quadratic_bezier(
        self, x0, y0, x1, y1, x2, y2, segments: int = 8
    ) -> List[Tuple[float, float]]:
        """Convert quadratic bezier curve to line segments."""
        min_seg = self.jscut.get("curve_to_line_conversion", {}).get(
            "min_num_segments", 1
        )
        segments = max(segments, min_seg)

        points = []
        for i in range(1, segments + 1):
            t = i / segments
            mt = 1 - t
            x = mt * mt * x0 + 2 * mt * t * x1 + t * t * x2
            y = mt * mt * y0 + 2 * mt * t * y1 + t * t * y2
            points.append((x, y))

        return points

    def _parse_line_elements(self, svg_content: str) -> List[List[Tuple[float, float]]]:
        """Parse <line> elements from SVG."""
        paths = []
        line_pattern = re.compile(
            r'<line[^>]*x1="([\d.]+)"[^>]*y1="([\d.]+)"[^>]*'
            r'x2="([\d.]+)"[^>]*y2="([\d.]+)"', re.DOTALL
        )
        for m in line_pattern.finditer(svg_content):
            paths.append([
                (float(m.group(1)), float(m.group(2))),
                (float(m.group(3)), float(m.group(4)))
            ])
        return paths

    def _parse_polyline_elements(self, svg_content: str) -> List[List[Tuple[float, float]]]:
        """Parse <polyline> and <polygon> elements from SVG."""
        paths = []
        poly_pattern = re.compile(r'<poly(?:line|gon)[^>]*points="([^"]+)"', re.DOTALL)
        for m in poly_pattern.finditer(svg_content):
            points_str = m.group(1).strip()
            coords = re.findall(r'([-\d.]+)[,\s]+([-\d.]+)', points_str)
            if coords:
                path = [(float(x), float(y)) for x, y in coords]
                if len(path) >= 2:
                    paths.append(path)
        return paths

    def _pixels_to_mm(
        self, paths: List[List[Tuple[float, float]]]
    ) -> List[List[Tuple[float, float]]]:
        """Convert pixel coordinates to mm using SVG px_per_inch."""
        mm_per_px = 25.4 / self.px_per_inch
        scale = self.gcode_cfg.get("scaling_factor", 1.0) * mm_per_px

        converted = []
        for path in paths:
            converted.append([(x * scale, y * scale) for x, y in path])

        return converted

    def _scale_to_bed(
        self, paths: List[List[Tuple[float, float]]]
    ) -> List[List[Tuple[float, float]]]:
        """Scale all paths to fit within the CNC bed dimensions."""
        if not paths:
            return paths

        # Find bounding box of all paths
        all_x = [x for path in paths for x, y in path]
        all_y = [y for path in paths for x, y in path]
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)

        src_w = max_x - min_x
        src_h = max_y - min_y

        if src_w <= 0 or src_h <= 0:
            return paths

        bed_w = self.config.get("machine", {}).get("bed_size_x", 300)
        bed_h = self.config.get("machine", {}).get("bed_size_y", 200)
        margin = self.gcode_cfg.get("margin_mm", 5.0)
        maintain_aspect = self.gcode_cfg.get("maintain_aspect_ratio", True)

        scale_x, scale_y, _ = compute_scaling(
            src_w, src_h, bed_w, bed_h, maintain_aspect, margin
        )

        # Only scale down, never up (unless source is tiny)
        if scale_x >= 1.0 and scale_y >= 1.0 and src_w <= bed_w and src_h <= bed_h:
            # Just center it
            offset_x = margin - min_x
            offset_y = margin - min_y
            scaled = []
            for path in paths:
                scaled.append([(x + offset_x, y + offset_y) for x, y in path])
            return scaled

        # Apply scaling
        offset_x = margin
        offset_y = margin
        scaled = []
        for path in paths:
            scaled_path = [
                ((x - min_x) * scale_x + offset_x,
                 (y - min_y) * scale_y + offset_y)
                for x, y in path
            ]
            scaled.append(scaled_path)

        new_w = src_w * scale_x
        new_h = src_h * scale_y
        self.logger.info(
            f"Scaled paths: {src_w:.1f}x{src_h:.1f}mm -> "
            f"{new_w:.1f}x{new_h:.1f}mm (scale: {scale_x:.4f})"
        )

        return scaled

    def _optimize_path_order(
        self, paths: List[List[Tuple[float, float]]]
    ) -> List[List[Tuple[float, float]]]:
        """
        Optimize path ordering using nearest-neighbor heuristic.
        Minimizes rapid travel distance between paths.
        """
        if len(paths) <= 1:
            return paths

        self.logger.info(f"Optimizing path order ({len(paths)} paths)...")

        remaining = list(range(len(paths)))
        ordered = []
        current_pos = (0.0, 0.0)

        while remaining:
            # Find nearest path start
            best_idx = None
            best_dist = float("inf")
            best_reversed = False

            for idx in remaining:
                path = paths[idx]
                # Check distance to path start
                d_start = math.hypot(
                    path[0][0] - current_pos[0],
                    path[0][1] - current_pos[1]
                )
                # Check distance to path end (could traverse in reverse)
                d_end = math.hypot(
                    path[-1][0] - current_pos[0],
                    path[-1][1] - current_pos[1]
                )

                if d_start < best_dist:
                    best_dist = d_start
                    best_idx = idx
                    best_reversed = False
                if d_end < best_dist:
                    best_dist = d_end
                    best_idx = idx
                    best_reversed = True

            path = paths[best_idx]
            if best_reversed:
                path = list(reversed(path))

            ordered.append(path)
            current_pos = path[-1]
            remaining.remove(best_idx)

        self.logger.info("Path order optimized")
        return ordered

    def _enforce_bed_limits(
        self, paths: List[List[Tuple[float, float]]]
    ) -> List[List[Tuple[float, float]]]:
        """Clamp all coordinates to stay within bed limits."""
        limits = self.calibration.get_bed_limits()
        clamped = []
        violations = 0

        for path in paths:
            clamped_path = []
            for x, y in path:
                new_x = clamp(x, limits["x_min"], limits["x_max"])
                new_y = clamp(y, limits["y_min"], limits["y_max"])
                if new_x != x or new_y != y:
                    violations += 1
                clamped_path.append((new_x, new_y))
            clamped.append(clamped_path)

        if violations > 0:
            self.logger.warning(f"Clamped {violations} points to bed limits")

        return clamped

    def _generate_header(self) -> List[str]:
        """Generate G-code header with setup commands."""
        lines = [
            "; =========================================",
            "; CNC Auto - Generated G-code",
            "; =========================================",
            f"; Tool Diameter: {self.tool_diameter}mm",
            f"; Feed Rate: {self.feed_rate}mm/min",
            f"; Plunge Rate: {self.plunge_rate}mm/min",
            f"; Safe Z: {self.safe_z}mm",
            f"; Pass Depth: {self.pass_depth}mm",
            f"; Material Thickness: {self.material_thickness}mm",
            "; =========================================",
            "",
        ]

        # Add custom header commands
        for cmd in self.gcode_cfg.get("header_gcode", ["G90", "G21", "G17"]):
            lines.append(cmd)

        lines.append("")
        return lines

    def _generate_homing(self) -> List[str]:
        """Generate homing sequence."""
        return [
            "; --- Homing ---",
            "$H",
            f"G0 Z{format_gcode_float(self.safe_z)}",
            "G0 X0 Y0",
            "",
        ]

    def _generate_spindle_on(self) -> List[str]:
        """Generate spindle start commands if enabled."""
        if not self.gcode_cfg.get("spindle_enabled", False):
            return []

        speed = self.gcode_cfg.get("spindle_speed", 10000)
        delay = self.gcode_cfg.get("spindle_delay_seconds", 2.0)
        return [
            "; --- Spindle On ---",
            f"M3 S{speed}",
            f"G4 P{delay}  ; Wait for spindle",
            "",
        ]

    def _generate_spindle_off(self) -> List[str]:
        """Generate spindle stop commands if enabled."""
        if not self.gcode_cfg.get("spindle_enabled", False):
            return []
        return [
            "",
            "; --- Spindle Off ---",
            "M5",
        ]

    def _generate_path_gcode(
        self,
        path: List[Tuple[float, float]],
        cut_depth: float,
        path_idx: int
    ) -> List[str]:
        """Generate G-code for a single path at given depth."""
        if len(path) < 2:
            return []

        f = format_gcode_float
        lines = [
            f"\n; Path {path_idx + 1}",
            # Rapid to safe Z
            f"G0 Z{f(self.safe_z)}",
            # Rapid to path start
            f"G0 X{f(path[0][0])} Y{f(path[0][1])}",
            # Plunge to cut depth
            f"G1 Z{f(cut_depth)} F{f(self.plunge_rate)}",
        ]

        # Cut along path
        for x, y in path[1:]:
            lines.append(f"G1 X{f(x)} Y{f(y)} F{f(self.feed_rate)}")

        # Retract to safe Z
        lines.append(f"G0 Z{f(self.safe_z)}")

        return lines

    def _generate_footer(self) -> List[str]:
        """Generate G-code footer with cleanup commands."""
        lines = [
            "",
            "; --- End ---",
        ]

        for cmd in self.gcode_cfg.get("footer_gcode", []):
            # Replace template variables
            cmd = cmd.replace("{safe_z}", format_gcode_float(self.safe_z))
            lines.append(cmd)

        lines.append("")
        lines.append("; Program complete")
        return lines

