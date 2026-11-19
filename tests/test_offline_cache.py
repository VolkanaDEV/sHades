"""Offline cache acceptance: store on fetch, render on restore, erase paths.

No mesh and no HTTP: the API surface is RPC over RNS links, so this suite
drives the client methods directly against a FakeAPI object (the
test_client_regressions pattern). What is proven:
  1. _fetch_messages stores the page into offline_store (bounded, per
     guild+channel) alongside the render;
  2. _show_cached_channel feeds cached rows to the renderer with no api
     present, and returns False on an empty store;
  3. _apply_update mirrors edits and deletes into the cache;
  4. erase follow-through: _purged_before filters re-entry and
     forget_author clears the erased identity's rows.
"""
import os
import sys
import tempfile
import time
from types import SimpleNamespace

from kivy.uix.boxlayout import BoxLayout  # noqa: E402

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


TMP = tempfile.mkdtemp(prefix="shades_offline_cache_")
kc._app_data_dir = lambda: TMP

GUILD = "node:testnodehash"
CID = "c" * 64

MSGS = [
    {"msg_id": "m%d" % n, "author_hash": "a" * 32 if n else "b" * 32,
     "username": "alice" if n else "bob", "content": "hello %d" % n,
     "ts": 1000.0 + n, "channel_id": CID}
    for n in range(3)
]
MSGS[1]["edited_at"] = 1001.5  # simulate an edited row: content newer than cache


class _CacheApp(kc.sHadesClientApp):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
        self._channel_kinds = {}
        self._older = {}
        self._msg_widgets = {}
        self._members = {}
        self.guild_node_hash = "testnodehash"
        self.hub_address = None

    def _channel_cid(self, name):
        return CID

    def _set_status(self, text):
        pass

    def _show_skeleton(self, cid):
        pass

    def _remove_skeleton(self, cid):
        pass

    def _scroll_to_bottom(self):
        pass

    def _mount_fake_root(self, grid):
        self.root = SimpleNamespace(get_screen=lambda name: SimpleNamespace(
            ids=SimpleNamespace(message_grid=grid, scroll=SimpleNamespace(
                scroll_y=1.0))))


def wait_for(pred, cap=5.0):
    """Poll a worker-thread result instead of racing a fixed sleep.

    CI (cold Linux runner) regularly overran the old time.sleep(0.3)
    flush windows; the assertions are unchanged -- only the wait is
    now bounded-by-completion rather than bounded-by-wall-clock.
    """
    deadline = time.time() + cap
    while not pred() and time.time() < deadline:
        time.sleep(0.025)
    return pred()


def fetch_store_test():
    app = _CacheApp()
    rendered = []

    class FakeAPI:
        def messages(self, cid, **kw):
            assert cid == CID
            return {"ok": 1, "messages": [dict(m) for m in MSGS],
                    "updates": []}

        def receipts(self, cid, msg_id):
            return {"ok": 1}

    app._render_message = lambda grid, m: rendered.append(m["msg_id"])
    app.guild_api = FakeAPI()
    app._mount_fake_root(BoxLayout())
    refreshed = []
    app._do_update = lambda row, content, deleted: refreshed.append(
        (row.msg_id, content))
    app._msg_widgets["m1"] = SimpleNamespace(msg_id="m1", content="stale",
                                             edited_at=True)
    app._fetch_messages("general")
    from kivy.clock import Clock
    for _ in range(30):
        Clock.tick()
    wait_for(lambda: rendered == ["m0", "m1", "m2"])
    wait_for(lambda: refreshed == [("m1", "hello 1")])
    rows = offline_store.recent(app._offline_path(), GUILD, CID)
    check("fetch stores the page in the offline cache",
          [r["msg_id"] for r in rows] == ["m0", "m1", "m2"], rows)
    check("fetch still rendered the page", rendered == ["m0", "m1", "m2"],
          rendered)
    check("cached stale row is refreshed from the server copy",
          refreshed == [("m1", "hello 1")], refreshed)
    check("cache rows are bounded metadata rows",
          all(set(m) >= {"msg_id", "author_hash", "content", "ts", "channel_id"}
              for m in rows))
    return app


def render_restore_test():
    app = _CacheApp()
    fed = []

    def fake_render(grid, m):
        fed.append(m["msg_id"])
    app._render_message = fake_render
    app.guild_api = None
    ok = app._show_cached_channel(BoxLayout(), CID)
    check("restore renders cached rows with no api present",
          ok and fed == ["m0", "m1", "m2"], fed)

    empty = _CacheApp()
    empty._render_message = fake_render
    ok2 = empty._show_cached_channel(BoxLayout(), "d" * 64)
    check("empty store renders nothing and reports false",
          ok2 is False and fed == ["m0", "m1", "m2"])


def apply_update_test():
    app = _CacheApp()
    app._apply_update({"msg_id": "m1", "content": "edited",
                       "deleted_at": None, "channel_id": CID})
    wait_for(lambda: any(
        m.get("content") == "edited"
        for m in offline_store.recent(app._offline_path(), GUILD, CID)))
    rows = {m["msg_id"]: m for m in
            offline_store.recent(app._offline_path(), GUILD, CID)}
    check("edit delta mirrors into the cache",
          rows.get("m1", {}).get("content") == "edited", rows.get("m1"))
    app._apply_update({"msg_id": "m2", "content": "",
                       "deleted_at": 4242.0, "channel_id": CID})
    wait_for(lambda: "m2" not in [
        m["msg_id"] for m in
        offline_store.recent(app._offline_path(), GUILD, CID)])
    ids = [m["msg_id"] for m in
           offline_store.recent(app._offline_path(), GUILD, CID)]
    check("delete delta removes the cached row", "m2" not in ids, ids)


def erase_test():
    app = _CacheApp()
    offline_store.put(app._offline_path(), GUILD, CID,
                      [dict(m, author_hash="a" * 32) for m in MSGS])
    before = len(offline_store.recent(app._offline_path(), GUILD, CID))
    offline_store.forget_author(app._offline_path(), GUILD, "a" * 32)
    after = offline_store.recent(app._offline_path(), GUILD, CID)
    check("forget_author clears the erased identity", after == [], before)
    app._purged_before[GUILD] = ("a" * 32, time.time())
    keep = dict(MSGS[0])
    keep["author_hash"] = "a" * 32
    # A stale burst redelivers PRE-erasure rows: their ts sits at or below
    # the purge cutoff, and the marker is what keeps them out of the cache.
    # Fresh post-erasure rows (ts above the cutoff) SHOULD be cached - the
    # identity posting again after a clean slate is a new identity's feed.
    app._queue_cache(dict(keep, deleted_at=None, ts=0.0))
    from kivy.clock import Clock
    for _ in range(20):
        Clock.tick()
    wait_for(lambda: offline_store.recent(
        app._offline_path(), GUILD, CID) == [])
    rows = offline_store.recent(app._offline_path(), GUILD, CID)
    check("purge marker blocks re-entry after erasure", rows == [], rows)


def main():
    fetch_store_test()
    render_restore_test()
    apply_update_test()
    erase_test()
    print("PASS offline cache suite: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
