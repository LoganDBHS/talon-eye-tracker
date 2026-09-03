# Live-tunable settings for head_offset.py. Edit a number, save: Talon reloads
# the file and head_offset.py picks the value up within 250 ms - no restart.
# Full explanations in README.md.
os: windows
-
settings():
    # PAUSED 2026-09-01 for the small-monitor test: F4 zoom uses raw gaze
    # (the gaze hook itself is unhooked via user.head_offset_uninstall).
    # Set back to 1 when resuming the layer.
    user.zoom_follows_head_offset = 1

    # ---- how the head is used (rewritten 2026-09-02) ----
    # The head no longer pushes the cursor. The tracker's gaze point is already
    # head-compensated; what the ctrl-alt-m correction fixes is the error the
    # EYES make when they roll far inside the head. So a head tilt is credited
    # AGAINST that correction: eyes-only to the top edge -> full correction
    # (as before); head tilted + eyes near neutral -> no correction, raw gaze
    # trusted. Both ways of looking at a spot land on the same spot.
    # radius (mm) from the neck pivot to the eyes: pitch = rise / pivot.
    # Head-tilt look lands SHORT of the eyes-only look -> lower (80);
    # lands BEYOND it -> raise (130-150).
    user.head_pivot_mm = 100.0
    # 1 = full credit (correction by eye-in-head angle); 0 = old behaviour
    # (correction by screen position). Blend if 1 overshoots on tilts.
    user.head_credit_y = 1.0
    user.head_credit_x = 1.0

    # ---- vertical (head pitch, measured as eye-centre "rise" in mm) ----
    # EXTRA PUSH, cursor mm per mm of head rise, added on top (head as a
    # mouse). 0 = off. Was 8 until 2026-09-02; only raise it if a head tilt
    # with the eyes on the target still lands short with the credit above.
    # (recorded 2026-09-01: your natural look-up is only 8-16 mm of rise)
    user.head_gain_y = 0.0
    # mm of rise ignored around neutral. Nodding/breathing changes the
    # correction -> raise. First bit of a tilt is not credited -> lower.
    user.head_deadzone_y_mm = 3.0

    # ---- horizontal (head yaw in degrees) ----
    # EXTRA PUSH, cursor mm per degree of turning. 0 = off (was 10).
    user.head_gain_x = 0.0
    # degrees of yaw ignored around neutral (yaw noise is ~1 degree).
    user.head_deadzone_x_deg = 1.5

    # ---- dynamics ----
    # low-pass time constant (ms): shaky -> raise; laggy -> lower.
    user.head_smoothing_ms = 80.0
    # clamp on the extra push per axis (cursor mm). 0 = no clamp.
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
    # 1 = only LIFTING the head counts; lowering it (reading low on the
    # screen) does nothing and just becomes the new resting height.
    user.head_lift_only = 1
    # resting height follows the head down this fast (s) ...
    user.head_neutral_down_seconds = 2.0
    # ... and creeps up this slowly when the head stays high while looking
    # away from the middle (sitting up straighter). Slow so a tilt isn't eaten.
    user.head_neutral_up_seconds = 60.0

    # ---- precision ----
    # extra low-pass on the corrected gaze before Talon sees it (ms).
    # Steadier for small targets -> raise (150); laggy -> lower; 0 = off.
    user.gaze_smoothing_ms = 90.0
    # a sample further than this (px) from the smoothed point is a real eye
    # movement: the low-pass restarts there instead of curving toward it (big
    # saccades arrive at once). Jumps land late / in two hops -> lower (40);
    # cursor twitches on plain noise -> raise (80). 0 = always smooth.
    user.gaze_smoothing_jump_px = 60

    # ---- target magnet (target_magnet.py, ctrl-alt-t) ----
    # Snaps the cursor onto the button / tab / link / list row you are looking
    # at, using Windows UI Automation, and holds it there until your gaze
    # clearly leaves. Aim: no F4 zoom for ordinary clicking. 0 = off.
    user.magnet_on = 1
    # gaze must stay within settle_px for settle_ms before an element is looked
    # up. Grabs too eagerly while reading -> raise settle_ms (150); feels slow
    # to lock on -> lower (60).
    user.magnet_settle_ms = 60
    user.magnet_settle_px = 40
    # how far around the settled point to look for a clickable element (px).
    # Misses small buttons you are clearly looking at -> raise (40); grabs
    # neighbours you did not mean -> lower (20).
    user.magnet_reach_px = 30
    # an element side up to this long snaps to its centre on that axis; longer
    # sides (list rows, address bar) let the cursor follow the gaze along them.
    user.magnet_axis_snap_px = 110
    # the gaze must leave the element by more than release_px, for release_ms,
    # before it lets go. Hard to get off a target -> lower; loses the target
    # while you blink/glance -> raise.
    user.magnet_release_px = 40
    user.magnet_release_ms = 80
    # 1 = also grab small icons/labels that report no click behaviour (they
    # usually sit inside a button Windows did not report).
    user.magnet_passive = 1
    # 1 = empty space next to buttons (title bar, end of a toolbar, taskbar) is
    # a target too: a free strip whose short side is <= axis_snap_px snaps the
    # cursor to its centre line (e.g. the tab-strip space you double-click to
    # maximise Chrome). Wide-and-tall space (page body, desktop) never is.
    user.magnet_gap_targets = 1
    # empty space only wins when no clickable element is within this many px
    # of the settled point. Lands in the gap when you meant the button next
    # to it -> raise (32); cannot reach narrow gaps between buttons -> lower (16).
    user.magnet_gap_clear_px = 24

    # Talon's control mouse runs in jump mode (45 mm dead zone): it will not
    # move the cursor 20 px on its own, so the magnet places the cursor itself
    # if it is not on the target after place_ms. 0 = never touch the cursor.
    user.magnet_place_cursor = 1
    user.magnet_place_ms = 30
    # optional ease-out glide for the placement (ms). 0 = instant hop. Keep 0:
    # a run of small moves makes Talon think the physical mouse is in use and
    # pause gaze control (measured 2026-09-02 with ctrl-alt-l).
    user.magnet_glide_ms = 0
    # thin outline around the held element (user preferred none, 2026-09-02)
    user.magnet_highlight = 0
    # 1 = log every grab / release / placement to talon.log ([magnet] lines)
    user.magnet_debug = 0

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
    # last applied from ctrl-alt-m: 2026-09-02 11:00 (verdict: gain; previous file in C:\Users\logan\AppData\Roaming\talon\head_tracking_settings.backup)
    user.gaze_gain_left = 1.04
    user.gaze_curve_left = 0.00
    user.gaze_gain_right = 0.98
    user.gaze_curve_right = 0.00
    user.gaze_gain_up = 0.77
    user.gaze_curve_up = 0.38
    user.gaze_gain_down = 1.31
    user.gaze_curve_down = -0.30
    user.gaze_map_rotation_deg = 0.1
    # eye distance (mm) the values above were measured at. The correction is
    # really a function of eye angle, so it is rescaled to your live distance
    # (lean in -> stronger, sit back -> weaker). ctrl-alt-m Apply writes the
    # measured value; 0 = off. 532 = live reading 2026-09-02 16:16 (the 11:00
    # run did not record its distance - re-run ctrl-alt-m + Apply to lock it).
    user.gaze_ref_distance_mm = 532
