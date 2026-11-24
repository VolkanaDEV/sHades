#!/usr/bin/env python3
"""Task 2 group E: avatar-less users get a letter tile, not a white square.

An Image with no texture draws its rect solid white, so every
avatar-less user showed a white square (message rows, member rows,
composer, profile, onboarding preview) and an empty contact-card QR
showed a white 180px box. Fix: PfpImage hides its rect while it has
no texture and draws a theme disc with the user's initial; the QR
block hides until a card is rendered.

Run: xvfb-run -a python tests/test_task2_avatar_fallback.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_png(path, rgb=(40, 120, 110)):
    import struct
    import zlib

    def chunk(kind, data):
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b""
    for _y in range(32):
        raw += b"\x00" + bytes(rgb) * 32
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


class Probe(kc.sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.passes = 0
        self.my_hash = "a" * 32
        self.username = "probeuser"
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
        grid = main.ids.message_grid
        now = time.time()
        self._render_message(grid, {
            "msg_id": "m1", "author_hash": "b" * 32, "username": "alice",
            "content": "hello without an avatar", "ts": now - 60})
        self._presence_ok = True
        self._active_users = {"b" * 32: {"username": "alice"}}
        self._refresh_members_ui_main("general")
        Clock.schedule_once(self.check_rows, 0.9)

    def check_rows(self, dt):
        main = self.root.get_screen("main")
        row = self._msg_widgets["m1"]
        pfp = row.ids.pfp
        self.check(pfp.texture is None, "message avatar has no texture")
        self.check(list(pfp.color) == [1, 1, 1, 0],
                   "message avatar rect hidden (no white square)",
                   list(pfp.color))
        self.check(pfp.fallback_letter == "a",
                   "message avatar fallback letter is the initial (%r)"
                   % pfp.fallback_letter)
        self.check(pfp._fb_letter_rect.texture is not None,
                   "fallback letter texture rendered")
        self.check(pfp._fb_color.a == 1, "fallback disc visible")
        mrow = [w for w in main.ids.member_grid.children
                if type(w).__name__ == "MemberRow"]
        self.check(len(mrow) == 1, "one member row rendered")
        if mrow:
            mpfp = mrow[0].ids.pfpx
            self.check(mpfp.texture is None, "member avatar has no texture")
            self.check(list(mpfp.color) == [1, 1, 1, 0],
                       "member avatar rect hidden")
            self.check(mpfp.fallback_letter == "a",
                       "member avatar fallback letter is the initial")
        comp = main.ids.composer_pfp
        self.check(list(comp.color) == [1, 1, 1, 0],
                   "composer avatar rect hidden")
        self.check(comp.fallback_letter == "p",
                   "composer fallback letter is the initial (%r)"
                   % comp.fallback_letter)
        # pixel check: no pure white inside the message avatar box
        Window.screenshot(name="/tmp/t2e-rows.png")
        Clock.schedule_once(self.check_pixels, 0.5)

    def check_pixels(self, dt):
        from PIL import Image
        import glob
        path = sorted(glob.glob("/tmp/t2e-rows*.png"))[-1]
        im = Image.open(path).convert("RGB")
        px = im.load()
        row = self._msg_widgets["m1"]
        pfp = row.ids.pfp
        x, y = pfp.to_window(*pfp.pos)
        w, h = pfp.size
        # window (x, y) is bottom-left; image rows run top-down
        white, disc = 0, 0
        for ix in range(int(x), int(x + w)):
            for iy in range(int(y), int(y + h)):
                got = px[ix, Window.height - 1 - iy]
                if got == (255, 255, 255):
                    white += 1
                elif got == (23, 34, 49):
                    disc += 1
        self.check(white == 0, "no white pixels inside the avatar box",
                   white)
        self.check(disc > 100, "fallback disc paints the avatar box",
                   disc)
        # profile: no avatar -> fallback; no card -> QR hidden
        prof = self.root.get_screen("profile")
        prof.target_username = "probeuser"
        prof.target_hash = self.my_hash
        prof.target_delivery = "d" * 32
        prof.target_avatar = ""
        self.root.current = "profile"
        Clock.schedule_once(self.check_profile, 0.8)

    def check_profile(self, dt):
        prof = self.root.get_screen("profile")
        pfp = prof.ids.profile_pfp
        self.check(list(pfp.color) == [1, 1, 1, 0],
                   "profile avatar rect hidden")
        self.check(pfp.fallback_letter == "p",
                   "profile fallback letter is the initial")
        qr = prof.ids.profile_card_qr
        self.check(qr.source == "", "no card rendered in this probe")
        self.check(qr.opacity == 0, "empty QR block hidden")
        # positive control: a real avatar hides the fallback
        png = "/tmp/t2e-avatar.png"
        make_png(png)
        img = kc.PfpImage(source=png, fallback_letter="z",
                          size_hint=(None, None), size=(32, 32))
        Clock.schedule_once(lambda d: self.check_real(img), 0.9)

    def check_real(self, img):
        self.check(img.texture is not None, "real avatar texture loads")
        self.check(list(img.color) == [1, 1, 1, 1],
                   "real avatar rect visible")
        self.check(img._fb_color.a == 0, "fallback disc hidden with avatar")
        self.check(list(img._fb_letter_rect.size) == [0, 0]
                   and img._fb_letter_color.a == 0,
                   "fallback letter hidden with avatar")
        # layout-sized avatar (height from its parent, not KV): the
        # onboarding PfpImage is 44 wide in a 52px row. Its letter
        # must centre on the widget, not on a transient size.
        from kivy.uix.boxlayout import BoxLayout
        row = BoxLayout(size_hint_y=None, height=52,
                        pos=(100, 200), size=(200, 52))
        tall = kc.PfpImage(source="", fallback_letter="w",
                           size_hint=(None, 1), width=44)
        row.add_widget(tall)
        self.root.get_screen("main").add_widget(row)
        self._tall_img = tall
        Clock.schedule_once(self.check_tall, 0.6)

    def check_tall(self, dt):
        img = self._tall_img
        rect = img._fb_letter_rect
        cx = rect.pos[0] + rect.size[0] / 2
        cy = rect.pos[1] + rect.size[1] / 2
        self.check(abs(cx - img.center_x) < 1.5
                   and abs(cy - img.center_y) < 1.5,
                   "layout-sized avatar letter is centred",
                   (rect.pos, rect.size, img.center))
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (1280, 720)
    probe = Probe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
