#!/usr/bin/env python3
"""Polish (d): site pages fade in on every render, error page included.

Asserts STATE, never wall-clock timing or frame counts. The 0.42s
reveal animation is driven deterministically: the running Kivy
Animation is advanced by explicit _update(dt) calls, so no real time
has to elapse.

- _render_site_page: the page starts at opacity 0, passes through a
  strictly partial opacity mid-fade, and lands at opacity 1
- _site_empty_page (fetch failure / not-found): the same fade
- REDUCED_MOTION on: both land at opacity 1 at once, no animation
  started
- scroll restoration is untouched: a fresh render still sets
  scroll_y to 1.0, a Back render still restores the recorded position
- rendering either page never raises

Fake-app harness in the sitenav tests' style: the real SiteMixin
methods are bound onto a SimpleNamespace, Clock and Thread are
stubbed so scheduled work runs inline, and the reveal Animation is
pumped by hand (same pattern as test_polish_b_message_entrance.py).
"""
import base64
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_site  # noqa: E402
import kivy_client as kc  # noqa: E402
import ui_fx  # noqa: E402
from kivy.animation import Animation  # noqa: E402

PASS, FAIL = [], []
CID = "c" * 64


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


class _NowClock(object):
    @staticmethod
    def schedule_once(fn, dt=0):
        fn(0)


class _InlineThread(object):
    def __init__(self, target=None, daemon=False, **k):
        self._target = target

    def start(self):
        self._target()


class FakeApi(object):
    def __init__(self, pages):
        self.pages = pages

    def site_get(self, channel, path, offset=0, length=None):
        if path in self.pages:
            return {"ok": True,
                    "data_b64": base64.b64encode(
                        self.pages[path].encode()).decode("ascii"),
                    "can_edit": False}
        return {"ok": False, "error": "resource not found",
                "can_edit": False}

    def channels_info(self, cid):
        manifest = {"index": "/index.mu", "theme": {}}
        return {"ok": True, "channel": {"manifest": manifest}}


class FakeGrid(object):
    def __init__(self):
        self.children = []

    def clear_widgets(self):
        self.children = []

    def add_widget(self, w):
        self.children.append(w)


class FakeScroll(object):
    def __init__(self):
        self.scroll_y = 1.0


def make_app(pages):
    grid = FakeGrid()
    scroll = FakeScroll()
    main = types.SimpleNamespace(
        _current_channel="wiki",
        ids=types.SimpleNamespace(message_grid=grid, scroll=scroll))

    def get_screen(name):
        if name == "main":
            return main
        raise KeyError(name)

    root = types.SimpleNamespace(get_screen=get_screen)
    app = types.SimpleNamespace(
        root=root, guild_api=FakeApi(pages),
        _site_current=None, _site_stack=[], _site_forward=[],
        _site_theme={}, _site_pending_scroll=1.0,
        _msg_widgets=[], _skeleton_cid=None, _site_src_text=None,
        status=[])
    app._site_load_page = types.MethodType(
        kc.sHadesClientApp._site_load_page, app)
    app._render_site_page = types.MethodType(
        kc.sHadesClientApp._render_site_page, app)
    app._site_empty_page = types.MethodType(
        kc.sHadesClientApp._site_empty_page, app)
    app._micron_link_tapped = types.MethodType(
        kc.sHadesClientApp._micron_link_tapped, app)
    app._micron_label = types.MethodType(
        kc.sHadesClientApp._micron_label, app)
    app._site_go_back = types.MethodType(
        kc.sHadesClientApp._site_go_back, app)
    app._site_go_forward = types.MethodType(
        kc.sHadesClientApp._site_go_forward, app)
    app._site_scroll_y = types.MethodType(
        kc.sHadesClientApp._site_scroll_y, app)
    app._site_set_scroll = types.MethodType(
        kc.sHadesClientApp._site_set_scroll, app)
    app._site_show_loading = types.MethodType(
        kc.sHadesClientApp._site_show_loading, app)
    app._site_entry = kc.sHadesClientApp._site_entry
    app._micron_blocks_into = types.MethodType(
        kc.sHadesClientApp._micron_blocks_into, app)
    app._add_site_media = types.MethodType(
        kc.sHadesClientApp._add_site_media, app)
    app._hex6_to_rgba = kc.sHadesClientApp._hex6_to_rgba
    app._remove_skeleton = types.MethodType(
        kc.sHadesClientApp._remove_skeleton, app)
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID
    return app, grid, scroll


def _anim_for(widget):
    for anim in list(Animation._instances):
        if widget.uid in getattr(anim, "_widgets", {}):
            return anim
    return None


def _step(widget, dt):
    """Advance the widget's running opacity animation by dt seconds."""
    anim = _anim_for(widget)
    if anim is None:
        return False
    anim._update(dt)
    return True


def _pump_to_end(widget):
    for _ in range(10):
        if _anim_for(widget) is None:
            break
        _step(widget, 0.15)
    return widget.opacity


def grid_texts(grid):
    out = []
    for w in grid.children:
        for c in getattr(w, "children", []):
            t = getattr(c, "text", None)
            if isinstance(t, str):
                out.append(t)
    return out


def main():
    real_clock, real_thread = app_site.Clock, app_site.threading.Thread
    app_site.Clock = _NowClock
    app_site.threading.Thread = _InlineThread
    try:
        pages = {"/index.mu": "> Home\n[second`/file/sites/%s/second.mu]\n"
                              % CID,
                 "/second.mu": "> Second\n"}
        app, grid, scroll = make_app(pages)

        # --- core: a rendered page fades in ------------------------------
        app._render_site_page(CID, pages["/index.mu"])
        check("index renders", any("Home" in t for t in grid_texts(grid)),
              grid_texts(grid))
        page = grid.children[-1]
        check("rendered page starts at opacity 0", page.opacity == 0,
              repr(page.opacity))
        check("rendered page has a running opacity animation",
              _anim_for(page) is not None)
        _step(page, 0.21)  # first update arms the clock; opacity still 0
        check("rendered page still 0 after the arming update",
              page.opacity == 0, repr(page.opacity))
        _step(page, 0.21)  # halfway through the 0.42s fade
        check("rendered page is mid-fade (0 < opacity < 1)",
              0 < page.opacity < 1, repr(page.opacity))
        check("rendered page animation still running at mid-fade",
              _anim_for(page) is not None)
        check("rendered page lands at opacity 1",
              _pump_to_end(page) == 1, repr(page.opacity))
        check("rendered page animation finished after landing",
              _anim_for(page) is None)

        # scroll restoration untouched: a fresh render goes to the top
        check("fresh render still sets scroll_y to 1.0",
              scroll.scroll_y == 1.0, repr(scroll.scroll_y))

        # --- second page fades too (navigation, not just first load) -----
        app._site_load_page(CID, "/second.mu")
        page2 = grid.children[-1]
        check("second page renders",
              any("Second" in t for t in grid_texts(grid)), grid_texts(grid))
        check("second page starts at opacity 0", page2.opacity == 0,
              repr(page2.opacity))
        _step(page2, 0.21)  # first update arms the clock; opacity still 0
        check("second page still 0 after the arming update",
              page2.opacity == 0, repr(page2.opacity))
        _step(page2, 0.21)  # halfway through the 0.42s fade
        check("second page is mid-fade (0 < opacity < 1)",
              0 < page2.opacity < 1, repr(page2.opacity))
        check("second page lands at opacity 1",
              _pump_to_end(page2) == 1, repr(page2.opacity))

        # --- Back render restores the recorded scroll position -----------
        # _site_load_page pushed second onto the stack; Back re-renders
        # index with the scroll recorded at navigation time.
        app._site_stack = [(CID, "/index.mu", 0.30)]
        app._site_current = (CID, "/second.mu")
        app._micron_link_tapped("shades:back")
        back_page = grid.children[-1]
        check("Back re-renders index",
              any("Home" in t for t in grid_texts(grid)), grid_texts(grid))
        check("Back render fades in (starts at opacity 0)",
              back_page.opacity == 0, repr(back_page.opacity))
        check("Back render restores the recorded scroll position",
              scroll.scroll_y == 0.30, repr(scroll.scroll_y))
        check("Back render lands at opacity 1",
              _pump_to_end(back_page) == 1, repr(back_page.opacity))

        # --- error page: same fade ---------------------------------------
        app._site_empty_page(CID, "resource not found")
        err_page = grid.children[-1]
        check("empty page renders",
              any("This site is empty" in t for t in grid_texts(grid)),
              grid_texts(grid))
        check("empty page starts at opacity 0", err_page.opacity == 0,
              repr(err_page.opacity))
        check("empty page has a running opacity animation",
              _anim_for(err_page) is not None)
        _step(err_page, 0.21)
        _step(err_page, 0.21)
        check("empty page is mid-fade (0 < opacity < 1)",
              0 < err_page.opacity < 1, repr(err_page.opacity))
        check("empty page lands at opacity 1",
              _pump_to_end(err_page) == 1, repr(err_page.opacity))
        check("empty page animation finished after landing",
              _anim_for(err_page) is None)

        # --- reduced motion: both land at once ---------------------------
        ui_fx.set_reduced_motion(True)
        try:
            app._render_site_page(CID, pages["/index.mu"])
        finally:
            ui_fx.set_reduced_motion(False)
        rpage = grid.children[-1]
        check("reduced-motion render is opacity 1 at once",
              rpage.opacity == 1, repr(rpage.opacity))
        check("reduced-motion render starts no animation",
              _anim_for(rpage) is None)

        ui_fx.set_reduced_motion(True)
        try:
            app._site_empty_page(CID, "resource not found")
        finally:
            ui_fx.set_reduced_motion(False)
        rerr = grid.children[-1]
        check("reduced-motion empty page is opacity 1 at once",
              rerr.opacity == 1, repr(rerr.opacity))
        check("reduced-motion empty page starts no animation",
              _anim_for(rerr) is None)

        # --- rendering either page never raises --------------------------
        try:
            app._render_site_page(CID, pages["/second.mu"])
            noraise = grid.children[-1]
            check("rendering a page does not raise", True)
            check("no-raise page lands at opacity 1",
                  _pump_to_end(noraise) == 1, repr(noraise.opacity))
        except Exception as e:
            check("rendering a page does not raise", False, repr(e))
        try:
            app._site_empty_page(CID, "boom")
            noraise_e = grid.children[-1]
            check("rendering an empty page does not raise", True)
            check("no-raise empty page lands at opacity 1",
                  _pump_to_end(noraise_e) == 1, repr(noraise_e.opacity))
        except Exception as e:
            check("rendering an empty page does not raise", False, repr(e))
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread
        ui_fx.set_reduced_motion(False)
        # No cleanup of Animation._instances needed: every animation
        # started in this file was pumped to completion above, and the
        # file runs as its own process.

    print("PASS polish (d) site transition: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
