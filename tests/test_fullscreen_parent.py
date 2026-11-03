#!/usr/bin/env python3
"""Regression tests for fullscreen image overlay parenting.

The overlay must NOT be parented to the app root, because the root is a
ScreenManager which rejects non-Screen children
(screenmanager.add_widget raises ScreenManagerException). The overlay must be
parented to the current screen instead.
"""
import os
os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.lang import Builder
    from kivy.uix.screenmanager import ScreenManager, Screen
    from kivy.core.window import Window

    Builder.load_file("kv_md.kv")

    app = kc.sHadesClientApp()

    # Minimal root: ScreenManager with a main screen (mirrors build()).
    sm = ScreenManager()
    main = Screen(name="main")
    sm.add_widget(main)
    app.root = sm

    # This used to raise ScreenManagerException: ScreenManager rejects
    # non-Screen children (FullScreenImageOverlay is a FloatLayout).
    try:
        app.show_fullscreen_image("/tmp/test.png")
    except Exception as e:
        check("show_fullscreen_image does not crash", False,
              "%s: %s" % (type(e).__name__, e))
        print()
        print("%d passed, %d failed" % (len(PASS), len(FAIL)))
        return 1 if FAIL else 0

    overlay = getattr(app, "_fullscreen_overlay", None)
    check("overlay is created", overlay is not None)
    check("overlay is parented to Window (covers entire display)",
          overlay is not None
          and overlay.parent is not None
          and overlay.parent is Window,
          "parent=%s" % type(overlay.parent).__name__ if overlay else None)
    check("overlay is NOT parented to a Screen",
          overlay is None or type(overlay.parent).__name__ != "Screen")

    # Dismiss removes the overlay and clears the tracker.
    app.dismiss_fullscreen_image(overlay.embed if overlay and overlay.embed else overlay)
    check("dismiss removes overlay from parent", overlay is None or overlay.parent is None)
    check("dismiss clears _fullscreen_overlay",
          getattr(app, "_fullscreen_overlay", None) is None)

    # Re-show after dismiss works (no orphaned overlay reference).
    app.show_fullscreen_image("/tmp/test2.png")
    overlay2 = getattr(app, "_fullscreen_overlay", None)
    check("re-show after dismiss creates a new overlay", overlay2 is not None)
    check("new overlay parented to Window",
          overlay2 is not None
          and overlay2.parent is not None
          and overlay2.parent is Window)
    app.dismiss_fullscreen_image(
        overlay2.embed if overlay2 and overlay2.embed else overlay2)

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
