# Global hotkeys for eye tracking — no speech required.
# These work system-wide whenever Talon is running.
os: windows
-

# Toggle Control Mouse (cursor continuously follows gaze + head); shows a
# notification with the new state (control_toggle_notify.py)
key(ctrl-alt-e): user.control_mouse_toggle_notify()

# Toggle Zoom Mouse (fallback mode: discrete trigger + magnified precision stage)
key(ctrl-alt-z): tracking.control_zoom_toggle()

# Run Talon's eye tracking calibration - or CANCEL the run in progress.
# calibration_guard.py adds progress text, ignores focus loss and reports how
# the run ended (saved / cancelled / failed + why) in a banner and talon.log.
key(ctrl-alt-c): user.calibration_toggle()

# swap between the two most recent saved calibrations (glasses on / off); uploads at once
key(ctrl-alt-shift-c): user.calibration_swap()
