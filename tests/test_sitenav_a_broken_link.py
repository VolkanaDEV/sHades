#!/usr/bin/env python3
"""Sitenav (a): a broken link must offer a way back.

A tap on a link to a missing page renders the empty/error page; the
back stack already holds the previous page, so the empty page must
carry the same [ Back ] ref the rendered page does. The failed target
must not corrupt the stack: _site_current becomes the failed target
(the editor's create door keys off it) and the stack keeps the
previous page, so Back returns to it.
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
    app._remove_skeleton = lambda *a, **k: None
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID
    return app, grid


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
        pages = {"/index.mu": ("> Home\n[second`/file/sites/%s/second.mu]\n"
                               % CID),
                 "/second.mu": "> Second\n"}
        app, grid = make_app(pages)

        # index -> second -> broken: the empty page must offer Back
        app._site_load_page(CID, "/index.mu")
        check("index renders", any("Home" in t for t in grid_texts(grid)),
              grid_texts(grid))
        app._micron_link_tapped("/file/sites/%s/second.mu" % CID)
        check("second renders", any("Second" in t for t in grid_texts(grid)),
              grid_texts(grid))
        check("stack holds index after forward nav",
              app._site_stack == [(CID, "/index.mu", 1.0)], app._site_stack)
        app._micron_link_tapped("/file/sites/%s/missing.mu" % CID)
        texts = grid_texts(grid)
        check("broken link renders the empty page",
              any("This site is empty" in t for t in texts), texts)
        check("empty page offers Back",
              any("shades:back" in t for t in texts), texts)
        check("failed target becomes current (create door keys off it)",
              app._site_current == (CID, "/missing.mu"), app._site_current)
        check("failed load pushes the page the link was on",
              app._site_stack == [(CID, "/index.mu", 1.0),
                                  (CID, "/second.mu", 1.0)],
              app._site_stack)

        # Back from the broken page returns to the page the link was on
        app._micron_link_tapped("shades:back")
        check("Back from broken page renders second again",
              any("Second" in t for t in grid_texts(grid)), grid_texts(grid))
        check("Back restores current to second",
              app._site_current == (CID, "/second.mu"), app._site_current)
        check("stack holds index after one Back",
              app._site_stack == [(CID, "/index.mu", 1.0)], app._site_stack)
        app._micron_link_tapped("shades:back")
        check("second Back renders index",
              any("Home" in t for t in grid_texts(grid)), grid_texts(grid))
        check("stack empty after two Backs", app._site_stack == [],
              app._site_stack)

        # broken link with an empty stack: no Back to offer, no crash
        app2, grid2 = make_app(pages)
        app2._site_load_page(CID, "/missing.mu")
        texts2 = grid_texts(grid2)
        check("empty-stack broken page renders",
              any("This site is empty" in t for t in texts2), texts2)
        check("empty-stack broken page offers no Back",
              not any("shades:back" in t for t in texts2), texts2)
        check("empty-stack current is the failed target",
              app2._site_current == (CID, "/missing.mu"),
              app2._site_current)
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread

    print("PASS sitenav (a) broken-link Back: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
