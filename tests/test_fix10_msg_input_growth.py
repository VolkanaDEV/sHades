#!/usr/bin/env python3
"""Composer single-line recipe: msg_input one line dp(40), bar dp(56).

fixes.md #10 (G1) grew the field with multiline text and the bar with
the field (dp(160) cap). That recipe came in with the phase-2 tarball
and breaks the phone: tay 2026-11-24 shots show the field's fill
painting as a partial band beside the send pill under the
minimum_height rebind. The pre-tarball recipe is restored instead:
multiline stays, the height rebinding is gone. This suite now pins
the restored recipe: five lines must NOT grow the field past dp(40),
the bar must NOT move, and clearing stays clean. Re-introducing the
growth on a device-verified path flips this suite deliberately with
it.

Boots the real app to the main screen, sets 1-line then 5-line text,
and asserts the field height, the bar height and containment after the
Clock has pumped the layout. The message list above must shrink as the
bar grows.

Run: xvfb-run -a python tests/test_fix10_msg_input_growth.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy_client import sHadesClientApp  # noqa: E402


class Fix10Probe(sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.passes = 0
        self.scroll_h_1line = None
        Clock.schedule_once(self.check_1line, 2.2)

    def check(self, condition, message):
        print(("PASS " if condition else "FAIL ") + message, flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def _field_and_bar(self):
        main = self.root.get_screen("main")
        return main.ids.msg_input, main.ids.composer_bar, main.ids.scroll

    def check_1line(self, dt):
        self.root.current = "main"
        field, bar, scroll = self._field_and_bar()
        field.text = "hello world"
        Clock.schedule_once(self.check_1line_settled, 0.8)

    def check_1line_settled(self, dt):
        field, bar, scroll = self._field_and_bar()
        self.check(abs(field.height - dp(40)) <= 1,
                   "1-line input stays dp(40) (got %.1f)" % field.height)
        self.check(abs(bar.height - dp(56)) <= 1,
                   "composer bar stays dp(56) for 1 line (got %.1f)"
                   % bar.height)
        self.check(field.y >= bar.y - 1 and field.top <= bar.top + 1,
                   "1-line field stays inside the composer bar")
        self.scroll_h_1line = scroll.height
        field.text = "\n".join("line %d" % i for i in range(5))
        Clock.schedule_once(self.check_5line, 0.8)

    def check_5line(self, dt):
        field, bar, scroll = self._field_and_bar()
        self.check(abs(field.height - dp(40)) <= 1,
                   "5-line input still one line dp(40) (got %.1f)"
                   % field.height)
        self.check(abs(bar.height - dp(56)) <= 1,
                   "composer bar stays dp(56) with 5 lines (got %.1f)"
                   % bar.height)
        self.check(field.y >= bar.y - 1 and field.top <= bar.top + 1,
                   "5-line field stays inside the composer bar")
        inside = all(
            c.y >= bar.y - 1 and c.top <= bar.top + 1
            for c in bar.children)
        self.check(inside, "every composer child stays inside the bar")
        self.check(abs(scroll.height - self.scroll_h_1line) <= 1,
                   "message list unchanged with 5 lines (%.1f -> %.1f)"
                   % (self.scroll_h_1line, scroll.height))
        field.text = ""
        Clock.schedule_once(self.finish, 0.5)

    def finish(self, dt):
        field, bar, scroll = self._field_and_bar()
        self.check(abs(field.height - dp(40)) <= 1,
                   "clearing the input shrinks the field back to dp(40)")
        self.check(abs(bar.height - dp(56)) <= 1,
                   "clearing the input shrinks the bar back to dp(56)")
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (1280, 720)
    probe = Fix10Probe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
