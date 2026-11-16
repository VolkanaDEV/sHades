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


def test_attachment_kind_classifier():
    """One decision site governs embeds (tay v0.30): images inline, audio
    player row, video download-only for now, everything else file row."""
    k = kc.sHadesClientApp._attachment_kind
    cases = (
        ("photo.jpg", "image"),
        ("scan.PNG", "image"),
        ("blob.webp", "image"),
        ("song.mp3", "audio"),
        ("voice.ogg", "audio"),
        ("note.wav", "audio"),
        ("book.m4a", "audio"),
        ("clip.mp4", "video"),
        ("screen.webm", "video"),
        ("take.mov", "video"),
        ("report.pdf", "file"),
        ("data.csv", "file"),
        ("noext", "file"),
    )
    for name, want in cases:
        got = k(name)
        check("kind(%s) == %s" % (name, want), got == want, got)
    # mime wins over extension ambiguity
    check("mime audio overrides ext", k("weird.bin", "audio/mpeg") == "audio",
          k("weird.bin", "audio/mpeg"))
    check("mime video overrides ext", k("weird.bin", "video/mp4") == "video",
          k("weird.bin", "video/mp4"))
    check("application/ogg counts as audio",
          k("stream.ogg", "application/ogg") == "audio",
          k("stream.ogg", "application/ogg"))


def test_stream_lifecycle():
    """v0.31, receipts: channel switch never re-subscribed (live updates
    died after the first switch), quiet channels expired at SUB_TTL_S=120
    with no refresh, and a switch to site/term left the old subscription
    flowing into the void. This pins the fixed mechanics."""
    from kivy.clock import Clock

    class _H:
        text = ""

    class _Grid:
        def clear_widgets(self):
            pass

        def add_widget(self, w):
            pass

    class _Ids:
        def __init__(self):
            self.channel_header = _H()
            self.message_grid = _Grid()

    class _Screen:
        def __init__(self):
            self._current_channel = "general"
            self.ids = _Ids()

    class _FakeApi:
        def __init__(self, app):
            self.app = app

        def stream_subscribe(self, channel, cursor=None):
            self.app.subscribe_calls.append((channel, cursor))
            return {"ok": 1, "seq": 7}

        def stream_unsubscribe(self):
            self.app.unsubscribed.append(1)
            return {"ok": 1}

        def stream_poll(self, cursor=None, timeout_s=4.0):
            if self.app.poll_script:
                return self.app.poll_script.pop(0)
            return {"ok": 1, "events": [], "cursor": cursor}

    class _StreamApp(_StubApp):
        def __init__(self, kinds=None):
            super().__init__(**{})
            self._screen = _Screen()
            self.root = type(
                "R", (), {"get_screen": lambda s, n: self._screen})()
            self._channel_kinds = dict(kinds or {})
            self._channels = {"general": "c" + "0" * 31,
                              "lounge": "c" + "1" * 31}
            self._older = {}
            self.subscribe_calls = []
            self.unsubscribed = []
            self.poll_script = []
            self.fetched = None
            self.guild_api = _FakeApi(self)
            self._stream_cid = None
            self._stream_cursor = None
            self._stream_tick_event = None
            self._active_users = {}

        def _channel_cid(self, name):
            return self._channels.get(name)

        def _fetch_messages(self, name):
            self.fetched = name

        def _refresh_members_ui(self, name):
            pass

    def tick_until(app, cond, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            Clock.tick()
            if cond():
                return True
            time.sleep(0.02)
        return False

    # 1. chat -> chat: the switch RE-SUBSCRIBES on the new channel and the
    #    server learns the re-point (subscribe replaces the binding).
    app = _StreamApp()
    app._switch_channel("lounge")
    ok = tick_until(app, lambda: app.subscribe_calls)
    check("channel switch re-subscribes on the new channel",
          ok and app.subscribe_calls[-1][0] == app._channels["lounge"],
          repr(app.subscribe_calls))

    # 2. chat -> site: the slot is released (one subscription per link).
    app2 = _StreamApp(kinds={"s" + "0" * 31: "site"})
    app2._channels["sitech"] = "s" + "0" * 31
    app2._stream_cid = app2._channels["general"]
    app2._switch_channel("sitech")
    tick_until(app2, lambda: app2.unsubscribed)
    check("switch to a push-less kind releases the subscription",
          app2.unsubscribed and app2._stream_cid is None,
          (repr(app2.unsubscribed), repr(app2._stream_cid)))

    # 3. keepalive 409 -> resubscribe FROM THE WATERMARK (gap replays).
    app3 = _StreamApp()
    app3._stream_cid = app3._channels["general"]
    app3._stream_cursor = 5
    app3.poll_script = [{"ok": 0, "code": 409}]
    app3._stream_keepalive_once()
    ok = tick_until(app3, lambda: app3.subscribe_calls)
    check("keepalive self-heals an expired subscription",
          ok and app3.subscribe_calls[-1] == (app3._channels["general"], 5),
          repr(app3.subscribe_calls))

    # 4. keepalive wins a race: events apply, cursor advances.
    app4 = _StreamApp()
    app4._stream_cid = app4._channels["general"]
    app4._stream_cursor = 3
    app4.poll_script = [{
        "ok": 1, "cursor": 9,
        "events": [{"kind": "msg",
                    "data": {"msg": {"msg_id": "m1",
                                     "channel_id": app4._channels["general"],
                                     "content": "x"}}}]}]
    app4._stream_keepalive_once()
    tick_until(app4, lambda: app4.fetched is not None)
    check("keepalive advances the cursor on raced events",
          app4._stream_cursor == 9 and app4.fetched == "general",
          (repr(app4._stream_cursor), repr(app4.fetched)))

    # 5. keepalive start is idempotent (one interval, not two).
    app5 = _StreamApp()
    app5._start_stream_keepalive()
    first = app5._stream_tick_event
    app5._start_stream_keepalive()
    check("keepalive start is idempotent",
          first is not None and app5._stream_tick_event is first,
          repr(app5._stream_tick_event))


def main():
    for fn in (test_display_name_preserved,
               test_update_applied_once,
               test_attachment_kind_classifier,
               test_stream_lifecycle,
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
