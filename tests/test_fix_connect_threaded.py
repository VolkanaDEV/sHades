#!/usr/bin/env python3
"""H2: connect / re-establish must not block the main thread.

_connect_after_rns blocks up to ~150s (60s discover + 30s recall
wait + 3x20s link attempts). It used to run from a Clock callback --
i.e. on the main thread -- on the retry / server-switch paths, so the
UI froze for a minute. The fix runs it on a daemon worker and guards
it with a single in-flight slot.

Live probe (fresh HOME, refused hub 127.0.0.1:1, no node_hash, so the
worker sits in the 60s discovery wait):

  (a) a Clock callback scheduled while the connect is in flight fires
      on time (main thread responsive), with the connect still in
      flight when it fires
  (b) a second connect_to_guild while one is in flight is ignored:
      no second worker is spawned

Run: xvfb-run -a python tests/test_fix_connect_threaded.py
"""
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-fix-connect-threaded-")
os.environ["HOME"] = TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402
import app_settings  # noqa: E402

# Phase-1 lesson: _save_ui_config/_load_ui_config live in app_settings
# and use that module's _app_data_dir -- patch both namespaces, and
# HOME above covers client_widgets' own copy.
kc._app_data_dir = lambda: TMP
app_settings._app_data_dir = lambda: TMP

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


class ThreadedProbe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        self.t0 = time.time()
        self.probe_fired_at = None
        self.probe_scheduled_at = None
        self.in_flight_at_probe = None
        self.workers_before_second = None
        self.workers_after_second = None
        Clock.schedule_once(self.start_connect, 1.0)

    def start_connect(self, dt):
        # Refused hub, no node_hash: the worker blocks in the 60s
        # discovery wait; the main thread must stay free throughout.
        self.connect_to_guild("127.0.0.1:1", "tester")
        self.probe_scheduled_at = time.time() + 8.0
        Clock.schedule_once(self.probe_main_thread, 8.0)

    def probe_main_thread(self, dt):
        self.probe_fired_at = time.time()
        self.in_flight_at_probe = bool(
            getattr(self, "_connect_in_flight", False))
        # Second connect while the first is in flight: must be ignored.
        self.workers_before_second = getattr(
            self, "_connect_workers_started", 0)
        self.connect_to_guild("127.0.0.1:1", "tester")
        self.workers_after_second = getattr(
            self, "_connect_workers_started", 0)
        Clock.schedule_once(self.finish, 0.5)

    def finish(self, dt):
        delay = (self.probe_fired_at - self.probe_scheduled_at
                 if self.probe_fired_at and self.probe_scheduled_at
                 else 999.0)
        check("main-thread probe fired while connect in flight",
              self.in_flight_at_probe is True,
              "in_flight=%r" % self.in_flight_at_probe)
        check("probe fired on time (delay %.2fs < 2.5s)" % delay,
              delay < 2.5, "delay=%.2f" % delay)
        check("second connect spawned no second worker",
              self.workers_before_second == self.workers_after_second
              and self.workers_before_second == 1,
              "before=%r after=%r"
              % (self.workers_before_second, self.workers_after_second))
        print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)), flush=True)
        self.stop()


def _watchdog():
    # RNS leaves background threads behind; if the app does not exit
    # on its own, do not hang the suite -- the results are printed.
    time.sleep(45)
    print("WATCHDOG: app did not exit; forcing exit", flush=True)
    os._exit(2)


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()
    Window.size = (1280, 720)
    probe = ThreadedProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
