"""Tracking diagnostic logger (Talon 0.4.0, Tobii Eye Tracker 5).

Toggle with ctrl-alt-d (see head_tracking.talon) or `user.tracking_diag_toggle()`.
While ON it logs one line every 500 ms to talon.log (prefix `[trackdiag]`) and
writes a 10 Hz CSV to %APPDATA%/Talon/tracking_diag.csv. OFF by default; it
registers nothing on the tracking stream until enabled.

What Talon actually gets from the Tobii 5 (verified 2026-08-31 by introspection):
  * one stream, 'gaze' (id 1280); the 'head' topic re-delivers the same GazeFrame
  * per eye: 3D eyeball position `pos` (mm, tracker frame), gaze point, pupil,
    validity. There is NO head-rotation stream: Tobii's head-pose algorithm runs
    on the host inside Tobii's own software, which Talon bypasses (direct USB).
So "head pose" here is derived from the two eyeball positions:
  * rise  = eye-centre height along the SCREEN's up axis (mm). Pitching the head
            up swings the eyes up around the neck pivot -> rise increases.
            This is the pitch proxy (it also responds to sitting taller).
  * dist  = eye-centre distance from the screen plane (mm)
  * yaw   = heading of the left->right eye vector (deg, + = turned right); real rotation
  * roll  = tilt of that vector (deg, + = right eye higher)
The tracker frame is tilted by the mount angle (tracker.geom.angle, 20 deg for a
bottom-bezel tick); rise/dist are rotated into the screen frame so leaning in
does not masquerade as looking up.

Per-frame work in the callback is a single attribute store; all maths happens
in the 500 ms / 100 ms cron ticks.
"""
import csv
import math
import os
import time

from talon import Module, actions, app, cron, tracking_system, ui
from talon.scripting import rctx
from talon_init import TALON_HOME

mod = Module()
_ctx = rctx.active()  # this module's resource context

_CSV_PATH = os.path.join(TALON_HOME, "tracking_diag.csv")
_latest = [None]          # most recent GazeFrame (written by the tracker thread)
_jobs = {"log": None, "csv": None}
_csv = {"file": None, "writer": None}
_baseline = {}            # rise/yaw/dist at enable time, to show deltas
_tilt = {}                # cos/sin of the mount angle, filled from the first frame


def _on_gaze(frame):
    # tracker thread: keep this trivial
    _latest[0] = frame


def _tilt_cs(frame):
    if not _tilt:
        angle = 20.0
        try:
            angle = float(frame.tracker.geom.angle)
        except Exception:
            pass
        _tilt["c"] = math.cos(math.radians(angle))
        _tilt["s"] = math.sin(math.radians(angle))
        _tilt["deg"] = angle
    return _tilt["c"], _tilt["s"]


def head_from_frame(frame):
    """Derive (rise, dist, yaw, roll, centre_x) in the screen frame from a GazeFrame.
    Returns None unless both eyes are detected."""
    l, r = frame.left, frame.right
    if not (l.detected and r.detected):
        return None
    c, s = _tilt_cs(frame)
    lp, rp = l.pos, r.pos
    cx = (lp.x + rp.x) * 0.5
    cy = (lp.y + rp.y) * 0.5
    cz = (lp.z + rp.z) * 0.5
    rise = cy * c + cz * s          # along screen-up
    dist = cz * c - cy * s          # out of the screen plane, toward the user
    vx, vy, vz = rp.x - lp.x, rp.y - lp.y, rp.z - lp.z
    yaw = math.degrees(math.atan2(vz, vx))     # + when the head turns right
    roll = math.degrees(math.atan2(vy, vx))    # + when the right eye is higher
    return rise, dist, yaw, roll, cx


# head_offset.py registers its status function here when it loads (it imports
# this module, so it always re-runs after a reload of this file). A lazy
# relative import would fail intermittently under Talon's reloader.
_offset_provider = [None]


def set_offset_provider(fn):
    _offset_provider[0] = fn


def _offset_info():
    fn = _offset_provider[0]
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def _log_tick():
    frame = _latest[0]
    if frame is None:
        print("[trackdiag] no gaze frames yet (tracker attached? control mouse on?)")
        return
    age = time.perf_counter() - frame.ts
    g = frame.gaze
    scr = ui.main_screen().rect
    if g:
        px = (scr.x + g.x * scr.width, scr.y + g.y * scr.height)
        line = f"[trackdiag] age={age * 1000:.0f}ms gaze=({g.x:.3f},{g.y:.3f}) px=({px[0]:.0f},{px[1]:.0f})"
    else:
        line = f"[trackdiag] age={age * 1000:.0f}ms gaze=None"
    line += f" L={int(frame.left.detected)} R={int(frame.right.detected)}"
    head = head_from_frame(frame)
    if head:
        rise, dist, yaw, roll, cx = head
        if not _baseline:
            _baseline.update(rise=rise, yaw=yaw, dist=dist)
        line += (f" | head: rise={rise:.1f}mm dist={dist:.0f}mm x={cx:+.0f}mm"
                 f" yaw={yaw:+.1f}deg roll={roll:+.1f}deg"
                 f" | vs start: d_rise={rise - _baseline['rise']:+.1f}mm"
                 f" d_yaw={yaw - _baseline['yaw']:+.1f}deg d_dist={dist - _baseline['dist']:+.0f}mm")
    else:
        line += " | head: (eye(s) not detected)"
    info = _offset_info()
    if info:
        line += f" | head_offset: {info}"
    print(line)


def _csv_tick():
    frame = _latest[0]
    w = _csv["writer"]
    if frame is None or w is None:
        return
    g = frame.gaze
    head = head_from_frame(frame) or (None,) * 5
    lp, rp = frame.left.pos, frame.right.pos
    info = _offset_info() or {}
    off = info.get("offset_px", ("", ""))
    w.writerow([
        f"{frame.ts:.4f}", frame.num,
        f"{g.x:.4f}" if g else "", f"{g.y:.4f}" if g else "",
        int(frame.left.detected), int(frame.right.detected),
        f"{lp.x:.1f}", f"{lp.y:.1f}", f"{lp.z:.1f}", f"{rp.x:.1f}", f"{rp.y:.1f}", f"{rp.z:.1f}",
        *(f"{v:.2f}" if v is not None else "" for v in head),
        off[0], off[1],
    ])


def _enable():
    _latest[0] = None
    _baseline.clear()
    try:
        f = open(_CSV_PATH, "w", newline="")
        w = csv.writer(f)
        w.writerow(["ts", "num", "gaze_x", "gaze_y", "left_ok", "right_ok",
                    "lx", "ly", "lz", "rx", "ry", "rz",
                    "rise_mm", "dist_mm", "yaw_deg", "roll_deg", "centre_x_mm",
                    "offset_px_x", "offset_px_y"])
        _csv["file"], _csv["writer"] = f, w
    except OSError as ex:
        print(f"[trackdiag] CSV disabled: {ex}")
    # own these registrations from THIS module's resource context, not the
    # caller's (a hotkey/.talon file or another script), so a reload of the
    # caller doesn't silently tear the logger down
    with _ctx.enter():
        tracking_system.register("gaze", _on_gaze)
        _jobs["log"] = cron.interval("500ms", _log_tick)
        _jobs["csv"] = cron.interval("100ms", _csv_tick)
    print(f"[trackdiag] ENABLED - logging every 500ms; CSV at {_CSV_PATH}")


def _disable():
    # never unregister from inside a tracking callback (it stalls the tracker
    # thread and the Tobii drops off USB); this runs from an action/cron thread
    try:
        tracking_system.unregister("gaze", _on_gaze)
    except Exception:
        pass
    for k in _jobs:
        if _jobs[k]:
            cron.cancel(_jobs[k])
            _jobs[k] = None
    if _csv["file"]:
        try:
            _csv["file"].flush()
            _csv["file"].close()
        except Exception:
            pass
    _csv["file"] = _csv["writer"] = None
    print("[trackdiag] DISABLED")


def enabled():
    return _jobs["log"] is not None


# --- 4 s health check (ctrl-alt-shift-d): are both eyes seen, how far, how jittery ---
# Added 2026-09-03 after glasses cost the left eye (0 of 133 frames) and every
# symptom downstream looked like a magnet or calibration fault.
_health = {"frames": []}


def _health_on_gaze(frame):
    _health["frames"].append(frame)


def _health_report():
    try:
        tracking_system.unregister("gaze", _health_on_gaze)
    except Exception:
        pass
    fr = _health["frames"]
    n = len(fr)
    if n == 0:
        msg = "Tracking health: NO frames in 4 s (tracker attached? Tobii services stopped?)"
        print(f"[trackdiag] {msg}")
        actions.user.banner(msg, "warn")
        return
    both = sum(1 for f in fr if f.left.detected and f.right.detected)
    left = sum(1 for f in fr if f.left.detected)
    right = sum(1 for f in fr if f.right.detected)
    zs = [f.left.pos.z for f in fr if f.left.detected] + [f.right.pos.z for f in fr if f.right.detected]
    dist = sum(zs) / len(zs) if zs else 0.0
    r = ui.main_screen().rect
    pts = [(f.gaze.x * r.width, f.gaze.y * r.height) for f in fr if f.gaze is not None and (f.left.detected or f.right.detected)]
    jumps = sorted(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))
    p50 = jumps[len(jumps) // 2] if jumps else 0.0
    big = sum(1 for j in jumps if j > 120)
    kind = "on" if both >= 0.9 * n else ("warn" if max(left, right) >= 0.9 * n else "off")
    verdict = {"on": "OK", "warn": "ONE EYE ONLY (glasses reflection? lighting?)", "off": "EYES NOT SEEN"}[kind]
    msg = (f"Tracking health: {verdict} - both eyes {100 * both // n}% (L {100 * left // n}%, R {100 * right // n}%), "
           f"{dist:.0f} mm away, {n} frames/4 s, frame jump median {p50:.0f} px, {big} jumps > 120 px")
    print(f"[trackdiag] {msg}")
    actions.user.banner(msg, kind)


def health_check():
    _health["frames"] = []
    with _ctx.enter():
        tracking_system.register("gaze", _health_on_gaze)
        cron.after("4s", _health_report)
    actions.user.banner("Tracking health: sampling 4 s - look around normally", "info")


@mod.action_class
class Actions:
    def tracking_diag_toggle():
        """Toggle the eye/head tracking diagnostic logger (talon.log + CSV)"""
        if enabled():
            _disable()
            app.notify("Tracking diag", "OFF")
        else:
            _enable()
            app.notify("Tracking diag", "ON - see talon.log [trackdiag] / tracking_diag.csv")

    def tracking_diag_enabled() -> bool:
        """Is the tracking diagnostic logger running?"""
        return enabled()

    def tracking_health():
        """4 s check: are both eyes seen, at what distance, how jittery - result in a banner and talon.log"""
        health_check()
