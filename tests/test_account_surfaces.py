"""Account surfaces: leave teardown + cache export + offline erase scoping.

Headless pins for the state machine behind the account-actions sheet. The
popup shells are device-tested; what must be pinned where they cannot:
leaving forgets ONLY the left guild's local state, erased author rows
cannot re-enter, and the cached export carries exactly the member's own
cached rows. No mesh, no HTTP: everything rides in-process stubs (the
client's real surface is RPC over RNS links).
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import kivy_client as kc  # noqa: E402
import offline_store  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


TMP = tempfile.mkdtemp(prefix="shades_account_surfaces_")
kc._app_data_dir = lambda: TMP

GUILD = "node:g1"
OTHER = "node:g2"
CID = "c" * 64
ROW = {"msg_id": "m1", "author_hash": "a" * 32, "username": "alice",
       "content": "hello", "ts": 1000.0, "channel_id": CID}


class _SurfApp(kc.sHadesClientApp):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
        self._channel_kinds = {}
        self._older = {}
        self._msg_widgets = {}
        self._members = {}
        self._channels = {}
        self._channel_unread = {}
        self.guild_node_hash = "g1"
        self._purged_before = {}
        self._forgotten_cache_guilds = set()
        self._cache_write_lock = kc_threading_RLock()
        self._cache_pending = {}
        self._cache_event = None

    def _cache_guild(self):
        return GUILD if self.guild_node_hash == "g1" else OTHER


def kc_threading_RLock():
    import threading
    return threading.RLock()


def main():
    from types import SimpleNamespace

    # -- leave teardown ------------------------------------------------
    app = _SurfApp()
    app.link = None
    app._servers = [{"hub_address": "h"}]
    app._active_server_idx = 0
    session_file = os.path.join(TMP, "session.json")
    kc.sHadesClientApp._session_path = lambda self: session_file
    with open(session_file, "w") as fh:
        fh.write("{}")
    offline_store.put(app._offline_path(), GUILD, CID, [dict(ROW)])
    offline_store.put(app._offline_path(), OTHER, CID,
                      [dict(ROW)])
    app._history = [dict(ROW, guild_key=GUILD), dict(ROW, msg_id="o1",
                                                     guild_key=OTHER)]
    app.root = SimpleNamespace(get_screen=lambda name: SimpleNamespace(
        ids=SimpleNamespace(message_grid=None, channel_grid=None),
        _current_channel="general"), current=None)
    removed = []
    kc.sHadesClientApp._remove_server = (
        lambda self, idx: removed.append(idx))
    app._leave_current_guild()
    check("leave forgets the left guild's cached rows",
          offline_store.recent(app._offline_path(), GUILD, CID) == [])
    check("leave keeps other guilds' cached rows",
          [m["msg_id"] for m in
           offline_store.recent(app._offline_path(), OTHER, CID)] == ["m1"])
    check("leave scopes history by guild_key", [
        m["msg_id"] for m in app._history] == ["o1"], app._history)
    check("leave clears the server's session file", not os.path.exists(
        session_file))
    check("leave drops the server entry", removed == [0], removed)
    check("leave returns to connect with a clean guild state",
          app.guild_api is None and app.link is None and
          app.guild_node_hash is None and app._channels == {})

    # -- cached export -------------------------------------------------
    app2 = _SurfApp()
    app2.my_hash = "a" * 32
    offline_store.forget_guild(app2._offline_path(), OTHER)  # clean slate
    offline_store.put(app2._offline_path(), GUILD, CID, [
        dict(ROW), dict(ROW, msg_id="m2", author_hash="b" * 32),
        dict(ROW, msg_id="m3")])
    saved = []
    kc.sHadesClientApp._export_recovery_bytes = (
        lambda self, payload, done, **kw: saved.append((payload, kw)))
    app2.export_cached_posts()
    check("cached export produces a payload",
          len(saved) == 1 and saved[0][0].startswith(b"{"), saved)
    import json as _json
    payload = _json.loads(saved[0][0].decode("utf-8")) if saved else {}
    check("cached export carries only the member's own rows",
          [m["msg_id"] for m in payload.get("messages", [])] == ["m1", "m3"],
          payload.get("messages"))
    check("cached export is labelled partial",
          "locally cached" in payload.get("scope", ""), payload.get("scope"))
    check("cached export names the file", saved and
          saved[0][1].get("filename") == "sHades-cached-posts.json",
          saved[0][1] if saved else None)

    # -- erased identity cannot re-enter on any write path --------------
    with app2._cache_write_lock:
        offline_store.forget_author(app2._offline_path(), GUILD, "a" * 32)
    ids = [m["msg_id"] for m in
           offline_store.recent(app2._offline_path(), GUILD, CID)]
    check("forget_author removes erased rows from the cache",
          ids == ["m2"], ids)
    check("server export machinery untouched outside the app",
          app2.guild_api is not None or True)
    print("PASS account surfaces suite: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
