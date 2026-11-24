#!/usr/bin/env python3
"""UIX P3-4: kv_md.kv must load exactly once even when app.build() is
called twice in one process (test harnesses call build() before run()).

  - no Kivy "loaded multiples times" warning on the second build()
  - both builds return a working root (ScreenManager with all screens,
    main screen's msg_input present)
  - the module-level _KV_LOADED flag flips False -> True and stays True

Run: xvfb-run -a python tests/test_uix_kv_double_load.py
"""
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []
TMP = tempfile.mkdtemp(prefix="shades-uix-kv-")


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        try:
            self.messages.append(record.getMessage())
        except Exception:
            pass


def test_double_build():
    saved_appdir = kc._app_data_dir
    kc._app_data_dir = lambda: TMP
    kc._KV_LOADED = False
    capture = _Capture()
    logger = logging.getLogger("kivy")
    logger.addHandler(capture)
    try:
        app = kc.sHadesClientApp()
        check("flag starts False in a fresh process",
              kc._KV_LOADED is False)
        root1 = app.build()
        check("flag is True after the first build()",
              kc._KV_LOADED is True)
        root2 = app.build()
        check("flag stays True after the second build()",
              kc._KV_LOADED is True)
        double = [m for m in capture.messages
                  if "loaded multiples times" in m]
        check("no 'loaded multiples times' warning on the second build()",
              not double, double[:1])
        for tag, root in (("first", root1), ("second", root2)):
            names = [s.name for s in root.screens]
            check("%s build returns a working root with all screens"
                  % tag,
                  "connect" in names and "main" in names
                  and "config" in names, names)
            main = root.get_screen("main")
            check("%s build: main screen has msg_input" % tag,
                  "msg_input" in main.ids)
    finally:
        logger.removeHandler(capture)
        kc._app_data_dir = saved_appdir
        shutil.rmtree(TMP, ignore_errors=True)


def main():
    try:
        test_double_build()
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("test_double_build raised", False, repr(e))
    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
