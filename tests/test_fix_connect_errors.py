#!/usr/bin/env python3
"""H7a + H7b: the unreachable-hub message, and the empty-channel
placeholder.

(a) _connect_after_rns used to report "Could not discover guild
    node_hash from hub announces. Ask the hub admin for its
    node_hash" even when the real fault was a refused/unreachable TCP
    endpoint. The fix checks the TCPClientInterface's `online` flag
    and reports hub unreachable / connection refused instead.
    Probe: refused hub 127.0.0.1:1, no node_hash -- poll the connect
    status and assert it names unreachable/refused, never node_hash.
(b) An empty channel showed a blank grid. The fix adds a "No messages
    yet" placeholder, removed when a message arrives.
    Probe: main screen, offline, empty cache -- switch to #general,
    assert the placeholder is visible; render one message, assert it
    is gone.

Run: xvfb-run -a python tests/test_fix_connect_errors.py
"""
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-fix-connect-errors-")
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


class ErrorsProbe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        self.root.current = "main"
        self._status_deadline = None
        Clock.schedule_once(self.try_empty_channel, 1.0)

    # ---- (b) empty-channel placeholder -------------------------------
    def try_empty_channel(self, dt):
        self._switch_channel("general")
        Clock.schedule_once(self.check_placeholder, 0.8)

    def check_placeholder(self, dt):
        grid = self.root.get_screen("main").ids.message_grid
        texts = [c.text for c in grid.children
                 if c.__class__.__name__ == "Label"]
        check("empty channel shows a 'No messages yet' placeholder",
              "No messages yet" in texts, texts)
        # One message arrives: the placeholder must go away.
        self._render_message(grid, {
            "msg_id": "ph-1", "author_hash": "d" * 32,
            "username": "dana", "content": "first!",
            "ts": 1700000000.0, "channel_id": self._channel_cid("general")})
        Clock.schedule_once(self.check_placeholder_gone, 0.5)

    def check_placeholder_gone(self, dt):
        grid = self.root.get_screen("main").ids.message_grid
        texts = [c.text for c in grid.children
                 if c.__class__.__name__ == "Label"]
        check("placeholder is removed once a message arrives",
              "No messages yet" not in texts, texts)
        check("the message itself rendered",
              "ph-1" in self._msg_widgets)
        # ---- (a) refused hub --------------------------------------
        self.connect_to_guild("127.0.0.1:1", "tester")
        self._status_deadline = time.time() + 25.0
        Clock.schedule_once(self.poll_status, 0.5)

    def _status_text(self):
        try:
            return self.root.get_screen("connect").ids.status.text or ""
        except Exception:
            return ""

    def poll_status(self, dt):
        status = self._status_text()
        if "Could not reach hub" in status or "unreachable" in status \
                or "refused" in status:
            self.finish_status(status)
            return
        if time.time() > self._status_deadline:
            self.finish_status(status)
            return
        Clock.schedule_once(self.poll_status, 0.5)

    def finish_status(self, status):
        low = status.lower()
        check("refused hub reports unreachable/refused, not discovery",
              ("unreachable" in low or "refused" in low)
              and "could not reach hub" in low, status)
        check("refused hub message never blames node_hash discovery",
              "node_hash" not in low, status)
        check("failed connect lands on the connect screen",
              self.root.current == "connect", self.root.current)
        print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)), flush=True)
        self.stop()


def _watchdog():
    time.sleep(60)
    print("WATCHDOG: app did not exit; forcing exit", flush=True)
    os._exit(2)


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()
    Window.size = (1280, 720)
    probe = ErrorsProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
