"""Target magnet: snap the gaze cursor onto the UI element you are looking at.

WHY (2026-09-02)
  The Tobii 5's own accuracy is roughly 0.5-1 degree of visual angle: 15-30 px
  of scatter at 48 cm on this screen. Toolbar buttons are 43-60 px, taskbar
  buttons 77 px, tab close buttons 50 px - right at the noise floor, so
  reliable clicks needed the F4 zoom. This layer asks Windows where the
  clickable things are (UI Automation, via talon.ui.element_at) and, once the
  gaze has settled near one, holds the cursor on it until the gaze clearly
  leaves. You still click with the physical mouse.

HOW
  * head_offset.py calls `_filter(frame, x, y)` from the tracker thread (~90 Hz)
    with the CORRECTED gaze point in pixels. The filter only records samples
    and, while a target is held, rewrites the frame it hands on to Talon's
    control mouse: gaze = the snap point, eye positions = frozen at grab time.
    Freezing the eye positions matters: the control mouse runs in jump mode
    (45 mm zones, measured 2026-09-02) where small gaze changes are ignored
    and the HEAD does the fine positioning - a still head means Talon never
    drags the cursor off the target. Real head motion resumes on release,
    blended in over ~150 ms so there is no jump.
  * A background thread (Windows UI Automation works off the main thread,
    ~2-8 ms per lookup, verified) ticks every 40 ms: when the gaze has been
    within `magnet_settle_px` for `magnet_settle_ms` it hit-tests the settled
    point, recovers the enclosing button when the hit is only its icon or
    label (hit-tests just outside that child), and probes a ring of
    `magnet_reach_px` around the point when the hit is a container. The
    smallest clickable thing wins.
  * Sticky: the target is kept while the gaze stays inside its rectangle plus
    `magnet_release_px`; a settled gaze on a DIFFERENT clickable element
    switches at once. The element is re-checked every 400 ms (windows scroll
    and move). Wide elements (a list row, the omnibox) only snap the short
    axis; along the long axis the cursor follows the gaze inside the element.
  * Because of the jump-mode dead zone, Talon may not move the cursor onto a
    target 20 px away at all. If the cursor is not on the snap point
    `magnet_place_ms` after the grab, the thread places it with
    ctrl.mouse_move (throttled, only when it has drifted > 3 px).
  * A click-through canvas draws a thin rounded outline around the held
    element (verified not to disturb the hit-test).

  Nothing happens while the control mouse is off (ctrl-alt-e), while the F4
  zoom overlay is open, or over Talon's own windows (ctrl-alt-m page).

LIVE TUNING: `user.magnet_*` in head_tracking_settings.talon. ctrl-alt-t toggles
the magnet. `user.magnet_debug = 1` logs every grab / release / placement.
"""
import copy
import math
import os
import threading
import time
import traceback
from collections import deque

from talon import Module, app, canvas, cron, ctrl, settings, ui
from talon.plugins import eye_mouse_2 as _em2
from talon.types import Point2d, Point3d, Rect

from . import head_offset

mod = Module()

_SETTINGS = {
    "on": ("magnet_on", 1.0, "1 = the target magnet is active (ctrl-alt-t toggles it at runtime)."),
    "settle_ms": ("magnet_settle_ms", 90.0,
                  "The gaze must stay within magnet_settle_px for this long (ms) before an element is looked up."),
    "settle_px": ("magnet_settle_px", 40.0, "Radius (px) the gaze may wander in while still counting as settled."),
    "reach_px": ("magnet_reach_px", 30.0,
                 "When the settled point is not on a clickable element, look this far around it (px) for one."),
    "axis_px": ("magnet_axis_snap_px", 110.0,
                "An element side up to this long (px) snaps the cursor to its centre on that axis; a longer side "
                "lets the cursor follow the gaze along it (list rows, address bar)."),
    "release_px": ("magnet_release_px", 40.0,
                   "The gaze must leave the element's rectangle by more than this (px) before the target is let go."),
    "release_ms": ("magnet_release_ms", 80.0, "...and stay outside for this long (ms)."),
    "passive": ("magnet_passive", 1.0,
                "1 = also grab small icons/labels that report no click behaviour (they usually sit inside a "
                "button Windows did not report). 0 = only elements that say they are clickable."),
    "place": ("magnet_place_cursor", 1.0,
              "1 = if Talon has not moved the cursor onto the target after magnet_place_ms, put it there directly "
              "(needed in the control mouse's jump mode). 0 = only steer through the gaze frames."),
    "place_ms": ("magnet_place_ms", 120.0, "How long to give Talon before placing the cursor (ms)."),
    "highlight": ("magnet_highlight", 1.0, "1 = draw a thin outline around the held element."),
    "debug": ("magnet_debug", 0.0, "1 = log every grab / release / cursor placement to talon.log."),
}
for _key, (_name, _default, _desc) in _SETTINGS.items():
    mod.setting(_name, type=float, default=_default, desc=_desc)
_cfg = {k: v[1] for k, v in _SETTINGS.items()}

_ZOOM_FLAG = os.path.join(os.environ.get("TEMP", ""), "talon_zoom_open.flag")
_SELF_PID = os.getpid()
_THAW_S = 0.15          # blend frozen -> real eye positions over this long after a release
_REVALIDATE_S = 0.4     # re-hit-test the held element this often
_TICK_S = 0.04

_CLICK_TYPES = {"Button", "Hyperlink", "TabItem", "MenuItem", "CheckBox", "RadioButton", "ListItem",
                "TreeItem", "ComboBox", "SplitButton", "DataItem", "HeaderItem", "Thumb", "ScrollBar",
                "Slider", "Spinner", "Edit", "MenuBar"}
_CLICK_PATTERNS = {"Invoke", "Toggle", "SelectionItem", "ExpandCollapse"}
_PASSIVE_TYPES = {"Image", "Text", "Group", "Pane", "Custom"}
_PASSIVE_MAX_PX = 130.0
_IGNORED_PATTERNS = {"LegacyIAccessible", "ScrollItem", "TextChild"}


def _refresh_settings():
    for key, (name, default, _d) in _SETTINGS.items():
        try:
            v = settings.get(f"user.{name}")
            _cfg[key] = float(v) if v is not None else default
        except Exception:
            pass


# --- shared state (tuples are swapped atomically between the threads) ---------------
class _Target:
    __slots__ = ("x", "y", "w", "h", "kind", "name", "ctype", "grab_ts", "placed_ts")

    def __init__(self, r, kind, name, ctype):
        self.x, self.y, self.w, self.h = float(r.x), float(r.y), float(r.width), float(r.height)
        self.kind, self.name, self.ctype = kind, name, ctype
        self.grab_ts = time.perf_counter()
        self.placed_ts = 0.0

    def same_rect(self, r, tol=2.0):
        return (abs(self.x - r.x) <= tol and abs(self.y - r.y) <= tol
                and abs(self.w - r.width) <= tol and abs(self.h - r.height) <= tol)

    def contains(self, px, py, margin=0.0):
        return (self.x - margin <= px <= self.x + self.w + margin
                and self.y - margin <= py <= self.y + self.h + margin)

    def snap(self, gx, gy):
        """Snap point for gaze (gx, gy): short sides -> centre, long sides -> gaze clamped inside."""
        a = _cfg["axis_px"]
        if self.w <= a:
            sx = self.x + self.w / 2
        else:
            inset = min(8.0, self.w / 4)
            sx = min(max(gx, self.x + inset), self.x + self.w - inset)
        if self.h <= a:
            sy = self.y + self.h / 2
        else:
            inset = min(8.0, self.h / 4)
            sy = min(max(gy, self.y + inset), self.y + self.h - inset)
        return sx, sy

    def __repr__(self):
        return f"{self.kind}:{self.ctype} {self.name[:30]!r} @({self.x:.0f},{self.y:.0f} {self.w:.0f}x{self.h:.0f})"


class _State:
    enabled = True
    held = None             # _Target or None (written by the worker thread only)
    latest = None           # (ts, x, y, centroid_or_None) from the tracker thread
    frozen = None           # (Point3d left, Point3d right) eye positions while held
    thaw = None             # (release_perf_ts, frozen) - blend back to real positions
    grabs = releases = placements = 0
    last_err = ""


_st = _State()
_samples = deque()          # (perf_ts, x, y) corrected gaze, tracker thread only


# --- tracker-thread side: sample recording + frame rewriting -----------------------
def _filter(f, x, y):
    """Called by head_offset for every corrected frame (f is already a copy)."""
    now = time.perf_counter()
    _samples.append((now, x, y))
    win = _cfg["settle_ms"] / 1000.0
    while _samples and now - _samples[0][0] > max(win, 0.02) + 0.02:
        _samples.popleft()
    centroid = None
    if len(_samples) >= 3 and now - _samples[0][0] >= win:
        n = len(_samples)
        mx = sum(s[1] for s in _samples) / n
        my = sum(s[2] for s in _samples) / n
        r = _cfg["settle_px"]
        if all((s[1] - mx) ** 2 + (s[2] - my) ** 2 <= r * r for s in _samples):
            centroid = (mx, my)
    _st.latest = (now, x, y, centroid)

    held = _st.held
    l, r_ = f.left, f.right
    both = l.detected and r_.detected
    if held is None:
        th = _st.thaw
        if th is not None and both:
            t0, (fl, fr) = th
            a = (now - t0) / _THAW_S
            if a >= 1.0:
                _st.thaw = None
            else:
                f.left = _with_pos(l, _lerp3(fl, l.pos, a))
                f.right = _with_pos(r_, _lerp3(fr, r_.pos, a))
        return False
    # a target is held: feed Talon the snap point and a perfectly still head
    if _st.frozen is None and both:
        _st.frozen = (_copy3(l.pos), _copy3(r_.pos))
    sx, sy = held.snap(x, y)
    rect = head_offset.screen_rect()
    p = Point2d((sx - rect.x) / rect.width, (sy - rect.y) / rect.height)
    f.gaze = p
    fz = _st.frozen
    for name, eye, fpos in (("left", l, fz[0] if fz else None), ("right", r_, fz[1] if fz else None)):
        if not eye.detected:
            continue
        e = copy.copy(eye)
        if e.gaze is not None:
            e.gaze = p
        if fpos is not None:
            e.pos = fpos
        setattr(f, name, e)
    return True


def _copy3(p):
    return Point3d(p.x, p.y, p.z)


def _lerp3(a, b, t):
    return Point3d(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t)


def _with_pos(eye, pos):
    e = copy.copy(eye)
    e.pos = pos
    return e


# --- worker thread: element lookup, stickiness, cursor placement -----------------
def _elem(x, y):
    try:
        return ui.element_at(int(round(x)), int(round(y)))
    except Exception as ex:
        _st.last_err = repr(ex)[:120]
        return None


def _classify(e):
    """-> (_Target-ish tuple (rect, kind, name, ctype)) or None. kind: 'click' | 'passive'."""
    if e is None:
        return None
    try:
        if e.pid == _SELF_PID:
            return None
        r = e.rect
        if r is None or r.width <= 3 or r.height <= 3:
            return None
        ctype = e.control_type or ""
        pats = set(e.patterns or ()) - _IGNORED_PATTERNS
        clickable = ctype in _CLICK_TYPES or bool(pats & _CLICK_PATTERNS)
        if clickable:
            if not e.is_enabled or e.is_offscreen:
                return None
            if min(r.width, r.height) > _cfg["axis_px"]:
                return None   # a big clickable area (card, canvas): the gaze is precise enough there
            return (r, "click", e.name or "", ctype)
        if _cfg["passive"] and ctype in _PASSIVE_TYPES and not pats \
                and r.width <= _PASSIVE_MAX_PX and r.height <= _PASSIVE_MAX_PX:
            return (r, "passive", e.name or "", ctype)
    except Exception as ex:
        _st.last_err = repr(ex)[:120]
    return None


def _rect_dist(r, px, py):
    dx = max(r.x - px, 0.0, px - (r.x + r.width))
    dy = max(r.y - py, 0.0, py - (r.y + r.height))
    return math.hypot(dx, dy)


def _lookup(cx, cy):
    """Best target for a settled gaze point, or None."""
    hit = _classify(_elem(cx, cy))
    if hit and hit[1] == "click":
        return hit
    if hit:   # a small icon / label: the real control usually surrounds it
        r = hit[0]
        for px, py in ((r.x - 3, cy), (r.x + r.width + 3, cy), (cx, r.y - 3), (cx, r.y + r.height + 3)):
            t = _classify(_elem(px, py))
            if t and t[1] == "click" and t[0].x <= cx <= t[0].x + t[0].width and t[0].y <= cy <= t[0].y + t[0].height:
                return t
    reach = _cfg["reach_px"]
    best, best_d = None, reach + 1
    if reach > 0:
        for k in range(8):
            a = k * math.pi / 4
            t = _classify(_elem(cx + reach * math.cos(a), cy + reach * math.sin(a)))
            if t and t[1] == "click":
                d = _rect_dist(t[0], cx, cy)
                if d <= reach and d < best_d:
                    best, best_d = t, d
    return best or hit


def _grab(t, why):
    _st.frozen = None
    _st.thaw = None
    _st.held = _Target(*t)
    _st.grabs += 1
    _worker.outside_since = None
    _worker.checked_ts = time.perf_counter()
    _worker.miss = None
    if _cfg["debug"]:
        print(f"[magnet] grab ({why}): {_st.held}")
    _show_highlight(_st.held)


def _release(why):
    held = _st.held
    if held is None:
        return
    fz = _st.frozen
    _st.held = None
    _st.thaw = (time.perf_counter(), fz) if fz else None
    _st.frozen = None
    _st.releases += 1
    if _cfg["debug"]:
        print(f"[magnet] release ({why}): {held}")
    _show_highlight(None)


class _Worker:
    outside_since = None
    checked_ts = 0.0
    miss = None            # (ts, x, y) last settled point that found nothing
    last_place = 0.0
    stop = threading.Event()
    thread = None

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                _st.last_err = traceback.format_exc()[-300:]
            self.stop.wait(_TICK_S)

    def tick(self):
        now = time.perf_counter()
        latest = _st.latest
        active = (_st.enabled and _cfg["on"] and latest is not None and now - latest[0] < 0.5
                  and getattr(_em2.control2, "running", False) and not os.path.exists(_ZOOM_FLAG))
        if not active:
            if _st.held is not None:
                _release("inactive")
            return
        _ts, gx, gy, centroid = latest
        held = _st.held
        if held is not None:
            if held.contains(gx, gy, _cfg["release_px"]):
                self.outside_since = None
            elif self.outside_since is None:
                self.outside_since = now
            elif now - self.outside_since > _cfg["release_ms"] / 1000.0:
                _release("gaze left")
                return
            # settled on something else inside reach? switch
            if centroid is not None and not held.contains(*centroid):
                t = _lookup(*centroid)
                if t is not None and t[1] == "click" and not held.same_rect(t[0]):
                    _grab(t, "switch")
                    held = _st.held
            # the element may have moved / scrolled away
            if now - self.checked_ts > _REVALIDATE_S:
                self.checked_ts = now
                sx, sy = held.snap(gx, gy)
                t = _classify(_elem(sx, sy))
                if t is None or not held.same_rect(t[0]):
                    _release("element changed")
                    return
            self.place(held, gx, gy, now)
            return
        if centroid is None:
            return
        m = self.miss
        if m is not None and now - m[0] < 0.3 and math.hypot(centroid[0] - m[1], centroid[1] - m[2]) < 10:
            return
        t = _lookup(*centroid)
        if t is None:
            self.miss = (now, centroid[0], centroid[1])
            return
        _grab(t, "settled")

    def place(self, held, gx, gy, now):
        if not _cfg["place"] or now - held.grab_ts < _cfg["place_ms"] / 1000.0:
            return
        if now - self.last_place < 0.06:
            return
        sx, sy = held.snap(gx, gy)
        try:
            cx, cy = ctrl.mouse_pos()
        except Exception:
            return
        if math.hypot(cx - sx, cy - sy) <= 3.0:
            return
        self.last_place = now
        held.placed_ts = now
        _st.placements += 1
        try:
            ctrl.mouse_move(int(round(sx)), int(round(sy)))
        except Exception as ex:
            _st.last_err = repr(ex)[:120]
            return
        if _cfg["debug"]:
            ls = getattr(_em2.control2, "last_state", None)
            extra = f" mouse_active={ls.mouse_active} target_px={ls.target_px}" if ls is not None else ""
            print(f"[magnet] placed cursor ({cx:.0f},{cy:.0f}) -> ({sx:.0f},{sy:.0f}) for {held}{extra}")


_worker = _Worker()


# --- highlight canvas (main thread only) ---------------------------------------------
_hl = {"cv": None, "target": None}


def _show_highlight(target):
    if not _cfg["highlight"] and target is not None:
        return
    _hl["target"] = target
    cron.after("0ms", _update_highlight)


def _draw_highlight(c):
    t = _hl["target"]
    if t is None:
        return
    p = c.paint
    p.antialias = True
    p.style = p.Style.STROKE
    p.stroke_width = 2
    p.color = "ffb000cc" if t.kind == "click" else "ffb00077"
    r = Rect(c.rect.x + 2, c.rect.y + 2, c.rect.width - 4, c.rect.height - 4)
    try:
        c.draw_round_rect(r, 6, 6)
    except Exception:
        c.draw_rect(r)


def _update_highlight():
    t = _hl["target"]
    cv = _hl["cv"]
    try:
        if t is None:
            if cv is not None:
                cv.hide()
            return
        pad = 4
        x, y, w, h = t.x - pad, t.y - pad, t.w + 2 * pad, t.h + 2 * pad
        if cv is None:
            cv = canvas.Canvas(x, y, w, h)
            cv.register("draw", _draw_highlight)
            _hl["cv"] = cv
            setattr(_em2.control2, "_magnet_canvas", cv)
        else:
            cv.move(x, y)
            cv.resize(w, h)
        cv.show()
        cv.freeze()
    except Exception as ex:
        _st.last_err = repr(ex)[:120]


def _close_highlight():
    cv = _hl["cv"]
    _hl["cv"] = None
    _hl["target"] = None
    if cv is not None:
        try:
            cv.unregister("draw", _draw_highlight)
            cv.close()
        except Exception:
            pass


# --- lifecycle -------------------------------------------------------------------
def _shutdown_previous():
    """A reload creates a fresh module; stop the previous one's thread + canvas (kept on control2)."""
    ev = getattr(_em2.control2, "_magnet_stop", None)
    if ev is not None:
        ev.set()
    cv = getattr(_em2.control2, "_magnet_canvas", None)
    if cv is not None:
        try:
            cv.close()
        except Exception:
            pass
        _em2.control2._magnet_canvas = None
    fn = getattr(_em2.control2, "_magnet_filter", None)
    if fn is not None and head_offset.post_filter() is fn:
        head_offset.set_post_filter(None)


def status_snapshot():
    st = _st
    return {"on": st.enabled and bool(_cfg["on"]), "held": repr(st.held) if st.held else None,
            "grabs": st.grabs, "releases": st.releases, "placements": st.placements,
            "worker": bool(_worker.thread and _worker.thread.is_alive()), "err": st.last_err}


@mod.action_class
class Actions:
    def magnet_toggle():
        """Toggle the target magnet (snap the cursor onto the UI element you look at)"""
        _st.enabled = not _st.enabled
        if not _st.enabled:
            _release("toggled off")
        app.notify("Target magnet", "ON" if _st.enabled else "OFF")
        print(f"[magnet] {'ON' if _st.enabled else 'OFF'}")

    def magnet_enabled() -> bool:
        """Is the target magnet enabled?"""
        return _st.enabled and bool(_cfg["on"])

    def magnet_status():
        """Print the target magnet state to the log"""
        print(f"[magnet] state={status_snapshot()} cfg={_cfg}")
        app.notify("Target magnet", str(status_snapshot()))

    def magnet_uninstall():
        """Detach the magnet from the gaze stream until the next reload"""
        _release("uninstall")
        head_offset.set_post_filter(None)
        _worker.stop.set()
        _close_highlight()
        print("[magnet] uninstalled")


_shutdown_previous()
_refresh_settings()
cron.interval("250ms", _refresh_settings)
_em2.control2._magnet_stop = _worker.stop
_em2.control2._magnet_filter = _filter
head_offset.set_post_filter(_filter)
_worker.thread = threading.Thread(target=_worker.run, name="target_magnet", daemon=True)
_worker.thread.start()
print(f"[magnet] installed: UI-element magnet on the corrected gaze (worker thread, {_TICK_S*1000:.0f} ms tick), cfg={_cfg}")
