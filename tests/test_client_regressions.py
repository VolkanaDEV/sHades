#!/usr/bin/env python3
"""Regression tests for the client bugs found in the Nov 2026 audit.

Each test pins one bug so it cannot come back silently:

  1. _add_server() dropped a display name the user set. The "Add Server"
     popup names a guild, then connect_to_guild() re-adds the same server
     without a name — the entry was rebuilt with a host-derived fallback
     and "My Guild" became "192.168.1.50".

  2. _fetch_messages() applied every message delta TWICE: once inside the
     Clock-scheduled apply() and again on the worker thread.

  3. The admin panel and the roles popup called guild_api.* on the Kivy
     MAIN thread. Those calls block on the RNS link for up to the request
     timeout (10s), so an unresponsive guild froze the whole UI.

  4. on_stop() called term_close() for every open console session inline,
     freezing shutdown for N * 10s (30s measured with 3 sessions).

Run: /usr/bin/python tests/test_client_regressions.py
"""
import os
import sys
import time

os.environ.setdefault("KIVY_GL_BACKEND", "mock")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


class _StubApp(kc.sHadesClientApp):
    """A real MDApp (KivyMD widgets need one) with no RNS and no UI churn."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
        self._servers = []
        self._servers_path = "/tmp/shades_regression_servers.json"
        self._active_server_idx = -1
        self._app_data_dir = "/tmp/shades_regression"
        os.makedirs(self._app_data_dir, exist_ok=True)

    def _save_servers(self):
        pass

    def _set_status(self, text):
        pass


def test_display_name_preserved():
    """_add_server() must not overwrite a display name with a host fallback."""
    app = _StubApp()
    app._add_server("192.168.1.50:29221", "alice", "", "", "My Guild")
    check("display name is stored on first add",
          app._servers[0]["display_name"] == "My Guild",
          app._servers[0]["display_name"])

    # connect_to_guild() re-adds the same server WITHOUT a display name.
    app._add_server("192.168.1.50:29221", "alice", "", "")
    check("display name survives a re-add without one",
          app._servers[0]["display_name"] == "My Guild",
          app._servers[0]["display_name"])

    # An explicit new name must still win.
    app._add_server("192.168.1.50:29221", "alice", "", "", "Renamed")
    check("an explicit display name overrides the old one",
          app._servers[0]["display_name"] == "Renamed",
          app._servers[0]["display_name"])
    check("no duplicate entry was created", len(app._servers) == 1,
          len(app._servers))


def test_update_applied_once():
    """A message delta must be applied exactly once per fetch."""
    calls = []

    class Probe(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
            self._channel_kinds = {}
            self._older = {}
            self._msg_widgets = {}
            self._members = {}

        def _render_message(self, grid, m):
            pass

    app = Probe()
    root = app.build()
    app.root = root

    orig = kc.sHadesClientApp._apply_update

    def counting(self, u):
        calls.append(u.get("msg_id"))
        return orig(self, u)

    class FakeAPI:
        def messages(self, cid, **kw):
            return {"ok": 1, "messages": [],
                    "updates": [{"msg_id": "m1", "content": "edited"}]}

        def receipts(self, cid, msg_id):
            return {"ok": 1}

    app.guild_api = FakeAPI()
    kc.sHadesClientApp._apply_update = counting
    try:
        app._fetch_messages("general")
        from kivy.clock import Clock
        for _ in range(20):
            Clock.tick()
    finally:
        kc.sHadesClientApp._apply_update = orig

    check("each update delta is applied exactly once",
          len(calls) == 1, "applied %d times: %s" % (len(calls), calls))


def _blocking_api(block=3.0):
    class FakeAPI:
        def admin_config(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}

        def admin_mods(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}

        def admin_audit(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}

        def invites(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}

        def roles_list(self, *a, **k):
            time.sleep(block)
            return {"ok": 0, "error": "timeout"}
    return FakeAPI()


def test_admin_panel_not_blocking():
    """Opening the admin panel must not block the main thread."""
    app = _StubApp()
    app.i_am_admin = True
    app.guild_api = _blocking_api(3.0)
    root = app.build()
    app.root = root

    t0 = time.time()
    app._open_admin_panel()
    dt = time.time() - t0
    check("opening the admin panel does not block the main thread",
          dt < 1.0, "blocked for %.2fs" % dt)


def test_roles_popup_not_blocking():
    """Opening the roles popup must not block the main thread."""
    app = _StubApp()
    app.guild_api = _blocking_api(3.0)
    app.my_roles = [{"perms": 16}]
    root = app.build()
    app.root = root

    t0 = time.time()
    app._open_roles_popup("deadbeef" * 4, "someone")
    dt = time.time() - t0
    check("opening the roles popup does not block the main thread",
          dt < 1.0, "blocked for %.2fs" % dt)


def test_shutdown_not_blocking():
    """on_stop() must not block for the full request timeout per session."""
    app = _StubApp()

    class SlowAPI:
        def term_close(self, sid, timeout=10.0):
            time.sleep(min(timeout, 10.0))
            return {"ok": 1}

    app.guild_api = SlowAPI()
    app._term_sessions = {"s1": "c1", "s2": "c2", "s3": "c3"}
    app.lxmf_router = None

    t0 = time.time()
    app.on_stop()
    dt = time.time() - t0
    check("app shutdown does not block on term_close()",
          dt < 3.0, "blocked for %.2fs (3 sessions)" % dt)


def main():
    for fn in (test_display_name_preserved,
               test_update_applied_once,
               test_admin_panel_not_blocking,
               test_roles_popup_not_blocking,
               test_shutdown_not_blocking):
        try:
            fn()
        except Exception as e:
            check(fn.__name__ + " raised", False, repr(e))

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
