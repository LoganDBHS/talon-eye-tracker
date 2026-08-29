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
| `Ctrl+Alt+C` | run Talon's calibration |
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
- The three Tobii services must be **stopped/disabled** while Talon owns the tracker; Windows Update may re-enable them — first thing to check if tracking breaks.
- Talon runs elevated: killing it needs an elevated shell, and a plain `Stop-Process` fails *silently*.
- Never edit `community` files — override settings from your own folder with a more specific context (see `eye_tracking_settings.talon`).
- The double flag must be deleted **synchronously** before sending a synthetic click, or the bridge swallows Talon's own click.

## Known issue (open)

Tap-tap double-click in Explorer still doesn't register even though logs show two atomic clicks at the identical pixel ~130ms apart. Under investigation; `Shift`+click double-commit is the workaround.
