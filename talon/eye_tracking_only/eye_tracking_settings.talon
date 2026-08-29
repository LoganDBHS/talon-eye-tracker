# Eye-tracking-only overrides.
# The "os: windows" header makes this context more specific than community's
# top-level settings.talon, so these values win without editing community files.
os: windows
-
settings():
    # Community enables pop-noise clicking when an eye tracker is active.
    # We click with physical mouse/keyboard only, so force it off — this also
    # means Talon never needs the microphone.
    user.mouse_enable_pop_click = 0
    user.mouse_enable_pop_stops_scroll = false
    user.mouse_enable_pop_stops_drag = false
