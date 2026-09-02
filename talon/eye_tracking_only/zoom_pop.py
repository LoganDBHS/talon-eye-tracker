import ctypes
import os
import time

from talon import Module, actions, app, cron, ctrl, settings, tap
from talon.types import Point2d
from talon_plugins import eye_zoom_mouse

mod = Module()

_user32 = ctypes.windll.user32

# --- atomic clicks via SendInput -----------------------------------------------
# ctrl.mouse_move + ctrl.mouse_click(hold=32000) are separate injections, and
# the control mouse keeps injecting gaze moves between/during them: a cursor
# move during the 32ms button hold turns the click into a micro-drag, which is
# why Explorer never saw a double-click. Batching [abs move, down, up] into ONE
# SendInput call makes each click atomic — nothing can interleave.

_ULONG_PTR = ctypes.c_ulonglong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


_BTN_FLAGS = {0: (0x0002, 0x0004), 1: (0x0008, 0x0010)}  # (down, up) left/right
_ABS_MOVE = 0x0001 | 0x8000 | 0x4000  # MOVE | ABSOLUTE | VIRTUALDESK


def _click_at(x, y, button=0, count=1):
    vx = _user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    vy = _user32.GetSystemMetrics(77)
    vw = _user32.GetSystemMetrics(78)
    vh = _user32.GetSystemMetrics(79)
    nx = int((x - vx) * 65535 / max(vw - 1, 1))
    ny = int((y - vy) * 65535 / max(vh - 1, 1))
    down, up = _BTN_FLAGS[button]
    events = []
    for _ in range(count):
        events += [(nx, ny, _ABS_MOVE), (0, 0, down), (0, 0, up)]
    arr = (_INPUT * len(events))()
    for inp, (dx, dy, flags) in zip(arr, events):
        inp.type = 0  # INPUT_MOUSE
        inp.mi.dx = dx
        inp.mi.dy = dy
        inp.mi.dwFlags = flags
    sent = _user32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))
    if sent != len(arr):
        print(f"[zoom_pop] SendInput only sent {sent}/{len(arr)} events")


def _overlay_open():
    zm = eye_zoom_mouse.zoom_mouse
    return zm.enabled and zm.state == eye_zoom_mouse.STATE_OVERLAY


# last overlay commit, per button: (time, x, y). Needed because the control
# mouse keeps steering the cursor with gaze after a commit, so a follow-up
# click must re-warp to the ORIGINAL target — the cursor has already drifted.
_last_commit = {}


def _zoom_commit(zm, button, count=1):
    # mirror of ZoomMouse.on_pop's overlay branch, but for any mouse button
    zm.cancel()
    # drop the AHK flag NOW, before the 50ms poller does: otherwise the bridge
    # is still armed and swallows the synthetic click we are about to send
    _clear_flag()
    dot, origin = zm.get_pos()
    print(f"[zoom_pop] commit button={button} count={count} origin={origin}")
    if origin:
        _click_at(origin.x, origin.y, button, count)
        zm.last_click = time.time()
        _last_commit[button] = (time.time(), origin.x, origin.y)
        if button == 0 and count == 1:
            # arm the double-click window: a second physical left click while
            # the window lasts is relayed as F17 and replayed at these coords
            _arm_double()


def _followup(button):
    """Second physical click after a commit: replay it at the committed target
    (the control mouse has moved the cursor since), completing a double-click."""
    last = _last_commit.get(button)
    if not last or time.time() - last[0] > 1.0:
        return
    t, x, y = last
    # disarm BEFORE clicking, or the bridge swallows our own synthetic click
    _clear_double()
    # if the second tap came fast enough, one replayed click pairs with the
    # commit click into an OS double-click; past ~350ms the OS won't pair them
    # any more, so send a self-contained atomic double instead — the user's
    # tap-tap rhythm stops mattering
    count = 1 if time.time() - t < 0.35 else 2
    print(f"[zoom_pop] follow-up click button={button} count={count} at ({x:.0f}, {y:.0f})")
    _click_at(x, y, button, count)
    _last_commit[button] = (time.time(), x, y)
    if button == 0:
        _arm_double()  # allow a triple-click too


def _commit_or_followup(button):
    if _overlay_open():
        _zoom_commit(eye_zoom_mouse.zoom_mouse, button)
    else:
        # F13/F14 with the overlay closed: the user tapped again before Talon
        # processed the first commit, so the still-armed bridge relayed the
        # second physical click too
        _followup(button)


@mod.action_class
class Actions:
    def zoom_mouse_pop():
        """Open the zoom overlay at gaze; does nothing while already zoomed"""
        zm = eye_zoom_mouse.zoom_mouse
        if zm.enabled and zm.state == eye_zoom_mouse.STATE_IDLE:
            _freeze_head_offset()
            zm.on_pop(0)

    def zoom_mouse_cancel():
        """Close the zoom overlay without clicking"""
        if _overlay_open():
            eye_zoom_mouse.zoom_mouse.cancel()
            _clear_flag()

    def zoom_mouse_commit_left():
        """Left-click the gazed target in the zoom overlay and close it"""
        _commit_or_followup(0)

    def zoom_mouse_commit_right():
        """Right-click the gazed target in the zoom overlay and close it"""
        _commit_or_followup(1)

    def zoom_mouse_commit_double():
        """Double-click the gazed target in the zoom overlay and close it"""
        if _overlay_open():
            # the gesture is shift+click, so physical Shift is probably still
            # down; release it or the app sees shift-clicks (range select)
            actions.key("shift:up")
            _zoom_commit(eye_zoom_mouse.zoom_mouse, 0, count=2)

    def zoom_mouse_followup_click():
        """Second physical left click shortly after a zoom commit (relayed as
        F17 by the bridge): click the committed target again = double-click"""
        _followup(0)


# --- flag file for the AutoHotkey mouse bridge ---------------------------------
# Talon's mouse tap hook is broken on this machine (tap.MCLICK/MMOVE never fire,
# verified 2026-08-29), so physical mouse buttons are relayed by an AutoHotkey
# script (%LOCALAPPDATA%\Programs\TalonZoomBridge\zoom_bridge.ahk). It needs to
# know when the zoom overlay is open; we signal that with a flag file.

_FLAG = os.path.join(os.environ.get("TEMP", ""), "talon_zoom_open.flag")
# second flag: exists for 400ms after a left commit; while present the bridge
# relays LButton as F17 so a natural tap-tap becomes a real double-click even
# though the control mouse has moved the cursor off the target
_DOUBLE_FLAG = os.path.join(os.environ.get("TEMP", ""), "talon_zoom_double.flag")
_flag_present = [False]
_double_cron = [None]


def _clear_flag():
    _flag_present[0] = False
    try:
        os.remove(_FLAG)
    except OSError:
        pass


def _clear_double():
    if _double_cron[0]:
        cron.cancel(_double_cron[0])
        _double_cron[0] = None
    try:
        os.remove(_DOUBLE_FLAG)
    except OSError:
        pass


def _arm_double():
    _clear_double()
    try:
        with open(_DOUBLE_FLAG, "w") as f:
            f.write("1")
        _double_cron[0] = cron.after("900ms", _clear_double)
    except OSError:
        pass


def _flag_tick():
    open_now = _overlay_open()
    if open_now != _flag_present[0]:
        if open_now:
            _flag_present[0] = True
            _clear_double()  # a fresh overlay supersedes any pending double
            try:
                with open(_FLAG, "w") as f:
                    f.write("1")
            except OSError:
                pass
        else:
            _clear_flag()


# make sure stale flags from a previous session don't swallow clicks
for _f in (_FLAG, _DOUBLE_FLAG):
    try:
        os.remove(_f)
    except OSError:
        pass

cron.interval("50ms", _flag_tick)


# the zoom-mouse toggle does not persist across Talon restarts; enable it on
# startup so the F4 flow is always ready without pressing ctrl-alt-z first
def _auto_enable():
    try:
        if not eye_zoom_mouse.zoom_mouse.enabled:
            actions.tracking.control_zoom_toggle(True)
            print("[zoom_pop] zoom mouse auto-enabled")
    except Exception as ex:
        print(f"[zoom_pop] zoom mouse auto-enable failed: {ex}")


app.register("ready", lambda: cron.after("3s", _auto_enable))


# --- head offset / gaze gain integration (head_offset.py) ------------------------
# The zoom overlay opens around the GAZE point and its dot follows raw gaze, so
# without this the zoom would ignore the head offset and gain correction the
# control-mouse cursor has. eye_zoom_mouse reads gaze through the legacy
# EyeMouse's `mouse.eye_hist` (module global `mouse`); we swap that name for a
# proxy whose eye_hist yields frames with the same correction applied. The head
# offset is FROZEN at pop time so relaxing your head inside the zoom doesn't
# slide the dot. Setting user.zoom_follows_head_offset = 0 disables this.
try:
    from . import head_offset as _head_offset
except Exception as _ex:  # head_offset.py absent or broken: zoom stays stock
    _head_offset = None
    print(f"[zoom_pop] head_offset not available, zoom uses raw gaze: {_ex}")

mod.setting(
    "zoom_follows_head_offset",
    type=int,
    default=1,
    desc="1 = the F4 zoom opens where the head-offset/gain-corrected cursor is; 0 = stock behaviour (raw gaze)",
)

_frozen_offset = [((0.0, 0.0), (0.0, 0.0))]   # (extra push px, eye-in-head credit)


def _freeze_head_offset():
    if _head_offset is not None:
        _frozen_offset[0] = _head_offset.head_state()


class _ShiftedEye:
    __slots__ = ("gaze", "rel", "detected")

    def __init__(self, eye, gaze):
        self.gaze = gaze
        self.rel = eye.rel
        self.detected = eye.detected

    def __bool__(self):
        return bool(self.detected)


class _ShiftedFrame:
    """Looks enough like a GazeFrame for eye_zoom_mouse: iterates as (left, right)."""
    __slots__ = ("left", "right", "ts")

    def __init__(self, frame, rect, offset):
        self.ts = frame.ts
        self.left = self._shift(frame.left, rect, offset)
        self.right = self._shift(frame.right, rect, offset)

    @staticmethod
    def _shift(eye, rect, offset):
        g = eye.gaze
        px = rect.x + g.x * rect.width
        py = rect.y + g.y * rect.height
        cx, cy = _head_offset.transform_px(px, py, offset[0], offset[1])
        return _ShiftedEye(eye, Point2d((cx - rect.x) / rect.width, (cy - rect.y) / rect.height))

    def __iter__(self):
        yield self.left
        yield self.right


class _EyeMouseProxy:
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @property
    def eye_hist(self):
        hist = self._real.eye_hist
        if _head_offset is None or not settings.get("user.zoom_follows_head_offset"):
            return hist
        rect = eye_zoom_mouse.eye_config.rect
        off = _frozen_offset[0]
        # only the tail is ever read (eye_avg = 20 frames); keep it cheap
        return [_ShiftedFrame(f, rect, off) for f in hist[-32:]]


if _head_offset is not None:
    _real_mouse = getattr(eye_zoom_mouse.mouse, "_real", eye_zoom_mouse.mouse)
    eye_zoom_mouse.mouse = _EyeMouseProxy(_real_mouse)
    print("[zoom_pop] zoom overlay follows head offset / gaze gain (user.zoom_follows_head_offset)")

print("[zoom_pop] module loaded; AHK bridge flag poller running")
