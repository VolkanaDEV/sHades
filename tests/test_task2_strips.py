#!/usr/bin/env python3
"""Task 2 group B: drawer strips must not paint when hidden.

Two defects, both in the channel drawer:
  - The motd/update strips carried their text in an unbound child Label
    (a Button is not a layout, so the Label kept pos (0, 0) and painted
    "Update available: " at the window's bottom-left corner whenever the
    drawer opened, even with no update offered).
  - The update strip's constant maroon background_color painted through
    the Button's BorderImage borders, which spill outside a 0-height box:
    two phantom maroon bars across the channel column with no update.

Fix: the text lives on the Button itself (conditional, '' when hidden)
and the maroon background is conditional on an update being offered.

Run: xvfb-run -a python tests/test_task2_strips.py
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
        Clock.schedule_once(self.open_drawer, 1.8)

    def check(self, condition, message, extra=""):
        print(("PASS " if condition else "FAIL ") + message
              + ((" -- " + str(extra)) if extra and not condition else ""),
              flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def all_texts(self):
        out = []

        def walk(w):
            yield w
            for c in w.children:
                yield from walk(c)

        for w in walk(self.root):
            t = getattr(w, "text", None)
            if isinstance(t, str) and t:
                out.append(t)
        return out

    def open_drawer(self, dt):
        self.root.current = "main"
        main = self.root.get_screen("main")
        main.toggle_left()
        Clock.schedule_once(self.check_hidden, 0.8)

    def check_hidden(self, dt):
        main = self.root.get_screen("main")
        self.check(self.client_update_version == "",
                   "no update offered at boot")
        self.check(self.server_motd == "", "no motd at boot")
        motd = main.ids.motd_strip
        upd = main.ids.update_strip
        self.check(motd.height == 0, "motd strip collapses when empty")
        self.check(upd.height == 0, "update strip collapses when empty")
        self.check(motd.text == "", "motd strip text empty when hidden")
        self.check(upd.text == "", "update strip text empty when hidden")
        self.check(list(upd.background_color) == [0, 0, 0, 0],
                   "update strip background transparent when hidden")
        texts = self.all_texts()
        self.check(not any("Update available" in t for t in texts),
                   "no 'Update available' text anywhere while hidden",
                   [t for t in texts if "Update" in t][:3])
        # pixel check: the strip band (window y 314-346, x 64-284) must
        # be plain sidebar background, not phantom maroon bars.
        Window.screenshot(name="/tmp/t2b-hidden.png")
        Clock.schedule_once(self.check_pixels, 0.5)

    def check_pixels(self, dt):
        from PIL import Image
        import glob
        path = sorted(glob.glob("/tmp/t2b-hidden*.png"))[-1]
        im = Image.open(path).convert("RGB")
        px = im.load()
        # window (x, y) -> image (x, 480 - y); band is window y 314-346
        bad = []
        for wx in (80, 150, 220, 270):
            for wy in (320, 330, 340):
                got = px[wx, 480 - wy]
                if any(abs(got[i] - (14, 21, 31)[i]) > 4 for i in range(3)):
                    bad.append(((wx, wy), got))
        self.check(not bad, "strip band is plain sidebar background "
                   "(no phantom maroon bars)", bad[:4])
        # now offer a motd and an update: strips must show, text inside
        self.server_motd = "hello motd, this is a long message of the day"
        self.client_update_version = "9.9.9"
        Clock.schedule_once(self.check_shown, 0.8)

    def check_shown(self, dt):
        main = self.root.get_screen("main")
        motd = main.ids.motd_strip
        upd = main.ids.update_strip
        self.check(motd.height > 0, "motd strip opens when motd set")
        self.check(upd.height > 0, "update strip opens when update set")
        self.check(motd.text.startswith("hello motd"),
                   "motd text renders on its own strip (%r)" % motd.text)
        self.check(upd.text == "Update available: 9.9.9",
                   "update text renders on its own strip (%r)" % upd.text)
        self.check(list(upd.background_color)[:3] == [0.55, 0.13, 0.13],
                   "update strip maroon while offered")
        # the texts sit inside their strip buttons, not at the window origin
        for w, name in ((motd, "motd"), (upd, "update")):
            x, y = w.to_window(*w.pos)
            self.check(x >= 64 and y >= 200,
                       "%s strip sits inside the channels column (%s)"
                       % (name, (x, y)))
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (640, 480)
    probe = Probe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
