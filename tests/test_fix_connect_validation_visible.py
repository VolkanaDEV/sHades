#!/usr/bin/env python3
"""H5: connect validation errors must be visible on a fresh install.

connect_to_guild validation rejections only called _set_status, which
writes to the connect/profile screens' status label -- on a fresh
install the app sits on the onboarding screen, so the error was never
seen. The fix switches to the connect screen before writing the
rejection.

Probe: fresh HOME (no session, no profile) boots to onboarding, then
connect_to_guild is called with bad inputs. Asserts:

  (a) a malformed address lands on the connect screen with the
      validation error in its status line
  (b) a bad node_hash is likewise visible (screen stays connect,
      status names the hash)

Run: xvfb-run -a python tests/test_fix_connect_validation_visible.py
"""
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-fix-validation-")
os.environ["HOME"] = TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402
import app_settings  # noqa: E402

kc._app_data_dir = lambda: TMP
app_settings._app_data_dir = lambda: TMP

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


def _status(app):
    try:
        return app.root.get_screen("connect").ids.status.text
    except Exception as e:
        return "<unreadable: %r>" % (e,)


class ValidationProbe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        Clock.schedule_once(self.try_bad_address, 1.5)

    def try_bad_address(self, dt):
        check("fresh install starts on onboarding",
              self.root.current == "onboarding", self.root.current)
        self.connect_to_guild("not-an-address", "alice")
        Clock.schedule_once(self.check_bad_address, 0.5)

    def check_bad_address(self, dt):
        check("malformed address switches to the connect screen",
              self.root.current == "connect", self.root.current)
        check("malformed address error is shown on the connect screen",
              "ip:port" in _status(self), _status(self))
        self.connect_to_guild("127.0.0.1:29221", "alice",
                              node_hash="not-hex")
        Clock.schedule_once(self.check_bad_hash, 0.5)

    def check_bad_hash(self, dt):
        check("bad node_hash stays on the connect screen",
              self.root.current == "connect", self.root.current)
        check("bad node_hash error is shown",
              "Invalid node_hash" in _status(self), _status(self))
        print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)), flush=True)
        self.stop()


def _watchdog():
    time.sleep(45)
    print("WATCHDOG: app did not exit; forcing exit", flush=True)
    os._exit(2)


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()
    Window.size = (1280, 720)
    probe = ValidationProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
