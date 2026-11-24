"""Live-tree touch probe: build the REAL sHadesClientApp, render a site
page with can_edit=True into the real main screen, deliver a real touch
on the [ Edit page ] ref, and watch whether the editor opens."""
import os
import sys

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_NO_CONSOLE", "1")
sys.path.insert(0, os.path.abspath("."))
import traceback

import kivy_client as kc
from kivy.clock import Clock
from kivy.tests.common import UnitTestTouch
from kivy.core.window import Window
from kivy.uix.label import Label

# class-level walk logger, installed BEFORE any widgets exist so every
# captured default handler goes through the log
from kivy.uix.widget import Widget as _W
_orig_wtd = _W.on_touch_down

def _log_wtd(self, touch, _o=[_orig_wtd]):
    r = _o[0](self, touch)
    try:
        print("WALK %-18s pos=%r box=(%s,%s,%s,%s) r=%s"
              % (type(self).__name__, tuple(touch.pos),
                 round(self.x, 1), round(self.y, 1),
                 round(self.width, 1), round(self.height, 1), r),
              flush=True)
    except Exception:
        pass
    return r
_W.on_touch_down = _log_wtd


SCRATCH = "C:/Users/ashwo/AppData/Local/hermes/profiles/vi-gw/cache/scratch"
CID = "c" * 64

class MicronRefLabel(Label):
    """Probe-side fix candidate: ref label frame-correct for 2+ levels
    of nesting inside a ScrollView."""

    def on_touch_down(self, touch):
        ox, oy = self.to_window(0, 0, initial=False)
        print("REFDOWN delivered=%r tw=%r towin00=%r lblpos=%r page=%r"
              % (touch.pos, (ox, oy), (ox, oy),
                 tuple(self.pos), tuple(self.parent.pos)
                 if self.parent else None), flush=True)
        candidates = {
            "a_page": (touch.x - self.x - self.parent.x,
                       touch.y - self.y - self.parent.y),
            "b_self": (touch.x - self.x, touch.y - self.y),
        }
        tex_h = self.texture_size[1] if self.texture_size[1] else 18.0
        for tag, (lx, ly) in candidates.items():
            tyv = tex_h - ly
            for uid, zones in (self.refs or {}).items():
                for zone in zones:
                    x, y, w, h = zone
                    if x <= lx <= w and y <= tyv <= h:
                        print("REFDOWN %s FIRES on %r" % (tag, uid),
                              flush=True)
                        if tag == "a_page":
                            self.dispatch('on_ref_press', uid)
                            return True
        return False


ok = {}

class ProbeApp(kc.sHadesClientApp):
    def _micron_label(self, markup, heading=False, center=False,
                      code=False):
        from kivy.metrics import sp, dp
        lbl = MicronRefLabel(markup=True, text=markup)
        lbl.size_hint_y = None
        lbl.font_size = sp(18) if heading else (sp(13) if code else sp(15))
        lbl.halign = "center" if center else "left"
        lbl.color = ((0.62, 0.65, 0.7, 1) if code else
                     (0.88, 0.9, 0.93, 1))
        lbl.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
        lbl.bind(texture_size=lambda inst, ts: setattr(
            inst, "height", max(dp(18), ts[1] if ts else 0)))
        lbl.bind(on_ref_press=lambda inst, ref: self._micron_link_tapped(ref))
        return lbl

    def __init__(self, **kw):
        super().__init__(**kw)
        self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
        self._servers = []
        self._servers_path = os.path.join(SCRATCH, "probe_servers.json")
        self._active_server_idx = -1
        self._app_data_dir = os.path.join(SCRATCH, "probe_appdata")
        os.makedirs(self._app_data_dir, exist_ok=True)

    def _save_servers(self):
        return True

    def _set_status(self, text):
        print("STATUS:", text, flush=True)

    def _load_servers(self):
        return []

    def on_start(self):
        Clock.schedule_once(self._probe_show_main, 0.3)

    def _probe_show_main(self, dt):
        self.root.current = "main"
        Clock.schedule_once(self._probe_step1, 0.5)

    def _probe_step1(self, dt):
        try:
            root = self.root
            main = root.get_screen("main")
            self._channels = {"site-ch": "placeholder"}
            main._current_channel = "site-ch"
            cid = self._channel_cid("site-ch")
            if not hasattr(self, "_site_stack"):
                self._site_stack = []
            self._site_current = (cid, "/index.mu")
            ok["has_msg_widgets"] = hasattr(self, "_msg_widgets")
            self._render_site_page(cid, "#!c=0\n> Probe page\n\nhello line\n",
                                   can_edit=True)
            Clock.schedule_once(self._probe_step2, 0.4)
        except Exception:
            traceback.print_exc()
            self.stop_probe(exit_code=2)

    def _find_ref_label(self):
        main = self.root.get_screen("main")
        grid = main.ids.message_grid
        page = None
        lbl = None
        for child in grid.children:
            for sub in child.children:
                if (getattr(sub, "refs", None)
                        and "shades:site-edit" in sub.refs):
                    lbl = sub
                    page = child
                    break
            if lbl:
                break
            if (getattr(child, "refs", None)
                    and "shades:site-edit" in child.refs):
                page, lbl = None, child
                break
        return page, lbl

    def _probe_step2(self, dt):
        try:
            main = self.root.get_screen("main")
            grid = main.ids.message_grid
            page = None
            lbl = None
            for child in grid.children:
                for sub in child.children:
                    if getattr(sub, "refs", None):
                        lbl = sub
                        page = child
                        break
                if lbl:
                    break
            if lbl is None:
                print("NO-REF-LABEL: grid children shapes:",
                      [(type(c).__name__,
                        [type(s).__name__ for s in c.children])
                       for c in grid.children], flush=True)
                self.stop_probe(exit_code=3)
                return
            zone = lbl.refs.get("shades:site-edit")
            ok["zone"] = zone
            if not zone:
                print("LABEL WITHOUT SITE-EDIT REF:", lbl.text, flush=True)
                self.stop_probe(exit_code=4)
                return
            z = zone[0]
            cx = (z[0] + z[2]) / 2.0
            cy = (z[1] + z[3]) / 2.0
            wx, wy = lbl.to_window(cx, cy, initial=False)
            self._lbl = lbl
            self._scroll = None
            w2 = lbl
            while w2 is not None:
                if type(w2).__name__ == "ScrollView":
                    self._scroll = w2
                    break
                w2 = w2.parent
            self._P = (wx, wy)
            self.ref_hits = []
            self._ref_len_done = 0
            self._phases = []
            lbl.bind(on_ref_press=lambda inst, ref, hits=self.ref_hits:
                     hits.append(ref))
            print("PROBE label=%r refs=%r" % (lbl.text, z), flush=True)
            print("TAP local=(%.1f,%.1f) win=(%.1f,%.1f)"
                  % (cx, cy, wx, wy), flush=True)
            if self._scroll is not None:
                sc = self._scroll
                print("SCROLL pos=%r size=%r disabled=%r do_xy=%r/%r "
                      "type=%r sx=%r sy=%r timeout=%r"
                      % (tuple(sc.pos), tuple(sc.size), sc.disabled,
                         sc.do_scroll_x, sc.do_scroll_y, sc.scroll_type,
                         sc.scroll_x, sc.scroll_y, sc.scroll_timeout),
                      flush=True)
            print("GEOM grid=%r page=%r lbl=%r center_win=%r"
                  % (tuple(grid.pos), tuple(page.pos), tuple(lbl.pos),
                     lbl.to_window(*lbl.center, initial=False)), flush=True)
            print("SIZE grid=%r page=%r lbl=%r"
                  % (tuple(grid.size), tuple(page.size), tuple(lbl.size)),
                  flush=True)
            Clock.schedule_once(self._tap_loop, 0.2)
        except Exception:
            traceback.print_exc()
            self.stop_probe(exit_code=5)

    def _tap_loop(self, dt):
        main = self.root.get_screen("main")
        banner = main.ids.get("banner_channel")
        main_scr = self.root.get_screen("main")
        rail = main_scr.ids.get("server_sidebar")
        if rail is not None:
            print("RAIL pre box=%r" % ((rail.x, rail.y, rail.width,
                                        rail.height),), flush=True)
            rail.width = 0
        main_scr.ids["left_pane"].disabled = False
        print("PANE disabled=%r" % (main_scr.ids["left_pane"].disabled,),
              flush=True)
        if banner is not None:
            print("BANNER pre=%r pos_hint=%r parent=%r w=%r"
                  % (banner.size, dict(banner.pos_hint),
                     type(banner.parent).__name__, banner.parent.width),
                  flush=True)
            banner.pos_hint = {}
            banner.size = (banner.parent.width, banner.height)
            print("BANNER fixed size -> %r (strip w=%r)"
                  % (tuple(banner.size), banner.parent.width), flush=True)
        sc = self._scroll
        self._taps = [("mid", 0, 0.12, (62.5, 527.0))]
        self._tap_idx = 0
        Clock.schedule_once(lambda dtx: sys.exit(40), 40.0)  # watchdog
        self._next_tap(0)

    def _next_tap(self, dt):
        if self._tap_idx >= len(self._taps):
            self._summary()
            return
        tag, timeout, updelay, P = self._taps[self._tap_idx]
        if self._scroll is not None:
            self._scroll.scroll_timeout = timeout
        self._mark = len(self.ref_hits)
        self._t = UnitTestTouch(*P)
        self._t.touch_down(0.5, 0.5)
        Clock.schedule_once(self._tap_up(updelay), updelay)

    def _tap_up(self, delay):
        def go(dt):
            self._t.touch_up(0.5, 0.5)
            Clock.schedule_once(lambda dt2: self._tap_verdict(), 0.4)
        return go

    def _tap_verdict(self):
        idx = self._tap_idx
        tag = self._taps[idx][0]
        cur = self.root.current
        ec = getattr(self, "_edit_cid", None)
        fired = len(self.ref_hits) - self._mark
        print("VERDICT %s root=%r _edit_cid=%r ref_fired=%r %s"
              % (tag, cur, ec, fired,
                 "OK" if (fired or ec or cur == "site_editor")
                 else "DEAD"), flush=True)
        self._tap_idx += 1
        Clock.schedule_once(self._next_tap, 0.3)

    def _summary(self):
        cur = self.root.current
        ec = getattr(self, "_edit_cid", None)
        print("SUMMARY ref_hits=%r final_root=%r _edit_cid=%r"
              % (self.ref_hits, cur, ec), flush=True)
        self.stop_probe(0 if self.ref_hits else 1)

    def stop_probe(self, exit_code=0):
        Clock.schedule_once(lambda dt: sys.exit(exit_code), 0.1)


def main():
    app = ProbeApp()
    app.run()
    print("unreachable")


if __name__ == "__main__":
    main()
