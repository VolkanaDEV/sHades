#!/usr/bin/env python3
"""Sitenav (c): forward history.

After going Back, the forward page used to be lost (popped). The back
stack model carries it cleanly: Back shelves the current page (with
its scroll position) on a forward stack, Forward pops it back, and a
fresh navigation clears the forward stack. Empty stacks are no-ops.
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
        status=[])
    for name in ("_site_load_page", "_render_site_page", "_site_empty_page",
                 "_micron_link_tapped", "_micron_label",
                 "_micron_blocks_into", "_add_site_media",
                 "_site_show_loading", "_site_go_back", "_site_go_forward",
                 "_site_scroll_y", "_site_set_scroll"):
        setattr(app, name, types.MethodType(
            getattr(kc.sHadesClientApp, name), app))
    app._hex6_to_rgba = kc.sHadesClientApp._hex6_to_rgba
    app._site_entry = kc.sHadesClientApp._site_entry
    app._remove_skeleton = lambda *a, **k: None
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID
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
        pages = {"/index.mu": "> Home\n",
                 "/second.mu": "> Second\n",
                 "/third.mu": "> Third\n"}
        app, grid, scroll = make_app(pages)

        check("Back on an empty stack is a no-op",
              app._site_go_back() is False, app._site_current)
        check("Forward on an empty stack is a no-op",
              app._site_go_forward() is False, app._site_current)

        app._site_load_page(CID, "/index.mu")
        scroll.scroll_y = 0.3
        app._micron_link_tapped("/file/sites/%s/second.mu" % CID)
        scroll.scroll_y = 0.6
        app._micron_link_tapped("/file/sites/%s/third.mu" % CID)
        check("third renders",
              any("Third" in t for t in grid_texts(grid)), grid_texts(grid))
        check("back stack holds index and second with scroll",
              app._site_stack == [(CID, "/index.mu", 0.3),
                                  (CID, "/second.mu", 0.6)],
              app._site_stack)

        # Back shelves the current page on the forward stack
        check("Back succeeds", app._site_go_back() is True)
        check("Back renders second",
              any("Second" in t for t in grid_texts(grid)), grid_texts(grid))
        check("forward stack holds third with its scroll",
              app._site_forward == [(CID, "/third.mu", 1.0)],
              app._site_forward)
        check("Back restores second's scroll",
              scroll.scroll_y == 0.6, scroll.scroll_y)
        check("Back again renders index",
              app._site_go_back() is True
              and any("Home" in t for t in grid_texts(grid)),
              grid_texts(grid))
        check("forward stack now holds third and second",
              app._site_forward == [(CID, "/third.mu", 1.0),
                                    (CID, "/second.mu", 0.6)],
              app._site_forward)
        check("Back again restores index's scroll",
              scroll.scroll_y == 0.3, scroll.scroll_y)

        # Forward walks back up, re-shelving on the back stack
        check("Forward renders second",
              app._site_go_forward() is True
              and any("Second" in t for t in grid_texts(grid)),
              grid_texts(grid))
        check("Forward restores second's scroll",
              scroll.scroll_y == 0.6, scroll.scroll_y)
        check("Forward renders third",
              app._site_go_forward() is True
              and any("Third" in t for t in grid_texts(grid)),
              grid_texts(grid))
        check("forward stack empty at the end",
              app._site_forward == [], app._site_forward)
        check("back stack rebuilt by Forward",
              app._site_stack == [(CID, "/index.mu", 0.3),
                                  (CID, "/second.mu", 0.6)],
              app._site_stack)
        check("Forward past the end is a no-op",
              app._site_go_forward() is False)

        # a fresh navigation after going Back drops the forward stack
        app._site_go_back()
        check("Back shelves third again",
              app._site_forward == [(CID, "/third.mu", 1.0)],
              app._site_forward)
        app._micron_link_tapped("/file/sites/%s/third.mu" % CID)
        check("fresh navigation clears the forward stack",
              app._site_forward == [], app._site_forward)
        check("fresh navigation renders third",
              any("Third" in t for t in grid_texts(grid)), grid_texts(grid))

        # the in-page Back ref still routes through the same path
        app._micron_link_tapped("shades:back")
        check("shades:back ref goes Back",
              any("Second" in t for t in grid_texts(grid)), grid_texts(grid))
        app._micron_link_tapped("shades:forward")
        check("shades:forward ref goes Forward",
              any("Third" in t for t in grid_texts(grid)), grid_texts(grid))
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread

    print("PASS sitenav (c) forward history: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
