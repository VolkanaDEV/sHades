#!/usr/bin/env python3
"""Sitenav (d): the viewer can see which page they are on.

_refresh_site_nav mirrors the navigation state into app properties
(site_nav_visible / site_nav_path / site_nav_can_back /
site_nav_can_forward) that the KV nav bar binds to. The breadcrumb
updates on every navigation -- forward, Back, Forward, and the error
page -- and hides when the viewer leaves the site channel. The bar
itself (geometry, buttons) is covered live by test_sitenav_g_navchrome.
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
        site_nav_visible=False, site_nav_path="",
        site_nav_can_back=False, site_nav_can_forward=False,
        status=[])
    for name in ("_site_load_page", "_render_site_page", "_site_empty_page",
                 "_micron_link_tapped", "_micron_label",
                 "_micron_blocks_into", "_add_site_media",
                 "_site_show_loading", "_site_go_back", "_site_go_forward",
                 "_site_scroll_y", "_site_set_scroll", "_refresh_site_nav",
                 "_open_site_channel"):
        setattr(app, name, types.MethodType(
            getattr(kc.sHadesClientApp, name), app))
    app._hex6_to_rgba = kc.sHadesClientApp._hex6_to_rgba
    app._site_entry = kc.sHadesClientApp._site_entry
    app._remove_skeleton = lambda *a, **k: None
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID
    app._channel_name = lambda cid: "wiki" if cid == CID else None
    return app, grid


def main():
    real_clock, real_thread = app_site.Clock, app_site.threading.Thread
    app_site.Clock = _NowClock
    app_site.threading.Thread = _InlineThread
    try:
        pages = {"/index.mu": "> Home\n",
                 "/second.mu": "> Second\n"}
        app, grid = make_app(pages)

        check("nav bar hidden before any site page",
              app.site_nav_visible is False and app.site_nav_path == "",
              (app.site_nav_visible, app.site_nav_path))

        app._open_site_channel("wiki", CID)
        check("opening the channel shows the bar",
              app.site_nav_visible is True, app.site_nav_visible)
        check("breadcrumb shows the index path after open",
              app.site_nav_path == "#wiki /index.mu", app.site_nav_path)
        check("no Back on the first page",
              app.site_nav_can_back is False, app.site_nav_can_back)
        check("no Forward on the first page",
              app.site_nav_can_forward is False, app.site_nav_can_forward)

        app._micron_link_tapped("/file/sites/%s/second.mu" % CID)
        check("breadcrumb follows a forward navigation",
              app.site_nav_path == "#wiki /second.mu", app.site_nav_path)
        check("Back enabled after a forward navigation",
              app.site_nav_can_back is True, app.site_nav_can_back)
        check("Forward still disabled",
              app.site_nav_can_forward is False, app.site_nav_can_forward)

        app._site_go_back()
        check("breadcrumb follows Back",
              app.site_nav_path == "#wiki /index.mu", app.site_nav_path)
        check("Back disabled at the first page again",
              app.site_nav_can_back is False, app.site_nav_can_back)
        check("Forward enabled after Back",
              app.site_nav_can_forward is True, app.site_nav_can_forward)

        app._site_go_forward()
        check("breadcrumb follows Forward",
              app.site_nav_path == "#wiki /second.mu", app.site_nav_path)

        app._micron_link_tapped("/file/sites/%s/missing.mu" % CID)
        check("breadcrumb shows the failed target too",
              app.site_nav_path == "#wiki /missing.mu", app.site_nav_path)
        check("Back enabled on the error page",
              app.site_nav_can_back is True, app.site_nav_can_back)

        app._refresh_site_nav(visible=False)
        check("hiding clears the breadcrumb",
              app.site_nav_visible is False and app.site_nav_path == "",
              (app.site_nav_visible, app.site_nav_path))
        check("hiding clears the gates",
              app.site_nav_can_back is False
              and app.site_nav_can_forward is False,
              (app.site_nav_can_back, app.site_nav_can_forward))
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread

    print("PASS sitenav (d) breadcrumb: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
