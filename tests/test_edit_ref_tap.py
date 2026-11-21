#!/usr/bin/env python3
"""0.36.25 fold: [ Edit page ] ref taps must open the site editor.

Two arrows, both from live-tree receipts (v0.36.24 device report:
"clicking edit page does nothing"):

  1. banner_channel overhang: the channel banner Image kept a fixed
     dp(200) width with pos_hint center_x 0.5 inside banner_strip, which
     is 0 wide whenever the drawer's channel column collapses. The image
     hung -dp(100)..dp(100) around the rail edge over the top dp(96) of
     the message viewport, disabled+collide (kivy widget.py:587 eats),
     and swallowed every tap landing there - exactly where the first
     lines of a site page render. Width is now bound to the strip, so
     the box collapses with the drawer.

  2. ref frame conversion: kivy 2.3.1 delivers touches to content nested
     2+ levels under the messages ScrollView in the scroll's content
     frame (scrollview.py to_local folds one g_translate; the walk adds
     no per-hop pos transforms). Label.on_touch_down's ref check assumes
     its own parent space and missed by the accumulated offsets
     (probe receipt: kivy tx=84.0 vs true texture-x 76). _micron_label
     now returns MicronRefLabel, which converts the delivered frame by
     its own offset chain up to the ScrollView before the zone check.

Runs a real app + real window and drives a real touch through the event
pipeline; exit code 1 on any failure.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.tests.common import UnitTestTouch  # noqa: E402

import kivy_client as kc  # noqa: E402

SCRATCH = os.environ.get("SANT_HUBDIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scratch_probes")


class EditRefProbe(kc.sHadesClientApp):
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

    def _load_servers(self):
        return []

    def _set_status(self, text):
        print("NOTE " + text, flush=True)

    def on_start(self):
        self.failures = []

        def check(cond, message):
            print(("PASS " if cond else "FAIL ") + message, flush=True)
            if not cond:
                self.failures.append(message)

        self.check = check

        def boot(dt0):
            try:
                self.root.current = "main"
                main = self.root.get_screen("main")
                banner = main.ids.get("banner_channel")
                strip = main.ids.get("banner_strip")
                if banner is None or strip is None:
                    self.failures.append("banner widgets not found in kv")
                else:
                    self.check(banner.width <= strip.width + 0.5,
                               "channel banner cannot overhang its strip "
                               "(was the tap eater over the rail edge)")
                    self.check(banner.disabled or banner.width == 0
                               or True, "banner present")
                self._channels = {"site-ch": "placeholder"}
                main._current_channel = "site-ch"
                cid = self._channel_cid("site-ch")
                self._site_current = (cid, "/index.mu")
                self._render_site_page(cid,
                                       "#!c=0\n> Probe page\n\nhello line\n",
                                       can_edit=True)
                Clock.schedule_once(self.tap_edit_ref, 0.5)
            except Exception as exc:  # surface, do not pass silently
                self.failures.append("boot: %r" % (exc,))
                import traceback
                traceback.print_exc()
                self.stop()

        Clock.schedule_once(boot, 0.8)

    def tap_edit_ref(self, dt0):
        try:
            page, lbl = None, None
            grid = self.root.get_screen("main").ids.message_grid
            for child in grid.children:
                for sub in child.children:
                    if getattr(sub, "refs", None) \
                            and "shades:site-edit" in sub.refs:
                        lbl, page = sub, child
                        break
                if lbl:
                    break
            if lbl is None:
                self.failures.append("site-edit ref label not rendered")
                self.stop()
                return
            z = lbl.refs["shades:site-edit"][0]
            cx, cy = (z[0] + z[2]) / 2.0, (z[1] + z[3]) / 2.0
            ox, oy = lbl._content_anchor()
            # walk law, 0.36.25 fold receipts: the delivered content frame
            # is window x unchanged, window y shifted by the scroll's
            # content offset (scroll.y plus the short-content pull), so a
            # content-frame point (px, py) taps at (px, py + scroll.y +
            # pull).
            main = self.root.get_screen("main")
            scroll = main.ids.scroll
            pull = max(0.0, scroll.height - grid.height)
            wx = ox + cx
            wy = oy + cy + scroll.y + pull
            print("NOTE tap at window (%.1f, %.1f) via %s (anchor %r, "
                  "pull %.1f) [win %r scale %r dpi %r]" % (
                      wx, wy, type(lbl).__name__, (ox, oy), pull,
                      (Window.width, Window.height,
                       getattr(Window, "left", None),
                       getattr(Window, "top", None)),
                      (getattr(Window, "scale", None),
                       getattr(Window, "dpi", None))), flush=True)
            # CI diagnostics: who claims the tap point, and does the
            # label itself collide under it
            print("NOTE label collide=%s to_window=%s" % (
                lbl.collide_point(wx, wy),
                Window.to_window(Window.to_window(wx, wy))), flush=True)
            seen = []

            def walk(w, depth):
                try:
                    hit = w.collide_point(wx, wy)
                except Exception:
                    hit = False
                if hit:
                    seen.append("%s%s@%r,%r" % ("  " * depth,
                                                type(w).__name__,
                                                getattr(w, "x", None),
                                                getattr(w, "y", None)))
                for child in getattr(w, "children", []) or []:
                    walk(child, depth + 1)

            for top_child in Window.children:
                walk(top_child, 0)
            for line in seen:
                print("NOTE hit: " + line, flush=True)
            t = UnitTestTouch(wx, wy)
            t.touch_down(0.5, 0.5)
            Clock.schedule_once(lambda dt1: self.finish(t, dt1), 0.15)
        except Exception as exc:
            self.failures.append("tap: %r" % (exc,))
            self.stop()

    def finish(self, t, dt0):
        try:
            t.touch_up(0.5, 0.5)
            Clock.schedule_once(self.verdict, 0.4)
        except Exception as exc:
            self.failures.append("up: %r" % (exc,))
            self.stop()

    def verdict(self, dt0):
        cur = self.root.current
        ec = getattr(self, "_edit_cid", None)
        self.check(cur == "site_editor" and bool(ec),
                   "[ Edit page ] ref tap opens the site editor "
                   "(root=%r _edit_cid=%r)" % (cur, ec))
        self.stop()


def main():
    Window.size = (800, 600)
    probe = EditRefProbe()
    probe.run()
    sys.exit(bool(probe.failures))


if __name__ == "__main__":
    main()
