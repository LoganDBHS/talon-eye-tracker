"""Head-pose cursor offset + gaze correction, applied UPSTREAM of Talon's
built-in Control Mouse (talon.plugins.eye_mouse_2, "Control Mouse 2").

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

HOW IT WORKS (rewritten 2026-09-01)
  The gaze frames the control mouse receives are replaced by corrected copies:
  gaze point (and per-eye gaze) -> gaze gain/curve/rotation correction (from
  ctrl-alt-m) -> plus the head offset -> clamped to the screen. Talon then does
  ALL the smoothing / jump logic on already-corrected data; nothing intercepts
  its cursor moves. (The first version wrapped ctrl.mouse_move instead; that
  made the cursor stutter because Talon glides toward its target by re-reading
  the cursor position through a path the wrapper couldn't see.)

  HEAD = "EYE-IN-HEAD CREDIT", NOT A PUSH (rewritten 2026-09-02)
  The tracker's gaze point is already head-compensated: tilting the head while
  keeping the eyes on a target does not move the point. What degrades is the
  gaze estimate when the EYES roll far inside the head (eyelid over the pupil,
  glints lost, one eye dropping out) - that is what the ctrl-alt-m gain/curve
  corrects, and it was measured with the head still, so it is a function of
  the eyes' angle inside the head, not of the screen position. So:
      pitch = lift_mm / head_pivot_mm         (rad; lift = rise above resting)
      credit_v = -pitch * dist / half_height_mm   (screen eccentricity the
                                                    head tilt accounts for)
      v_eye = v_gaze - credit_v * head_credit_y
      corrected v = v_gaze + [v_eye*(k-1) + q*v_eye*|v_eye|]   (k, q per side)
  and the same for yaw horizontally. Eyes-only up to the top edge: v_eye = v,
  (2026-09-02 evening: the correction is evaluated at v_eye*D0/D and scaled by
  D/D0, D0 = gaze_ref_distance_mm from ctrl-alt-m, D = live eye distance -
  same eye angle, whatever the sitting distance.)
  the full correction applies (as before). Head tilted, eyes near neutral:
  v_eye ~ 0, no correction, the raw point is trusted. Both ways of looking at
  a spot land on the same spot, and the head is not asked to do anything
  unnatural. head_credit_* = 0 restores the old "correct by screen position".
  The additive push (head_gain_y/x, cursor mm per mm/deg) still exists for
  anyone who wants the head to be a mouse too; it defaults to 0 now.

  The neutral pose ("resting head facing the centre") is the running average of
  the head pose taken only while the RAW gaze is in the central zone of the
  screen - you tilt to reach edges, never to look at the middle - so it tracks
  posture by itself and never adapts during a tilt. Persisted to
  %APPDATA%/Talon/head_offset_anchor.json across restarts.

  Per frame: a few multiplies, an EMA and three small object copies (~90 Hz).
  When the layer is off AND the gaze correction is identity, frames pass
  through untouched.

LIVE TUNING: every knob is a `user.*` setting in head_tracking_settings.talon -
edit, save, done (re-read every 250 ms). Hotkeys in head_tracking.talon:
  ctrl-alt-h toggle the head offset, ctrl-alt-r re-centre (current pose = neutral).

Nothing here touches community or talon_plugins files.
"""
import copy
import json
import math
import os
import time

from talon import Module, actions, app, cron, settings, tracking_system, ui
from talon.plugins import eye_mouse_2 as _em2
from talon.scripting import rctx
from talon.types import Point2d
from talon_init import TALON_HOME

from . import tracking_diag
from .tracking_diag import head_from_frame

mod = Module()
_ctx = rctx.active()

# key -> (setting name, default, description). All floats.
_SETTINGS = {
    "pivot_mm": ("head_pivot_mm", 100.0,
                 "Effective radius (mm) from the neck pivot to the eyes: head pitch = rise / pivot. Lower = a given "
                 "rise counts as MORE tilt (more credit); 100 fits a nod from the top of the neck, 150-200 a lean "
                 "from the shoulders."),
    "credit_y": ("head_credit_y", 1.0,
                 "How much of the head PITCH is subtracted from the gaze eccentricity BEFORE the up/down gain/curve "
                 "correction (1 = the correction depends only on the eyes' angle inside the head, so a head tilt + "
                 "small eye move and an eyes-only look land on the same spot; 0 = old behaviour, correct by screen "
                 "position). Values in between blend."),
    "credit_x": ("head_credit_x", 1.0,
                 "Same for head YAW and the left/right correction."),
    "gain_y": ("head_gain_y", 0.0,
               "EXTRA PUSH: cursor mm per mm of head rise ADDED to the corrected gaze (the head acts as a mouse). "
               "0 = off (the head only re-weights the gaze correction, see head_credit_y). Was 6-8 before 2026-09-02."),
    "dead_y": ("head_deadzone_y_mm", 5.0,
               "Head rise (mm) ignored around the neutral pose. Raise if nodding/posture moves the cursor; "
               "lower if the first part of a tilt does nothing."),
    "gain_x": ("head_gain_x", 0.0,
               "EXTRA PUSH: cursor mm per degree of head yaw ADDED to the corrected gaze. 0 = off."),
    "dead_x": ("head_deadzone_x_deg", 1.5,
               "Head yaw (degrees) ignored around neutral. Yaw noise is ~1 deg, so keep >= 1."),
    "smooth_ms": ("head_smoothing_ms", 80.0,
                  "Time constant (ms) of the head-pose low-pass. Higher = steadier but laggier; "
                  "lower = snappier but shakier. 0 = raw."),
    "max_mm": ("head_max_offset_mm", 400.0,
               "Clamp on the head offset magnitude in cursor mm (per axis). 0 = no clamp."),
    "neutral_s": ("head_neutral_seconds", 5.0,
                  "The neutral ('resting, facing the centre') pose is the running average of the head pose taken "
                  "ONLY while the gaze is in the central part of the screen; this is its time constant (s). "
                  "Lower = follows posture faster; 0 = frozen (ctrl-alt-r only)."),
    "neutral_zone": ("head_neutral_zone", 0.3,
                     "How far from the screen centre (fraction of the screen, 0.3 = middle 60%) the gaze may be "
                     "for the head to count as resting. Tilts happen when looking near the edges, never here."),
    "lift_only": ("head_lift_only", 1.0,
                  "1 = only LIFTING the head above the resting height moves the cursor (up); dropping the head "
                  "(reading low on the screen) does nothing and simply lowers the resting height. 0 = symmetric."),
    "down_s": ("head_neutral_down_seconds", 2.0,
               "How fast the resting height follows the head DOWN (s). A relaxed/lowered head is the resting head."),
    "up_s": ("head_neutral_up_seconds", 60.0,
             "How fast the resting height creeps UP when the head stays high while looking away from the centre "
             "(sitting up straighter). Slow, so a held tilt is not eaten. 0 = never."),
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
    "ref_dist": ("gaze_ref_distance_mm", 0.0,
                 "Eye distance (mm) at which the gain/curve/rotation were measured (ctrl-alt-m Apply writes it). "
                 "The correction is really a function of the eyes' angle, so at another distance it is evaluated "
                 "at eccentricity*ref/dist and scaled back by dist/ref. 0 = off (correct by screen position)."),
    "gsmooth_ms": ("gaze_smoothing_ms", 90.0,
                   "Extra low-pass (time constant, ms) on the corrected gaze before Talon sees it. "
                   "Higher = steadier cursor for small targets but laggier; 0 = off. Try 60-150."),
    "gjump_px": ("gaze_smoothing_jump_px", 60.0,
                 "A gaze sample further than this (px) from the smoothed point is a real eye movement, not "
                 "jitter: the low-pass restarts there instead of dragging the cursor along a curve. 0 = always smooth."),
    "snap_px": ("gaze_edge_snap_px", 30.0,
                "Edge magnet: a corrected gaze point within this many px of a screen edge is snapped onto the "
                "edge (menu bars, scrollbars, corners). 0 = off."),
    "mirror": ("gaze_mirror_lost_eye", 1.0,
               "1 = when the tracker loses one eye (happens at the top corners), feed Talon the surviving eye's "
               "gaze for both eyes instead of letting it blend in a stale prediction. 0 = off."),
}
for _key, (_name, _default, _desc) in _SETTINGS.items():
    mod.setting(_name, type=float, default=_default, desc=_desc)

_cfg = {k: v[1] for k, v in _SETTINGS.items()}
_identity = [True]      # True when the gaze correction settings are all neutral

ANCHOR_SETTLE_S = 2.0   # neutral pose follows the head quickly for this long after (re)acquisition
LOST_ZERO_S = 1.0       # eyes unseen this long -> offset decays to zero (no stale push)


def _refresh_settings():
    for key, (name, default, _d) in _SETTINGS.items():
        try:
            v = settings.get(f"user.{name}")
            _cfg[key] = float(v) if v is not None else default
        except Exception:
            pass
    c = _cfg
    _identity[0] = (c["k_left"] == 1.0 and c["k_right"] == 1.0 and c["k_up"] == 1.0 and c["k_down"] == 1.0
                    and c["q_left"] == 0.0 and c["q_right"] == 0.0 and c["q_up"] == 0.0 and c["q_down"] == 0.0
                    and c["rot"] == 0.0 and c["snap_px"] == 0.0 and c["gsmooth_ms"] == 0.0
                    and c["ref_dist"] == 0.0)
    # also runs every 250 ms: drop a stale offset once the eyes have been gone
    st = _st
    if st.last_seen and (st.offset_px != (0.0, 0.0) or st.credit != (0.0, 0.0)) \
            and time.perf_counter() - st.last_seen > LOST_ZERO_S:
        st.offset_px = (0.0, 0.0)
        st.credit = (0.0, 0.0)
    _save_anchor()


# --- screen geometry ------------------------------------------------------------
class _Geo:
    rect = None
    cx = cy = 0.0
    hx = hy = 1.0
    ppm_x = ppm_y = 1.0   # pixels per mm
    hx_mm = hy_mm = 1.0   # half screen size in mm


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
    _geo.hx_mm = _geo.hx / _geo.ppm_x
    _geo.hy_mm = _geo.hy / _geo.ppm_y


# --- state -------------------------------------------------------------------------
class _State:
    enabled = True
    s_rise = s_yaw = None      # smoothed pose
    s_dist = None              # smoothed eye distance (mm) for the distance normalisation
    a_rise = a_yaw = None      # anchor (neutral) pose
    raw_rise = raw_yaw = dist = None
    last_ts = 0.0
    last_seen = 0.0
    settle_until = 0.0         # anchor follows the head fast until this timestamp
    offset_px = (0.0, 0.0)     # extra push, pixels (tuple = atomic swap)
    credit = (0.0, 0.0)        # screen eccentricity (-1..1 per axis) the head pose accounts for
    pitch_deg = yaw_deg = 0.0  # inferred head rotation away from neutral (for status/diag)
    frames = 0


_st = _State()


def _deadzone(d, dz):
    if dz <= 0:
        return d
    if abs(d) < dz:
        return 0.0
    return d - math.copysign(dz, d)


def _correction(e, k_pos, q_pos, k_neg, q_neg):
    """Error term for one axis: what the per-side gain/curve ADDS to eccentricity e."""
    if e > 0:
        return e * (k_pos - 1.0) + q_pos * e * e
    return e * (k_neg - 1.0) - q_neg * e * e


def _transform(x, y, offset=None, credit=None):
    """Raw gaze pixel -> rotation-corrected, per-side gain/curve (evaluated at the EYE-IN-HEAD
    eccentricity), plus extra push, screen-clamped pixel.
    `offset` (px) / `credit` (eccentricity) override the live head state - the zoom mouse
    freezes them, ctrl-alt-m evaluates with the head at rest ((0, 0), (0, 0))."""
    g, c = _geo, _cfg
    dx, dy = x - g.cx, y - g.cy
    rot = c["rot"]
    if rot:
        # undo the measured clockwise rotation of the gaze map (pixel space, y down)
        th = math.radians(-rot)
        cs, sn = math.cos(th), math.sin(th)
        dx, dy = dx * cs - dy * sn, dx * sn + dy * cs
    u, v = dx / g.hx, dy / g.hy
    cu, cv = _st.credit if credit is None else credit
    # distance normalisation: the correction is a function of eye ANGLE. An eccentricity e
    # at the live distance D is the same angle as e*D0/D at the measurement distance D0,
    # and an angular error measured there spans D/D0 as much screen here.
    ref, d = c["ref_dist"], _st.s_dist
    rs = ref / d if ref > 0 and d and d > 100 else 1.0
    rs = max(0.5, min(2.0, rs))
    # the correction was measured with the head still, so it is a function of how far
    # the eyes are turned INSIDE the head: subtract the part of the eccentricity the
    # head pose already accounts for, evaluate there, add the result to the raw point
    u += _correction((u - cu) * rs, c["k_right"], c["q_right"], c["k_left"], c["q_left"]) / rs
    v += _correction((v - cv) * rs, c["k_down"], c["q_down"], c["k_up"], c["q_up"]) / rs
    ox, oy = _st.offset_px if offset is None else offset
    X = g.cx + u * g.hx + ox
    Y = g.cy + v * g.hy + oy
    r = g.rect
    x0, x1 = r.x, r.x + r.width - 1
    y0, y1 = r.y, r.y + r.height - 1
    s = c["snap_px"]
    if s > 0:   # edge magnet
        if X < x0 + s:
            X = x0
        elif X > x1 - s:
            X = x1
        if Y < y0 + s:
            Y = y0
        elif Y > y1 - s:
            Y = y1
    X = min(max(X, x0), x1)
    Y = min(max(Y, y0), y1)
    return X, Y


def _correct_norm(p):
    """Normalized (0..1) gaze point -> corrected normalized point."""
    r = _geo.rect
    X, Y = _transform(r.x + p.x * r.width, r.y + p.y * r.height)
    return Point2d((X - r.x) / r.width, (Y - r.y) / r.height)


def _corrected_frame(frame):
    """Copy of the GazeFrame with gaze points corrected; the original if nothing to do."""
    l, r = frame.left, frame.right
    one_eye = _cfg["mirror"] and (l.detected != r.detected)
    post = _post[0]
    if _identity[0] and _st.offset_px == (0.0, 0.0) and not one_eye and post is None:
        return frame   # (the credit only acts through the correction, which is identity here)
    f = copy.copy(frame)
    if one_eye:
        # the tracker drops one eye at the top corners; Talon would blend the
        # survivor with a stale prediction of the lost eye and park the cursor
        # short of the edge - give it the survivor for both eyes instead
        good = l if l.detected else r
        lost = r if l.detected else l
        m = copy.copy(lost)
        m.detected = True
        m.validity = good.validity
        m.gaze = good.gaze
        m.gaze3d = good.gaze3d
        if l.detected:
            f.right = m
        else:
            f.left = m
        f.gaze = good.gaze
        frame_gaze, eyes = good.gaze, (f.left, f.right)
    else:
        frame_gaze, eyes = frame.gaze, (l, r)
    if frame_gaze is not None:
        f.gaze = _smooth("gaze", _correct_norm(frame_gaze), frame.ts)
    for name, eye in zip(("left", "right"), eyes):
        if eye.detected and eye.gaze is not None:
            e = eye if eye is not getattr(frame, name) else copy.copy(eye)
            e.gaze = _smooth(name, _correct_norm(eye.gaze), frame.ts)
            setattr(f, name, e)
    if post is not None and f.gaze is not None:
        # target_magnet.py: may rewrite the frame (snap point, frozen head) before Talon sees it
        try:
            rr = _geo.rect
            post(f, rr.x + f.gaze.x * rr.width, rr.y + f.gaze.y * rr.height)
        except Exception:
            now = time.perf_counter()
            if now - _post_err[0] > 5.0:
                _post_err[0] = now
                import traceback
                print("[head_offset] post filter error:", traceback.format_exc()[-400:])
    return f


# post filter on the corrected frame (target_magnet.py registers itself here; it imports
# this module, so it re-registers after every reload of this file)
_post = [None]
_post_err = [0.0]


def set_post_filter(fn):
    _post[0] = fn


def post_filter():
    return _post[0]


def screen_rect():
    return _geo.rect


# extra steadiness low-pass on the corrected gaze (user.gaze_smoothing_ms);
# one EMA per point, each with its own timestamp
_sm = {"gaze": None, "left": None, "right": None}


def _smooth(key, p, ts):
    tau = _cfg["gsmooth_ms"] / 1000.0
    if tau <= 0:
        return p
    prev = _sm[key]
    if prev is not None:
        prev_ts, prev_p = prev
        gap = ts - prev_ts
        jump = _cfg["gjump_px"]
        if jump > 0:
            r = _geo.rect
            if math.hypot((p.x - prev_p.x) * r.width, (p.y - prev_p.y) * r.height) > jump:
                _sm[key] = (ts, p)      # saccade: pass it through at once, smooth from here
                return p
        if 0 < gap <= 0.3:
            a = gap / (tau + gap)
            p = Point2d(prev_p.x + a * (p.x - prev_p.x), prev_p.y + a * (p.y - prev_p.y))
    _sm[key] = (ts, p)
    return p


# --- hook into the control mouse's gaze subscription ------------------------------
_orig_on_gaze = _em2.BaseControlMouse.on_gaze.__get__(_em2.control2)


def _em2_on_gaze(frame):
    _orig_on_gaze(_corrected_frame(frame))


_em2_on_gaze._head_offset_wrapper = True


def _tracking_ctx():
    for cb, ctx in list(tracking_system.events.get("gaze", [])):
        if cb == _orig_on_gaze or getattr(cb, "_head_offset_wrapper", False):
            return ctx
    return None


def _install():
    ctx = _tracking_ctx()
    was_registered = False
    for cb, _c in list(tracking_system.events.get("gaze", [])):
        if cb == _orig_on_gaze or getattr(cb, "_head_offset_wrapper", False):
            try:
                tracking_system.unregister("gaze", cb)
                was_registered = True
            except Exception:
                pass
    # future start()/stop() of the control mouse (ctrl-alt-e) register self.on_gaze -> our wrapper
    _em2.control2.on_gaze = _em2_on_gaze
    if was_registered:
        if ctx is not None:
            with ctx.enter():          # owned by Talon's tracking context, not by this module
                tracking_system.register("gaze", _em2_on_gaze)
        else:
            tracking_system.register("gaze", _em2_on_gaze)
    return was_registered


def _uninstall():
    ctx = _tracking_ctx()
    was = False
    for cb, _c in list(tracking_system.events.get("gaze", [])):
        if getattr(cb, "_head_offset_wrapper", False):
            try:
                tracking_system.unregister("gaze", cb)
                was = True
            except Exception:
                pass
    try:
        del _em2.control2.on_gaze
    except AttributeError:
        pass
    if was:
        if ctx is not None:
            with ctx.enter():
                tracking_system.register("gaze", _orig_on_gaze)
        else:
            tracking_system.register("gaze", _orig_on_gaze)


# --- head pose -> offset -----------------------------------------------------------
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
        st.s_rise, st.s_yaw, st.s_dist = rise, yaw, dist     # (re)acquire: snap, don't slew
    else:
        a = dt / (tau + dt) if tau > 0 else 1.0
        st.s_rise += a * (rise - st.s_rise)
        st.s_yaw += a * (yaw - st.s_yaw)
        st.s_dist += a * (dist - st.s_dist)

    if st.a_rise is None or (c["lost_s"] > 0 and gap > c["lost_s"]):
        # first sight / came back: start a new neutral, but let it SETTLE over
        # the next ANCHOR_SETTLE_S rather than trusting the very first frame
        st.a_rise, st.a_yaw = st.s_rise, st.s_yaw
        st.settle_until = ts + ANCHOR_SETTLE_S
    elif ts < st.settle_until:
        b = min(dt / 0.4, 1.0)
        st.a_rise += b * (st.s_rise - st.a_rise)
        st.a_yaw += b * (st.s_yaw - st.a_yaw)
    else:
        # "resting head facing the centre": adapt the neutral pose only while
        # the RAW gaze is in the central zone - you tilt to reach edges, never
        # to look at the middle, so this tracks posture but never a tilt
        g = frame.gaze
        z = c["neutral_zone"]
        seen = g is not None and (frame.left.detected or frame.right.detected)
        central_x = seen and abs(g.x - 0.5) < z
        central = central_x and abs(g.y - 0.5) < z
        # vertical resting height (lift-only logic)
        if c["lift_only"] and st.s_rise < st.a_rise:
            tau_a = c["down_s"]          # head lowered: that IS the resting height, follow it down
        elif central:
            tau_a = c["neutral_s"]       # looking at the middle: head is at rest, learn it
        else:
            tau_a = c["up_s"]            # high and looking away from the middle: probably a tilt
        if tau_a > 0:
            st.a_rise += min(dt / tau_a, 1.0) * (st.s_rise - st.a_rise)
        # yaw resting direction: symmetric (turning either way is intentional),
        # learned while the gaze is horizontally central, creeps slowly otherwise
        tau_y = c["neutral_s"] if central_x else c["up_s"]
        if tau_y > 0:
            st.a_yaw += min(dt / tau_y, 1.0) * (st.s_yaw - st.a_yaw)

    if not st.enabled:
        return
    lift = st.s_rise - st.a_rise
    if c["lift_only"] and lift < 0:
        lift = 0.0
    lift = _deadzone(lift, c["dead_y"])                       # mm above resting
    turn = _deadzone(st.s_yaw - st.a_yaw, c["dead_x"])        # deg right of resting
    # inferred head rotation -> the screen eccentricity it accounts for (a pitch of p
    # at distance D covers D*tan(p) mm of screen). pitch up = toward the top = negative
    # v; yaw right = positive u.
    pivot = c["pivot_mm"] if c["pivot_mm"] > 0 else 100.0
    pitch = max(-1.0, min(1.0, lift / pivot))
    st.pitch_deg, st.yaw_deg = math.degrees(pitch), turn
    d = st.s_dist if st.s_dist and st.s_dist > 100 else 600.0
    cv = -c["credit_y"] * d * math.tan(pitch) / _geo.hy_mm
    cu = c["credit_x"] * d * math.tan(math.radians(max(-60.0, min(60.0, turn)))) / _geo.hx_mm
    st.credit = (max(-1.0, min(1.0, cu)), max(-1.0, min(1.0, cv)))
    # optional extra push (head as a mouse)
    dy_mm = -lift * c["gain_y"]   # head up -> cursor up (-y)
    dx_mm = turn * c["gain_x"]    # turn right -> cursor right
    m = c["max_mm"]
    if m > 0:
        dx_mm = max(-m, min(m, dx_mm))
        dy_mm = max(-m, min(m, dy_mm))
    st.offset_px = (dx_mm * _geo.ppm_x, dy_mm * _geo.ppm_y)


# --- neutral-pose persistence (survives reloads and Talon restarts) -------------
_ANCHOR_FILE = os.path.join(TALON_HOME, "head_offset_anchor.json")
_ANCHOR_MAX_AGE_S = 12 * 3600
_saved = {"rise": None, "yaw": None, "ts": 0.0}


def _save_anchor(force=False):
    st = _st
    if st.a_rise is None:
        return
    now = time.time()
    if not force and (now - _saved["ts"] < 5.0 or
                      (_saved["rise"] is not None and abs(st.a_rise - _saved["rise"]) < 0.5
                       and abs(st.a_yaw - _saved["yaw"]) < 0.2)):
        return
    try:
        with open(_ANCHOR_FILE, "w") as f:
            json.dump({"rise": st.a_rise, "yaw": st.a_yaw, "ts": now}, f)
        _saved.update(rise=st.a_rise, yaw=st.a_yaw, ts=now)
    except OSError:
        pass


def _load_anchor():
    try:
        with open(_ANCHOR_FILE) as f:
            d = json.load(f)
        if time.time() - float(d.get("ts", 0)) > _ANCHOR_MAX_AGE_S:
            return False
        _st.a_rise, _st.a_yaw = float(d["rise"]), float(d["yaw"])
        _saved.update(rise=_st.a_rise, yaw=_st.a_yaw, ts=float(d["ts"]))
        return True
    except Exception:
        return False


# --- public helpers (used by tracking_diag / zoom_pop / gaze_measure) ------------
def transform_px(x, y, offset=None, credit=None):
    """Apply gaze correction + head state (or a frozen `offset` px / `credit` eccentricity)
    to a screen-pixel point. Pass (0, 0), (0, 0) to evaluate as if the head were at rest."""
    return _transform(x, y, offset, credit)


def offset_px():
    return _st.offset_px


def head_state():
    """(extra push px, eccentricity credit) - freeze both with one call (zoom mouse)."""
    return _st.offset_px, _st.credit


def last_raw_target():
    """No longer available (nothing intercepts cursor moves any more)."""
    return None


def status_snapshot():
    st = _st
    d = {"on": st.enabled, "offset_px": (round(st.offset_px[0]), round(st.offset_px[1]))}
    if st.s_rise is not None and st.a_rise is not None:
        d["d_rise_mm"] = round(st.s_rise - st.a_rise, 1)
        d["d_yaw_deg"] = round(st.s_yaw - st.a_yaw, 1)
        d["pitch_deg"] = round(st.pitch_deg, 1)
        d["credit"] = (round(st.credit[0], 2), round(st.credit[1], 2))
    if st.s_dist:
        d["dist_mm"] = round(st.s_dist)
    return d


def live_distance_mm():
    """Smoothed eye distance (mm) or None."""
    return _st.s_dist


def _banner(text, kind="info"):
    """Top-of-screen banner via control_toggle_notify.py (falls back to app.notify)."""
    try:
        actions.user.banner(text, kind)
    except Exception:
        app.notify("Head offset", text)


def _set_enabled(state):
    _st.enabled = state
    if not state:
        _st.offset_px = (0.0, 0.0)
        _st.credit = (0.0, 0.0)


@mod.action_class
class Actions:
    def head_offset_toggle():
        """Toggle the head-pose cursor offset on/off"""
        _set_enabled(not _st.enabled)
        _banner(f"Head offset {'ON' if _st.enabled else 'OFF'}", "on" if _st.enabled else "off")
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
        st.settle_until = 0.0
        st.offset_px = (0.0, 0.0)
        st.credit = (0.0, 0.0)
        _save_anchor(force=True)
        _banner(f"Head offset re-centred (rise {st.s_rise:.0f} mm, yaw {st.s_yaw:+.1f} deg)", "info")
        print(f"[head_offset] re-centred at rise={st.s_rise:.1f}mm yaw={st.s_yaw:+.1f}deg dist={st.dist:.0f}mm")

    def head_offset_status():
        """Print the head-offset state and current settings to the log"""
        print(f"[head_offset] state={status_snapshot()} frames={_st.frames} cfg={_cfg}")
        app.notify("Head offset", str(status_snapshot()))

    def head_offset_uninstall():
        """Give the control mouse its original, uncorrected gaze stream back"""
        _uninstall()
        _st.offset_px = (0.0, 0.0)
        _st.credit = (0.0, 0.0)
        print("[head_offset] uninstalled; control mouse receives raw gaze frames again")


# --- install ---------------------------------------------------------------------
_update_screen()
ui.register("screen_change", _update_screen)
_refresh_settings()
_restored = _load_anchor()   # keep the neutral pose across reloads/restarts
cron.interval("250ms", _refresh_settings)
tracking_system.register("gaze", _on_gaze)
tracking_diag.set_offset_provider(status_snapshot)
_hooked = _install()
print(f"[head_offset] installed: gaze-frame hook on ControlMouse2 ({'live' if _hooked else 'armed for next ctrl-alt-e'}), "
      f"screen {_geo.rect} ({_geo.ppm_x:.2f} px/mm, half {_geo.hx_mm:.0f}x{_geo.hy_mm:.0f} mm), anchor {'restored rise=%.1f' % _st.a_rise if _restored else 'fresh'}, "
      f"correction {'identity' if _identity[0] else 'active'}, cfg={_cfg}")
