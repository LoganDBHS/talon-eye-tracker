"""Gaze gain measurement (Task 4 fallback support).

ctrl-alt-m (or `user.gaze_measure_start()`) shows a fullscreen overlay with a
sequence of targets at known screen fractions - centre, then 50% and 95% of the
way to each edge - and records where the gaze / control-mouse target actually
lands for each one. It then computes the implied gain

    k = target_eccentricity / measured_eccentricity

at both eccentricities per direction and says whether a LINEAR gain
(user.gaze_gain_x / _y) is sufficient (k50 ~= k95) or whether the shortfall grows
faster than linearly toward the edge, in which case it solves the quadratic
model used by head_offset.py,  u_true = a*u + b*u*|u|,  and reports a (gain) and
b (curve) for user.gaze_gain_* / user.gaze_curve_*.

KEEP YOUR HEAD STILL during the run (eyes only): this measures the gaze mapping,
not head movement. Esc cancels. Results go to talon.log (prefix [gazemeasure]),
%APPDATA%/Talon/gaze_measure_last.json, and stay on screen until Esc.

Measurement uses the RAW control-mouse target (before any gain correction or
head offset), so it is valid whatever the current settings are; if the control
mouse is off it falls back to the raw gaze point from the tracker.
"""
import json
import os
import statistics
import time

from talon import Module, app, canvas, cron, tracking_system, ui
from talon.scripting import rctx
from talon_init import TALON_HOME

from . import head_offset

mod = Module()
_ctx = rctx.active()

_OUT = os.path.join(TALON_HOME, "gaze_measure_last.json")
SETTLE_S = 1.5      # time to let the eyes arrive on a target
SAMPLE_S = 1.2      # sampling window per target
LINEAR_TOL = 0.08   # |k95/k50 - 1| below this -> linear gain is enough

# (label, ecc_x, ecc_y): eccentricity as a fraction of the half-screen, -1..1
_TARGETS = [
    ("centre", 0.0, 0.0),
    ("up 50%", 0.0, -0.5), ("up 95%", 0.0, -0.95),
    ("down 50%", 0.0, 0.5), ("down 95%", 0.0, 0.95),
    ("left 50%", -0.5, 0.0), ("left 95%", -0.95, 0.0),
    ("right 50%", 0.5, 0.0), ("right 95%", 0.95, 0.0),
]


class _Run:
    canvas = None
    job = None
    idx = 0
    phase = "idle"       # idle | settle | sample | results
    phase_t0 = 0.0
    latest = None        # latest GazeFrame
    samples = []         # (gaze_px, raw_px) tuples for the current target
    per_target = []      # results per target
    report_lines = []
    rect = None


_run = _Run()


def _on_gaze(frame):
    _run.latest = frame          # tracker thread: store only


def _px_from_gaze(frame):
    g = frame.gaze if frame else None
    if not g or not (frame.left.detected or frame.right.detected):
        return None
    r = _run.rect
    return (r.x + g.x * r.width, r.y + g.y * r.height)


def _target_px(ecc_x, ecc_y):
    r = _run.rect
    return (r.x + r.width / 2 + ecc_x * r.width / 2, r.y + r.height / 2 + ecc_y * r.height / 2)


def _tick():
    run = _run
    now = time.perf_counter()
    if run.phase == "settle":
        if now - run.phase_t0 >= SETTLE_S:
            run.phase, run.phase_t0, run.samples = "sample", now, []
    elif run.phase == "sample":
        g = _px_from_gaze(run.latest)
        raw = head_offset.last_raw_target()
        if g is not None:
            run.samples.append((g, raw))
        if now - run.phase_t0 >= SAMPLE_S:
            _finish_target()
            run.idx += 1
            if run.idx >= len(_TARGETS):
                _compute_results()
                run.phase = "results"
                run.job = None
                return False      # stop this cron interval
            run.phase, run.phase_t0 = "settle", now


def _finish_target():
    run = _run
    label, ex, ey = _TARGETS[run.idx]
    r = run.rect
    cx, cy, hx, hy = r.x + r.width / 2, r.y + r.height / 2, r.width / 2, r.height / 2
    entry = {"label": label, "target_ecc": (ex, ey), "n": len(run.samples)}
    if run.samples:
        gx = statistics.median(s[0][0] for s in run.samples)
        gy = statistics.median(s[0][1] for s in run.samples)
        entry["gaze_ecc"] = ((gx - cx) / hx, (gy - cy) / hy)
        raws = [s[1] for s in run.samples if s[1] is not None]
        if len(raws) >= 3:
            rx = statistics.median(p[0] for p in raws)
            ry = statistics.median(p[1] for p in raws)
            entry["cursor_ecc"] = ((rx - cx) / hx, (ry - cy) / hy)
        entry["measured_ecc"] = entry.get("cursor_ecc", entry["gaze_ecc"])
        entry["source"] = "cursor" if "cursor_ecc" in entry else "gaze"
    run.per_target.append(entry)
    print(f"[gazemeasure] {label}: {entry}")


def _solve_quadratic(m1, t1, m2, t2):
    """Solve a*m + b*m*|m| = t for the two points; returns (a, b) or None."""
    a11, a12 = m1, m1 * abs(m1)
    a21, a22 = m2, m2 * abs(m2)
    det = a11 * a22 - a12 * a21
    if abs(det) < 1e-9:
        return None
    a = (t1 * a22 - t2 * a12) / det
    b = (a11 * t2 - a21 * t1) / det
    return a, b


def _compute_results():
    run = _run
    by = {e["label"]: e for e in run.per_target}
    lines = []
    out = {"targets": run.per_target, "directions": {}, "suggest": {}}

    c = by.get("centre", {}).get("measured_ecc")
    if c:
        r = run.rect
        lines.append(f"centre bias: {c[0] * r.width / 2:+.0f} px x, {c[1] * r.height / 2:+.0f} px y "
                     f"(calibration offset; not a gain issue)")

    axis_fits = {"x": [], "y": []}
    for direction, axis, idx in (("up", "y", 1), ("down", "y", 1), ("left", "x", 0), ("right", "x", 0)):
        e50, e95 = by.get(f"{direction} 50%"), by.get(f"{direction} 95%")
        if not (e50 and e95 and "measured_ecc" in e50 and "measured_ecc" in e95):
            lines.append(f"{direction}: no data")
            continue
        m1, m2 = e50["measured_ecc"][idx], e95["measured_ecc"][idx]
        t1, t2 = e50["target_ecc"][idx], e95["target_ecc"][idx]
        if abs(m1) < 0.05 or abs(m2) < 0.05:
            lines.append(f"{direction}: measured eccentricity ~0, cannot fit (were you looking at the dot?)")
            continue
        k1, k2 = t1 / m1, t2 / m2
        ratio = k2 / k1 if k1 else float("nan")
        d = {"k50": round(k1, 3), "k95": round(k2, 3), "ratio": round(ratio, 3),
             "measured50": round(m1, 3), "measured95": round(m2, 3), "source": e95.get("source")}
        if abs(ratio - 1) <= LINEAR_TOL:
            d["model"] = "linear"
            d["gain"] = round((k1 + k2) / 2, 3)
            d["curve"] = 0.0
            lines.append(f"{direction}: k50={k1:.2f} k95={k2:.2f} (ratio {ratio:.2f}) -> LINEAR ok, gain ~{d['gain']:.2f}")
        else:
            q = _solve_quadratic(m1, t1, m2, t2)
            if q:
                d["model"] = "quadratic"
                d["gain"], d["curve"] = round(q[0], 3), round(q[1], 3)
                shape = "grows faster than linear near the edge (extrapolation curvature)" if ratio > 1 \
                    else "shrinks toward the edge (edge compression / clamp)"
                lines.append(f"{direction}: k50={k1:.2f} k95={k2:.2f} (ratio {ratio:.2f}) -> NOT linear: error {shape}; "
                             f"quadratic fit gain={q[0]:.2f} curve={q[1]:.2f}")
            else:
                d["model"] = "unfit"
                lines.append(f"{direction}: k50={k1:.2f} k95={k2:.2f} (ratio {ratio:.2f}) -> could not fit")
        out["directions"][direction] = d
        if "gain" in d:
            axis_fits[axis].append(d)

    for axis in ("x", "y"):
        fits = axis_fits[axis]
        if not fits:
            continue
        gain = sum(f["gain"] for f in fits) / len(fits)
        curve = sum(f["curve"] for f in fits) / len(fits)
        model = "quadratic" if any(f["model"] == "quadratic" for f in fits) else "linear"
        out["suggest"][axis] = {"gain": round(gain, 3), "curve": round(curve, 3), "model": model}
        asym = ""
        if len(fits) == 2 and abs(fits[0]["gain"] - fits[1]["gain"]) > 0.15 * max(abs(gain), 1e-6):
            asym = "  (sides differ >15%: consider recalibrating before trusting this)"
        lines.append(f"SUGGEST {axis}: user.gaze_gain_{axis} = {gain:.2f}" +
                     (f"   user.gaze_curve_{axis} = {curve:.2f}" if model == "quadratic" else "   (curve stays 0)") + asym)

    run.report_lines = lines
    for ln in lines:
        print(f"[gazemeasure] {ln}")
    try:
        with open(_OUT, "w") as f:
            json.dump(out, f, indent=2)
        print(f"[gazemeasure] wrote {_OUT}")
    except OSError as ex:
        print(f"[gazemeasure] could not write {_OUT}: {ex}")
    app.notify("Gaze measure", "; ".join(ln for ln in lines if ln.startswith("SUGGEST")) or "done - see talon.log")


def _draw(c):
    run = _run
    if run.phase == "idle":
        return False
    paint = c.paint
    paint.style = paint.Style.FILL
    paint.color = "3c3c3c"
    c.draw_rect(c.rect)
    paint.color = "ffffff"
    paint.textsize = 28
    r = c.rect
    if run.phase in ("settle", "sample"):
        label, ex, ey = _TARGETS[run.idx]
        tx, ty = _target_px(ex, ey)
        paint.style = paint.Style.STROKE
        paint.stroke_width = 3
        paint.color = "ffffff"
        c.draw_circle(tx, ty, 22)
        paint.style = paint.Style.FILL
        paint.color = "00ff00" if run.phase == "sample" else "ffb000"
        c.draw_circle(tx, ty, 8)
        paint.color = "dddddd"
        msg = (f"Gaze measurement {run.idx + 1}/{len(_TARGETS)}: look at the dot, keep your HEAD STILL "
               f"({'sampling' if run.phase == 'sample' else 'settling'}). Esc cancels.")
        c.draw_text(msg, r.x + 40, r.y + r.height / 2 + 120)
    else:
        y = r.y + 80
        c.draw_text("Gaze gain measurement - results (Esc to close)", r.x + 60, y)
        paint.textsize = 24
        for ln in run.report_lines:
            y += 40
            c.draw_text(ln, r.x + 60, y)
        y += 60
        paint.color = "aaaaaa"
        c.draw_text(f"Also written to talon.log and {_OUT}", r.x + 60, y)


def _on_key(e):
    if e.key == "esc":
        stop()


def start():
    run = _run
    if run.phase != "idle":
        return
    run.rect = ui.main_screen().rect
    run.idx, run.samples, run.per_target, run.report_lines, run.latest = 0, [], [], [], None
    with _ctx.enter():
        tracking_system.register("gaze", _on_gaze)
        cv = canvas.Canvas.from_screen(ui.main_screen())
        cv.fullscreen = True
        cv.blocks_mouse = True
        cv.focused = True
        cv.register("draw", _draw)
        cv.register("key", _on_key)
        run.canvas = cv
        run.phase, run.phase_t0 = "settle", time.perf_counter()
        run.job = cron.interval("50ms", _tick)
    print("[gazemeasure] started")


def stop():
    run = _run
    # runs from the canvas key handler / an action, never inside a tracking callback
    try:
        tracking_system.unregister("gaze", _on_gaze)
    except Exception:
        pass
    if run.job:
        cron.cancel(run.job)
        run.job = None
    if run.canvas:
        try:
            run.canvas.unregister("draw", _draw)
            run.canvas.unregister("key", _on_key)
            run.canvas.fullscreen = False
            run.canvas.blocks_mouse = False
            run.canvas.close()
        except Exception:
            pass
        run.canvas = None
    run.phase = "idle"
    print("[gazemeasure] stopped")


@mod.action_class
class Actions:
    def gaze_measure_start():
        """Run the gaze gain measurement overlay (Esc cancels)"""
        start()

    def gaze_measure_stop():
        """Close the gaze gain measurement overlay"""
        stop()
