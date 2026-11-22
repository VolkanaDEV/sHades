#!/usr/bin/env python3
"""UIX P2-4: every MDTextField on the connect and main screens carries
a visible boundary in the border tokens.

  - theme_line_color is 'Custom' (KivyMD 2.0 ignores the line colors
    while it stays 'Primary')
  - line_color_normal resolves to theme border_subtle, line_color_focus
    to theme border
  - the colour actually drawn matches too: the outlined-mode rectangle
    on the connect fields, the filled-mode bottom indicator on msg_input
  - focusing a field flips the drawn colour to the border token

Run: xvfb-run -a python tests/test_uix_field_borders.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.graphics import Color  # noqa: E402
from kivymd.uix.textfield import MDTextField  # noqa: E402
from kivy_client import sHadesClientApp  # noqa: E402


def walk(widget):
    yield widget
    for child in widget.children:
        yield from walk(child)


def drawn_color(field):
    """The colour KivyMD actually paints for this field's boundary."""
    group = ("rectangle-color" if field.mode == "outlined"
             else "active-indicator-color")
    for inst in field.canvas.before.children:
        if isinstance(inst, Color) and getattr(inst, "group", None) == group:
            return tuple(inst.rgba)
    return None


def close(a, b):
    return (a is not None and b is not None
            and len(a) == len(b)
            and all(abs(x - y) < 0.005 for x, y in zip(a, b)))


class BorderProbe(sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.passes = 0
        self.root.current = "connect"
        Clock.schedule_once(self.check_unfocused, 2.0)

    def check(self, condition, message):
        print(("PASS " if condition else "FAIL ") + message, flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def fields_on(self, name):
        screen = self.root.get_screen(name)
        return [w for w in walk(screen) if isinstance(w, MDTextField)]

    def check_unfocused(self, dt):
        border = tuple(self.theme_colors["border"])
        subtle = tuple(self.theme_colors["border_subtle"])
        self.fields = {}
        for screen in ("connect", "main"):
            found = self.fields_on(screen)
            self.fields[screen] = found
            self.check(len(found) >= 1,
                       "%s screen has MDTextField(s) (got %d)"
                       % (screen, len(found)))
            for f in found:
                tag = "%s/%s" % (screen, f.hint_text if hasattr(f, "hint_text") else "?")
                self.check(f.theme_line_color == "Custom",
                           "%s uses Custom line colours" % tag)
                self.check(close(tuple(f.line_color_normal or ()), subtle),
                           "%s line_color_normal is border_subtle" % tag)
                self.check(close(tuple(f.line_color_focus or ()), border),
                           "%s line_color_focus is border" % tag)
                self.check(close(drawn_color(f), subtle),
                           "%s draws its boundary in border_subtle "
                           "(got %r)" % (tag, drawn_color(f)))
        self.check(len(self.fields["connect"]) == 4,
                   "connect screen has exactly the 4 expected fields "
                   "(got %d)" % len(self.fields["connect"]))
        hub = self.root.get_screen("connect").ids.hub_address
        hub.focus = True
        Clock.schedule_once(self.check_focused, 0.8)

    def check_focused(self, dt):
        border = tuple(self.theme_colors["border"])
        hub = self.root.get_screen("connect").ids.hub_address
        self.check(close(drawn_color(hub), border),
                   "focused hub_address draws its boundary in border "
                   "(got %r)" % (drawn_color(hub),))
        main = self.root.get_screen("main")
        main.ids.msg_input.focus = True
        Clock.schedule_once(self.check_main_focused, 0.8)

    def check_main_focused(self, dt):
        border = tuple(self.theme_colors["border"])
        field = self.root.get_screen("main").ids.msg_input
        self.check(close(drawn_color(field), border),
                   "focused msg_input draws its indicator in border "
                   "(got %r)" % (drawn_color(field),))
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (1280, 720)
    probe = BorderProbe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
