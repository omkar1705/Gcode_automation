def plan_rectangle(rect):
    bl = rect["bottom_left"]
    br = rect["bottom_right"]
    tr = rect["top_right"]
    tl = rect["top_left"]

    return [
        ("line", bl, br),
        ("line", br, tr),
        ("line", tr, tl),
        ("line", tl, bl),
    ]
