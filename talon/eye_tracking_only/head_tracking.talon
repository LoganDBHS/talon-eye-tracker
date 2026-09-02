# Hotkeys for the head-offset / diagnostics layer (no speech needed).
# See README.md in this folder.
os: windows
-

# Toggle the head-pose cursor offset on/off (head_offset.py)
key(ctrl-alt-h): user.head_offset_toggle()

# Re-centre: make the CURRENT head pose the neutral pose (zero offset)
key(ctrl-alt-r): user.head_offset_recenter()

# Toggle the tracking diagnostic logger (tracking_diag.py)
key(ctrl-alt-d): user.tracking_diag_toggle()

# Run the gaze gain measurement overlay (gaze_measure.py); Esc cancels
key(ctrl-alt-m): user.gaze_measure_start()

# On the measurement results page: write its SUGGEST values into
# head_tracking_settings.talon (previous file kept in %APPDATA%\Talon\
# head_tracking_settings.backup) / close without changing anything.
# Same as clicking the Apply / Discard buttons on the results page.
key(ctrl-alt-y): user.gaze_measure_apply()
key(ctrl-alt-n): user.gaze_measure_discard()

# Unhook the gaze-frame correction entirely until the next Talon restart / reload
# (raw gaze to the control mouse; used for the small-monitor test)
key(ctrl-alt-u): user.head_offset_uninstall()

# Toggle the target magnet (target_magnet.py): snap the cursor onto the UI
# element you are looking at and hold it there
key(ctrl-alt-t): user.magnet_toggle()
