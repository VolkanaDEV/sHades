#!/usr/bin/env python3
"""Sitenav (g): persistent nav chrome, live on the real app.

The nav bar lives outside the page content, under the channel header:
40px tall while a site page is on screen, collapsed to 0px on chat
channels. Back/Forward are touch-sized controls bound to the nav
gates, the breadcrumb label shows the current page path, and pressing
the bar's Back/Forward buttons navigates. The in-page < Back link is
covered by test_sitenav_a_broken_link / test_sitenav_c_forward.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy_client import sHadesClientApp  # noqa: E402

SHOTS = os.path.expanduser("~/workspace/sHades-fix/sitenav/shots")
CID_PAGES = {}


class FakeApi(object):
    def channels_info(self, cid):
        manifest = {"index": "/index.mu", "theme": {}}
        return {"ok": True, "channel": {"manifest": manifest}}

    def site_get(self, channel, path, offset=0, length=None):
        if path in CID_PAGES:
            return {"ok": True,
                    "data_b64": base64.b64encode(
                        CID_PAGES[path].encode()).decode("ascii"),
                    "can_edit": False}
        return {"ok": False, "error": "resource not found",
                "can_edit": False}


class NavChromeProbe(sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.cid = self._channel_cid("wiki")
        CID_PAGES["/index.mu"] = (
            "> Home\n[second`/file/sites/%s/second.mu]\n" % self.cid)
        CID_PAGES["/second.mu"] = "> Second\n"
        self.guild_api = FakeApi()
        Clock.schedule_once(self.step_open, 1.5)

    def check(self, condition, message):
        print((("PASS " if condition else "FAIL ") + message), flush=True)
        if not condition:
            self.failures.append(message)

    def main(self):
        return self.root.get_screen("main")

    def shot(self, name):
        try:
            Window.screenshot(name=os.path.join(SHOTS, name))
        except Exception as e:
            print("shot failed:", e, flush=True)

    def step_open(self, dt):
        self.root.current = "main"
        main = self.main()
        main._current_channel = "wiki"
        self._open_site_channel("wiki", self.cid)
        Clock.schedule_once(self.step_index, 1.5)

    def step_index(self, dt):
        main = self.main()
        bar = main.ids.site_nav_bar
        self.check(abs(bar.height - dp(40)) <= 1,
                   "nav bar is 40px tall on a site page (got %r)"
                   % bar.height)
        self.check(bar.opacity == 1, "nav bar is visible on a site page")
        self.check(main.ids.site_nav_path.text == "#wiki /index.mu",
                   "breadcrumb shows the index path (got %r)"
                   % main.ids.site_nav_path.text)
        back, fwd = main.ids.site_nav_back, main.ids.site_nav_forward
        self.check(back.height == dp(40) and back.width >= dp(48),
                   "Back is touch-sized (got %sx%s)"
                   % (round(back.width), round(back.height)))
        self.check(fwd.height == dp(40) and fwd.width >= dp(48),
                   "Forward is touch-sized (got %sx%s)"
                   % (round(fwd.width), round(fwd.height)))
        self.check(back.disabled is True, "Back disabled on the first page")
        self.check(fwd.disabled is True,
                   "Forward disabled on the first page")

        def _btn_text(btn):
            for c in btn.children:
                t = getattr(c, "text", None)
                if isinstance(t, str) and t:
                    return t
            return ""
        self.check(_btn_text(back) == "< Back",
                   "Back control reads '< Back' (got %r)" % _btn_text(back))
        self.check(_btn_text(fwd) == "Forward >",
                   "Forward control reads 'Forward >' (got %r)"
                   % _btn_text(fwd))
        self.shot("test-g-index.png")
        self._micron_link_tapped("/file/sites/%s/second.mu" % self.cid)
        Clock.schedule_once(self.step_second, 1.2)

    def step_second(self, dt):
        main = self.main()
        self.check(main.ids.site_nav_path.text == "#wiki /second.mu",
                   "breadcrumb follows navigation (got %r)"
                   % main.ids.site_nav_path.text)
        self.check(main.ids.site_nav_back.disabled is False,
                   "Back enabled after a forward navigation")
        self.check(main.ids.site_nav_forward.disabled is True,
                   "Forward still disabled")
        self.shot("test-g-second.png")
        main.ids.site_nav_back.dispatch("on_press")
        Clock.schedule_once(self.step_back, 1.2)

    def step_back(self, dt):
        main = self.main()
        self.check(main.ids.site_nav_path.text == "#wiki /index.mu",
                   "bar Back button returns to the index (got %r)"
                   % main.ids.site_nav_path.text)
        self.check(main.ids.site_nav_forward.disabled is False,
                   "Forward enabled after Back")
        main.ids.site_nav_forward.dispatch("on_press")
        Clock.schedule_once(self.step_forward, 1.2)

    def step_forward(self, dt):
        main = self.main()
        self.check(main.ids.site_nav_path.text == "#wiki /second.mu",
                   "bar Forward button goes forward again (got %r)"
                   % main.ids.site_nav_path.text)
        self.guild_api = None
        self._switch_channel("general")
        Clock.schedule_once(self.step_chat, 1.0)

    def step_chat(self, dt):
        main = self.main()
        bar = main.ids.site_nav_bar
        self.check(bar.height == 0,
                   "nav bar collapses on a chat channel (got %r)"
                   % bar.height)
        self.check(bar.opacity == 0, "nav bar hidden on a chat channel")
        self.shot("test-g-chat.png")
        print("PASS sitenav (g) nav chrome: %d failures"
              % len(self.failures))
        self.stop()


if __name__ == "__main__":
    Window.size = (1280, 720)
    probe = NavChromeProbe()
    probe.run()
    sys.exit(bool(probe.failures))
