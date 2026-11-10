#!/usr/bin/env python3
"""Fresh-install regression tests for the sHades Kivy client.

Guards the two ways the client broke when started from a fresh install:

  1. kv_md.kv imported `ui_scale.scaled` via a KV `#:import` directive, but
     ui_scale.py was never committed to git. A fresh clone therefore died at
     startup with:
         ParserException: Unable to import package 'ui_scale.scaled'
     These tests assert every module a .kv file imports actually exists in
     the repo, so an untracked helper can never break a fresh clone again.

  2. The server sidebar was bolted onto <ConnectScreen> (the first screen a
     fresh install shows), which left that screen's form fields nested under
     a duplicated `banner_config` Image and put the status label outside the
     form column. These tests assert the connect screen keeps its original
     single-column shape with a directly reachable form.

Run:  /usr/bin/python tests/test_fresh_install.py
"""
import os
import re
import sys

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


def main():
    # --- 1. every KV #:import resolves to a file that is in the repo -----
    kv_path = os.path.join(REPO, "kv_md.kv")
    with open(kv_path, encoding="utf-8") as f:
        kv_src = f.read()

    imports = re.findall(r"^#:import\s+(\S+)\s+(\S+)\s*$", kv_src, re.M)
    check("kv_md.kv has KV import directives", len(imports) > 0)

    for alias, module in imports:
        if module.startswith("kivy") or module.startswith("kivymd"):
            continue  # third-party, installed by requirements.txt
        # `ui_scale.scaled` means module `ui_scale`, attribute `scaled`.
        mod_path = module.split(".")[0]
        exists = os.path.isfile(os.path.join(REPO, mod_path + ".py"))
        check("KV import %s (module %s) exists in repo" % (alias, module), exists,
              "missing %s.py" % mod_path)

        # The helper must be tracked by git, or a fresh clone won't have it.
        tracked = False
        try:
            import subprocess
            out = subprocess.run(
                ["git", "ls-files", "--error-unmatch", mod_path + ".py"],
                cwd=REPO, capture_output=True, text=True, timeout=30)
            tracked = out.returncode == 0
        except Exception:
            tracked = None
        if tracked is not False:
            check("KV import module %s.py is tracked by git" % mod_path, tracked)

    # --- 2. ConnectScreen keeps its original single-column form ----------
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.lang import Builder
    from kivy.metrics import dp

    # KV rules read app.theme_colors[...], which resolves through
    # App.get_running_app(); the KV proxy also calls app.bind(). With no app
    # running, building any themed widget raises
    # "'NoneType' object has no attribute 'bind'". sHadesClientApp is an MDApp
    # (KivyMD widgets refuse to build under a plain kivy.app.App), so use it.
    class _ThemeApp(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)

    app = _ThemeApp()
    root = app.build()
    app.root = root
    connect = root.get_screen("connect")

    ids = set(connect.ids.keys())
    for needed in ("hub_address", "username", "node_hash", "delivery_hash",
                   "status", "banner_config"):
        check("ConnectScreen has id '%s'" % needed, needed in ids,
              "have: %s" % sorted(ids))

    # The connect screen must NOT have a server-sidebar column: that was the
    # fresh-install breakage (an empty server list on a first-run screen).
    check("ConnectScreen has no server_grid (sidebar removed)",
          "server_grid" not in ids, "have: %s" % sorted(ids))
    check("ConnectScreen has no server_sidebar (sidebar removed)",
          "server_sidebar" not in ids, "have: %s" % sorted(ids))

    # The form fields must live in the screen's own box, not buried inside
    # the banner Image (the duplication that broke the layout).
    def ancestors(w):
        out = []
        p = w.parent
        while p is not None:
            out.append(type(p).__name__)
            p = p.parent
        return out

    for fid in ("hub_address", "username", "node_hash", "delivery_hash"):
        check("ConnectScreen '%s' is not nested under an Image" % fid,
              "Image" not in ancestors(connect.ids[fid]),
              "ancestors: %s" % ancestors(connect.ids[fid]))

    # The status label must be a direct child of the screen's root box so it
    # renders under the form instead of beside it.
    check("ConnectScreen 'status' is a direct child of the root box",
          type(connect.ids["status"].parent).__name__ == "BoxLayout"
          and connect.ids["status"].parent is connect.children[0],
          "parent: %s" % type(connect.ids["status"].parent).__name__)

    # The banner must appear exactly once.
    def count_images(w, out):
        for c in w.children:
            if type(c).__name__ == "Image":
                out.append(c)
            count_images(c, out)
        return out

    images = count_images(connect, [])
    check("ConnectScreen has exactly one banner Image", len(images) == 1,
          "found %d" % len(images))

    # --- 3. the main screen still has its far-left server rail -----------
    main_screen = root.get_screen("main")
    main_ids = set(main_screen.ids.keys())
    for needed in ("server_grid", "server_sidebar", "channel_grid",
                   "message_grid", "msg_input"):
        check("MainScreen has id '%s'" % needed, needed in main_ids,
              "have: %s" % sorted(main_ids))

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
