#!/usr/bin/env python3
"""Task 2 group F: the site editor screen is themed like the rest of
the app -- screen background, bar buttons, source field and status
line all follow theme_colors live, instead of the default grey
buttons, white TextInput and transparent (black) background.

Run: xvfb-run -a python tests/test_task2_site_editor.py
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
        self.my_hash = "a" * 32
        self.username = "probeuser"
        Clock.schedule_once(self.go, 1.8)

    def check(self, condition, message, extra=""):
        print(("PASS " if condition else "FAIL ") + message
              + ((" -- " + str(extra)) if extra and not condition else ""),
              flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def go(self, dt):
        self.root.current = "site_editor"
        scr = self.root.get_screen("site_editor")
        self.check(list(scr._bg_color.rgba)
                   == list(self.theme_colors["bg_main"]),
                   "editor screen background is bg_main",
                   scr._bg_color.rgba)
        btns = {text: btn for btn, _primary in scr._bar_btns
                for text in [btn.text]}
        self.check(list(btns["< Back"].background_color)
                   == list(self.theme_colors["sidebar_ctrl"]),
                   "Back button uses sidebar_ctrl")
        self.check(list(btns["< Back"].color)
                   == list(self.theme_colors["text"]),
                   "Back button uses theme text")
        self.check(list(btns["Preview"].background_color)
                   == list(self.theme_colors["sidebar_ctrl"]),
                   "Preview button uses sidebar_ctrl")
        self.check(list(btns["Publish"].background_color)
                   == list(self.theme_colors["primary"]),
                   "Publish button uses primary")
        self.check(list(btns["Publish"].color)
                   == list(self.theme_colors["btn_text"]),
                   "Publish button uses btn_text")
        self.check(list(scr._src.background_color)
                   == list(self.theme_colors["input_bg"]),
                   "source field uses input_bg")
        self.check(list(scr._src.foreground_color)
                   == list(self.theme_colors["text"]),
                   "source field uses theme text")
        self.check(list(scr._src.cursor_color)
                   == list(self.theme_colors["text"]),
                   "source field uses theme cursor")
        self.check(list(scr._status.color)
                   == list(self.theme_colors["text_muted"]),
                   "status line uses text_muted")
        # live theme binding: change one key, the editor re-tints
        old = dict(self.theme_colors)
        new = dict(old)
        new["primary"] = (1, 0, 0, 1)
        new["bg_main"] = (0.02, 0.02, 0.02, 1)
        self.theme_colors = new
        Clock.schedule_once(lambda d: self.check_live(old), 0.4)

    def check_live(self, old):
        scr = self.root.get_screen("site_editor")
        btns = {btn.text: btn for btn, _ in scr._bar_btns}
        self.check(list(btns["Publish"].background_color) == [1, 0, 0, 1],
                   "Publish re-tints on theme change",
                   btns["Publish"].background_color)
        self.check(list(scr._bg_color.rgba) == [0.02, 0.02, 0.02, 1],
                   "screen background re-tints on theme change")
        self.theme_colors = old
        Clock.schedule_once(self.check_pixels, 0.4)

    def check_pixels(self, dt):
        Window.screenshot(name="/tmp/t2f-editor.png")
        Clock.schedule_once(self.read_pixels, 0.5)

    def read_pixels(self, dt):
        from PIL import Image
        import glob
        path = sorted(glob.glob("/tmp/t2f-editor*.png"))[-1]
        im = Image.open(path).convert("RGB")
        px = im.load()
        # bg_main #0B1017 -> (11, 16, 23); the window behind the
        # screen used to show through as black (0, 0, 0). At 1280x720
        # the preview ScrollView occupies roughly y 8..423, so (640,
        # 200) and (100, 100) are empty preview area.
        bg = (11, 16, 23)
        for point in ((640, 200), (100, 100)):
            got = px[point[0], Window.height - 1 - point[1]]
            self.check(got == bg,
                       "preview area paints bg_main, not black, at %s"
                       % (point,), got)
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (1280, 720)
    probe = Probe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
