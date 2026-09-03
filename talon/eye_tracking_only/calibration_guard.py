"""Calibration guard: progress text, a cancel hotkey and a plain-language report
of how Talon's built-in eye calibration (ctrl-alt-c) ended.

Talon's calibration (talon_plugins/eye_mouse.py, class Calibration) is silent
about how a run ends. A tracker error while adding a point is only written to
talon.log ("calibration failed"; seen here as EyeCmdErr 0x20000502 on
CALIBRATE_POINT_ADD2D), and the run is also cancelled without a word whenever
its fullscreen window loses focus. This module wraps the calibration INSTANCE's
methods (instance attributes only - the plugin file is untouched):

  * progress text on the calibration screen ("point 3 of 9 ... ctrl-alt-c cancels")
  * ctrl-alt-c during a run cancels it (the fullscreen canvas often has no key
    focus, so Esc is unreliable); otherwise ctrl-alt-c starts calibration
  * focus loss no longer cancels the run unless
    user.calibration_cancel_on_focus_loss = 1 (Talon's default behaviour)
  * when the run ends, a banner at the top of the screen plus a notification say
    what happened - SAVED, CANCELLED (how) or FAILED (which tracker command,
    which error, at which point) - and what to do next.

  * every SAVED calibration is copied to %APPDATA%/talon/calib_backups/
    calib-YYYYMMDD-HHMM.bin (Talon overwrites calib.bin on each run - three
    calibrations were lost that way on 2026-09-03). ctrl-alt-shift-c swaps
    to the most recent backup that differs from the one in use (glasses on /
    glasses off) and uploads it to the tracker at once, no restart.

Everything is also logged with the prefix [calibguard].
"""
import hashlib
import os
import shutil
import textwrap
import threading
import time

from talon import Module, actions, app, canvas, cron, settings, ui
from talon.scripting import rctx
from talon.types import Rect

try:
    from talon.plugins import eye_mouse as _em
except ImportError:  # pragma: no cover - older layout
    from talon.plugins import eye_mouse_2 as _em2
    _em = _em2.eye_mouse

mod = Module()
_ctx = rctx.active()

mod.setting("calibration_cancel_on_focus_loss", type=int, default=0,
            desc="1 = Talon's default: cancel the calibration when its window loses focus. "
                 "0 = keep going (re-focus the window); ctrl-alt-c / Esc cancel instead.")
mod.setting("calibration_banner_seconds", type=float, default=15.0,
            desc="How long the calibration outcome banner stays on screen.")

TOTAL_POINTS = 9      # 1 centre + 4 edges + 4 corners (3 stages)
_tobii = _em.tobii
_BACKUP_DIR = os.path.join(os.path.dirname(_em.config.calib_file), "calib_backups")


# --- calibration file backups -----------------------------------------------------

def _digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


def _backups():
    """[(path, digest)] newest first."""
    try:
        names = sorted(n for n in os.listdir(_BACKUP_DIR) if n.startswith("calib-") and n.endswith(".bin"))
    except OSError:
        return []
    out = []
    for n in reversed(names):
        path = os.path.join(_BACKUP_DIR, n)
        out.append((path, _digest(path)))
    return out


def _backup_current(reason):
    """Copy calib.bin into the backup folder unless an identical copy is already there."""
    src = _em.config.calib_file
    d = _digest(src)
    if d is None:
        return None
    for path, dg in _backups():
        if dg == d:
            return path
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(os.path.getmtime(src)))
    dst = os.path.join(_BACKUP_DIR, f"calib-{stamp}.bin")
    k = 1
    while os.path.exists(dst):
        k += 1
        dst = os.path.join(_BACKUP_DIR, f"calib-{stamp}-{k}.bin")
    shutil.copy2(src, dst)
    print(f"[calibguard] calibration backed up ({reason}): {dst}")
    return dst


def _restore(path):
    """Make `path` the calibration in use: copy it over calib.bin and upload it to the tracker."""
    tr = _em.tracker
    if tr is None:
        raise RuntimeError("no tracker attached")
    with open(path, "rb") as f:
        data = f.read()
    shutil.copy2(path, _em.config.calib_file)

    def upload():
        try:
            tr.cmd(_tobii.CALIBRATE_UPLOAD, data)
            print(f"[calibguard] calibration restored and uploaded: {path} ({len(data)} bytes)")
        except Exception as ex:
            print(f"[calibguard] calibration upload FAILED: {ex!r} (calib.bin is replaced; a Talon restart loads it)")
            cron.after("0ms", lambda: _show_banner([f"Calibration upload failed: {ex!r}",
                                                    "calib.bin was replaced; restart Talon to load it."], "error"))
    threading.Thread(target=upload, daemon=True).start()


def _cmd_name(cmd):
    for n in dir(_tobii):
        if n.startswith("CALIBRATE_"):
            try:
                if getattr(_tobii, n) == cmd:
                    return n
            except Exception:
                pass
    return str(cmd)


class _State:
    reason = None          # why stop() was called, when we know
    error = None           # (command name, exception) of the FIRST failing tracker command
    points_done = 0
    started = 0.0


_st = _State()


def _calib():
    return _em.calib


# --- wrappers (installed as instance attributes on the Calibration object) -----

def _wrap_start(orig):
    def start(*a, **k):
        _st.reason, _st.error, _st.points_done, _st.started = None, None, 0, time.time()
        print(f"[calibguard] calibration started ({TOTAL_POINTS} points; ctrl-alt-c cancels)")
        return orig(*a, **k)
    return start


def _wrap_cmd(orig):
    def cmd(*args, **kw):
        try:
            r = orig(*args, **kw)
        except Exception as ex:
            if _st.error is None:
                _st.error = (_cmd_name(args[0] if args else None), ex)
            print(f"[calibguard] tracker command {_cmd_name(args[0] if args else None)} failed: {ex!r}")
            raise
        if args and args[0] == _tobii.CALIBRATE_POINT_ADD2D:
            _st.points_done += 1
            print(f"[calibguard] point {_st.points_done}/{TOTAL_POINTS} accepted")
        return r
    return cmd


def _wrap_on_canvas_key(orig):
    def on_canvas_key(e):
        if getattr(e, "key", None) == "esc":
            _st.reason = "Esc was pressed"
        return orig(e)
    return on_canvas_key


def _wrap_on_canvas_focus(orig):
    def on_canvas_focus(focused):
        c = _calib()
        if focused or not c.running:
            return orig(focused)
        if settings.get("user.calibration_cancel_on_focus_loss"):
            _st.reason = "the calibration window lost focus (another window came to the front)"
            return orig(focused)
        print("[calibguard] calibration window lost focus - ignoring it and re-focusing")
        try:
            if c.canvas:
                c.canvas.focused = True
        except Exception:
            pass
    return on_canvas_focus


def _wrap_stop(orig):
    def stop(*a, **k):
        c = _calib()
        was_active = c.running or c.canvas is not None
        try:
            return orig(*a, **k)
        finally:
            if was_active:
                # stop() can run on the eye-mouse thread; report from the main thread
                cron.after("50ms", _report)
    return stop


def _wrap_draw(orig):
    def draw(c):
        r = orig(c)
        calib = _calib()
        if calib.running:
            paint = c.paint
            paint.style = paint.Style.FILL
            paint.color = "dddddd"
            paint.textsize = 26
            c.draw_text(f"Eye calibration: point {min(_st.points_done + 1, TOTAL_POINTS)} of {TOTAL_POINTS} "
                        f"(stage {min(calib.test_num + 1, 3)}/3). Look at the dot until it fills green and moves on. "
                        f"Head still. ctrl-alt-c cancels.",
                        c.rect.x + 40, c.rect.y + c.rect.height - 60)
        return r
    return draw


_WRAPS = {"start": _wrap_start, "cmd": _wrap_cmd, "stop": _wrap_stop, "on_canvas_key": _wrap_on_canvas_key,
          "on_canvas_focus": _wrap_on_canvas_focus, "draw": _wrap_draw}


def _install():
    c = _calib()
    for name, mk in _WRAPS.items():
        cur = c.__dict__.get(name)
        if cur is not None and getattr(cur, "_calibguard", False):
            del c.__dict__[name]            # wrapper left by a previous load of this module
        w = mk(getattr(c, name))            # bound original from the class
        w._calibguard = True
        setattr(c, name, w)
    print("[calibguard] installed on Talon's calibration (progress, ctrl-alt-c cancel, outcome report)")


# --- outcome report -------------------------------------------------------------

def _advice(ex):
    s = repr(ex)
    if "EyeCmdErr" in s or "0x2000" in s:
        return ("The tracker rejected that sample. Sit still with both eyes open and inside the tracker's box, "
                "then retry with ctrl-alt-c. If it keeps failing, unplug and replug the tracker's USB cable.")
    return "See talon.log for the traceback, then retry with ctrl-alt-c."


def _report():
    c = _calib()
    dur = time.time() - _st.started if _st.started else 0.0
    unchanged = ("Nothing was saved (calib.bin unchanged). If pointing now seems worse than before, "
                 "replug the tracker or restart Talon to reload the last saved calibration.")
    if c.success:
        kind = "ok"
        lines = [f"Calibration SAVED: {_st.points_done}/{TOTAL_POINTS} points, {c.data_size} bytes, {dur:.0f} s.",
                 "Next: ctrl-alt-m to re-measure the gaze gains, then Apply."]
        try:
            dst = _backup_current("saved")
            if dst:
                lines.append(f"Backed up to {os.path.basename(dst)} (ctrl-alt-shift-c swaps back to the previous one).")
        except Exception as ex:
            lines.append(f"Backup failed: {ex!r}")
    elif _st.error:
        kind = "error"
        name, ex = _st.error
        lines = [f"Calibration FAILED at point {min(_st.points_done + 1, TOTAL_POINTS)} of {TOTAL_POINTS} "
                 f"after {dur:.0f} s.",
                 f"Tracker command {name} raised {ex!r}.",
                 unchanged, _advice(ex)]
    elif _st.reason:
        kind = "warn"
        lines = [f"Calibration CANCELLED after {_st.points_done}/{TOTAL_POINTS} points ({dur:.0f} s): {_st.reason}.",
                 unchanged]
    else:
        kind = "warn"
        lines = [f"Calibration STOPPED after {_st.points_done}/{TOTAL_POINTS} points ({dur:.0f} s) "
                 f"for a reason this guard did not see (screen change, or 'eye mouse thread error' in talon.log).",
                 unchanged]
    for ln in lines:
        print(f"[calibguard] {ln}")
    try:
        app.notify("Eye calibration", lines[0])
    except Exception:
        pass
    _show_banner(lines, kind)


# --- banner ---------------------------------------------------------------------

_banner = None
_banner_job = None
_COLORS = {"ok": "1f7a1f", "warn": "9a6a00", "error": "a02020"}


def _close_banner():
    global _banner, _banner_job
    if _banner_job:
        cron.cancel(_banner_job)
        _banner_job = None
    if _banner:
        try:
            _banner.close()
        except Exception:
            pass
        _banner = None


def _show_banner(lines, kind):
    global _banner, _banner_job
    _close_banner()
    r = ui.main_screen().rect
    w = min(1800, r.width - 80)
    chars = max(60, int(w / 13))
    wrapped = [wl for ln in lines for wl in (textwrap.wrap(ln, chars) or [""])]
    h = 40 + 38 * len(wrapped)
    rect = Rect(r.x + (r.width - w) / 2, r.y + 40, w, h)

    def draw(c):
        paint = c.paint
        paint.style = paint.Style.FILL
        paint.color = "202020"
        c.draw_rect(c.rect)
        paint.color = _COLORS.get(kind, "555555")
        c.draw_rect(Rect(c.rect.x, c.rect.y, 14, c.rect.height))
        paint.color = "ffffff"
        paint.textsize = 26
        y = c.rect.y + 44
        for wl in wrapped:
            c.draw_text(wl, c.rect.x + 40, y)
            y += 38

    try:
        with _ctx.enter():
            cv = canvas.Canvas.from_rect(rect)
            cv.register("draw", draw)
            try:
                cv.freeze()
            except Exception:
                pass
            _banner = cv
            secs = settings.get("user.calibration_banner_seconds") or 15.0
            _banner_job = cron.after(f"{int(secs * 1000)}ms", _close_banner)
    except Exception as ex:
        print(f"[calibguard] could not show banner: {ex!r}")


# --- actions --------------------------------------------------------------------

@mod.action_class
class Actions:
    def calibration_toggle():
        """Start Talon's eye calibration, or cancel the run in progress (ctrl-alt-c)"""
        c = _calib()
        if c.running:
            _st.reason = "ctrl-alt-c was pressed"
            print("[calibguard] cancel requested with ctrl-alt-c")
            _em.calib_stop()
        else:
            _close_banner()
            actions.tracking.calibrate()

    def calibration_cancel():
        """Cancel the eye calibration in progress (no-op if none is running)"""
        c = _calib()
        if c.running:
            _st.reason = "cancelled by user.calibration_cancel"
            _em.calib_stop()

    def calibration_swap():
        """Switch to the most recent backed-up calibration that differs from the one in use (ctrl-alt-shift-c)"""
        _close_banner()
        try:
            _backup_current("before swap")
            current = _digest(_em.config.calib_file)
            other = next((path for path, dg in _backups() if dg != current), None)
            if other is None:
                _show_banner(["No other calibration to swap to.",
                              f"Backups live in {_BACKUP_DIR}; every saved ctrl-alt-c run adds one."], "warn")
                return
            _restore(other)
            _show_banner([f"Calibration swapped to {os.path.basename(other)}.",
                          "Uploading to the tracker; ctrl-alt-shift-c again swaps back."], "ok")
        except Exception as ex:
            print(f"[calibguard] swap failed: {ex!r}")
            _show_banner([f"Calibration swap failed: {ex!r}"], "error")

    def calibration_backups():
        """List the backed-up calibration files in the log and a banner"""
        current = _digest(_em.config.calib_file)
        rows = [f"{os.path.basename(p)}{'  <- in use' if dg == current else ''}" for p, dg in _backups()]
        for r in rows:
            print(f"[calibguard] backup: {r}")
        _show_banner(rows[:6] or ["No calibration backups yet."], "ok")


_install()
try:
    _backup_current("startup")     # the calibration in use is always kept
except Exception as _ex:
    print(f"[calibguard] startup backup failed: {_ex!r}")
