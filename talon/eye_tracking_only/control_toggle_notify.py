"""ctrl-alt-e with feedback: toggle Talon's control mouse and say which way it went."""
from talon import Module, actions, app

mod = Module()


@mod.action_class
class Actions:
    def control_mouse_toggle_notify():
        """Toggle the control mouse (tracking.control_toggle) and notify ON / OFF"""
        actions.tracking.control_toggle()
        try:
            on = actions.tracking.control_enabled()
        except Exception:
            on = None
        state = "ON" if on else "OFF" if on is not None else "toggled"
        app.notify("Control mouse", state)
        print(f"[control_mouse] {state}")
