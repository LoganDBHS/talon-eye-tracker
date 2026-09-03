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
    and, while a target is held, freezes the eye POSITIONS in the frame it
    hands on to Talon's control mouse (the gaze itself is passed through
    untouched). Freezing the eye positions matters: the control mouse runs in
    jump mode (45 mm zones, measured 2026-09-02) where small gaze changes are
    ignored and the HEAD does the fine positioning - a still head means Talon
    never drags the cursor off the target. Real head motion resumes on
    release, blended in over ~150 ms so there is no jump. (An earlier version
    also replaced the gaze with the snap point; the step back to the real gaze
    on release made Talon suppress its hop for 300-1300 ms - measured with
    ctrl-alt-l on 2026-09-02 - so it was dropped.)
  * A background thread (Windows UI Automation works off the main thread,
    ~2-8 ms per lookup, verified) ticks every 40 ms: when the gaze has been
    within `magnet_settle_px` for `magnet_settle_ms` it hit-tests the settled
    point, recovers the enclosing button when the hit is only its icon or
    label (hit-tests just outside that child), and probes a ring of
    `magnet_reach_px` around the point when the hit is a container. The
    smallest clickable thing wins.
  * Gap targets (2026-09-03): empty space is a target too. When the settled
    point is on a container (a window pane, toolbar, title bar - not a button,
    not text) and no clickable element is within `magnet_gap_clear_px`, the
    thread walks a cross from the point (10 px steps, 20 px beyond 30; the
    long axis is walked along the strip's centre line) until each arm hits a
    clickable element, a different element that is neither
    the container's parent nor its child (a title bar ends where the document
    below it begins), a big text/document/image area (content, even when it
    is a child of the container), the screen edge, or `magnet_axis_snap_px`. The free
    strip that gives is grabbed exactly like a button: a short side (<= axis
    px) snaps the cursor to its centre line, a long side follows the gaze.
    Chrome's empty tab-strip space right of the last button, for example,
    measures ~80 px tall (tabs above the toolbar), so the cursor sits on its
    centre line and you can double-click there to maximise the window. Space
    that is wide AND tall (a page body, the desktop) is not a target - the
    gaze is precise enough there and the head stays free. Buttons keep
    priority for the FIRST grab: within magnet_gap_clear_px of one, the reach
    ring below wins. A HELD gap is sticky the other way: a neighbouring
    button only takes over once the settled gaze is `magnet_gap_stick_px`
    inside it (first test 2026-09-03: gaze noise a few px into the toolbar
    under the tab strip kept stealing the gap).
  * Sticky: the target is kept while the gaze stays inside its rectangle plus
    `magnet_release_px` (a gaze further than 3x that / 120 px releases on the
    very next frame, so a saccade away is never delayed); a settled gaze on a
    DIFFERENT clickable element switches at once. The element is re-checked every 400 ms (windows scroll
    and move). Wide elements (a list row, the omnibox) only snap the short
    axis; along the long axis the cursor follows the gaze inside the element.
  * Because of the jump-mode dead zone, Talon may not move the cursor onto a
    target 20 px away at all. If the cursor is not on the snap point
    `magnet_place_ms` after the grab, the thread places it with
    ctrl.mouse_move (throttled, only when it has drifted > 3 px) - as a short
    ease-out glide (`magnet_glide_ms`, 0 = instant hop). Talon reports
    mouse_active=False after these moves, i.e. it does not mistake them for
    the physical mouse (verified 2026-09-02).
  * A click-through canvas draws a thin rounded outline around the held
    element (verified not to disturb the hit-test).

  Nothing happens while the control mouse is off (ctrl-alt-e), while the F4
  zoom overlay is open, or over Talon's own windows (ctrl-alt-m page).
  Frames without a detected eye carry a (0, 0) gaze; they are ignored (seen
  2026-09-03: the magnet was grabbing a window's system-menu button at the
  top-left corner while nobody sat at the screen), and a held target is let
  go after 0.4 s without eyes (a blink is shorter).

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

from talon import Module, actions, app, canvas, cron, ctrl, settings, ui
from talon.plugins import eye_mouse_2 as _em2
from talon.types import Point2d, Point3d, Rect

from . import head_offset

mod = Module()

_SETTINGS = {
    "on": ("magnet_on", 1.0, "1 = the target magnet is active (ctrl-alt-t toggles it at runtime)."),
    "settle_ms": ("magnet_settle_ms", 60.0,
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
    "place_ms": ("magnet_place_ms", 30.0, "How long to give Talon before placing the cursor (ms)."),
    "glide_ms": ("magnet_glide_ms", 0.0,
                 "Placement glides to the target with an ease-out over about this long (ms; shorter hops are "
                 "quicker, far jumps up to 1.6x longer). 0 = instant hop."),
    "gap": ("magnet_gap_targets", 1.0,
            "1 = empty space next to buttons (title bars, toolbar ends, taskbar) is a target too: a free "
            "strip whose short side is <= magnet_axis_snap_px snaps the cursor to its centre line."),
    "gap_clear_px": ("magnet_gap_clear_px", 24.0,
                     "Empty space only wins when no clickable element is within this many px of the settled "
                     "point (closer = the button is meant, the reach ring grabs it)."),
    "gap_stick_px": ("magnet_gap_stick_px", 16.0,
                     "While empty space is held, a neighbouring button only takes over once the settled gaze "
                     "is this far inside it (px; capped at a third of the button's side)."),
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
_GAP_SKIP_TYPES = {"Text", "Image", "Edit", "Document"}   # a settled point on these never starts a gap
_GAP_STEPS = (10.0, 20.0, 30.0)   # then 20 px steps (a button within gap_clear_px must be seen exactly)
_EYES_LOST_S = 0.4                # no eye detected for this long -> let go of the target (blink < this)


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
    eyes_lost_ts = None     # perf ts since which no eye has been detected
    grabs = releases = placements = 0
    last_err = ""


_st = _State()
_samples = deque()          # (perf_ts, x, y) corrected gaze, tracker thread only


# --- tracker-thread side: sample recording + frame rewriting -----------------------
def _filter(f, x, y):
    """Called by head_offset for every corrected frame (f is already a copy)."""
    now = time.perf_counter()
    if not (f.left.detected or f.right.detected):
        # no eyes: Talon still fills in a (0, 0) gaze - never settle on it
        _samples.clear()
        prev = _st.latest
        if prev is not None:
            _st.latest = (now, prev[1], prev[2], None)
        if _st.eyes_lost_ts is None:
            _st.eyes_lost_ts = now
        elif _st.held is not None and now - _st.eyes_lost_ts > _EYES_LOST_S:
            _release("eyes lost")
        return False
    _st.eyes_lost_ts = None
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
    if held is not None and not held.contains(x, y, max(3.0 * _cfg["release_px"], 120.0)):
        # clearly gone (a saccade away): let go NOW on the tracker thread rather than
        # feeding Talon the old snap point for another worker tick + release_ms
        _release("gaze far")
        held = None
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
    # a target is held: Talon keeps receiving the REAL gaze (replacing it with the snap
    # point and stepping back on release made Talon's hop logic see a huge velocity and
    # suppress hops for 300-1300 ms - measured 2026-09-02 16:36) but a perfectly still
    # head, so its head control cannot drag the cursor off the target. The cursor itself
    # is placed by the worker.
    if _st.frozen is None and both:
        _st.frozen = (_copy3(l.pos), _copy3(r_.pos))
    fz = _st.frozen
    if fz is None:
        return False
    if l.detected:
        f.left = _with_pos(l, fz[0])
    if r_.detected:
        f.right = _with_pos(r_, fz[1])
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
def _move(x, y):
    """Move the cursor the way Talon's own control mouse does: register the target in
    ControlMouse2.ctrl_history first, so Talon's mouse hook does not take the move for
    the physical mouse (that flagged mouse_active and suppressed gaze hops for up to a
    second - measured 2026-09-02 16:36-16:55; registering first = no flag, verified)."""
    xi, yi = int(round(x)), int(round(y))
    try:
        _em2.control2.ctrl_history.append(Point2d(xi, yi))
    except Exception:
        pass
    ctrl.mouse_move(xi, yi)


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


def _wall(e):
    """Is element e something a gap must stop at? Any enabled clickable, whatever its size
    (big clickables are not magnet targets but they are not empty space either), and
    Talon's own windows."""
    if e is None:
        return False
    try:
        if e.pid == _SELF_PID:
            return True
        ctype = e.control_type or ""
        pats = set(e.patterns or ()) - _IGNORED_PATTERNS
        return (ctype in _CLICK_TYPES or bool(pats & _CLICK_PATTERNS)) and e.is_enabled
    except Exception:
        return False


def _foreign(e, r0):
    """Is e a different element that is neither an ancestor nor a descendant of the
    container with rectangle r0 (by geometry - UIA elements here have no parent link)?
    Such a neighbour (the document under a title bar, the window above the taskbar) ends
    the free strip even though nothing there is clickable."""
    if e is None:
        return True
    try:
        r = e.rect
    except Exception:
        return False
    if r is None:
        return False
    same = abs(r.x - r0.x) <= 1 and abs(r.y - r0.y) <= 1 and abs(r.width - r0.width) <= 1 \
        and abs(r.height - r0.height) <= 1
    if same:
        return False
    tol = 1.0
    ancestor = (r.x <= r0.x + tol and r.y <= r0.y + tol
                and r.x + r.width >= r0.x + r0.width - tol and r.y + r.height >= r0.y + r0.height - tol)
    descendant = (r0.x <= r.x + tol and r0.y <= r.y + tol
                  and r0.x + r0.width >= r.x + r.width - tol and r0.y + r0.height >= r.y + r.height - tol)
    return not (ancestor or descendant)


def _content(e):
    """A big text / document / image area: content, not empty space - an arm walking from
    a window border or a toolbar into it stops there even though it is a child of the
    container it started on."""
    try:
        r = e.rect
        return (e.control_type or "") in _GAP_SKIP_TYPES and r is not None             and min(r.width, r.height) > _PASSIVE_MAX_PX
    except Exception:
        return False


def _walk(dx, dy, px, py, gx, gy, r0, clear, cap, scr):
    """Two arms (-/+) along one axis from (px, py). Returns [[.., .., extent, done, ender] x 2]
    or None when a clickable element is within `clear` px of the GAZE point (gx, gy) - then
    the button is meant. Extents are measured from the walk point; a side is 'long' at
    cap + 1, and the walk stops once both extents together exceed cap."""
    if dx:
        edges = (px - scr.x, scr.x + scr.width - 1 - px)
    else:
        edges = (py - scr.y, scr.y + scr.height - 1 - py)
    arms = [[-dx, -dy, 0.0, False, ""], [dx, dy, 0.0, False, ""]]
    for i, a in enumerate(arms):
        if edges[i] <= 0:
            a[2], a[3], a[4] = 0.0, True, "edge"
    d, k = 0.0, 0
    while not (arms[0][3] and arms[1][3]):
        d = _GAP_STEPS[k] if k < len(_GAP_STEPS) else d + 20.0
        k += 1
        for i, a in enumerate(arms):
            if a[3]:
                continue
            if d > edges[i]:
                a[2], a[3], a[4] = edges[i], True, "edge"
                continue
            if d > cap:
                a[2], a[3], a[4] = cap + 1, True, "long"     # long side: exact length does not matter
                continue
            e = _elem(px + a[0] * d, py + a[1] * d)
            if _wall(e):
                try:
                    r = e.rect
                    a[2], near = _rect_dist(r, px, py), _rect_dist(r, gx, gy)
                except Exception:
                    a[2] = near = d
                a[3], a[4] = True, "wall " + _brief(e)
                if near < clear:
                    return None            # a button is meant: let the reach ring have it
            elif _foreign(e, r0) or _content(e):
                try:
                    a[2] = _rect_dist(e.rect, px, py)
                except Exception:
                    a[2] = d
                a[3], a[4] = True, "other " + _brief(e)
            else:
                a[2] = d
        if arms[0][2] + arms[1][2] > cap:
            for a in arms:
                if not a[3]:
                    a[2], a[3], a[4] = cap + 1, True, "long"
    return arms


def _measure_gap(cx, cy, hit):
    """Free space around the settled point (cx, cy), whose direct hit `hit` is a container.
    Walks the vertical arms from the point first; if that gives a strip (height <= the
    axis snap length) the horizontal arms are walked ALONG ITS CENTRE LINE - at the gaze
    height near a strip's edge Chrome does not report its buttons (the New Tab button
    was walked through at y=59 although its rectangle reaches y=72, seen 2026-09-03).
    A tall space is walked horizontally at the point and, if narrow, vertically again
    along the vertical centre line. Returns (Rect, 'gap', name, ctype) or None: a
    clickable element within gap_clear_px of the point, both sides longer than the snap
    length, a side thinner than 20 px (border, seam) or a snap point on a clickable
    element all mean no target."""
    clear = _cfg["gap_clear_px"]
    cap = _cfg["axis_px"]
    scr = head_offset.screen_rect()
    if scr is None:
        return None
    try:
        r0 = hit.rect
    except Exception:
        return None
    v = _walk(0, 1, cx, cy, cx, cy, r0, clear, cap, scr)
    if v is None:
        return None
    h = v[0][2] + v[1][2]
    if h < 20.0:
        return None
    if h <= cap:
        cyl = cy - v[0][2] + h / 2.0
        hz = _walk(1, 0, cx, cyl, cx, cy, r0, clear, cap, scr)
        if hz is None:
            return None
        w = hz[0][2] + hz[1][2]
        if w < 20.0:
            return None
        r = Rect(cx - hz[0][2], cy - v[0][2], w, h)
        walks = (("left", hz[0]), ("right", hz[1]), ("up", v[0]), ("down", v[1]))
    else:
        hz = _walk(1, 0, cx, cy, cx, cy, r0, clear, cap, scr)
        if hz is None:
            return None
        w = hz[0][2] + hz[1][2]
        if w > cap or w < 20.0:
            return None
        cxl = cx - hz[0][2] + w / 2.0
        v2 = _walk(0, 1, cxl, cy, cx, cy, r0, clear, cap, scr)
        if v2 is None:
            return None
        h = v2[0][2] + v2[1][2]
        if h < 20.0:
            return None
        r = Rect(cx - hz[0][2], cy - v2[0][2], w, h)
        walks = (("left", hz[0]), ("right", hz[1]), ("up", v2[0]), ("down", v2[1]))
    try:
        name, ctype = (hit.name or ""), (hit.control_type or "")
    except Exception:
        name, ctype = "", ""
    t = (r, "gap", name, ctype)
    if _cfg["debug"]:
        print(f"[magnet] gap from ({cx:.0f},{cy:.0f}) on {_brief(hit)}: "
              + ", ".join(f"{n} {a[2]:.0f} ({a[4]})" for n, a in walks))
    # the point the cursor would be put on must itself be empty
    sx, sy = _Target(*t).snap(cx, cy)
    if _wall(_elem(sx, sy)):
        if _cfg["debug"]:
            print(f"[magnet] gap rejected: snap point ({sx:.0f},{sy:.0f}) is on a clickable element")
        return None
    return t


def _brief(e):
    try:
        r = e.rect
        return f"{e.control_type}:{(e.name or '')[:14]!r}@({r.x:.0f},{r.y:.0f} {r.width:.0f}x{r.height:.0f})"
    except Exception:
        return repr(e)[:30]


def _lookup(cx, cy):
    """Best target for a settled gaze point, or None."""
    e = _elem(cx, cy)
    hit = _classify(e)
    if hit and hit[1] == "click":
        return hit
    if hit is None and _cfg["gap"] and e is not None:
        try:
            container = e.pid != _SELF_PID and (e.control_type or "") not in _GAP_SKIP_TYPES and not _wall(e)
        except Exception:
            container = False
        if container:
            g = _measure_gap(cx, cy, e)
            if g is not None:
                return g
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


def _well_inside(r, p):
    """Is point p at least magnet_gap_stick_px (capped at a third of the side) inside r?"""
    ix = min(_cfg["gap_stick_px"], r.width / 3.0)
    iy = min(_cfg["gap_stick_px"], r.height / 3.0)
    return r.x + ix <= p[0] <= r.x + r.width - ix and r.y + iy <= p[1] <= r.y + r.height - iy


def _same_line(held, r):
    """Two gap rectangles that snap to the same centre line(s): just widen the held one
    instead of re-grabbing (the cross measured from a new point gives a slightly different
    rectangle every time the gaze wanders along a strip)."""
    a = _cfg["axis_px"]
    if held.h <= a and r.height <= a and abs((held.y + held.h / 2) - (r.y + r.height / 2)) <= 3:
        return True
    if held.w <= a and r.width <= a and abs((held.x + held.w / 2) - (r.x + r.width / 2)) <= 3:
        return True
    return False


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
                t = self.lookup(centroid, now)
                if t is not None and held.same_rect(t[0]):
                    self.miss = (now, centroid[0], centroid[1])   # still ours: do not re-probe every tick
                if t is not None and t[1] == "click" and held.kind == "gap" and not _well_inside(t[0], centroid):
                    # gaze noise nibbling at a neighbouring button: the gap stays held
                    # (the normal release still applies once the gaze is release_px outside)
                    self.miss = (now, centroid[0], centroid[1])
                    t = None
                if t is not None and t[1] in ("click", "gap") and not held.same_rect(t[0]):
                    if t[1] == "gap" and held.kind == "gap" and _same_line(held, t[0]):
                        held.x, held.y, held.w, held.h = t[0].x, t[0].y, t[0].width, t[0].height
                    else:
                        _grab(t, "switch")
                        held = _st.held
            # the element may have moved / scrolled away
            if now - self.checked_ts > _REVALIDATE_S:
                self.checked_ts = now
                sx, sy = held.snap(gx, gy)
                e = _elem(sx, sy)
                t = _classify(e)
                if held.kind == "gap":
                    # the space must still be empty; something clickable there (a menu
                    # opened, a window moved in) is grabbed instead
                    if t is not None and t[1] == "click":
                        _grab(t, "gap filled")
                        held = _st.held
                    elif t is not None or _wall(e):
                        _release("gap filled")
                        return
                elif t is None or not held.same_rect(t[0]):
                    _release("element changed")
                    return
            self.place(held, gx, gy, now)
            return
        if centroid is None:
            return
        t = self.lookup(centroid, now)
        if t is None:
            return
        _grab(t, "settled")

    def lookup(self, centroid, now):
        """_lookup with a miss cache: a settled point that found nothing (or only the held
        target) is not probed again for 0.3 s within 10 px - a gap measurement can cost
        20-30 hit-tests, far too much to repeat every 40 ms tick."""
        m = self.miss
        if m is not None and now - m[0] < 0.3 and math.hypot(centroid[0] - m[1], centroid[1] - m[2]) < 10:
            return None
        t = _lookup(*centroid)
        if t is None:
            self.miss = (now, centroid[0], centroid[1])
        return t

    def place(self, held, gx, gy, now):
        if not _cfg["place"] or now - held.grab_ts < _cfg["place_ms"] / 1000.0:
            return
        sx, sy = held.snap(gx, gy)
        try:
            cx, cy = ctrl.mouse_pos()
        except Exception:
            return
        # single hops only: a run of small moves (the glide / continuous follow tried
        # 2026-09-02) makes Talon flag the physical mouse as active and pause gaze control
        if now - self.last_place < 0.1:
            return
        if math.hypot(cx - sx, cy - sy) <= (6.0 if held.placed_ts else 3.0):
            return
        self.last_place = now
        held.placed_ts = now
        _st.placements += 1
        try:
            self.glide(held, cx, cy, sx, sy)
        except Exception as ex:
            _st.last_err = repr(ex)[:120]
            return
        self.last_place = time.perf_counter()
        if _cfg["debug"]:
            ls = getattr(_em2.control2, "last_state", None)
            extra = f" mouse_active={ls.mouse_active} target_px={ls.target_px}" if ls is not None else ""
            print(f"[magnet] placed cursor ({cx:.0f},{cy:.0f}) -> ({sx:.0f},{sy:.0f}) for {held}{extra}")


    def glide(self, held, x0, y0, x1, y1):
        """Ease-out glide of the cursor to the snap point (runs synchronously in the worker)."""
        base = _cfg["glide_ms"] / 1000.0
        dist = math.hypot(x1 - x0, y1 - y0)
        if base <= 0 or dist < 4:
            _move(x1, y1)
            return
        dur = base * min(1.6, max(0.6, math.sqrt(dist / 60.0)))
        t0 = time.perf_counter()
        while True:
            t = (time.perf_counter() - t0) / dur
            if t >= 1.0:
                break
            e = 1.0 - (1.0 - t) ** 3
            latest = _st.latest
            if latest is not None:          # long-axis targets follow the gaze while gliding
                x1, y1 = held.snap(latest[1], latest[2])
            _move(x0 + (x1 - x0) * e, y0 + (y1 - y0) * e)
            time.sleep(0.008)
        _move(x1, y1)


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
        try:
            actions.user.banner(f"Target magnet {'ON' if _st.enabled else 'OFF'}", "on" if _st.enabled else "off")
        except Exception:
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
