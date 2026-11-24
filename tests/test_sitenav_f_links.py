#!/usr/bin/env python3
"""Sitenav (f): link dispatch handles what the format actually supports.

- Relative targets resolve against the current page's path ('..'
  normalized, never escaping the root); a resolved .mu loads, any
  other resolved site path downloads.
- http(s) targets open in the system browser via webbrowser.open.
- Non-media /file/... links (blob .pdf, site doc) download like media.
- Bare 32-hex targets are profile-node hashes (the guild's
  username_fragment emits exactly these): they resolve through
  presence to a user hash, else open_profile falls back to the node
  hash as its lookup hash.
- Anchors: micron has no anchor syntax and nothing emits '#' targets,
  so an anchor target stays an unknown link (status line, no invented
  behaviour). Verified by grep over sHades.py / micron.py / md_to_micron.
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
USER = "u" * 32
NODE = "a" * 32
BLOB = "b" * 32


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


def make_app():
    main = types.SimpleNamespace(_current_channel="wiki")

    def get_screen(name):
        if name == "main":
            return main
        raise KeyError(name)

    root = types.SimpleNamespace(get_screen=get_screen)
    app = types.SimpleNamespace(
        root=root, guild_api=None,
        _site_current=(CID, "/index.mu"), _site_stack=[],
        _site_forward=[], _active_users={USER: {"username": "bob",
                                                "node_hash": NODE}},
        my_hash="m" * 32, status=[], loads=[], downloads=[],
        site_downloads=[], profiles=[], opened=[],
        went_back=[], went_forward=[], edited=[], created=[])
    app._micron_link_tapped = types.MethodType(
        kc.sHadesClientApp._micron_link_tapped, app)
    app._open_profile_node = types.MethodType(
        kc.sHadesClientApp._open_profile_node, app)
    app._site_go_back = lambda: app.went_back.append(True)
    app._site_go_forward = lambda: app.went_forward.append(True)
    app._open_site_editor = lambda: app.edited.append(True)
    app._open_new_site_page = lambda: app.created.append(True)
    app._site_load_page = lambda c, p, push=False, restore_y=None: \
        app.loads.append((c, p, push))
    app._site_download_media = lambda t: app.downloads.append(t)
    app._site_download_site_resource = lambda c, p: \
        app.site_downloads.append((c, p))
    app.open_profile = lambda h, u: app.profiles.append((h, u))
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID
    return app


def main():
    # --- the resolver, direct ---------------------------------------
    resolve = app_site._resolve_micron_path
    cases = [
        (("/index.mu", "second.mu"), "/second.mu"),
        (("/docs/a.mu", "../index.mu"), "/index.mu"),
        (("/docs/a.mu", "../../escape.mu"), "/escape.mu"),
        (("/docs/a.mu", "sub/deep.mu"), "/docs/sub/deep.mu"),
        (("/docs/a.mu", "./same.mu"), "/docs/same.mu"),
        (("/index.mu", "/abs/../other.mu"), "/other.mu"),
        (("/page/sHades/messages.mu", "other.mu"),
         "/page/sHades/other.mu"),
        (("/index.mu", "/"), "/"),
    ]
    for (base, target), want in cases:
        got = resolve(base, target)
        check("resolve %r against %r -> %r" % (target, base, want),
              got == want, got)

    app = make_app()

    # --- relative targets -------------------------------------------
    app._micron_link_tapped("second.mu")
    check("relative page loads in the current site",
          app.loads == [(CID, "/second.mu", True)], app.loads)
    app._site_current = (CID, "/docs/page.mu")
    app._micron_link_tapped("../other.mu")
    check("relative .. climbs one directory",
          app.loads[-1] == (CID, "/other.mu", True), app.loads[-1])
    app._micron_link_tapped("../../escape.mu")
    check("relative .. never escapes the root",
          app.loads[-1] == (CID, "/escape.mu", True), app.loads[-1])
    app._micron_link_tapped("pic.png")
    check("relative media downloads as a site resource",
          app.site_downloads[-1] == (CID, "/docs/pic.png"),
          app.site_downloads[-1])
    app._micron_link_tapped("doc.pdf")
    check("relative non-page file downloads",
          app.site_downloads[-1] == (CID, "/docs/doc.pdf"),
          app.site_downloads[-1])

    # --- absolute site paths ----------------------------------------
    app._site_current = (CID, "/index.mu")
    app._micron_link_tapped("/other.mu")
    check("absolute site page loads",
          app.loads[-1] == (CID, "/other.mu", True), app.loads[-1])
    app._micron_link_tapped("/guide.pdf")
    check("absolute site file downloads",
          app.site_downloads[-1] == (CID, "/guide.pdf"),
          app.site_downloads[-1])

    # --- guild /page/ targets, incl. relative from a guild page -----
    app._micron_link_tapped("/page/sHades/members.mu")
    check("guild /page/ target loads via the channel cid",
          app.loads[-1] == (CID, "/page/sHades/members.mu", True),
          app.loads[-1])
    app._site_current = (CID, "/page/sHades/messages.mu")
    app._micron_link_tapped("other.mu")
    check("relative from a guild page stays under /page/",
          app.loads[-1] == (CID, "/page/sHades/other.mu", True),
          app.loads[-1])

    # --- external links ----------------------------------------------
    real_open = app_site.webbrowser.open
    app_site.webbrowser.open = lambda url: app.opened.append(url)
    try:
        app._micron_link_tapped("https://example.org/page")
        check("https link opens in the system browser",
              app.opened == ["https://example.org/page"], app.opened)
        check("https link reports opened",
              any("opened https://example.org/page" in s
                  for s in app.status), app.status)
        app._micron_link_tapped("http://example.org")
        check("http link opens too",
              app.opened[-1] == "http://example.org", app.opened)
    finally:
        app_site.webbrowser.open = real_open

    # --- non-media /file/ links download -----------------------------
    app._micron_link_tapped("/file/sites/%s/doc.pdf" % CID)
    check("site .pdf downloads like media",
          app.downloads[-1] == "/file/sites/%s/doc.pdf" % CID,
          app.downloads[-1])
    app._micron_link_tapped("/file/blobs/%s.pdf" % BLOB)
    check("blob .pdf downloads like media",
          app.downloads[-1] == "/file/blobs/%s.pdf" % BLOB,
          app.downloads[-1])
    app._micron_link_tapped("/file/sites/%s/pic.png" % CID)
    check("site image still downloads (media branch)",
          app.downloads[-1] == "/file/sites/%s/pic.png" % CID,
          app.downloads[-1])

    # --- bare 32-hex profile nodes ------------------------------------
    app._micron_link_tapped(NODE)
    check("known node hash resolves to its user via presence",
          app.profiles[-1] == (USER, "bob"), app.profiles[-1])
    app._micron_link_tapped("d" * 32)
    check("unknown node hash passes through to open_profile",
          app.profiles[-1] == ("d" * 32, ""), app.profiles[-1])

    # --- anchors: no invented behaviour -------------------------------
    before = (list(app.loads), list(app.downloads),
              list(app.site_downloads), list(app.opened))
    app._micron_link_tapped("#section")
    check("anchor target falls through to the status line",
          app.status[-1] == "link: #section", app.status[-1])
    check("anchor target triggers no navigation or download",
          (list(app.loads), list(app.downloads),
           list(app.site_downloads), list(app.opened)) == before,
          (app.loads, app.downloads))

    # --- the old dispatches still route --------------------------------
    app._micron_link_tapped("shades:back")
    check("shades:back still routes", app.went_back == [True], app.went_back)
    app._micron_link_tapped("shades:forward")
    check("shades:forward still routes", app.went_forward == [True],
          app.went_forward)
    app._micron_link_tapped("shades:site-edit")
    check("shades:site-edit still routes", app.edited == [True], app.edited)
    app._micron_link_tapped("shades:site-create")
    check("shades:site-create still routes", app.created == [True],
          app.created)
    app._site_current = None
    app._micron_link_tapped("second.mu")
    check("relative with no current page falls to status",
          app.status[-1] == "link: second.mu", app.status[-1])

    # --- the site-resource download itself ----------------------------
    class FakeApi(object):
        def site_get(self, channel, path, offset=0, length=None):
            if path == "/doc.pdf":
                return {"ok": True,
                        "data_b64": base64.b64encode(b"%PDF-1.4").decode(
                            "ascii")}
            return {"ok": False, "error": "resource not found"}

    real_clock, real_thread = app_site.Clock, app_site.threading.Thread
    app_site.Clock = _NowClock
    app_site.threading.Thread = _InlineThread
    try:
        app2 = make_app()
        app2.guild_api = FakeApi()
        app2._site_download_site_resource = types.MethodType(
            kc.sHadesClientApp._site_download_site_resource, app2)
        app2._save_attachment_blob = lambda blob, name: "/tmp/x_" + name
        app2._site_download_site_resource(CID, "/doc.pdf")
        check("site resource download saves the blob",
              app2.status[-1] == "saved /tmp/x_doc.pdf", app2.status[-1])
        app2._site_download_site_resource(CID, "/gone.pdf")
        check("missing site resource reports download failed",
              app2.status[-1] == "download failed: gone.pdf",
              app2.status[-1])
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread

    print("PASS sitenav (f) link dispatch: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
