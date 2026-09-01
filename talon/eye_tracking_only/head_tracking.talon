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
