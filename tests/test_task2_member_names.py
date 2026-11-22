#!/usr/bin/env python3
"""Task 2 group C: member names stay on one line.

A long member name wrapped to 2-3 lines and painted outside its 36px
row (measured texture 48px tall in a 36px row at 640x480 and 1280x720).
Fix: shorten on the MemberRow username label, like the message author
label already has.

Run: xvfb-run -a python tests/test_task2_member_names.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402


class Probe(kc.sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.passes = 0
        Clock.schedule_once(self.render, 1.8)

    def check(self, condition, message, extra=""):
        print(("PASS " if condition else "FAIL ") + message
              + ((" -- " + str(extra)) if extra and not condition else ""),
              flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def render(self, dt):
        self.root.current = "main"
        main = self.root.get_screen("main")
        self._presence_ok = True
        self._active_users = {
            "b" * 32: {"username": "alice"},
            "c" * 32: {"username":
                       "a-very-long-member-name-that-keeps-going"},
            "d" * 32: {"username": "bob"},
        }
        self._refresh_members_ui_main("general")
        main.toggle_right()
        Clock.schedule_once(self.check_rows, 0.8)

    def check_rows(self, dt):
        main = self.root.get_screen("main")
        grid = main.ids.member_grid
        rows = [w for w in grid.children
                if type(w).__name__ == "MemberRow"]
        self.check(len(rows) == 3, "three member rows rendered (%d)"
                   % len(rows))
        for row in rows:
            label = None
            for c in row.children:
                if type(c).__name__ == "ClickableLabel":
                    label = c
            self.check(label is not None, "row has a username label")
            if label is None:
                continue
            tw, th = label.texture_size
            self.check(th <= row.height + 1,
                       "username texture fits its row (tex %.0f <= h %.0f)"
                       % (th, row.height),
                       (label.text, tw, th))
            self.check(tw <= label.width + 1,
                       "username texture fits its width (tex %.0f <= w %.0f)"
                       % (tw, label.width),
                       (label.text, tw, th))
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (640, 480)
    probe = Probe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
