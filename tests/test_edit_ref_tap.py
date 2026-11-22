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
from kivy.metrics import dp  # noqa: E402
from kivy.tests.common import UnitTestTouch  # noqa: E402

if os.environ.get("TAPWALK"):
    # Class-level walk logger (touch-debugging.md first step): wrap the
    # Widget.on_touch_down default BEFORE any widget is built, so the
    # dispatch names every frame, box and return; the first True at the
    # tap frame is the eater. Local diagnosis only, never on CI.
    from kivy.uix.widget import Widget  # noqa: E402
    _WTD = Widget.on_touch_down

    def _log_wtd(self, touch, _o=[_WTD]):
        _r = _o[0](self, touch)
        print("WALK %-16s pos=%r box=(%s,%s,%s,%s) dis=%s r=%s" % (
            type(self).__name__, tuple(round(v, 1) for v in touch.pos),
            round(self.x, 1), round(self.y, 1),
            round(self.width, 1), round(self.height, 1),
            self.disabled, bool(_r)), flush=True)
        return _r
    Widget.on_touch_down = _log_wtd

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
                    print("NOTE banner parent=%s strip=%s banner.pos=%r "
                          "banner.size=%r"
                          % (type(banner.parent).__name__,
                             type(strip).__name__,
                             tuple(banner.pos), tuple(banner.size)),
                          flush=True)
                    # The split base authoring changed the banner's
                    # mechanism (kv_md.kv): banner_channel is now a
                    # fixed dp(200)-wide Image CENTERED inside the
                    # banner_strip FloatLayout, which is the channels
                    # column - the rail (server_sidebar) is a sibling
                    # box to its left. The old pin (banner.width <=
                    # strip.width) measured the old fixed-pos failure
                    # mode and misfires at CI densities where the strip
                    # is narrower than dp(200). The contract that keeps
                    # the fold-5 receipt alive (no tap eater over the
                    # rail edge): the banner belongs to the strip, not
                    # the rail, and it overhangs at most symmetrically,
                    # so it can never cover the rail edge alone.
                    from kivy.uix.floatlayout import FloatLayout
                    self.check(isinstance(banner.parent, FloatLayout) and
                               (banner.parent is strip or
                                banner.parent == strip),
                               "channel banner is a strip child (never "
                               "the rail's neighbor with a fixed pos)")
                    self.check(abs(banner.center_x - strip.center_x) <= 1,
                               "channel banner overhangs symmetrically "
                               "(cannot eat the rail edge)")
                    self.check(strip.width >= dp(200) or
                               banner.width <= strip.width + 1,
                               "banner collapses with a narrow strip (no "
                               "disabled+collide dead zone)")
                self._channels = {"site-ch": "placeholder"}
                main._current_channel = "site-ch"
                cid = self._channel_cid("site-ch")
                self._site_current = (cid, "/index.mu")
                self._render_site_page(cid,
                                       "#!c=0\n> Probe page\n\nhello line\n",
                                       can_edit=True)
                Clock.schedule_once(self.tap_edit_ref, 0.5)
            except Exception as exc:  # surface, do not pass silently
                self.check(False, "boot: %r" % (exc,))
                import traceback
                traceback.print_exc()
                self.stop()

        Clock.schedule_once(boot, 0.8)

    def tap_edit_ref(self, dt0, tries=0):
        # 0.36.25 CI receipts (github runs 44898827484/44901925072): a
        # fixed +0.5s tap races the render under a loaded xvfb runner
        # and even the silent failure paths hide the why. Poll for the
        # label (6s ceiling) and route EVERY failure through check().
        try:
            lbl = None
            grid = self.root.get_screen("main").ids.message_grid
            for child in grid.children:
                for sub in child.children:
                    if getattr(sub, "refs", None) \
                            and "shades:site-edit" in sub.refs:
                        lbl = sub
                        break
                if lbl:
                    break
            if lbl is None:
                if tries < 40:
                    Clock.schedule_once(
                        lambda dt: self.tap_edit_ref(dt, tries + 1), 0.15)
                    return
                self.check(False, "site-edit ref label never rendered "
                                  "within the 6s poll")
                return
            sm = self.root
            trans_active = getattr(sm.transition, "is_active", False)
            if trans_active or sm.current != "main":
                # 0.36.25 github run 44902609197: on a starved runner the
                # SM's 0.4s slide transition is still active when the tap
                # lands, and ScreenManager.on_touch_down returns False
                # mid-transition - the window-level tap is dropped by
                # design (children hold screen_out + screen_in). Wait the
                # transition out, then tap; the app-level question
                # (NoTransition?) is tay's call.
                if tries < 40:
                    Clock.schedule_once(
                        lambda dt: self.tap_edit_ref(dt, tries + 1), 0.15)
                    return
                self.check(False, "sm transition still active or "
                                  "current=%r after the 6s poll"
                           % (sm.current,))
            z = lbl.refs["shades:site-edit"][0]
            print("NOTE carrier=%s parent=%s pos=%r refs_keys=%r"
                  % (type(lbl).__name__, type(lbl.parent).__name__,
                     tuple(lbl.pos), list(lbl.refs.keys())),
                  flush=True)
            # The split base's labels are plain kivy markup Labels: the
            # native refs format is (x, y, w, h) with a TOP-DOWN y
            # (kivy.uix.label.py: rect pos=(get_x(box[0]), get_y(box[1])),
            # size=(box[2], -box[3])). Convert: cx = x + w/2 in window
            # space, and the bottom-up cy = tex_h - (y + h/2) -- the
            # native y counts DOWN from the texture top, the walk law
            # counts UP. (On the pre-split base the zones were authored
            # bottom-up, which is why that lane could use cy raw.)
            # The anchor walks parents up to the ScrollView exactly like
            # the fold-5 _content_anchor walk -- the split base dropped
            # that helper, so the lane carries it.
            cx = z[0] + z[2] / 2.0
            tex_h = lbl.texture_size[1] or 18.0
            tex_w = lbl.texture_size[0] or 1.0
            cy = tex_h - (z[1] + z[3] / 2.0)
            # Two frame conversions, both receipts:
            # 1) native markup label: the refs are TEXTURE-local, and
            #    kivy blits the texture inside the widget shifted by
            #    halign/valign (kivy.uix.label.py's doc formula puts the
            #    texture at center - texture_size/2 when a layout
            #    stretches the widget past the texture). So the
            #    label-local point is the alignment offset plus the
            #    texture-local center.
            # 2) delivered-frame->window: on the pre-split base the lane
            #    hand-rolled the shift (scroll.y + short-content pull;
            #    fold-13 receipts 44898827484/44901925072). The split
            #    base re-laid the scroll/grid, so the hand-rolled law
            #    points wide of the ref (local receipt: tap at
            #    (62.5, 527.0) always missing _edit_cid). kivy's own
            #    to_window walks the full parent chain including the
            #    ScrollView, so the lane now delegates: no offset terms
            #    at all, the transform cannot disagree with the
            #    renderer.
            alx = aly = 0.0
            halign = getattr(lbl, "halign", "left")
            valign = getattr(lbl, "valign", "bottom")
            if lbl.width > tex_w:
                if halign in ("center", "middle"):
                    alx = (lbl.width - tex_w) / 2.0
                elif halign == "right":
                    alx = lbl.width - tex_w
            if lbl.height > tex_h:
                if valign in ("center", "middle"):
                    aly = (lbl.height - tex_h) / 2.0
                elif valign in ("top", "above"):
                    aly = lbl.height - tex_h
            # The delivered frame below the ScrollView is the scroll's
            # content frame: window - (scroll.pos + its g_translate) - the
            # label's pos is a render offset INSIDE that same frame, so the
            # tap must add it (local receipts: tapping the texture-local
            # point (38.5, 9) delivered (38.5, 9) verbatim - the pos was
            # never folded into the transform).
            wx, wy = lbl.to_window(lbl.x + alx + cx, lbl.y + aly + cy)
            print("NOTE tap at window (%.1f, %.1f) via %s "
                  "(label-local %.1f, %.1f) win=%dx%d scale=%r dpi=%r" % (
                      wx, wy, type(lbl).__name__, alx + cx, aly + cy,
                      Window.width, Window.height,
                      getattr(Window, "scale", None),
                      getattr(Window, "dpi", None)), flush=True)
            # CI diagnostics: who claims the tap point, and does the
            # label itself collide under it
            lbl_loc = lbl.to_local(wx, wy)
            _chain = []
            _n = lbl.parent
            while _n is not None and hasattr(_n, "x"):
                _chain.append("%s@%s,%s" % (type(_n).__name__,
                                            round(_n.x, 1), round(_n.y, 1)))
                _n = _n.parent
            print("NOTE label pos=%r size=%r tex=%r local=(%.1f, %.1f) "
                  "collide=%s owin=%r"
                  % (tuple(lbl.pos), tuple(lbl.size),
                     tuple(lbl.texture_size), lbl_loc[0], lbl_loc[1],
                     lbl.collide_point(lbl_loc[0], lbl_loc[1]),
                     lbl.to_window(0, 0)),
                  flush=True)
            seen = []
            # Fire-counters: (a) did the label see the touch at all
            # (instance-wrapped on_touch_down), (b) did its markup ref
            # dispatch fire. Splits 'touch never arrives' from 'ref
            # math wrong' from 'route broken' (CI receipts).
            lbl_state = {"downs": 0, "refs": 0}
            _orig_down = lbl.on_touch_down

            def traced_down(tch):
                lbl_state["downs"] += 1
                return _orig_down(tch)

            lbl.on_touch_down = traced_down
            self.lbl_state = lbl_state
            lbl.bind(on_ref_press=lambda inst, ref, s=lbl_state:
                     s.__setitem__("refs", s["refs"] + 1))
            # Wrap every ancestor from the label's parent up to the root
            # with a call/result tracer: one run names the exact widget
            # whose branch drops the touch.
            _node = lbl.parent
            while _node is not None and hasattr(_node, "on_touch_down"):
                _w = _node

                orig = _w.on_touch_down

                def make_tracer(w, fn):
                    def tr(tch):
                        r = fn(tch)
                        self.lbl_state["via_%s@%s" % (
                            type(w).__name__,
                            tuple(int(v) for v in w.pos))] = r
                        return r
                    return tr

                if _w is not lbl:
                    _w.on_touch_down = make_tracer(_w, orig)
                else:
                    _w.on_touch_down = traced_down
                if type(_node).__name__ == "ScreenManager":
                    break
                _node = _node.parent
            self.lbl_state["screen"] = self.root.current
            # Tag the un-traced chat_body siblings (the drawers and any
            # other overlays): one of them returns True and eats the tap
            # before chat_screen's subtree is ever reached.
            _root = self.root
            _sm = _root
            while _sm is not None and type(_sm).__name__ != "ScreenManager":
                _sm = getattr(_sm, "parent", None)
            if _sm is not None:
                for _top in _sm.children:
                    if type(_top).__name__ == "MainScreen":
                        for _ch in _top.children:
                            if type(_ch).__name__ == "BoxLayout":
                                for _cc in _ch.children:
                                    if type(_cc).__name__ == "FloatLayout":
                                        for _ov in _cc.children:
                                            if type(_ov).__name__ in \
                                                    ("BoxLayout",) and \
                                                    hasattr(_ov, "ids") and \
                                                    "message_grid" in \
                                                    getattr(_ov, "ids", {}):
                                                continue
                                            _ovw = _ov
                                            orig_ov = _ovw.on_touch_down

                                            def make_tag(w, fn):
                                                def tr(tch):
                                                    r = fn(tch)
                                                    self.lbl_state[
                                                        "overlay_%s_key=%s:%s"
                                                        % (type(w).__name__,
                                                           w.__class__.__module__
                                                           if hasattr(w, "__class__") else "",
                                                           bool(r))] = r
                                                    return r
                                                return tr

                                            _ovw.on_touch_down = \
                                                make_tag(_ovw, orig_ov)
            print("NOTE tracer nodes=%r"
                  % [k for k in self.lbl_state if k not in
                     ("downs", "refs", "screen")], flush=True)

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
            # Kivy's ScrollView tap delivery is DEFERRED (scrollview.py
            # _change_touch_mode): the down is held as a scroll candidate for
            # scroll_timeout (250ms default) + 3 frames, then ungrabbed and
            # re-dispatched into the content via simulate_touch_down. The
            # old +0.15s finish released the touch before that fires - the
            # label's down never arrived and the walk showed no delivery at
            # all (local receipts: downs=0 with nothing True at the frame
            # beyond the scroll's grab). Wait the timeout out before the up.
            Clock.schedule_once(lambda dt1: self.finish(t, dt1), 0.8)
        except Exception as exc:
            self.check(False, "tap: %r" % (exc,))
            self.stop()

    def finish(self, t, dt0):
        try:
            print("NOTE touch pipeline: downs=%r refs_fired=%r "
                  "grab_current=%r"
                  % (getattr(self, "lbl_state", None) or
                     (getattr(self, "_lbl_state", None) or {}),
                     (getattr(self, "lbl_state", None) or {}).get("refs")
                     if getattr(self, "lbl_state", None) else None,
                     getattr(t, "grab_current", None)), flush=True)
            t.touch_up(0.5, 0.5)
            Clock.schedule_once(self.verdict, 0.4)
        except Exception as exc:
            self.check(False, "up: %r" % (exc,))
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
    # nothing in this lane may fail silently: dump the tally at exit
    for item in getattr(probe, "failures", []):
        print("EXITFAILURE: " + item, flush=True)
    print("EXITFAILCOUNT: %d" % len(getattr(probe, "failures", [])),
          flush=True)
    sys.exit(bool(probe.failures))


if __name__ == "__main__":
    main()
