#!/usr/bin/env python3
"""H3: a corrupt identity file must never be silently replaced.

The old boot path minted a fresh identity when the stored one was
corrupt, changing my_hash with no warning -- the user permanently
lost their identity, username claim, DMs and roles. The fix applies
the onboarding behaviour at boot too: stop on the connect screen
with a recovery message and leave the damaged file untouched.

Probe: temp HOME with a valid session and a garbage identity file.
Boots the real app and asserts:

  (a) the identity file bytes are unchanged (nothing minted over it)
  (b) client_identity / my_hash were NOT replaced (still None)
  (c) the app lands on the connect screen showing the damaged
      identity message

Run: xvfb-run -a python tests/test_fix_corrupt_identity.py
"""
import json
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-fix-corrupt-ident-")
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
GARBAGE = b"this is not a valid RNS identity file\n" * 4


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


def _seed():
    # _app_data_dir is patched to TMP below, so TMP *is* the data dir.
    data_dir = TMP
    os.makedirs(os.path.join(data_dir, "rns"), exist_ok=True)
    with open(os.path.join(data_dir, "rns", "identity"), "wb") as f:
        f.write(GARBAGE)
    with open(os.path.join(data_dir, "session.json"), "w") as f:
        json.dump({"node_hash": "aa" * 16, "delivery_hash": None,
                   "username": "tester", "hub_address": "127.0.0.1:29221",
                   "hub_host": "127.0.0.1", "hub_port": 29221}, f)


class CorruptIdentityProbe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        Clock.schedule_once(self.check, 6.0)

    def check(self, dt):
        ipath = os.path.join(TMP, "rns", "identity")
        with open(ipath, "rb") as f:
            after = f.read()
        check("damaged identity file bytes are unchanged",
              after == GARBAGE, "len=%d" % len(after))
        check("no replacement identity was minted",
              getattr(self, "_identity_damaged", False) is True)
        check("client_identity was not replaced",
              self.client_identity is None)
        check("my_hash was not replaced",
              self.my_hash is None)
        check("app stopped on the connect screen",
              self.root.current == "connect", self.root.current)
        try:
            status = self.root.get_screen("connect").ids.status.text
        except Exception as e:
            status = "<unreadable: %r>" % (e,)
        check("connect screen shows the damaged-identity message",
              "Identity damaged" in status
              and "recovery kit" in status, status)
        print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)), flush=True)
        self.stop()


def _watchdog():
    time.sleep(45)
    print("WATCHDOG: app did not exit; forcing exit", flush=True)
    os._exit(2)


if __name__ == "__main__":
    _seed()
    threading.Thread(target=_watchdog, daemon=True).start()
    Window.size = (1280, 720)
    probe = CorruptIdentityProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
