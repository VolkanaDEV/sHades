#!/usr/bin/env python3
"""H7c: phantom seeded channels must be reconciled against the guild
list, and seeds the guild *does* have must not be duplicated.

The client always seeded general/announcements/random; selecting one
the guild lacked gave an empty grid + "fetch failed: unknown
channel". Worse, a guild channel that matched a seed was added a
second time (verified: the sidebar held two 'general' buttons after
a guild list arrived). The fix, in _refresh_channels_from_guild:

  - adopts an existing seed button for a guild channel instead of
    adding a duplicate
  - drops seeds the guild list does not name
  - keeps all seeds when offline / the guild list is unavailable

Probe (fake guild API, no mesh):

  (a) seeds present after _refresh_channels_ui; with guild_api None
      a refresh leaves them all in place (offline keeps seeds)
  (b) guild list with only #general: announcements/random disappear,
      general appears exactly once
  (c) the visible channel falling back: switching to #random first,
      then reconciling, lands on #general

Run: xvfb-run -a python tests/test_fix_channels_reconcile.py
"""
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-fix-channels-reconcile-")
os.environ["HOME"] = TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402
import app_settings  # noqa: E402
from client_widgets import ChannelButton  # noqa: E402

kc._app_data_dir = lambda: TMP
app_settings._app_data_dir = lambda: TMP

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


class FakeGuild:
    """channels/list with only #general; every other call declines,
    so the fallback _switch_channel inside the reconcile stays
    quiet (no tracebacks from the fetch/stream threads)."""

    def channels_list(self):
        return {"ok": 1, "channels": [
            {"id": "c" * 64, "name": "general", "kind": "chat",
             "position": 1}]}

    def messages(self, *args, **kwargs):
        return {"ok": 0, "error": "offline-test"}

    def receipts(self, *args, **kwargs):
        return {"ok": 0, "error": "offline-test"}

    def stream_subscribe(self, *args, **kwargs):
        return {"ok": 0, "error": "offline-test"}


def _sidebar_names(app):
    grid = app.root.get_screen("main").ids.channel_grid
    return sorted(c.channel_name for c in grid.children
                  if isinstance(c, ChannelButton))


class ReconcileProbe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        self.root.current = "main"
        self._refresh_channels_ui()
        self._deadline = None
        Clock.schedule_once(self.check_seeds, 0.8)

    def check_seeds(self, dt):
        check("seeds present before any guild list",
              _sidebar_names(self) == ["announcements", "general",
                                       "random"], _sidebar_names(self))
        # (a) offline: no guild_api, refresh is a no-op, seeds stay.
        self.guild_api = None
        self._refresh_channels_from_guild()
        Clock.schedule_once(self.check_offline_kept, 0.8)

    def check_offline_kept(self, dt):
        check("offline refresh keeps all seeds",
              _sidebar_names(self) == ["announcements", "general",
                                       "random"], _sidebar_names(self))
        # (c) sit on #random, then reconcile against a general-only
        # guild list: the visible phantom must fall back to #general.
        self._switch_channel("random")
        self.guild_api = FakeGuild()
        self._refresh_channels_from_guild()
        self._deadline = time.time() + 8.0
        Clock.schedule_once(self.poll_reconciled, 0.5)

    def poll_reconciled(self, dt):
        names = _sidebar_names(self)
        if names == ["general"] or time.time() > self._deadline:
            self.finish(names)
            return
        Clock.schedule_once(self.poll_reconciled, 0.5)

    def finish(self, names):
        check("guild list without announcements/random drops those seeds",
              names == ["general"], names)
        check("general appears exactly once (seed adopted, not duplicated)",
              names.count("general") == 1, names)
        try:
            current = self.root.get_screen("main")._current_channel
        except Exception:
            current = "<unreadable>"
        check("visible phantom channel falls back to #general",
              current == "general", current)
        print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)), flush=True)
        self.stop()


def _watchdog():
    time.sleep(45)
    print("WATCHDOG: app did not exit; forcing exit", flush=True)
    os._exit(2)


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()
    Window.size = (1280, 720)
    probe = ReconcileProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
