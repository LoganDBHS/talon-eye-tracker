"""Head-pose cursor offset + gaze gain correction, layered on Talon's built-in
Control Mouse (talon.plugins.eye_mouse_2, "Control Mouse 2").

WHY THIS EXISTS (2026-08-31)
  Talon 0.4.0's Head Control only nudges the cursor by the *translation* of the
  eye midpoint (gain 0.1-2.25 mm/mm, reset on every gaze jump), and Tobii's gaze
  point is geometrically head-compensated, so pitching the head up to reach the
  top of a 32" screen moves the cursor almost nothing. Talon never receives a
  head-rotation stream from the Eye Tracker 5 (that runs on the host inside
  Tobii's own software, which Talon bypasses), so rotation has to be inferred
  from the two 3D eyeball positions the tracker does send:
    * vertical  : "rise" = eye-centre height along the screen's up axis. Pitching
                  up swings the eyes up around the neck pivot (~30 mm for a
                  comfortable look-up). Proxy for pitch.
    * horizontal: "yaw"  = heading of the left->right eye vector (degrees). This
                  IS a real rotation; pure sideways head translation leaves it 0.

HOW IT WORKS
  * subscribes to the tracker's 'gaze' stream (one cheap callback per frame,
    ~90 Hz: a few multiplies, an EMA, no allocation)
  * keeps a smoothed head pose and an "anchor" (neutral pose). Offset =
    gain * (pose - anchor) beyond a dead zone, clamped to head_max_offset_mm.
  * replaces the `ctrl` name inside eye_mouse_2 with a proxy: every
    ctrl.mouse_move(x, y) Talon's control mouse makes gets the gaze gain
    correction (Task 4 fallback; identity by default) and the head offset added,
    then is clamped to the screen. ctrl.mouse_pos() is un-offset for Talon so
    its "did the user touch the physical mouse?" detection keeps working.
  * if the head moves while the gaze target is steady, Talon issues no move, so
    the gaze callback re-applies the last target itself - unless something else
    (hand mouse, zoom overlay) has moved the cursor since, in which case it
    backs off and lets Talon resume normally.

LIVE TUNING: every knob is a `user.*` setting in head_tracking_settings.talon -
edit, save, done (re-read every 250 ms). Hotkeys in head_tracking.talon:
  ctrl-alt-h toggle the offset, ctrl-alt-r re-centre (current pose = neutral).

Nothing here touches community or talon_plugins files.
"""
import math
import time

from talon import Module, app, cron, ctrl as _real_ctrl, settings, tracking_system, ui
from talon.plugins import eye_mouse_2 as _em2
from talon.scripting import rctx

from . import tracking_diag
from .tracking_diag import head_from_frame

mod = Module()
_ctx = rctx.active()  # this module's resource context (used for late registrations)

# key -> (setting name, default, description). All floats.
_SETTINGS = {
    "gain_y": ("head_gain_y", 6.0,
               "Vertical head gain: cursor mm per mm of head rise (eye-centre moving up the screen's axis). "
               "Higher = a smaller head tilt reaches the top edge; too high = jitter/overshoot. 0 disables vertical."),
    "dead_y": ("head_deadzone_y_mm", 3.0,
               "Head rise (mm) ignored around the neutral pose. Raise if breathing/posture moves the cursor; "
               "lower if the first part of a tilt does nothing."),
    "gain_x": ("head_gain_x", 0.0,
               "Horizontal head gain: cursor mm per degree of head yaw (turning left/right). 0 = off. "
               "Try 8-12; higher = less turn needed to reach a side edge."),
    "dead_x": ("head_deadzone_x_deg", 1.5,
               "Head yaw (degrees) ignored around neutral. Yaw noise is ~1 deg, so keep >= 1."),
    "smooth_ms": ("head_smoothing_ms", 80.0,
                  "Time constant (ms) of the head-pose low-pass. Higher = steadier but laggier cursor; "
                  "lower = snappier but shakier. 0 = raw."),
    "max_mm": ("head_max_offset_mm", 400.0,
               "Clamp on the head offset magnitude in cursor mm (per axis). 0 = no clamp."),
    "recenter_s": ("head_recenter_seconds", 0.0,
                   "If > 0, the neutral pose slowly drifts toward the current pose with this time constant "
                   "(absorbs slow postural drift, but also slowly cancels a held tilt). 0 = never; use ctrl-alt-r."),
    "lost_s": ("head_lost_recenter_seconds", 5.0,
               "If the eyes were not seen for this many seconds (you got up), the next pose becomes the new "
               "neutral. 0 = never."),
    # gaze correction, per side of centre: u -> u*(gain + curve*|u|), u = 0..1 eccentricity
    # toward that edge. 1.0/0.0 = untouched. ctrl-alt-m prints the values to use.
    "k_left": ("gaze_gain_left", 1.0, "Gaze gain toward the LEFT edge (>1 stretches, <1 shrinks)."),
    "k_right": ("gaze_gain_right", 1.0, "Gaze gain toward the RIGHT edge."),
    "k_up": ("gaze_gain_up", 1.0, "Gaze gain toward the TOP edge."),
    "k_down": ("gaze_gain_down", 1.0, "Gaze gain toward the BOTTOM edge."),
    "q_left": ("gaze_curve_left", 0.0, "Quadratic term toward the LEFT edge (only if ctrl-alt-m says non-linear)."),
    "q_right": ("gaze_curve_right", 0.0, "Quadratic term toward the RIGHT edge."),
    "q_up": ("gaze_curve_up", 0.0, "Quadratic term toward the TOP edge."),
    "q_down": ("gaze_curve_down", 0.0, "Quadratic term toward the BOTTOM edge."),
    "rot": ("gaze_map_rotation_deg", 0.0,
            "Rotation of the gaze map as MEASURED by ctrl-alt-m (deg, + = clockwise); it is undone here."),
}
for _key, (_name, _default, _desc) in _SETTINGS.items():
    mod.setting(_name, type=float, default=_default, desc=_desc)

_cfg = {k: v[1] for k, v in _SETTINGS.items()}

ANCHOR_SETTLE_S = 2.0   # neutral pose follows the head quickly for this long after (re)acquisition
LOST_ZERO_S = 1.0       # eyes unseen this long -> offset decays to zero (no stale push)


def _refresh_settings():
    for key, (name, default, _d) in _SETTINGS.items():
        try:
            v = settings.get(f"user.{name}")
            _cfg[key] = float(v) if v is not None else default
        except Exception:
            pass
    # also runs every 250 ms: drop a stale offset once the eyes have been gone
    st = _st
    if st.last_seen and st.offset_px != (0.0, 0.0) and time.perf_counter() - st.last_seen > LOST_ZERO_S:
        st.offset_px = (0.0, 0.0)
        _reapply()


# --- screen geometry ------------------------------------------------------------
class _Geo:
    rect = None
    cx = cy = 0.0
    hx = hy = 1.0
    ppm_x = ppm_y = 1.0   # pixels per mm


_geo = _Geo()


def _update_screen(*_args):
    s = ui.main_screen()
    r = s.rect
    _geo.rect = r
    _geo.cx = r.x + r.width / 2
    _geo.cy = r.y + r.height / 2
    _geo.hx = max(r.width / 2, 1)
    _geo.hy = max(r.height / 2, 1)
    _geo.ppm_x = r.width / (s.mm_x or 699.0)
    _geo.ppm_y = r.height / (s.mm_y or 393.0)


# --- state -------------------------------------------------------------------------
class _State:
    enabled = True
    s_rise = s_yaw = None      # smoothed pose
    a_rise = a_yaw = None      # anchor (neutral) pose
    raw_rise = raw_yaw = dist = None
    last_ts = 0.0
    last_seen = 0.0
    settle_until = 0.0         # anchor follows the head fast until this timestamp
    offset_px = (0.0, 0.0)     # current head offset, pixels (tuple = atomic swap)
    frames = 0
    # proxy bookkeeping
    last_raw = None            # last (x, y) Talon's control mouse asked for
    last_real = None           # where we actually put the cursor
    applied = (0.0, 0.0)       # last_real - last_raw
    last_reapply = 0.0


_st = _State()


def _deadzone(d, dz):
    if dz <= 0:
        return d
    if abs(d) < dz:
        return 0.0
    return d - math.copysign(dz, d)


def _transform(x, y, offset=None):
    """Raw control-mouse target -> gain-corrected, head-offset, screen-clamped target.
    `offset` overrides the live head offset (px) - the zoom mouse freezes it."""
    g, c = _geo, _cfg
    dx, dy = x - g.cx, y - g.cy
    rot = c["rot"]
    if rot:
        # undo the measured clockwise rotation of the gaze map (pixel space, y down)
        th = math.radians(-rot)
        cs, sn = math.cos(th), math.sin(th)
        dx, dy = dx * cs - dy * sn, dx * sn + dy * cs
    u, v = dx / g.hx, dy / g.hy
    if u > 0:
        u *= c["k_right"] + c["q_right"] * u
    else:
        u *= c["k_left"] - c["q_left"] * u
    if v > 0:
        v *= c["k_down"] + c["q_down"] * v
    else:
        v *= c["k_up"] - c["q_up"] * v
    ox, oy = _st.offset_px if offset is None else offset
    X = g.cx + u * g.hx + ox
    Y = g.cy + v * g.hy + oy
    r = g.rect
    X = min(max(X, r.x), r.x + r.width - 1)
    Y = min(max(Y, r.y), r.y + r.height - 1)
    return X, Y


# --- the ctrl proxy seen by eye_mouse_2 ----------------------------------------
class _CtrlProxy:
    _is_head_offset_proxy = True

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def mouse_move(self, x, y, **kw):
        st = _st
        st.last_raw = (x, y)
        rx, ry = _transform(x, y)
        st.applied = (rx - x, ry - y)
        st.last_real = (rx, ry)
        return self._real.mouse_move(rx, ry, **kw)

    def mouse_pos(self):
        px, py = self._real.mouse_pos()
        ax, ay = _st.applied
        return (px - ax, py - ay)

    def mouse_click(self, *a, **kw):
        if kw.get("pos"):
            kw["pos"] = _transform(*kw["pos"])
        return self._real.mouse_click(*a, **kw)

    def mouse_scroll(self, *a, **kw):
        if kw.get("pos"):
            kw["pos"] = _transform(*kw["pos"])
        return self._real.mouse_scroll(*a, **kw)


def _reapply():
    """Head moved but Talon's target didn't: move the cursor ourselves."""
    st = _st
    if st.last_raw is None or st.last_real is None or not _em2.control2.running:
        return
    now = time.perf_counter()
    if now - st.last_reapply < 0.012:
        return
    try:
        px, py = _real_ctrl.mouse_pos()
    except Exception:
        return
    lx, ly = st.last_real
    if abs(px - lx) > 1.5 or abs(py - ly) > 1.5:
        return  # hand mouse / zoom overlay moved it: don't fight, Talon will resume
    rx, ry = _transform(*st.last_raw)
    if abs(rx - lx) < 0.5 and abs(ry - ly) < 0.5:
        return
    st.last_reapply = now
    st.applied = (rx - st.last_raw[0], ry - st.last_raw[1])
    st.last_real = (rx, ry)
    _real_ctrl.mouse_move(rx, ry)


def _on_gaze(frame):
    # tracker thread, ~90 Hz: keep it cheap
    head = head_from_frame(frame)
    if head is None:
        return
    rise, dist, yaw, _roll, _cx = head
    st, c = _st, _cfg
    ts = frame.ts
    dt = ts - st.last_ts if st.last_ts else 0.011
    if dt <= 0 or dt > 0.5:
        dt = 0.011
    st.last_ts = ts
    st.raw_rise, st.raw_yaw, st.dist = rise, yaw, dist
    st.frames += 1

    gap = ts - st.last_seen if st.last_seen else 0.0
    st.last_seen = ts
    tau = c["smooth_ms"] / 1000.0
    if st.s_rise is None or gap > 1.0:
        st.s_rise, st.s_yaw = rise, yaw            # (re)acquire: snap, don't slew
    else:
        a = dt / (tau + dt) if tau > 0 else 1.0
        st.s_rise += a * (rise - st.s_rise)
        st.s_yaw += a * (yaw - st.s_yaw)

    if st.a_rise is None or (c["lost_s"] > 0 and gap > c["lost_s"]):
        # first sight / came back: start a new neutral, but let it SETTLE over
        # the next ANCHOR_SETTLE_S rather than trusting the very first frame
        # (which is often mid-sit-down or a half-detected face)
        st.a_rise, st.a_yaw = st.s_rise, st.s_yaw
        st.settle_until = ts + ANCHOR_SETTLE_S
    elif ts < st.settle_until:
        b = min(dt / 0.4, 1.0)
        st.a_rise += b * (st.s_rise - st.a_rise)
        st.a_yaw += b * (st.s_yaw - st.a_yaw)
    elif c["recenter_s"] > 0:
        b = min(dt / c["recenter_s"], 1.0)
        st.a_rise += b * (st.s_rise - st.a_rise)
        st.a_yaw += b * (st.s_yaw - st.a_yaw)

    if not st.enabled:
        return
    dy_mm = -_deadzone(st.s_rise - st.a_rise, c["dead_y"]) * c["gain_y"]   # head up -> cursor up (-y)
    dx_mm = _deadzone(st.s_yaw - st.a_yaw, c["dead_x"]) * c["gain_x"]      # turn right -> cursor right
    m = c["max_mm"]
    if m > 0:
        dx_mm = max(-m, min(m, dx_mm))
        dy_mm = max(-m, min(m, dy_mm))
    st.offset_px = (dx_mm * _geo.ppm_x, dy_mm * _geo.ppm_y)
    _reapply()


# --- public helpers (used by tracking_diag / zoom_pop) --------------------------
def transform_px(x, y, offset=None):
    """Apply gaze gain correction + head offset (or a frozen `offset`) to a screen-pixel point."""
    return _transform(x, y, offset)


def offset_px():
    return _st.offset_px


def last_raw_target():
    """Last uncorrected target Talon's control mouse asked for, or None."""
    return _st.last_raw


def status_snapshot():
    st = _st
    d = {"on": st.enabled, "offset_px": (round(st.offset_px[0]), round(st.offset_px[1]))}
    if st.s_rise is not None and st.a_rise is not None:
        d["d_rise_mm"] = round(st.s_rise - st.a_rise, 1)
        d["d_yaw_deg"] = round(st.s_yaw - st.a_yaw, 1)
    return d


def _set_enabled(state):
    _st.enabled = state
    if not state:
        _st.offset_px = (0.0, 0.0)
        _reapply()


@mod.action_class
class Actions:
    def head_offset_toggle():
        """Toggle the head-pose cursor offset on/off"""
        _set_enabled(not _st.enabled)
        app.notify("Head offset", "ON" if _st.enabled else "OFF")
        print(f"[head_offset] {'ON' if _st.enabled else 'OFF'}")

    def head_offset_enabled() -> bool:
        """Is the head-pose cursor offset enabled?"""
        return _st.enabled

    def head_offset_recenter():
        """Make the current head pose the neutral pose (zero offset)"""
        st = _st
        if st.s_rise is None:
            app.notify("Head offset", "No head data yet - are your eyes in view?")
            return
        st.a_rise, st.a_yaw = st.s_rise, st.s_yaw
        st.offset_px = (0.0, 0.0)
        _reapply()
        app.notify("Head offset", f"Re-centred (rise {st.s_rise:.0f} mm, yaw {st.s_yaw:+.1f} deg)")
        print(f"[head_offset] re-centred at rise={st.s_rise:.1f}mm yaw={st.s_yaw:+.1f}deg dist={st.dist:.0f}mm")

    def head_offset_status():
        """Print the head-offset state and current settings to the log"""
        print(f"[head_offset] state={status_snapshot()} frames={_st.frames} cfg={_cfg}")
        app.notify("Head offset", str(status_snapshot()))

    def head_offset_uninstall():
        """Restore Talon's control mouse to the real ctrl module (removes the proxy)"""
        _em2.ctrl = _real_ctrl
        _st.offset_px = (0.0, 0.0)
        print("[head_offset] proxy uninstalled; eye_mouse_2 uses the real ctrl again")


# --- install ---------------------------------------------------------------------
_update_screen()
ui.register("screen_change", _update_screen)
_refresh_settings()
cron.interval("250ms", _refresh_settings)
tracking_system.register("gaze", _on_gaze)
tracking_diag.set_offset_provider(status_snapshot)
_em2.ctrl = _CtrlProxy(_real_ctrl)   # always wrap the real module (never an older proxy)
print(f"[head_offset] installed: proxy on eye_mouse_2.ctrl, screen {_geo.rect} "
      f"({_geo.ppm_x:.2f} px/mm), cfg={_cfg}")
