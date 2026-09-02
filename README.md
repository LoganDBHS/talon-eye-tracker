# Talon Eye Tracker Setup

Hands-free-ish mouse control on Windows 11 using **Talon** with a **Tobii Eye Tracker 5** — eye tracking only, no speech, no microphone. Coarse pointing comes from Talon's control mouse (gaze + head); precision clicks come from the zoom mouse, committed with physical mouse buttons via an AutoHotkey bridge.

> Windows Eye Control does **not** work with the Tobii Eye Tracker 5 (Tobii confirms the ET5 never exposes the gaze HID device Windows requires). Talon is the workable path.

## Layout

| Repo path | Deploys to |
|---|---|
| `talon/eye_tracking_only/` | `%APPDATA%\talon\user\eye_tracking_only\` |
| `bridge/zoom_bridge.ahk` | anywhere (mine: `%LOCALAPPDATA%\Programs\TalonZoomBridge\`), autostarted via a Startup-folder shortcut, needs AutoHotkey v2 |

Also required: [talonhub/community](https://github.com/talonhub/community) cloned at `%APPDATA%\talon\user\community` (not vendored here).

## Hotkeys

| Key | Action |
|---|---|
| `Ctrl+Alt+E` | toggle control mouse (cursor follows gaze + head) |
| `Ctrl+Alt+Z` | toggle zoom mouse |
| `Ctrl+Alt+C` | run Talon's calibration; press again mid-run to cancel it. A banner reports how the run ended (saved / cancelled / failed and why) |
| `F4` | open zoom overlay at gaze point |
| `Shift+F4` / `Esc` | cancel zoom |
| left / right click (while zoomed) | commit left / right click at the gazed target |
| middle click (while zoomed) | cancel zoom |
| `Shift` + left click (while zoomed) | commit a double-click |
| second left click shortly after a commit | replayed at the committed spot (double-click) |

## How the bridge works

Talon's mouse tap hook (`tap.MCLICK`) is broken on this machine, so Talon can't see physical mouse buttons directly. Instead `zoom_pop.py` maintains flag files in `%TEMP%` (`talon_zoom_open.flag` while the zoom overlay is open, `talon_zoom_double.flag` for ~900ms after a commit), and `zoom_bridge.ahk` intercepts physical mouse buttons only while a flag exists, relaying them to Talon as `F13`–`F17` keystrokes. When no flag exists the AHK script does nothing at all.

Clicks are synthesized with a raw `SendInput` batch (absolute move + down + up in one call) so the always-running control mouse can't drag the cursor mid-click.

## Gotchas learned the hard way

- Talon uses its **own** calibration (`Ctrl+Alt+C`), not Tobii's. A failed run can wedge the tracker (`EyeCmdErr 0x20000502` in `talon.log`); unplug/replug the USB and recalibrate.
- Talon's calibration is silent about how it ended and cancels itself whenever its window loses focus. `calibration_guard.py` wraps it: progress text on the dots screen, `Ctrl+Alt+C` cancels, focus loss is ignored (`user.calibration_cancel_on_focus_loss = 1` restores Talon's behaviour), and the outcome (saved / cancelled / failed with the tracker error and point number) is shown in a banner, a notification and `talon.log` (`[calibguard]`).
- The three Tobii services must be **stopped/disabled** while Talon owns the tracker; Windows Update may re-enable them — first thing to check if tracking breaks.
- Talon runs elevated: killing it needs an elevated shell, and a plain `Stop-Process` fails *silently*.
- Never edit `community` files — override settings from your own folder with a more specific context (see `eye_tracking_settings.talon`).
- The double flag must be deleted **synchronously** before sending a synthetic click, or the bridge swallows Talon's own click.

## Known issue (open)

Tap-tap double-click in Explorer still doesn't register even though logs show two atomic clicks at the identical pixel ~130ms apart. Under investigation; `Shift`+click double-commit is the workaround.

## Head-pitch cursor offset layer (added 2026-08-31)

Gaze accuracy falls off toward the edges and the user reaches the top of the
32" screen by pitching the head up — which Tobii's head-compensated gaze cancels
out. The Tobii 5 gives Talon **no head-rotation data** (only the two 3-D
eyeball positions), so `talon/eye_tracking_only/head_offset.py` infers pitch
from the eye-centre's rise along the screen axis (and yaw from the inter-ocular
vector), turns that into a cursor offset on top of Talon's control mouse, and
also carries an optional gaze gain/curve correction. All knobs are live
settings in `head_tracking_settings.talon`; `tracking_diag.py` logs the raw
head data on demand and `gaze_measure.py` measures the implied gaze gain.

| Key | Action |
|---|---|
| `Ctrl+Alt+H` | head offset on/off |
| `Ctrl+Alt+R` | re-centre (current pose = neutral) |
| `Ctrl+Alt+D` | tracking diagnostic logger on/off |
| `Ctrl+Alt+M` | gaze gain measurement overlay (Esc cancels) |
| `Ctrl+Alt+T` | target magnet on/off: the cursor snaps onto the button / tab / link you look at and stays there until your gaze leaves (`target_magnet.py`, added 2026-09-02, uses Windows UI Automation) |
| `Ctrl+Alt+Y` / `Ctrl+Alt+N` | on the measurement results page: apply its SUGGEST values to `head_tracking_settings.talon` (previous file kept as `%APPDATA%\Talon\head_tracking_settings.backup`) / discard. Same as the Apply / Discard buttons; nothing changes unless you apply |

Full details, tuning table and gotchas: `talon/eye_tracking_only/README.md`.
Findings: the public Talon build is still 0.4.0 (Jul 2023) and already has the
Gaze/Head Control toggles (tray → Eye Tracking); the newer settings in the
changelog are Patreon-beta only.
