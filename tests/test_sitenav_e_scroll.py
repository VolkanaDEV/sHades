#!/usr/bin/env python3
"""Sitenav (e): returning to a long page restores its scroll position.

_site_load_page records the ScrollView position into the stack entry
for the page being left; _render_site_page restores the recorded
position for Back/Forward navigations (set at once, then once more on
the next clock tick, because the layout pass can move the ScrollView
after the widgets land). A fresh navigation still goes to the top, and
the error page always renders at the top.
"""
import base64
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_site  # noqa: E402
import kivy_client as kc  # noqa: E402

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
        site_nav_visible=False, site_nav_path="",
        site_nav_can_back=False, site_nav_can_forward=False,
        status=[])
    for name in ("_site_load_page", "_render_site_page", "_site_empty_page",
                 "_micron_link_tapped", "_micron_label",
                 "_micron_blocks_into", "_add_site_media",
                 "_site_show_loading", "_site_go_back", "_site_go_forward",
                 "_site_scroll_y", "_site_set_scroll", "_refresh_site_nav"):
        setattr(app, name, types.MethodType(
            getattr(kc.sHadesClientApp, name), app))
    app._hex6_to_rgba = kc.sHadesClientApp._hex6_to_rgba
    app._site_entry = kc.sHadesClientApp._site_entry
    app._remove_skeleton = lambda *a, **k: None
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID
    app._channel_name = lambda cid: "wiki" if cid == CID else None
    return app, grid, scroll


def grid_texts(grid):
    out = []
    for w in grid.children:
        t = getattr(w, "text", None)
        if isinstance(t, str):
            out.append(t)
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
        long_index = "> Home\n" + "".join(
            "filler line %02d of the home page\n" % i for i in range(60))
        pages = {"/index.mu": long_index,
                 "/second.mu": "> Second\n",
                 "/third.mu": "> Third\n"}
        app, grid, scroll = make_app(pages)

        app._site_load_page(CID, "/index.mu")
        check("fresh load starts at the top",
              scroll.scroll_y == 1.0, scroll.scroll_y)

        # scroll mid-page, navigate away and back: position is restored
        scroll.scroll_y = 0.2
        app._micron_link_tapped("/file/sites/%s/second.mu" % CID)
        check("forward navigation goes to the top",
              scroll.scroll_y == 1.0, scroll.scroll_y)
        check("leaving records the scroll position on the stack",
              app._site_stack == [(CID, "/index.mu", 0.2)],
              app._site_stack)
        app._site_go_back()
        check("Back restores the recorded scroll position",
              scroll.scroll_y == 0.2, scroll.scroll_y)
        check("Back renders the long page again",
              any("filler line 59" in t for t in grid_texts(grid)),
              grid_texts(grid)[:2])

        # each page keeps its own position across a round trip
        scroll.scroll_y = 0.45
        app._micron_link_tapped("/file/sites/%s/second.mu" % CID)
        scroll.scroll_y = 0.7
        app._micron_link_tapped("/file/sites/%s/third.mu" % CID)
        app._site_go_back()
        check("Back restores second's own position (0.7)",
              scroll.scroll_y == 0.7, scroll.scroll_y)
        app._site_go_back()
        check("Back restores index's newer position (0.45)",
              scroll.scroll_y == 0.45, scroll.scroll_y)
        app._site_go_forward()
        check("Forward restores second's position again",
              scroll.scroll_y == 0.7, scroll.scroll_y)

        # the error page renders at the top; Back from it still restores
        app._micron_link_tapped("/file/sites/%s/missing.mu" % CID)
        check("error page renders at the top",
              scroll.scroll_y == 1.0, scroll.scroll_y)
        scroll.scroll_y = 0.9
        app._site_go_back()
        check("Back from the error page restores second's position",
              scroll.scroll_y == 0.7, scroll.scroll_y)

        # a reload of the same page (no push) goes to the top
        app._site_load_page(CID, "/second.mu")
        check("same-page reload goes to the top",
              scroll.scroll_y == 1.0, scroll.scroll_y)
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread

    print("PASS sitenav (e) scroll restore: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
