from config import (

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

    lines.append("G21")        # mm mode

    lines.append("G90")        # absolute positioning

    lines.append("G54")        # work coordinate system



    first_start = toolpaths[0][1]



    # -- Move above first point --

    lines.append(f"G0 X{first_start[0]} Y{first_start[1]} Z{SAFE_HEIGHT}")



    # -- Spindle ON --

    lines.append("M3")

    lines.append(f"G4 P{SPINDLE_DWELL}")   # dwell for spin-up



    # -- Plunge --

    lines.append(f"G1 Z{CUT_DEPTH} F{PLUNGE_FEED}")



    # -- Cut perimeter --

    for _, start, end in toolpaths:

        lines.append(f"G1 X{end[0]} Y{end[1]} F{CUT_FEED}")



    # -- Retract --

    lines.append(f"G0 Z{SAFE_HEIGHT}")



    # -- Spindle OFF --

    lines.append("M5")



    # -- Return home --

    lines.append(f"G0 X{HOME_X} Y{HOME_Y} Z{HOME_Z}")



    return lines