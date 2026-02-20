from ..config import (
    SAFE_HEIGHT,
    CUT_DEPTH,
    PLUNGE_FEED,
    CUT_FEED,
    SPINDLE_DWELL,
    HOME_X,
    HOME_Y,
    HOME_Z,
)


def generate_gcode(toolpaths):
    lines = []

    # -- Setup --
    lines.append("G21")  # mm mode
    lines.append("G90")  # absolute positioning

    # -- Retract Z to safe height first --
    lines.append(f"G0 Z{SAFE_HEIGHT}")

    # -- Move above first point --
    first_start = toolpaths[0][1]
    lines.append(f"G0 X{first_start[0]} Y{first_start[1]}")

    # -- Spindle ON and dwell for spin-up --
    lines.append("M3")
    lines.append(f"G4 P{SPINDLE_DWELL}")

    # -- Cut each segment with retract between disconnected moves --
    prev_end = None
    for _, start, end in toolpaths:
        if prev_end is None or start != prev_end:
            if prev_end is not None:
                lines.append(f"G0 Z{SAFE_HEIGHT}")
            lines.append(f"G0 X{start[0]} Y{start[1]}")
            lines.append(f"G1 Z{CUT_DEPTH} F{PLUNGE_FEED}")
        lines.append(f"G1 X{end[0]} Y{end[1]} F{CUT_FEED}")
        prev_end = end

    # -- Retract Z --
    lines.append(f"G0 Z{SAFE_HEIGHT}")

    # -- Spindle OFF --
    lines.append("M5")

    # -- Return home: Z first for safety, then XY --
    lines.append(f"G0 Z{HOME_Z}")
    lines.append(f"G0 X{HOME_X} Y{HOME_Y}")

    # -- Program end --
    lines.append("M2")

    return lines