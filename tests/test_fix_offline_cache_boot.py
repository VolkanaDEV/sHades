#!/usr/bin/env python3
"""H4: the offline cache must render at boot, even with a dead hub.

With the hub unreachable, _load_history_ui returned early and
rescheduled forever while guild_api was None, and _open_main never
rendered the cache -- the user saw an empty channel although cached
messages existed. The fix renders the offline cache in _open_main
regardless of guild_api.

Probe: temp HOME with a valid session (dead hub 127.0.0.1:29221) and
5 messages seeded into offline.sqlite3 for #general. Boots the real
app and asserts:

  (a) the message grid renders the cached messages
  (b) the mesh state still shows OFFLINE

Run: xvfb-run -a python tests/test_fix_offline_cache_boot.py
"""
import json
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-fix-offline-boot-")
os.environ["HOME"] = TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402
import app_settings  # noqa: E402
import offline_store  # noqa: E402
import RNS  # noqa: E402
from client_util import hexstr  # noqa: E402

kc._app_data_dir = lambda: TMP
app_settings._app_data_dir = lambda: TMP

PASS, FAIL = [], []
NODE_HASH = "aa" * 16
N_MSGS = 5


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


def _seed():
    with open(os.path.join(TMP, "session.json"), "w") as f:
        json.dump({"node_hash": NODE_HASH, "delivery_hash": None,
                   "username": "tester", "hub_address": "127.0.0.1:29221",
                   "hub_host": "127.0.0.1", "hub_port": 29221}, f)
    # on_start treats a session with no identity file as "identity
    # missing" and parks on connect -- seed a real identity.
    os.makedirs(os.path.join(TMP, "rns"), exist_ok=True)
    RNS.Identity().to_file(os.path.join(TMP, "rns", "identity"))
    cid = hexstr(RNS.Identity.full_hash(b"main_guild:general"))
    msgs = [{"msg_id": "cached-%d" % n, "author_hash": "b" * 32,
             "username": "bob", "content": "cached hello %d" % n,
             "ts": 1700000000.0 + n, "channel_id": cid}
            for n in range(N_MSGS)]
    offline_store.put(os.path.join(TMP, "offline.sqlite3"),
                      "node:" + NODE_HASH, cid, msgs)


class OfflineBootProbe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        Clock.schedule_once(self.check, 4.0)

    def check(self, dt):
        check("app is on the main screen",
              self.root.current == "main", self.root.current)
        rendered = sorted(self._msg_widgets)
        check("cached messages rendered at boot (%d expected)" % N_MSGS,
              len(rendered) == N_MSGS, rendered)
        try:
            grid = self.root.get_screen("main").ids.message_grid
            texts = [c.content for c in grid.children
                     if hasattr(c, "content")]
        except Exception as e:
            texts = ["<unreadable: %r>" % (e,)]
        check("rendered rows carry the cached content",
              any("cached hello 0" in t for t in texts), texts)
        check("mesh state shows OFFLINE with a dead hub",
              self.mesh_label == "OFFLINE" and self.mesh_mode == "offline",
              "%s/%s" % (self.mesh_label, self.mesh_mode))
        check("no guild API while the hub is unreachable",
              self.guild_api is None)
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
    probe = OfflineBootProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
