"""Latency diagnostic (ctrl-alt-l): how long after a big eye movement does the
cursor follow?

For every saccade larger than JUMP_PX (raw gaze, as it arrives from the
tracker) it records the arrival time of that frame and then polls the cursor
every 5 ms:
    moved   = cursor has covered 30 % of the way
    arrived = cursor within ARRIVE_PX of the current raw gaze and > 50 % of the way
and prints one line per saccade to talon.log, plus a summary every 10:
    [latency] saccade 1480 px: moved +72 ms, arrived +95 ms (frame lag 21 ms)
"frame lag" = time between the tracker's own frame timestamp and the moment the
frame reached Talon's user code (tracker + USB + Talon pipeline). What is left
(moved - frame lag) is Talon's control-mouse decision time on top of the layers
in this folder.
"""
import statistics
import time

from talon import Module, actions, app, cron, ctrl, tracking_system, ui
from talon.scripting import rctx

mod = Module()
_ctx = rctx.active()

JUMP_PX = 300.0
ARRIVE_PX = 100.0
TIMEOUT_S = 1.5


class _S:
    on = False
    job = None
    last_px = None
    rest_px = None          # gaze position before the jump (slowly tracked)
    pending = None          # dict for the saccade being timed
    results = []            # (moved_ms, arrived_ms, lag_ms)
    clock_printed = False
    frames = 0


_s = _S()


def _gaze_px(frame):
    g = frame.gaze
    if g is None or not (frame.left.detected or frame.right.detected):
        return None
    r = ui.main_screen().rect
    return (r.x + g.x * r.width, r.y + g.y * r.height)


def _on_gaze(frame):
    s = _s
    p = _gaze_px(frame)
    if p is None:
        return
    now = time.perf_counter()
    s.frames += 1
    if not s.clock_printed:
        s.clock_printed = True
        print(f"[latency] clocks: frame.ts={frame.ts:.3f} perf={now:.3f} monotonic={time.monotonic():.3f} "
              f"time={time.time():.3f}")
    if s.rest_px is None:
        s.rest_px = p
    if s.pending is None:
        dx, dy = p[0] - s.rest_px[0], p[1] - s.rest_px[1]
        d = (dx * dx + dy * dy) ** 0.5
        if d > JUMP_PX:
            try:
                c = ctrl.mouse_pos()
            except Exception:
                c = s.rest_px
            s.pending = {"t0": now, "frame_ts": frame.ts, "start_cursor": c, "from": s.rest_px,
                         "dist": d, "moved": None, "arrived": None}
        else:
            s.rest_px = (s.rest_px[0] + 0.2 * dx, s.rest_px[1] + 0.2 * dy)
    s.last_px = p


def _poll():
    s = _s
    ev = s.pending
    if ev is None:
        return
    now = time.perf_counter()
    try:
        cx, cy = ctrl.mouse_pos()
    except Exception:
        return
    sx, sy = ev["start_cursor"]
    gone = ((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5
    frac = gone / max(ev["dist"], 1.0)
    if ev["moved"] is None and frac >= 0.3:
        ev["moved"] = now
    if s.last_px is not None:
        gx, gy = s.last_px
        near = ((cx - gx) ** 2 + (cy - gy) ** 2) ** 0.5 <= ARRIVE_PX
        if ev["arrived"] is None and near and frac >= 0.5:
            ev["arrived"] = now
    done = ev["arrived"] is not None or now - ev["t0"] > TIMEOUT_S
    if not done:
        return
    lag_ms = (ev["t0"] - ev["frame_ts"]) * 1000.0 if abs(ev["t0"] - ev["frame_ts"]) < 10 else None
    m = f"{(ev['moved'] - ev['t0']) * 1000:+.0f} ms" if ev["moved"] else "never"
    a = f"{(ev['arrived'] - ev['t0']) * 1000:+.0f} ms" if ev["arrived"] else f"not within {TIMEOUT_S:.1f} s"
    lag = f"{lag_ms:.0f} ms" if lag_ms is not None else "n/a (different clock)"
    print(f"[latency] saccade {ev['dist']:.0f} px: moved {m}, arrived {a} (frame lag {lag})")
    if ev["moved"] and ev["arrived"]:
        s.results.append(((ev["moved"] - ev["t0"]) * 1000, (ev["arrived"] - ev["t0"]) * 1000, lag_ms or 0.0))
        if len(s.results) % 10 == 0:
            mv = statistics.median(r[0] for r in s.results)
            ar = statistics.median(r[1] for r in s.results)
            print(f"[latency] SUMMARY over {len(s.results)}: median moved +{mv:.0f} ms, arrived +{ar:.0f} ms")
    s.pending = None
    s.rest_px = s.last_px


def _banner(text, kind):
    try:
        actions.user.banner(text, kind)
    except Exception:
        app.notify("Latency diag", text)


def _start():
    s = _s
    if s.on:
        return
    with _ctx.enter():
        tracking_system.register("gaze", _on_gaze)
        s.job = cron.interval("5ms", _poll)
    s.on = True
    s.results = []
    s.pending = None
    s.rest_px = None
    _banner("Latency diag ON - make big eye movements", "on")
    print("[latency] ON")


def _stop():
    s = _s
    if not s.on:
        return
    try:
        tracking_system.unregister("gaze", _on_gaze)
    except Exception:
        pass
    if s.job:
        cron.cancel(s.job)
        s.job = None
    s.on = False
    if s.results:
        mv = statistics.median(r[0] for r in s.results)
        ar = statistics.median(r[1] for r in s.results)
        msg = f"{len(s.results)} saccades: median cursor moved +{mv:.0f} ms, arrived +{ar:.0f} ms"
    else:
        msg = "no complete saccades recorded"
    _banner(f"Latency diag OFF - {msg}", "off")
    print(f"[latency] OFF - {msg}")


@mod.action_class
class Actions:
    def latency_diag_toggle():
        """Toggle the eye-movement -> cursor latency diagnostic"""
        if _s.on:
            _stop()
        else:
            _start()
