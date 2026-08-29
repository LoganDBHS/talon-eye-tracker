; Talon zoom-mouse bridge
; While Talon's zoom overlay is open (signalled by a flag file), intercept the
; physical mouse buttons and relay them to Talon as F13/F14/F15 keystrokes,
; which Talon's keyboard hook can see (its mouse hook is broken on this PC).
;   left click         -> F13 -> commit left click at gazed target
;   right click        -> F14 -> commit right click at gazed target
;   middle click       -> F15 -> cancel the zoom
;   shift+left click   -> F16 -> commit double left click at gazed target
; When the flag file is absent, this script does nothing at all.
#Requires AutoHotkey v2.0
#SingleInstance Force
Persistent

ZoomFlag := A_Temp "\talon_zoom_open.flag"
; exists for ~900ms after a commit: a second physical left click in that window
; is relayed as F17 so Talon can replay it at the committed target (the eye
; mouse has already dragged the cursor away) -> natural tap-tap double-click
DoubleFlag := A_Temp "\talon_zoom_double.flag"

#HotIf FileExist(ZoomFlag)
LButton::Send "{F13}"
RButton::Send "{F14}"
MButton::Send "{F15}"
+LButton::Send "{F16}"
#HotIf FileExist(DoubleFlag)
LButton::Send "{F17}"
#HotIf
