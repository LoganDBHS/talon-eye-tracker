"""Optional tuning for Talon eye tracking. Everything here is commented out;
uncomment a line and save to apply (Talon hot-reloads this file).

There are TWO control-mouse implementations:

1. MODERN control mouse  (ctrl-alt-e -> tracking.control_toggle) - RECOMMENDED.
   Fuses gaze + head tracking. Its curve is tuned inside Talon itself; the
   exposed knobs are menu/action toggles, not numbers:
     - tracking.control_gaze_toggle()       gaze moves the cursor (coarse)
     - tracking.control_head_toggle()       head movement refines it (fine)
     - tracking.control_mouse_jump_toggle() jump vs glide to new gaze point
     - tracking.control_debug_toggle()      overlay showing raw gaze
   Turning gaze OFF and leaving head ON gives a pure head mouse (steadier for
   reading-heavy work); both ON is the standard continuous-follow setup.

2. LEGACY control mouse  (tracking.control1_toggle). Only this one exposes
   numeric gain/smoothing, via the config objects below.
"""

# from talon.types import Point2d
# from talon_plugins import eye_mouse, eye_zoom_mouse

### Legacy control-mouse head gain (pixels of cursor travel per head movement).
### Default is Point2d(150, 225) = (x, y). Raise for a faster cursor on the 32"
### screen, lower for precision.
# eye_mouse.config.velocity = Point2d(150, 225)

### Zoom mouse (ctrl-alt-z) tuning. Defaults shown.
# eye_zoom_mouse.config.img_scale = 3            # magnification factor
# eye_zoom_mouse.config.screen_area = Point2d(400, 300)  # captured region size
# eye_zoom_mouse.config.eye_avg = 20             # gaze samples averaged (higher = steadier, laggier)
