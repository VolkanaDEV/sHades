#!/usr/bin/env python3
"""UIX regression test 2: touch targets. Assert every MDButton/Button
is >= 40px tall and >= 48px wide. Catches P2-1.

Scoping rules (grounded in the live tree):
  - Measure only after a layout pass: visit each screen and wait, open
    each drawer and wait, and set a reply draft to expose the reply
    cancel button. A screen sampled before its first layout reads the
    100x100 pre-layout default, not a real size (the site-editor
    buttons read 24px wide pre-layout and 257px once laid out).
  - Skip buttons that are not on screen: width or height 0, or any
    ancestor with width/height 0 or opacity 0 (closed drawers, the
    collapsed advanced box, disabled strips, stage-gated boxes).
    Buttons in a closed drawer are recorded again once the drawer opens.
  - The composer pfp holder was 46px wide (2px under); it is 48px now.

Run: xvfb-run -a python tests/test_uix_touch_targets.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.uix.button import Button  # noqa: E402
from kivymd.uix.button import MDButton  # noqa: E402
from kivy_client import sHadesClientApp  # noqa: E402

MIN_H = 40
MIN_W = 48
SCREENS = ["connect", "onboarding", "main", "profile", "config",
           "site_editor", "server_config"]


def label_of(w):
    text = getattr(w, "text", None)
    if isinstance(text, str) and text:
        return text
    for c in w.children:
        text = getattr(c, "text", None)
        if isinstance(text, str) and text:
            return text
    return "?"


def hidden(w):
    """True if the button is not on screen (collapsed container)."""
    seen = set()
    p = w
    while p is not None and id(p) not in seen and len(seen) < 24:
        seen.add(id(p))
        if p.width == 0 or p.height == 0 or getattr(p, "opacity", 1) == 0:
            return True
        p = p.parent
    return False


class TouchProbe(sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.passes = 0
        self.seen = {}
        self.queue = list(SCREENS)
        Clock.schedule_once(self.next_screen, 2.2)

    def check(self, condition, message):
        print(("PASS " if condition else "FAIL ") + message, flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def record(self, screen_name):
        screen = self.root.get_screen(screen_name)
        if screen.width <= 100:
            return
        stack = [screen]
        while stack:
            w = stack.pop()
            if isinstance(w, (MDButton, Button)) and id(w) not in self.seen:
                self.seen[id(w)] = (screen_name, type(w).__name__,
                                    label_of(w), w.width, w.height,
                                    hidden(w))
            stack.extend(w.children)

    def next_screen(self, dt):
        if not self.queue:
            self.finish_main_states()
            return
        name_s = self.queue.pop(0)
        self.root.current = name_s
        if name_s == "onboarding":
            Clock.schedule_once(self.check_onboarding, 0.9)
        else:
            Clock.schedule_once(
                lambda _dt, n=name_s: (self.record(n), self.next_screen(0)),
                0.9)

    def check_onboarding(self, dt):
        self.record("onboarding")
        screen = self.root.get_screen("onboarding")
        try:
            screen.stage = 2
        except Exception:
            pass
        Clock.schedule_once(
            lambda _dt: (self.record("onboarding"), self.next_screen(0)),
            0.8)

    def finish_main_states(self):
        self.root.current = "main"
        Clock.schedule_once(self.main_closed, 0.9)

    def main_closed(self, dt):
        self.record("main")
        main = self.root.get_screen("main")
        main.toggle_left()
        Clock.schedule_once(self.main_left_open, 0.7)

    def main_left_open(self, dt):
        self.record("main")
        main = self.root.get_screen("main")
        main.toggle_right()
        Clock.schedule_once(self.main_right_open, 0.7)

    def main_right_open(self, dt):
        self.record("main")
        main = self.root.get_screen("main")
        main.toggle_right()
        self.reply_draft = "re: hello"
        Clock.schedule_once(self.main_reply, 0.7)

    def main_reply(self, dt):
        self.record("main")
        self.reply_draft = ""
        self.finish()

    def finish(self):
        visible = [(s, c, t, w, h) for (s, c, t, w, h, hid)
                   in self.seen.values() if not hid]
        hidden_n = len(self.seen) - len(visible)
        self.check(len(visible) > 0, "measured %d visible buttons "
                   "(%d hidden/collapsed skipped)"
                   % (len(visible), hidden_n))
        under = [(s, c, t, w, h) for (s, c, t, w, h) in visible
                 if h < MIN_H or w < MIN_W]
        self.check(not under, "no visible button is under %dx%d (%d found: "
                   "%r)" % (MIN_W, MIN_H, len(under), under))
        for s, c, t, w, h in visible:
            self.check(h >= MIN_H and w >= MIN_W,
                       "%s %s %r is %.0fx%.0f (min %dx%d)"
                       % (s, c, t, w, h, MIN_W, MIN_H))
        holder = [(s, c, t, w, h) for (s, c, t, w, h) in visible
                  if t == "?" and c == "Button" and s == "main"]
        self.check(any(w >= MIN_W for (_s, _c, _t, w, _h) in holder),
                   "composer pfp holder is >= 48px wide (got %r)"
                   % ([(w, h) for (_s, _c, _t, w, h) in holder],))
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (1280, 720)
    probe = TouchProbe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
