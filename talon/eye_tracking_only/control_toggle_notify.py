"""ctrl-alt-e with feedback, and a record of every control-mouse start/stop.

* `user.control_mouse_toggle_notify()` toggles Talon's control mouse and shows
  a banner at the top of the screen (green ON / red OFF, ~2.5 s). No
  app.notify: Windows toasts were slow / not shown for the user (2026-09-02),
  the banner is preferred. `user.banner(text, kind)` is the shared helper the
  other toggles use (kind: on / off / info / warn).
* The INSTANCE methods start / stop / delayed_stop of Talon's ControlMouse2
  are wrapped so any transition - hotkey, tray menu, zoom mouse, tracker loss,
  whatever - shows the same banner and logs `[control_mouse] ... called from
  <frames>` to talon.log. Motivation: the user reported the control mouse
  "auto turning off". No plugin files are edited; a reload re-wraps from the
  class, never double-wraps.
"""
import traceback

from talon import Module, actions, canvas, cron, ui
from talon.plugins import eye_mouse_2 as _em2
from talon.scripting import rctx
from talon.types import Rect

mod = Module()
_ctx = rctx.active()
BANNER_S = 2.5

_banner = {"cv": None, "job": None}


def _close_banner():
    cv = _banner["cv"]
    _banner["cv"] = None
    if _banner["job"]:
        cron.cancel(_banner["job"])
        _banner["job"] = None
    if cv is not None:
        try:
            cv.close()
        except Exception:
            pass


def _show_banner(text, color):
    _close_banner()
    r = ui.main_screen().rect
    w, h = 520, 70
    rect = Rect(r.x + (r.width - w) / 2, r.y + 30, w, h)

    def draw(c):
        p = c.paint
        p.style = p.Style.FILL
        p.color = "202020e0"
        c.draw_rect(c.rect)
        p.color = color
        c.draw_rect(Rect(c.rect.x, c.rect.y, 16, c.rect.height))
        p.color = "ffffff"
        p.textsize = 30
        c.draw_text(text, c.rect.x + 40, c.rect.y + 46)

    try:
        with _ctx.enter():
            cv = canvas.Canvas.from_rect(rect)
            cv.register("draw", draw)
            try:
                cv.freeze()
            except Exception:
                pass
            _banner["cv"] = cv
            _banner["job"] = cron.after(f"{int(BANNER_S * 1000)}ms", _close_banner)
    except Exception as ex:
        print(f"[control_mouse] could not show banner: {ex!r}")


def _announce(state, why):
    text = f"Control mouse {state}"
    if why:
        text += f"  ({why})"
    print(f"[control_mouse] {text}")
    cron.after("0ms", lambda: _show_banner(text, "30c060" if state == "ON" else "e04040"))


def _caller():
    """Short description of who called start/stop (user files and plugin frames only)."""
    frames = traceback.extract_stack(limit=14)[:-3]
    keep = []
    for f in frames:
        fn = f.filename.replace("\\", "/")
        if "/user/" in fn or "eye_mouse" in fn or "talon/plugins" in fn or "menu" in fn or "tracking" in fn:
            keep.append(f"{fn.rsplit('/', 1)[-1]}:{f.lineno} {f.name}")
    return " <- ".join(reversed(keep[-5:])) or "?"


# --- wrap the control mouse's start / stop -------------------------------------------
_c2 = _em2.control2
_cls = type(_c2)
_orig = {name: getattr(_cls, name).__get__(_c2) for name in ("start", "stop", "delayed_stop")}


def _wrapped_start():
    was = _c2.running
    _orig["start"]()
    if not was and _c2.running:
        _announce("ON", _caller())


def _wrapped_stop():
    was = _c2.running
    _orig["stop"]()
    if was and not _c2.running:
        _announce("OFF", _caller())


def _wrapped_delayed_stop():
    print(f"[control_mouse] delayed_stop requested from {_caller()}")
    _orig["delayed_stop"]()


_c2.start = _wrapped_start
_c2.stop = _wrapped_stop
_c2.delayed_stop = _wrapped_delayed_stop


_KIND_COLORS = {"on": "30c060", "off": "e04040", "info": "4090e0", "warn": "e0a020"}


@mod.action_class
class Actions:
    def banner(text: str, kind: str = "info"):
        """Show a short top-of-screen banner (kind: on / off / info / warn). Faster than app.notify."""
        color = _KIND_COLORS.get(kind, _KIND_COLORS["info"])
        cron.after("0ms", lambda: _show_banner(text, color))

    def control_mouse_toggle_notify():
        """Toggle the control mouse (tracking.control_toggle) with an on-screen ON / OFF banner"""
        before = _c2.running
        actions.tracking.control_toggle()
        # the wrappers above announce the transition; if nothing changed, say so
        if _c2.running == before:
            _announce("ON" if _c2.running else "OFF", "no change")


print("[control_mouse] start/stop wrapped; ctrl-alt-e shows a banner")
