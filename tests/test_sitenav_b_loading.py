#!/usr/bin/env python3
"""Sitenav (b): tapping a link must show something is happening.

The fetch runs on a worker thread; before this fix the old page sat
unchanged for the whole fetch and the tap read as dead. _site_load_page
now renders a loading line synchronously on the main thread, inside
the page area, without blocking the UI thread. A fetch for a channel
the viewer has already left must not clobber the new channel's grid.
"""
import base64
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []
CID = "c" * 64
OTHER = "g" * 64


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


class FakeApi(object):
    """site_get for /second.mu blocks on gate until the test releases it."""

    def __init__(self, pages, gate):
        self.pages = pages
        self.gate = gate

    def site_get(self, channel, path, offset=0, length=None):
        if path == "/second.mu":
            self.gate.wait(5)
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


def make_app(pages, gate):
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
        root=root, guild_api=FakeApi(pages, gate),
        _site_current=None, _site_stack=[], _site_theme={},
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
    app._channel_cid = lambda name: CID if name == "wiki" else OTHER
    return app, grid, main


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


def pump_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        Clock.tick()
        if pred():
            return True
        time.sleep(0.02)
    Clock.tick()
    return pred()


def main():
    pages = {"/index.mu": "> Home\n",
             "/second.mu": "> Second\n"}
    gate = threading.Event()
    app, grid, main = make_app(pages, gate)

    app._site_load_page(CID, "/index.mu")
    check("index renders before the tap",
          pump_until(lambda: any("Home" in t for t in grid_texts(grid))),
          grid_texts(grid))

    # tap with the fetch blocked: the old page must be gone at once,
    # replaced by a loading line, while the main thread stays live
    app._micron_link_tapped("/file/sites/%s/second.mu" % CID)
    texts = grid_texts(grid)
    check("old page cleared synchronously on tap",
          not any("Home" in t for t in texts), texts)
    check("loading line shown while the fetch runs",
          any("loading" in t.lower() for t in texts), texts)
    ran = []
    Clock.schedule_once(lambda dt: ran.append(True), 0)
    Clock.tick()
    check("UI thread stays responsive mid-fetch", ran == [True], ran)

    gate.set()
    check("second page renders once the fetch lands",
          pump_until(lambda: any("Second" in t for t in grid_texts(grid))),
          grid_texts(grid))

    # a load for a channel the viewer has left must not clobber the grid
    gate2 = threading.Event()
    app2, grid2, main2 = make_app(pages, gate2)
    app2._site_load_page(CID, "/index.mu")
    check("second app index renders",
          pump_until(lambda: any("Home" in t for t in grid_texts(grid2))),
          grid_texts(grid2))
    main2._current_channel = "general"
    app2._site_load_page(CID, "/second.mu")
    Clock.tick()
    time.sleep(0.1)
    Clock.tick()
    check("stale load leaves the new channel's grid alone",
          any("Home" in t for t in grid_texts(grid2)), grid_texts(grid2))

    print("PASS sitenav (b) loading state: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
