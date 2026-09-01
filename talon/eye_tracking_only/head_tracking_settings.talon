# Live-tunable settings for head_offset.py. Edit a number, save: Talon reloads
# the file and head_offset.py picks the value up within 250 ms - no restart.
# Full explanations in README.md.
os: windows
-
settings():
    # ---- vertical (head pitch, measured as eye-centre "rise" in mm) ----
    # cursor mm per mm of head rise. A comfortable look-up raises the eyes
    # ~25-35 mm; half the screen is 196 mm, so ~6 reaches the top edge from
    # centre. Cursor stops short of the top -> raise; overshoots/jittery -> lower.
    # (recorded 2026-09-01: your natural look-up is only 8-16 mm of rise, so
    # gain 8 / dead zone 3 -> 150-380 px of lift)
    user.head_gain_y = 8.0
    # mm of rise ignored around neutral. Nodding/breathing moves the cursor ->
    # raise. First bit of a tilt does nothing -> lower.
    user.head_deadzone_y_mm = 3.0

    # ---- horizontal (head yaw in degrees; 0 = disabled) ----
    # cursor mm per degree of yaw. 8-12 is a sensible start if you enable it.
    user.head_gain_x = 0.0
    # degrees of yaw ignored around neutral (yaw noise is ~1 degree).
    user.head_deadzone_x_deg = 1.5

    # ---- dynamics ----
    # low-pass time constant (ms): shaky -> raise; laggy -> lower.
    user.head_smoothing_ms = 80.0
    # clamp on the offset per axis (cursor mm). 0 = no clamp.
    user.head_max_offset_mm = 400.0
    # ---- neutral pose ("resting head, facing the centre") ----
    # The neutral pose is the running average of your head height taken ONLY
    # while your gaze is in the middle of the screen (you tilt to reach edges,
    # never to look at the centre). So it tracks posture by itself and never
    # adapts during a tilt - a held tilt keeps working indefinitely. Saved to
    # %APPDATA%\Talon\head_offset_anchor.json across restarts; ctrl-alt-r
    # overrides it immediately.
    # seconds: lower = follows posture faster (5 is a few glances at the middle)
    user.head_neutral_seconds = 5.0
    # how far from centre still counts as "the middle" (0.3 = middle 60%)
    user.head_neutral_zone = 0.3
    # 1 = only LIFTING the head moves the cursor; lowering it (reading low on
    # the screen) does nothing and just becomes the new resting height.
    user.head_lift_only = 1
    # resting height follows the head down this fast (s) ...
    user.head_neutral_down_seconds = 2.0
    # ... and creeps up this slowly when the head stays high while looking
    # away from the middle (sitting up straighter). Slow so a tilt isn't eaten.
    user.head_neutral_up_seconds = 60.0

    # ---- edge behaviour ----
    # magnet: corrected gaze within this many px of an edge snaps onto it.
    user.gaze_edge_snap_px = 30.0
    # the tracker drops one eye at the top corners; 1 = feed Talon the surviving
    # eye for both (otherwise it parks the cursor 60-90 px short of the edge).
    user.gaze_mirror_lost_eye = 1
    # eyes unseen for this long (you got up) -> next pose is the new neutral.
    user.head_lost_recenter_seconds = 5.0

    # ---- gaze correction, per side of centre (from ctrl-alt-m) ----
    # corrected = centre + (gain + curve*u) * (gaze - centre), u = 0..1 toward
    # that edge. 1.0 / 0.0 = untouched. Values below are from the run of
    # 2026-09-01 10:04 (after the 10:03 calibration, ~60 cm): right edge landed
    # 140 px short, bottom 50 px short, up over-reached mid-way, map rotated
    # +2.7 deg clockwise. Re-run ctrl-alt-m after any recalibration and paste
    # its SUGGEST lines here (the "residual" line tells you how well they work).
    user.gaze_gain_left = 0.95
    user.gaze_curve_left = 0.0
    user.gaze_gain_right = 0.81
    user.gaze_curve_right = 0.38
    user.gaze_gain_up = 0.76
    user.gaze_curve_up = 0.29
    user.gaze_gain_down = 1.08
    user.gaze_curve_down = 0.0
    user.gaze_map_rotation_deg = 2.7
