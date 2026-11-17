#!/usr/bin/env python3
"""Exercise the connect form and drawer navigation with a real Kivy window."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy.uix.widget import Widget  # noqa: E402
from kivy_client import sHadesClientApp  # noqa: E402


class LayoutProbe(sHadesClientApp):
    def on_start(self):
        self.failures = []
        Clock.schedule_once(self.check_connect, 1.2)

    def check(self, condition, message):
        print(("PASS " if condition else "FAIL ") + message, flush=True)
        if not condition:
            self.failures.append(message)

    def check_connect(self, dt):
        screen = self.root.get_screen("connect")
        fields = [screen.ids[k] for k in
                  ("hub_address", "username", "advanced_toggle")]
        self.check(all(w.width >= dp(220) for w in fields),
                   "connection controls retain a readable width on mobile")
        self.check(all(0 <= w.to_window(*w.pos)[0]
                       and w.to_window(*w.pos)[0] + w.width <= Window.width
                       for w in fields), "connection controls fit the viewport")
        self.check(fields[0].y > fields[1].top - dp(1)
                   and fields[1].y > fields[2].top - dp(1),
                   "connection controls do not overlap")
        screen.toggle_advanced()
        self.check(screen.ids.advanced_box.height >= dp(98),
                   "advanced connection fields expand")

        self.root.current = "main"
        main = self.root.get_screen("main")
        main.toggle_left()
        main.toggle_right()
        self.check(main.right_open and not main.left_open,
                   "opening members closes channels")
        main.toggle_left()
        self.check(main.left_open and not main.right_open,
                   "opening channels closes members")
        Clock.schedule_once(self.check_main, 0.3)

    def check_main(self, dt):
        main = self.root.get_screen("main")
        self.check(main.ids.left_sidebar.width <= main.width * 0.8 + 1,
                   "channel drawer leaves a visible chat edge")
        self.check(main.ids.msg_input.height <= dp(40) + 1,
                   "composer stays inside its toolbar")

        # ---- task 9 composer geometry (additive, tay's file) ----
        def walk(w):
            yield w
            for c in w.children:
                yield from walk(c)
        bar = main.ids.msg_input.parent
        self.check(abs(bar.height - dp(56)) <= 1,
                   "composer bar grew to hold the pfp (got %r)" % bar.height)
        pfp = main.ids.composer_pfp
        self.check(pfp is not None and pfp.width == dp(42),
                   "composer pfp occupies the rail slot (got " +
                   repr(None if pfp is None else (pfp.width, pfp.height)) + ")")
        field = main.ids.msg_input
        helper_ok = getattr(field, "helper_text_mode", "") == "persistent"
        self.check(helper_ok, "attach-progress helper line stays visible")
        holder = main.ids.composer_pfp_holder
        self.check(pfp.x >= bar.x and pfp.right <= holder.right
                   and pfp.y >= bar.y and pfp.top <= bar.top,
                   "pfp stays inside the bar")
        flat = None
        for w in walk(bar):
            if isinstance(w, Widget) and w is not bar and w.width == dp(38):
                flat = w
                break
        self.check(flat is not None and flat.x < field.x,
                   "flat '+' extension sits left of the field")
        self.check(flat is not None and pfp.x < flat.x < field.x,
                   "composer order: pfp, flat '+', field (got %s)" %
                   repr((None if pfp is None else pfp.x,
                         None if flat is None else flat.x, field.x)))
        # v0.32 (tay device note): the plate must match the field
        # height and the '+' glyph must live INSIDE the plate (an
        # unbound Label defaults to a 100x100 box and its glyph
        # floats past the plate edge).
        self.check(flat is not None
                   and abs(flat.height - field.height) <= 1,
                   "attach plate matches the field height (got %r)"
                   % (None if flat is None else flat.height))
        glyph = None if flat is None else flat.children[0]
        self.check(glyph is not None
                   and glyph.x >= flat.x - 1
                   and glyph.right <= flat.right + 1
                   and glyph.y >= flat.y - 1
                   and glyph.top <= flat.top + 1,
                   "attach '+' glyph stays inside its plate")

        self.root.current = "config"
        Clock.schedule_once(self.check_config, 0.3)

    def check_config(self, dt):
        config = self.root.get_screen("config")
        original = dict(self.theme_colors)
        config._color_fields["bg_main"].text = "#123456"
        config._color_fields["border_subtle"].text = "invalid"
        config.save_config()
        self.check(dict(self.theme_colors) == original,
                   "invalid theme cannot partially change live colors")

        class InvalidAddress:
            def __init__(self):
                self.status = ""

            def _set_status(self, message):
                self.status = message

            def _add_server(self, *args):
                raise AssertionError("invalid address was persisted")

        for address in (":29221", "example.com:0", "example.com:65536",
                        "example.com\nextra:29221"):
            target = InvalidAddress()
            sHadesClientApp.connect_to_guild(target, address, "tester")
            self.check(bool(target.status),
                       "invalid hub address rejected: " + address)
        self.stop()


if __name__ == "__main__":
    Window.size = (360, 640)
    probe = LayoutProbe()
    probe.run()
    sys.exit(bool(probe.failures))
