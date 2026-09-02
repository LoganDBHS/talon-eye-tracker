"""Gaze gain measurement (Task 4 fallback support).

ctrl-alt-m (or `user.gaze_measure_start()`) shows a fullscreen overlay with a
sequence of targets at known screen fractions - centre, then 50% and 95% of the
way to each edge - and records where the RAW GAZE actually lands for each one
(median of ~1.2 s once your gaze has dwelt near the dot). It then reports:

  * global bias (mean gaze-minus-target) and a rotation estimate of the map:
    those are CALIBRATION errors - recalibrate, don't fix them with gain
  * per direction the implied gain k = target/measured at 50% and 95% and
    whether a LINEAR gain (user.gaze_gain_x/_y) is enough (k50 ~= k95) or the
    shortfall grows faster than linearly toward the edge, in which case it
    solves the quadratic model used by head_offset.py, u_true = a*u + b*u*|u|
  * per-eye results: how far the two eyes disagree and which eye is closer to
    the targets (tray > Eye Tracking > Only Left/Right Eye if one is much worse)

KEEP YOUR HEAD STILL during the run (eyes only). Esc cancels. Results go to
talon.log (prefix [gazemeasure]), %APPDATA%/Talon/gaze_measure_last.json, and
stay on screen with two buttons: APPLY writes the SUGGEST values into
head_tracking_settings.talon (Talon hot-reloads it; the previous file is saved
to %APPDATA%/Talon/head_tracking_settings.backup) and DISCARD just closes.
Click a button, or use ctrl-alt-y (apply) / ctrl-alt-n (discard); nothing is
changed unless you apply. The control-mouse cursor target is recorded too, for
information only; it lags/freezes when Talon pauses the control mouse.
"""
import datetime
import json
import math
import os
import re
import shutil
import statistics
import time

from talon import Module, app, canvas, cron, tracking_system, ui
from talon.scripting import rctx
from talon.types import Rect
from talon_init import TALON_HOME

from . import head_offset

mod = Module()
_ctx = rctx.active()

_OUT = os.path.join(TALON_HOME, "gaze_measure_last.json")
_SETTINGS = os.path.join(os.path.dirname(__file__), "head_tracking_settings.talon")
_BACKUP = os.path.join(TALON_HOME, "head_tracking_settings.backup")
RESULTS_TIMEOUT_S = 180   # results page auto-closes (discards) after this
DWELL_S = 0.6        # gaze must stay near the dot this long before sampling starts
DWELL_RADIUS = 0.18  # "near" = within this fraction of the half-screen (~230 px)
SETTLE_MAX_S = 5.0   # give up waiting after this and sample anyway (flagged)
SAMPLE_S = 1.2       # sampling window per target
LINEAR_TOL = 0.08    # |k95/k50 - 1| below this -> linear gain is enough
BIAS_WARN = 0.06     # global bias above this (fraction of half-screen) -> recalibrate
ROT_WARN = 2.0       # degrees of map rotation -> recalibrate
EYE_WARN_PX = 60     # inter-eye disagreement above this -> suggest single-eye mode
HEAD_MOVE_WARN_MM = 4.0   # head rise above resting during a target beyond this -> warn (eyes-only run wanted)

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
    near_since = None    # when the gaze first came near the current dot
    unsettled = False
    latest = None        # latest GazeFrame
    samples = []         # (gaze_ecc, left_ecc|None, right_ecc|None, cursor_ecc|None)
    lifts = []           # head rise above resting (mm) per sample, from head_offset
    dists = []           # eye distance (mm) per sample, from head_offset
    per_target = []
    report_lines = []
    rect = None
    suggest = {}         # direction -> {gain, curve}; "rotation" -> deg (from the last run)
    verdict = None       # "gain" | "recalibrate"
    buttons = {}         # name -> Rect, laid out by _draw on the results page
    status = None        # list of lines shown after apply (or an apply error)
    applied = False


_run = _Run()


def _on_gaze(frame):
    _run.latest = frame          # tracker thread: store only


def _ecc_from_norm(g):
    return (g.x * 2 - 1, g.y * 2 - 1)


def _ecc_from_px(x, y):
    r = _run.rect
    return ((x - (r.x + r.width / 2)) / (r.width / 2), (y - (r.y + r.height / 2)) / (r.height / 2))


def _sample_now():
    """Current (gaze, left, right, cursor) eccentricities or None if no gaze."""
    f = _run.latest
    if f is None or not (f.left.detected or f.right.detected) or not f.gaze:
        return None
    g = _ecc_from_norm(f.gaze)
    l = _ecc_from_norm(f.left.gaze) if f.left.detected else None
    r = _ecc_from_norm(f.right.gaze) if f.right.detected else None
    raw = head_offset.last_raw_target()
    c = _ecc_from_px(*raw) if raw else None
    return g, l, r, c


def _target_px(ecc_x, ecc_y):
    r = _run.rect
    return (r.x + r.width / 2 + ecc_x * r.width / 2, r.y + r.height / 2 + ecc_y * r.height / 2)


def _tick():
    run = _run
    now = time.perf_counter()
    if run.phase == "settle":
        _label, ex, ey = _TARGETS[run.idx]
        s = _sample_now()
        near = s is not None and math.hypot(s[0][0] - ex, s[0][1] - ey) < DWELL_RADIUS
        if near:
            run.near_since = run.near_since or now
        else:
            run.near_since = None
        dwelt = run.near_since is not None and now - run.near_since >= DWELL_S
        timed_out = now - run.phase_t0 >= SETTLE_MAX_S
        if dwelt or timed_out:
            run.unsettled = not dwelt
            run.phase, run.phase_t0, run.samples, run.lifts, run.dists = "sample", now, [], [], []
    elif run.phase == "sample":
        s = _sample_now()
        if s is not None:
            run.samples.append(s)
            snap = head_offset.status_snapshot()
            lift = snap.get("d_rise_mm")
            if lift is not None:
                run.lifts.append(lift)
            if snap.get("dist_mm"):
                run.dists.append(snap["dist_mm"])
        if now - run.phase_t0 >= SAMPLE_S:
            _finish_target()
            run.idx += 1
            if run.idx >= len(_TARGETS):
                run.phase = "results"      # before computing: _draw would index _TARGETS[9]
                _compute_results()
                run.job = None
                cron.after(f"{RESULTS_TIMEOUT_S}s", _auto_close)
                return False      # stop this cron interval
            run.phase, run.phase_t0, run.near_since = "settle", now, None


def _median_pair(pairs):
    pairs = [p for p in pairs if p is not None]
    if len(pairs) < 3:
        return None
    return (statistics.median(p[0] for p in pairs), statistics.median(p[1] for p in pairs))


def _finish_target():
    run = _run
    label, ex, ey = _TARGETS[run.idx]
    entry = {"label": label, "target_ecc": (ex, ey), "n": len(run.samples), "unsettled": run.unsettled}
    if run.samples:
        entry["gaze_ecc"] = _median_pair([s[0] for s in run.samples])
        entry["left_ecc"] = _median_pair([s[1] for s in run.samples])
        entry["right_ecc"] = _median_pair([s[2] for s in run.samples])
        entry["cursor_ecc"] = _median_pair([s[3] for s in run.samples])
        prev = run.per_target[-1].get("cursor_ecc") if run.per_target else None
        if entry["cursor_ecc"] and prev and entry["cursor_ecc"] == prev:
            entry["cursor_stale"] = True   # control mouse paused; cursor didn't follow
        entry["measured_ecc"] = entry["gaze_ecc"]
        if run.lifts:
            entry["head_lift_mm"] = round(statistics.median(run.lifts), 1)
        if run.dists:
            entry["dist_mm"] = round(statistics.median(run.dists))
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
    r = run.rect
    hx, hy = r.width / 2, r.height / 2
    by = {e["label"]: e for e in run.per_target}
    good = [e for e in run.per_target if e.get("measured_ecc") and not e.get("unsettled")]
    lines = []
    out = {"targets": run.per_target, "directions": {}, "suggest": {}, "calibration": {}}
    recalibrate = []

    skipped = [e["label"] for e in run.per_target if e.get("unsettled") or not e.get("measured_ecc")]
    if skipped:
        lines.append(f"ignored (gaze never settled on the dot): {', '.join(skipped)}")
    # the fit is a function of the eyes' angle INSIDE the head (head_offset credits
    # head tilt against it live), so the run itself must be eyes-only
    moved = [(e["label"], e["head_lift_mm"]) for e in good
             if e.get("head_lift_mm") is not None and abs(e["head_lift_mm"]) > HEAD_MOVE_WARN_MM]
    if moved:
        worst = max(moved, key=lambda m: abs(m[1]))
        lines.append(f"HEAD MOVED during {', '.join(m[0] for m in moved)} (up to {worst[1]:+.0f} mm at {worst[0]}): "
                     f"the fit assumes eyes-only - redo with the head still before applying")
        out["head_moved"] = moved

    # --- calibration-type errors: global bias and rotation ---
    if good:
        bx = sum(e["measured_ecc"][0] - e["target_ecc"][0] for e in good) / len(good)
        by_ = sum(e["measured_ecc"][1] - e["target_ecc"][1] for e in good) / len(good)
        out["calibration"]["bias_px"] = (round(bx * hx), round(by_ * hy))
        lines.append(f"global bias: gaze lands {bx * hx:+.0f} px x, {by_ * hy:+.0f} px y from the targets on average")
        if abs(bx) > BIAS_WARN or abs(by_) > BIAS_WARN:
            recalibrate.append("bias")
    rot = None
    l95, r95 = by.get("left 95%"), by.get("right 95%")
    if l95 and r95 and l95.get("measured_ecc") and r95.get("measured_ecc"):
        dy = (r95["measured_ecc"][1] - l95["measured_ecc"][1]) * hy
        dx = (r95["measured_ecc"][0] - l95["measured_ecc"][0]) * hx
        rot = math.degrees(math.atan2(dy, dx))
        out["calibration"]["rotation_deg"] = round(rot, 2)
        lines.append(f"map rotation: {rot:+.1f} deg ({'clockwise' if rot > 0 else 'counter-clockwise'}; sit square, tracker level)")
        if abs(rot) > ROT_WARN:
            recalibrate.append("rotation")

    # --- per-eye ---
    eyes = [(e, e["left_ecc"], e["right_ecc"]) for e in good if e.get("left_ecc") and e.get("right_ecc")]
    if eyes:
        dis = sum(math.hypot((l[0] - rr[0]) * hx, (l[1] - rr[1]) * hy) for _e, l, rr in eyes) / len(eyes)
        err_l = sum(math.hypot((l[0] - e["target_ecc"][0]) * hx, (l[1] - e["target_ecc"][1]) * hy) for e, l, _r in eyes) / len(eyes)
        err_r = sum(math.hypot((rr[0] - e["target_ecc"][0]) * hx, (rr[1] - e["target_ecc"][1]) * hy) for e, _l, rr in eyes) / len(eyes)
        out["calibration"]["eyes"] = {"disagree_px": round(dis), "left_err_px": round(err_l), "right_err_px": round(err_r)}
        line = f"eyes: disagree by {dis:.0f} px on average; mean error left {err_l:.0f} px, right {err_r:.0f} px"
        if dis > EYE_WARN_PX and min(err_l, err_r) < 0.7 * max(err_l, err_r):
            better = "Left" if err_l < err_r else "Right"
            line += f" -> try tray > Eye Tracking > 'Only {better} Eye'"
        lines.append(line)

    # --- gain / curvature per direction ---
    axis_fits = {"x": [], "y": []}
    for direction, axis, idx in (("up", "y", 1), ("down", "y", 1), ("left", "x", 0), ("right", "x", 0)):
        e50, e95 = by.get(f"{direction} 50%"), by.get(f"{direction} 95%")
        if not (e50 and e95 and e50.get("measured_ecc") and e95.get("measured_ecc")) or e50.get("unsettled") or e95.get("unsettled"):
            lines.append(f"{direction}: no usable data")
            continue
        m1, m2 = e50["measured_ecc"][idx], e95["measured_ecc"][idx]
        t1, t2 = e50["target_ecc"][idx], e95["target_ecc"][idx]
        if abs(m1) < 0.05 or abs(m2) < 0.05:
            lines.append(f"{direction}: measured eccentricity ~0, cannot fit")
            continue
        k1, k2 = t1 / m1, t2 / m2
        ratio = k2 / k1 if k1 else float("nan")
        d = {"k50": round(k1, 3), "k95": round(k2, 3), "ratio": round(ratio, 3),
             "measured50": round(m1, 3), "measured95": round(m2, 3)}
        if abs(ratio - 1) <= LINEAR_TOL:
            d["model"], d["gain"], d["curve"] = "linear", round((k1 + k2) / 2, 3), 0.0
            lines.append(f"{direction}: k50={k1:.2f} k95={k2:.2f} (ratio {ratio:.2f}) -> LINEAR ok, gain ~{d['gain']:.2f}")
        else:
            q = _solve_quadratic(m1, t1, m2, t2)
            if q:
                d["model"], d["gain"], d["curve"] = "quadratic", round(q[0], 3), round(q[1], 3)
                shape = "grows faster than linear near the edge (extrapolation curvature)" if ratio > 1 \
                    else "shrinks toward the edge (edge compression)"
                lines.append(f"{direction}: k50={k1:.2f} k95={k2:.2f} (ratio {ratio:.2f}) -> NOT linear: error {shape}; "
                             f"quadratic fit gain={q[0]:.2f} curve={q[1]:.2f}")
            else:
                d["model"] = "unfit"
                lines.append(f"{direction}: k50={k1:.2f} k95={k2:.2f} (ratio {ratio:.2f}) -> could not fit")
        out["directions"][direction] = d
        if "gain" in d:
            axis_fits[axis].append(d)

    # per-side settings (head_offset.py applies gain/curve per side of centre)
    sugg = []
    for direction, d in out["directions"].items():
        if "gain" in d:
            sugg.append(f"user.gaze_gain_{direction} = {d['gain']:.2f}   user.gaze_curve_{direction} = {d['curve']:.2f}")
            out["suggest"][direction] = {"gain": d["gain"], "curve": d["curve"]}
    if rot is not None:
        sugg.append(f"user.gaze_map_rotation_deg = {rot:.1f}")
        out["suggest"]["rotation"] = round(rot, 2)
    dists = [e["dist_mm"] for e in good if e.get("dist_mm")]
    if dists:
        dist_mm = round(statistics.median(dists))
        sugg.append(f"user.gaze_ref_distance_mm = {dist_mm}")
        out["suggest"]["distance_mm"] = dist_mm
        lines.append(f"measured at eye distance {dist_mm} mm (range {min(dists)}-{max(dists)}); the gains above "
                     f"are rescaled live when you sit nearer/farther (gaze_ref_distance_mm)")
    for s in sugg:
        lines.append("SUGGEST " + s)

    # how good is the correction that is CURRENTLY in head_tracking_settings.talon?
    resid = []
    for e in good:
        gx, gy = e["measured_ecc"]
        px = r.x + hx + gx * hx
        py = r.y + hy + gy * hy
        cx_, cy_ = head_offset.transform_px(px, py, (0.0, 0.0), (0.0, 0.0))
        ex_, ey_ = (cx_ - (r.x + hx)) / hx, (cy_ - (r.y + hy)) / hy
        err = math.hypot((ex_ - e["target_ecc"][0]) * hx, (ey_ - e["target_ecc"][1]) * hy)
        raw = math.hypot((gx - e["target_ecc"][0]) * hx, (gy - e["target_ecc"][1]) * hy)
        e["corrected_err_px"], e["raw_err_px"] = round(err), round(raw)
        resid.append((err, raw, e["label"]))
    if resid:
        worst = max(resid)
        lines.append(f"residual with the CURRENT settings: mean {sum(x[0] for x in resid) / len(resid):.0f} px "
                     f"(raw {sum(x[1] for x in resid) / len(resid):.0f} px); worst {worst[0]:.0f} px at {worst[2]}")
        out["residual_px"] = {"mean": round(sum(x[0] for x in resid) / len(resid)), "worst": round(worst[0]), "worst_at": worst[2]}

    if recalibrate:
        lines.append(f"VERDICT: {' + '.join(recalibrate)} error dominates -> RECALIBRATE (ctrl-alt-c, head still, at the "
                     f"distance you sit) before applying any gain; then re-run ctrl-alt-m.")
        out["verdict"] = "recalibrate"
    else:
        lines.append("VERDICT: no large bias/rotation; the SUGGEST gains above are meaningful.")
        out["verdict"] = "gain"

    run.report_lines = lines
    run.suggest, run.verdict, run.status, run.applied = out["suggest"], out["verdict"], None, False
    for ln in lines:
        print(f"[gazemeasure] {ln}")
    try:
        with open(_OUT, "w") as f:
            json.dump(out, f, indent=2)
        print(f"[gazemeasure] wrote {_OUT}")
    except OSError as ex:
        print(f"[gazemeasure] could not write {_OUT}: {ex}")
    app.notify("Gaze measure", next((ln for ln in lines if ln.startswith("VERDICT")), "done - see talon.log"))


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
        _label, ex, ey = _TARGETS[run.idx]
        tx, ty = _target_px(ex, ey)
        paint.style = paint.Style.STROKE
        paint.stroke_width = 3
        paint.color = "ffffff"
        c.draw_circle(tx, ty, 22)
        paint.style = paint.Style.FILL
        if run.phase == "sample":
            paint.color = "00ff00"
        elif run.near_since:
            paint.color = "ffe000"
        else:
            paint.color = "ff8000"
        c.draw_circle(tx, ty, 8)
        paint.color = "dddddd"
        state = "sampling - hold" if run.phase == "sample" else ("locking on..." if run.near_since else "waiting for your gaze")
        msg = (f"Gaze measurement {run.idx + 1}/{len(_TARGETS)}: look at the dot, keep your HEAD STILL "
               f"({state}). Esc cancels.")
        c.draw_text(msg, r.x + 40, r.y + r.height / 2 + 120)
    else:
        y = r.y + 80
        c.draw_text("Gaze gain measurement - results", r.x + 60, y)
        paint.textsize = 24
        for ln in run.report_lines:
            y += 40
            c.draw_text(ln, r.x + 60, y)
        y += 50
        paint.color = "aaaaaa"
        c.draw_text(f"Also written to talon.log and {_OUT}", r.x + 60, y)
        y += 70
        _draw_buttons(c, r.x + 60, y)


def _draw_buttons(c, x, y):
    """Apply / Discard buttons (or Close after applying); records their rects for hit-testing."""
    run = _run
    paint = c.paint
    run.buttons = {}
    w, h, gap = 520, 90, 40
    if run.applied:
        specs = [("close", "Close", "555555")]
    elif run.suggest:
        if run.verdict == "recalibrate":
            specs = [("apply", "Apply anyway (verdict says recalibrate)", "9a6a00"),
                     ("discard", "Discard", "555555")]
        else:
            specs = [("apply", "Apply to settings", "1f7a1f"), ("discard", "Discard", "555555")]
    else:
        specs = [("discard", "Close (nothing to apply)", "555555")]
    x0 = x
    for name, label, color in specs:
        rect = Rect(x, y, w, h)
        run.buttons[name] = rect
        paint.style = paint.Style.FILL
        paint.color = color
        c.draw_rect(rect)
        paint.style = paint.Style.STROKE
        paint.stroke_width = 2
        paint.color = "dddddd"
        c.draw_rect(rect)
        paint.style = paint.Style.FILL
        paint.color = "ffffff"
        paint.textsize = 26
        c.draw_text(label, x + 24, y + h / 2 + 9)
        x += w + gap
    paint.textsize = 22
    paint.color = "aaaaaa"
    ty = y + h + 40
    for ln in run.status or ():
        c.draw_text(ln, x0, ty)
        ty += 34
    c.draw_text(f"Click a button, or ctrl-alt-y = apply, ctrl-alt-n = discard. "
                f"Auto-discards after {RESULTS_TIMEOUT_S} s.", x0, ty)


def _fmt(v, nd):
    return f"{v:.{nd}f}"


def apply_suggestions():
    """Write the last run's SUGGEST values into head_tracking_settings.talon.

    Only the `user.gaze_gain_*`, `user.gaze_curve_*`, `user.gaze_map_rotation_deg` and
    `user.gaze_ref_distance_mm` lines that have a suggestion are rewritten in place (everything else in the
    file, comments included, is untouched). Directions with no usable data keep
    their current values. The previous file is copied to _BACKUP first. Talon
    hot-reloads the settings file, so head_offset.py picks the values up at once.
    """
    run = _run
    if not run.suggest:
        run.status = ["nothing to apply (no usable SUGGEST values in the last run)"]
        return False
    try:
        with open(_SETTINGS, encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError as ex:
        run.status = [f"apply FAILED: cannot read {_SETTINGS}: {ex}"]
        print(f"[gazemeasure] {run.status[0]}")
        return False
    nl = "\r\n" if "\r\n" in text else "\n"
    wanted = {}
    for direction, d in run.suggest.items():
        if direction == "rotation":
            wanted["gaze_map_rotation_deg"] = _fmt(d, 1)
        elif direction == "distance_mm":
            wanted["gaze_ref_distance_mm"] = _fmt(d, 0)
        else:
            wanted[f"gaze_gain_{direction}"] = _fmt(d["gain"], 2)
            wanted[f"gaze_curve_{direction}"] = _fmt(d["curve"], 2)
    changed, missing = [], []
    for key, val in wanted.items():
        pat = re.compile(r"^([ \t]*user\." + re.escape(key) + r"[ \t]*=[ \t]*)(-?[0-9]*\.?[0-9]+)([ \t]*(?:#.*)?)$", re.M)
        m = pat.search(text)
        if not m:
            missing.append(key)
            continue
        if abs(float(m.group(2)) - float(val)) > 1e-9:
            changed.append(f"{key} {m.group(2)} -> {val}")
        text = text[:m.start()] + m.group(1) + val + m.group(3) + text[m.end():]
    if missing:
        run.status = [f"apply FAILED: not found in settings file: {', '.join(missing)} (file unchanged)"]
        print(f"[gazemeasure] {run.status[0]}")
        return False
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    note = f"    # last applied from ctrl-alt-m: {stamp} (verdict: {run.verdict}; previous file in {_BACKUP})"
    note_pat = re.compile(r"^[ \t]*# last applied from ctrl-alt-m:.*$", re.M)
    if note_pat.search(text):
        text = note_pat.sub(lambda _m: note, text, count=1)
    else:
        first = re.search(r"^[ \t]*user\.gaze_gain_", text, re.M)
        if first:
            text = text[:first.start()] + note + nl + text[first.start():]
    try:
        shutil.copyfile(_SETTINGS, _BACKUP)
        with open(_SETTINGS, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as ex:
        run.status = [f"apply FAILED: {ex}"]
        print(f"[gazemeasure] {run.status[0]}")
        return False
    run.applied = True
    # on-screen status: a few changes per line so it never runs off the screen
    head = f"APPLIED {len(changed)} change(s) to head_tracking_settings.talon (previous file: {_BACKUP})"
    run.status = [head] + ["; ".join(changed[i:i + 3]) for i in range(0, len(changed), 3)]
    if not changed:
        run.status.append("values were already current")
    print(f"[gazemeasure] {head}: {'; '.join(changed) or 'no value differed'}")
    app.notify("Gaze measure", f"settings applied ({len(changed)} changed)")
    return True


def _hit(name, pos):
    rect = _run.buttons.get(name)
    return rect is not None and pos is not None and \
        rect.x <= pos.x <= rect.x + rect.width and rect.y <= pos.y <= rect.y + rect.height


def _on_key(e):
    # Esc cancels the run / discards the results; Enter or y applies. (The
    # fullscreen canvas may not get keyboard focus at all - the ctrl-alt-y /
    # ctrl-alt-n hotkeys in head_tracking.talon are the reliable way.)
    key = getattr(e, "key", None)
    if key in ("esc", "escape"):
        stop()
    elif _run.phase == "results" and key in ("return", "enter", "y") and not _run.applied:
        apply_suggestions()


def _on_mouse(e):
    run = _run
    if run.phase != "results" or getattr(e, "event", None) != "mousedown":
        return
    pos = getattr(e, "gpos", None) or getattr(e, "pos", None)
    if _hit("apply", pos) and not run.applied:
        apply_suggestions()
    elif _hit("discard", pos) or _hit("close", pos):
        stop()
    # clicks elsewhere on the results page do nothing


def _auto_close():
    if _run.phase == "results":
        print("[gazemeasure] closed" if _run.applied else "[gazemeasure] results page timed out - discarded")
        stop()


def start():
    run = _run
    if run.phase != "idle":
        return
    run.rect = ui.main_screen().rect
    run.idx, run.samples, run.per_target, run.report_lines, run.latest = 0, [], [], [], None
    run.near_since, run.unsettled = None, False
    with _ctx.enter():
        tracking_system.register("gaze", _on_gaze)
        cv = canvas.Canvas.from_screen(ui.main_screen())
        cv.fullscreen = True
        cv.blocks_mouse = True
        cv.focused = True
        cv.register("draw", _draw)
        cv.register("key", _on_key)
        cv.register("mouse", _on_mouse)
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
            run.canvas.unregister("mouse", _on_mouse)
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
        """Run the gaze gain measurement overlay; press again (ctrl-alt-m) to close it"""
        if _run.phase != "idle":
            stop()
        else:
            start()

    def gaze_measure_apply():
        """Write the SUGGEST values of the finished measurement into head_tracking_settings.talon"""
        if _run.phase != "results":
            app.notify("Gaze measure", "no results on screen - run ctrl-alt-m first")
            return
        if _run.applied:
            app.notify("Gaze measure", "already applied")
            return
        apply_suggestions()

    def gaze_measure_discard():
        """Close the measurement results without changing any settings"""
        if _run.phase != "idle":
            if _run.phase == "results" and not _run.applied:
                print("[gazemeasure] discarded")
            stop()

    def gaze_measure_stop():
        """Close the gaze gain measurement overlay"""
        stop()
