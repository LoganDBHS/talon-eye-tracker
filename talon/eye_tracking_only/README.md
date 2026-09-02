# eye_tracking_only — Talon head/gaze cursor layer

Eye-tracking-only mouse control (no speech, no noise input) for a Tobii Eye
Tracker 5 on a 32" 16:9 monitor, Talon 0.4.0, Windows 11. Everything lives in
this folder; nothing in `community/` or `C:\Program Files\Talon` is edited.

## Files

| File | Purpose |
|---|---|
| `eye_tracking_hotkeys.talon` | ctrl-alt-e control mouse, ctrl-alt-z zoom mouse, ctrl-alt-c calibrate |
| `eye_tracking_settings.talon` | forces pop-click off (no mic) |
| `zoom_pop.py` / `zoom_pop.talon` | F4 zoom overlay + AutoHotkey mouse bridge (F13–F17) |
| **`head_offset.py`** | head-pose cursor offset + gaze gain correction (added 2026-08-31) |
| **`head_tracking_settings.talon`** | the live-tunable numbers for `head_offset.py` |
| **`head_tracking.talon`** | hotkeys for the layer (below) |
| **`tracking_diag.py`** | on-demand tracking logger (talon.log + CSV) |
| **`gaze_measure.py`** | gaze-gain measurement overlay (Task 4 fallback) |
| **`target_magnet.py`** | snaps the cursor onto the UI element you look at (added 2026-09-02) |
| `eye_tracking_tuning.py` | commented-out legacy/zoom knobs |

## Hotkeys

| Key | Action |
|---|---|
| ctrl-alt-e | Control Mouse on/off (built-in; gaze + head control are both ON in the tray menu) |
| ctrl-alt-h | head offset on/off (`user.head_offset_toggle`) |
| ctrl-alt-r | re-centre: the pose you hold NOW becomes neutral (`user.head_offset_recenter`) |
| ctrl-alt-d | diagnostic logger on/off (`user.tracking_diag_toggle`) |
| ctrl-alt-m | gaze gain measurement overlay, Esc cancels (`user.gaze_measure_start`) |
| ctrl-alt-t | target magnet on/off (`user.magnet_toggle`) |
| F4 / Shift-F4 | zoom overlay open / cancel (unchanged) |

## What the tracker actually gives Talon (why this layer exists)

Verified by introspection on 2026-08-31 (`actions.list("tracking")`, tracker
stream table, `ControlMouse2` state):

* Talon 0.4.0 (Jul 2023) is the current public build; the "Gaze Control / Head
  Control" toggles already exist in it under tray → Eye Tracking, and both were
  already ON. The separate settings listed in the changelog's *0.4.0 beta* are
  the Patreon beta only.
* The Tobii 5 sends ONE stream (`gaze`, id 1280). Per eye: 3D eyeball position
  in mm, gaze point, pupil, validity. **No head-rotation data** — Tobii's head
  pose runs inside Tobii's host software, which Talon bypasses (direct USB).
  Talon's "head" is literally the two eyeball positions (`FilterFrame.head` is a
  2×3 array).
* Built-in Head Control therefore nudges by eye-midpoint *translation* with a
  small velocity-dependent gain (0.1–2.25 mm/mm) that resets on every gaze jump.
  A head *pitch* barely moves the eyes' position, so it barely moves the cursor.

`head_offset.py` infers pose from the two eye positions:

* **rise** — eye-centre height along the *screen's* up axis (mm; the tracker
  frame is tilted 20°, this is rotated out). Pitching up swings the eyes up
  around the neck → ~25–35 mm for a comfortable look-up. Pitch proxy.
* **yaw** — heading of the left→right eye vector, degrees. Real rotation.

**The head is a credit, not a push (since 2026-09-02).** The tracker's gaze
point is already head-compensated: tilt your head with your eyes on a target
and the point stays put. What actually goes wrong is the gaze estimate when
the *eyes* roll far inside the head (eyelid over the pupil, glints lost, one
eye dropping out at the top corners) — and that is what the ctrl-alt-m
gain/curve corrects, measured with the head still, so it is a function of the
eyes' angle inside the head. The layer therefore turns the head pose into a
pitch (`rise / head_pivot_mm`) and yaw, converts them to the screen
eccentricity they account for (`dist × tan(angle) / half-screen-mm`) and
subtracts that from the gaze eccentricity *before* evaluating the correction:

* eyes only, head still → full correction, exactly as before;
* head tilted, eyes near neutral → no correction, the raw point is trusted.

So looking at a spot either way lands the cursor on the same spot, and the
head is never asked to do more than it naturally does. `head_credit_y/x`
blends between this (1) and the old correct-by-screen-position (0). The old
additive push still exists (`head_gain_y/x`, cursor mm per mm/deg) but is 0.
Everything is applied **upstream**: the gaze frames Talon's control mouse
receives are replaced by corrected copies, so Talon does all its own
smoothing/jump logic on corrected data and nothing intercepts its cursor
moves. (The first version wrapped
`ctrl.mouse_move` instead — that stuttered, because Talon glides toward its
target by re-reading the cursor through a path the wrapper couldn't see.)
The hook is registered in Talon's own tracking context and survives reloads of
this folder; `user.head_offset_uninstall()` restores the raw stream.

**Lift-only** (`head_lift_only = 1`): only raising the head above its resting
height counts as a tilt. Lowering it — which happens naturally when
reading low on the screen — does nothing and simply becomes the new resting
height (`head_neutral_down_seconds`). The resting height is also learned
whenever the gaze is in the middle of the screen (`head_neutral_zone`,
`head_neutral_seconds`) — you tilt to reach edges, never to look at the centre —
and only creeps up slowly otherwise (`head_neutral_up_seconds`), so a held tilt
is not eaten. Saved in `%APPDATA%\Talon\head_offset_anchor.json`; ctrl-alt-r
overrides.

The F4 zoom follows the same correction (the overlay opens where the offset
cursor is; the offset is frozen while the overlay is open). Disable with
`user.zoom_follows_head_offset = 0` in `head_tracking_settings.talon`.

## Tuning — which setting for which symptom

Edit `head_tracking_settings.talon`, save; values apply within 250 ms.

| Symptom | Change |
|---|---|
| Head-tilt + eyes on a spot lands SHORT of the eyes-only look | lower `user.head_pivot_mm` (100 → 80: a given rise counts as more tilt) |
| Head-tilt look lands BEYOND the eyes-only look / cursor moves when I tilt with eyes fixed | raise `user.head_pivot_mm` (100 → 130-150) or blend `user.head_credit_y` (1 → 0.6) |
| Want the head to push the cursor as well (head as a mouse) | `user.head_gain_y = 6-8` (cursor mm per mm of rise); `head_gain_x` likewise |
| Cursor shakes when tilted | raise `user.head_smoothing_ms` (80 → 150) |
| Breathing / small posture changes the correction | raise `user.head_deadzone_y_mm` (3 → 5) |
| First part of a tilt is not credited | lower `user.head_deadzone_y_mm` (3 → 1.5) |
| Cursor drifts over minutes (slouching) | glance at the middle of the screen for a few seconds (neutral re-learns there); lower `user.head_neutral_seconds` to make that faster; ctrl-alt-r forces it |
| Cursor sits pushed after I sit back down | ctrl-alt-r (or wait: `user.head_lost_recenter_seconds` re-anchors after you were away ≥ 5 s) |
| Cursor lags the head | lower `user.head_smoothing_ms` (80 → 40) |
| Big eye movements land late or in two hops | lower `user.gaze_smoothing_jump_px` (60 → 40) so saccades bypass the gaze low-pass sooner; if the cursor then twitches on noise, raise it (80) |
| Anything goes wild | ctrl-alt-h turns the head part off (correction stays); `user.head_max_offset_mm` clamps the push (400) |
| Gaze itself stops short of / overshoots an edge (head still) | run ctrl-alt-m and paste its `SUGGEST` lines: per-side `user.gaze_gain_left/right/up/down`, `user.gaze_curve_*` (only where it says non-linear) and `user.gaze_map_rotation_deg`. Its `residual` line shows the error left with the current values |
| Edges stop short when I lean in / overshoot when I sit back | `user.gaze_ref_distance_mm` must be the distance the gains were measured at (ctrl-alt-m Apply writes it; ctrl-alt-d prints `dist_mm` live). 0 turns the rescaling off |
| Everything is off in the same direction / eyes disagree a lot | that's calibration, not gain: ctrl-alt-c at the distance you sit; ctrl-alt-m says which eye is worse (tray → Eye Tracking → Only Left/Right Eye) |

Distance normalisation (2026-09-02): the gain/curve correction is a function
of eye angle, not screen position. With `gaze_ref_distance_mm` = D0 set, an
eccentricity e at the live distance D is evaluated as e*D0/D and the result is
scaled by D/D0, so the values measured once stay right when you lean in or
sit back (10 cm nearer = roughly 20 % more correction at the edge). ctrl-alt-m
now records the median eye distance of the run and Apply writes it.

Numbers: on the 24" 1080p at ~48 cm, half the screen height is 152 mm; a
natural 12 mm rise with `head_pivot_mm = 100` is a 7° pitch that accounts for
0.28 of the half-screen, so the correction at the top edge is evaluated at
eccentricity −0.67 instead of −0.95 and comes out near zero. ctrl-alt-d prints
`pitch_deg` and `credit` live; ctrl-alt-m warns if the head moved during a run.

## Target magnet (added 2026-09-02)

The tracker's own accuracy is about 0.5-1° of visual angle: 15-30 px of
scatter at 48 cm on this screen, while Chrome toolbar buttons are 43-60 px,
tab close buttons 50 px and taskbar buttons 77 px (measured with a UI
Automation census of the screen). Small targets were therefore hit-or-miss
without the F4 zoom. `target_magnet.py` asks Windows what is under and around
the settled gaze (`talon.ui.element_at`, UI Automation) and holds the cursor
on the element until the gaze clearly leaves. Clicking stays physical.

How it behaves:

* The gaze must sit still (`magnet_settle_px` for `magnet_settle_ms`) before
  anything is looked up. Buttons, links, tabs, menu items, check boxes, list
  and tree rows, combo boxes, scrollbars and small edit fields count as
  targets; documents, big panes and text areas do not.
* If the hit is only a button's icon or label (Windows often reports the
  child), the enclosing button is recovered by hit-testing just outside the
  child. If the hit is a container, a ring of `magnet_reach_px` around the
  point is probed and the nearest clickable element wins.
* Short sides snap to the centre; a side longer than `magnet_axis_snap_px`
  lets the cursor follow the gaze along it (list rows, the address bar).
* Sticky: the target is kept while the gaze stays inside its rectangle plus
  `magnet_release_px`; settling on a different clickable element switches at
  once; the element is re-checked every 400 ms so scrolling releases it.
* While held, Talon receives the real gaze and a **frozen head pose**. The control mouse runs in jump mode (zone 45 mm, measured
  2026-09-02) where small gaze changes are ignored and the head does fine
  positioning, so a still head means Talon never drags the cursor off the
  target. Real head motion is blended back in over 150 ms on release.
* Because of that dead zone Talon may not move the cursor 20 px on its own,
  so the magnet places it with `ctrl.mouse_move` if it is not on the target
  after `magnet_place_ms` (`magnet_place_cursor = 0` disables this) - as an
  ease-out glide (`magnet_glide_ms`). Along a long target the cursor then
  follows the gaze continuously. Talon's own 45 mm hop cannot be smoothed:
  Control Mouse 2 has no continuous gaze mode (the tray's "mouse jump" toggle
  is `use_mouse`, the physical-mouse flag - verified 2026-09-02).
* An orange outline marks the held element (`magnet_highlight`). Nothing
  happens while the control mouse is off, while the F4 overlay is open, or
  over Talon's own windows.

| Symptom | Change |
|---|---|
| Grabs things while I am just reading | raise `user.magnet_settle_ms` (90 → 150) or set `user.magnet_passive = 0` |
| Slow to lock on | lower `user.magnet_settle_ms` (90 → 60) |
| Misses a small button I am clearly looking at | raise `user.magnet_reach_px` (30 → 40) |
| Grabs the neighbour instead | lower `user.magnet_reach_px` (30 → 20) |
| Hard to get off a target | lower `user.magnet_release_px` (40 → 25) |
| Loses the target when I blink or glance | raise `user.magnet_release_px` / `user.magnet_release_ms` |
| Cursor sits next to the target instead of on it | check `user.magnet_place_cursor = 1`; `user.magnet_debug = 1` logs placements and Talon's `mouse_active` state |
| Wants to snap along a long row/bar | raise `user.magnet_axis_snap_px` |
| Snap looks jerky / too abrupt | `user.magnet_glide_ms` > 0 glides instead of hopping, but multi-step moves make Talon pause gaze control (measured: 300-1300 ms hop delays) - prefer 0 |
| Cursor is slow to follow big eye movements | ctrl-alt-l, do ten sweeps, ctrl-alt-l: `[latency]` lines in talon.log show moved/arrived times per sweep. Typical 2026-09-02 after fixes: see the module notes |
| Anything odd | ctrl-alt-t turns it off; `user.magnet_uninstall()` detaches it until the next reload |

## Verifying

1. ctrl-alt-d → sit normally, tilt your head up, look back. `talon.log` gets a
   `[trackdiag]` line every 500 ms: `rise` should climb ~20–40 mm on the tilt,
   `d_rise` shows it relative to when you switched the logger on, and
   `head_offset: {'offset_px': (0, -N)}` shows the resulting push. A CSV at
   `%APPDATA%\Talon\tracking_diag.csv` (10 Hz) has the raw eye positions.
   ctrl-alt-d again to stop — it does not run by itself.
2. `[head_offset] installed …` in the log after every start/reload confirms the
   proxy is in place.
3. ctrl-alt-m: keep the head still, follow the dots (centre, then 50 % and 95 %
   toward each edge). It prints per direction `k50`, `k95`, whether a linear
   gain is enough (`k95/k50` within ±8 %) or whether the shortfall grows faster
   than linearly (then it solves gain + curve), and a `SUGGEST` line with the
   exact settings. Also in `%APPDATA%\Talon\gaze_measure_last.json`.

## Gotchas learned building this

* Talon runs elevated here, so `repl.py` from a normal shell is "Access is
  denied". Hot-loading a throwaway `.py` into this folder and reading
  `talon.log` works without UAC.
* Never call `tracker.unregister`/`tracking_system.unregister` from *inside* a
  tracking callback: it stalled the tracker thread and the Tobii dropped off
  USB and re-enumerated. All (un)registrations here happen from actions/cron.
* Registrations made from a hotkey action are owned by the caller's resource
  context; `tracking_diag`/`gaze_measure` wrap theirs in their own module
  context so a `.talon` reload doesn't silently tear them down.
* A lazy `from .x import y` between two user modules fails intermittently
  under Talon's reloader; `head_offset` pushes its status function into
  `tracking_diag` instead.
* Removing the layer: delete `head_offset.py` *after* running
  `user.head_offset_uninstall()` (or restart Talon); ctrl-alt-h alone leaves
  the proxy installed but inert.
* UI Automation from Talon (`talon.ui.element_at`): wants **int** coordinates
  (screen rects are floats), takes 2-8 ms, works from a background thread,
  and is NOT disturbed by a click-through Talon canvas over the point. It has
  no `parent` accessor; hit-testing 3 px outside a child's rect finds the
  enclosing control. Chrome, XAML (taskbar) and Win32 all answer.
* Talon warns "User script started a thread" for `target_magnet.py`; user
  threads are not stopped on reload, so the module keeps its stop event on
  `eye_mouse_2.control2` and the new instance stops the old thread.
* ControlMouse2 internals (via `control2.last_state`): `zone1_mm = zone2_mm =
  45`, `gaze_active1/2`, `head_active`, `mouse_active`, `target_px`,
  `ctrl_px`. Gaze changes inside the zone do not move the cursor - the head
  does - which is why the magnet must freeze the head pose and place the
  cursor itself.
