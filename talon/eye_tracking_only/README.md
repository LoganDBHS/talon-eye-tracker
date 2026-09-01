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
| `eye_tracking_tuning.py` | commented-out legacy/zoom knobs |

## Hotkeys

| Key | Action |
|---|---|
| ctrl-alt-e | Control Mouse on/off (built-in; gaze + head control are both ON in the tray menu) |
| ctrl-alt-h | head offset on/off (`user.head_offset_toggle`) |
| ctrl-alt-r | re-centre: the pose you hold NOW becomes neutral (`user.head_offset_recenter`) |
| ctrl-alt-d | diagnostic logger on/off (`user.tracking_diag_toggle`) |
| ctrl-alt-m | gaze gain measurement overlay, Esc cancels (`user.gaze_measure_start`) |
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

Offset = gain × (pose − neutral) beyond a dead zone, low-passed, clamped. It is
applied **upstream**: the gaze frames Talon's control mouse receives are
replaced by corrected copies (gaze correction from ctrl-alt-m, then the head
offset), so Talon does all its own smoothing/jump logic on corrected data and
nothing intercepts its cursor moves. (The first version wrapped
`ctrl.mouse_move` instead — that stuttered, because Talon glides toward its
target by re-reading the cursor through a path the wrapper couldn't see.)
The hook is registered in Talon's own tracking context and survives reloads of
this folder; `user.head_offset_uninstall()` restores the raw stream.

**Lift-only** (`head_lift_only = 1`): only raising the head above its resting
height moves the cursor (up). Lowering it — which happens naturally when
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
| Head-up doesn't reach the top edge | raise `user.head_gain_y` (6 → 8 → 10) |
| Cursor overshoots / feels twitchy on tilt | lower `user.head_gain_y`, or raise `user.head_smoothing_ms` (80 → 150) |
| Breathing / small posture moves the cursor | raise `user.head_deadzone_y_mm` (3 → 5) |
| First part of a tilt does nothing | lower `user.head_deadzone_y_mm` (3 → 1.5) |
| Cursor drifts over minutes (slouching) | glance at the middle of the screen for a few seconds (neutral re-learns there); lower `user.head_neutral_seconds` to make that faster; ctrl-alt-r forces it |
| Cursor sits pushed after I sit back down | ctrl-alt-r (or wait: `user.head_lost_recenter_seconds` re-anchors after you were away ≥ 5 s) |
| Want head-turn to reach the side edges | `user.head_gain_x = 8` (0 = off); `user.head_deadzone_x_deg` ≥ 1 |
| Cursor lags the head | lower `user.head_smoothing_ms` (80 → 40) |
| Offset ever goes wild | `user.head_max_offset_mm` clamps it (400); ctrl-alt-h turns the layer off |
| Gaze itself stops short of / overshoots an edge (head still) | run ctrl-alt-m and paste its `SUGGEST` lines: per-side `user.gaze_gain_left/right/up/down`, `user.gaze_curve_*` (only where it says non-linear) and `user.gaze_map_rotation_deg`. Its `residual` line shows the error left with the current values |
| Everything is off in the same direction / eyes disagree a lot | that's calibration, not gain: ctrl-alt-c at the distance you sit; ctrl-alt-m says which eye is worse (tray → Eye Tracking → Only Left/Right Eye) |

Default gains: half the screen height is 196 mm; 6 mm/mm × ~30 mm of rise
≈ 180 mm, i.e. a natural look-up lands near the top edge.

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
