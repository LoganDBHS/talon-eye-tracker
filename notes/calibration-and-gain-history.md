# Calibration and gain history

Why this file exists: Talon keeps one `calib.bin` and one `gaze_measure_last.json`,
both overwritten on every run, and `talon.log` rotates. On 2026-09-03 three
calibrations and a day of measurements were lost that way and the "up" correction
drifted away from the approved values without anyone noticing. Since that day the
guard backs up calibrations (`%APPDATA%\talon\calib_backups\`) and ctrl-alt-m keeps
dated copies (`%APPDATA%\talon\gaze_measure_history\`). This file is the human
summary; extend it whenever a value is approved or rejected.

## Approved states

| date | what | where |
|---|---|---|
| 2026-09-02 11:00 | head layer as eye-in-head credit; gains from the 11:00 measurement below | git tag `good-v1-eye-in-head-credit` |
| 2026-09-02 16:53 | target magnet + latency fixes on top of the above; up gain 0.77 / curve 0.38 confirmed in use | git tag `good-v2-magnet-latency` (= master after the 2026-09-03 revert) |

## Every ctrl-alt-m run so far (raw gaze, head still)

| run | calibration in use | bias x/y px | up gain / curve | ref dist mm | applied? |
|---|---|---|---|---|---|
| 09-02 10:00 | 09-01 | +28 / +21 | 0.79 / 0.47 | - | no (verdict: recalibrate) |
| 09-02 10:06 | 09-01 | -3 / +9 | 0.91 / 0.22 | - | no |
| 09-02 10:39 | 09-02 10:38 (419 KB) | +3 / -1 | 0.79 / 0.21 | - | yes |
| 09-02 11:00 | 09-02 10:59 (399 KB) | +14 / -8 | **0.77 / 0.38** | - | yes, **approved in use** |
| 09-02 16:23 | same | 0 / -5 | 0.79 / 0.32 | 473 | yes (ref distance 532 kept) |
| 09-03 10:54 | 09-03 10:53 bare eyes (427 KB) | +1 / -17 | 0.60 / 0.53 | 460 | yes |
| 09-03 11:28 | 09-03 11:28 glasses (420 KB) | +13 / -12 | 0.73 / 0.34 | 497 | yes |
| 09-03 11:40 | 09-03 11:40 bare eyes (450 KB, backed up) | -1 / -12 | 0.82 / 0.24 | 481 | yes, then reverted by hand to 0.77 / 0.38 |

Spread of the "up" suggestion across eight runs: gain 0.60 to 0.91, curve 0.21
to 0.53. Treat a single run as noisy. The still-head measurement also under-reports
the in-use undershoot at the top rows (natural head tilt): recorded 2026-09-03,
looking at Chrome's New Tab button (centre y 36) the raw gaze sat at y 80 to 100.

## Lessons

- Run the tracking health check (ctrl-alt-shift-d) before changing anything when
  pointing suddenly feels worse. Glasses lost the left eye completely on 2026-09-03.
- Compare a SUGGEST "up" pair against the approved 0.77 / 0.38 before Apply; a
  weaker curve makes the top tabs fall into the toolbar.
- One calibration per condition (glasses on / off); swap with ctrl-alt-shift-c
  instead of recalibrating. The gains belong to a calibration; when a calibration
  is swapped the settings should follow (not automated yet).
- Today's gap-target work lives on branch `gap-targets`; its no-eyes fix (frames
  with no detected eye carry a (0, 0) gaze) is worth cherry-picking on its own.
