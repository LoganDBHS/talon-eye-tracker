# Global hotkeys for eye tracking — no speech required.
# These work system-wide whenever Talon is running.
os: windows
-

# Toggle Control Mouse (cursor continuously follows gaze + head)
key(ctrl-alt-e): tracking.control_toggle()

# Toggle Zoom Mouse (fallback mode: discrete trigger + magnified precision stage)
key(ctrl-alt-z): tracking.control_zoom_toggle()

# Re-run Talon's eye tracking calibration
key(ctrl-alt-c): tracking.calibrate()
