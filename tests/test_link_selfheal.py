#!/usr/bin/env python3
"""v0.31.2 link self-heal regression: a dead or risen-from-the-dead link
must never leave the client mute until restart (tay's exact symptom:
"cannot send chat messages until client is restarted").

Pins:

  1. GuildAPI._request_json on a non-ACTIVE link returns
     {"ok": 0, "error": "link dead"} and signals on_dead exactly once.
     Callers surface that as a visible failure instead of a worker
     thread dying silently in a receipt wait.

  2. A link.request() raise (RNS refuses a dead link) gives the same
     result through the on_dead path.

  3. A PLAIN object link (no .status attribute -- the legacy shape every
     pre-self-heal test stub uses) performs NO dead-check and reaches
     the transport as before. The self-heal must not rewire tests or
     stubs that never carried liveness state.
"""

import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RNS  # noqa: E402

import kivy_client as kc  # noqa: E402


class _DeadLink(object):
    """RNS-shaped link stuck CLOSED: status present, sends refused."""

    def __init__(self):
        self.status = RNS.Link.CLOSED
        self.asks = 0

    def request(self, *a, **k):
        self.asks += 1
        raise RuntimeError("refusing: link CLOSED")


def test_link_dead_reported_and_signalled():
    seen = []

    api = kc.GuildAPI(object(), _DeadLink(),
                      on_dead=lambda st: seen.append(("status", st)))

    res = api._request_json("/api/v1/post", {"text": "hello"})
    assert res.get("ok") == 0, res
    assert res.get("error") == "link dead", res
    assert len(seen) == 1, seen
    assert seen[0][0] == "status", seen

    # the dead check fires BEFORE the transport sees the call
    assert api.link.asks == 0, api.link.asks

    res2 = api._request("/api/v1/post", {"text": "hello"})
    assert res2.get("ok") == 0 and res2.get("error") == "link dead", res2
    assert len(seen) == 2, seen


def test_link_raise_reported_and_signalled():
    class _RaiseLink(object):
        status = RNS.Link.ACTIVE

        def request(self, *a, **k):
            raise RuntimeError("rns refused the call")

    seen = []
    api = kc.GuildAPI(object(), _RaiseLink(),
                      on_dead=lambda st: seen.append(st))
    res = api._request_json("/api/v1/post", {"text": "hello"})
    assert res.get("ok") == 0, res
    assert res.get("error") == "link dead", res
    assert seen == ["raise"], seen


def test_plain_object_link_untouched():
    """No .status attribute -> no dead-check -> request reaches transport.

    Every pre-self-heal stub (GuildAPI(reticulum=..., link=object()))
    depends on this: object() has no status attribute, and getattr must
    treat that as "liveness unknown", not "dead".
    """

    class _PlainLink(object):
        def request(self, *a, **k):
            return {"sent": True}

    seen = []
    api = kc.GuildAPI(object(), _PlainLink(),
                      on_dead=lambda st: seen.append(st))
    res = api._request_json("/api/v1/post", {"text": "hello"})
    # _request_json proceeds to the receipt-wait path; a dict receipt has
    # no .response, so the result is a timeout -- but it must NOT be
    # "link dead", and on_dead must not have fired.
    assert res.get("error") != "link dead", res
    assert seen == [], seen


def test_heal_never_nulls_healthy_router():
    """0.31.8 (boot.log receipt): _init_lxmf_router used to rebuild a HEALTHY
    router whenever the resume heal met it -- every SAF pick -- then crash
    its own rebuild with KeyError('Attempt to register an already
    registered destination.') and null the router in the except: the
    healer itself disabled DMs ("LXMF router init failed (DMs disabled)")
    boot after boot. A healthy router must be left alone.
    """

    app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    app.client_identity = object()
    router = object()
    delivery = object()
    app.lxmf_router = router
    app.lxmf_delivery = delivery

    app._init_lxmf_router()

    assert app.lxmf_router is router, "heal rebuilt a healthy router"
    assert app.lxmf_delivery is delivery, "heal rebuilt a healthy delivery"


def test_heal_rebuild_only_when_router_gone():
    """router None -> the rebuild path is reached (recovery still exists
    for the genuinely-dead case; the old boot-time behavior is intact).
    The rebuild attempt is observed by substituting the LXMF import.
    """

    import types

    built = {"n": 0}

    class _FakeRouter(object):
        def __init__(self, identity=None, storagepath=None):
            built["n"] += 1

        def register_delivery_identity(self, identity, display_name=None):
            return types.SimpleNamespace(hash=b"fake-delivery-hash")

        def register_delivery_callback(self, cb):
            pass

        def announce(self, *_a, **_k):
            pass

    fake = types.ModuleType("LXMF")
    fake.LXMRouter = _FakeRouter
    saved = sys.modules.get("LXMF")
    saved_appdir = getattr(kc, "_app_data_dir", None)
    sys.modules["LXMF"] = fake
    kc._app_data_dir = lambda: "/tmp/sHades-lxmf-test"
    built["n"] = 0
    try:
        app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
        app.client_identity = object()
        app.lxmf_router = None
        app.lxmf_delivery = None
        app.username = "tester"
        app._init_lxmf_router()
        assert built["n"] == 1, built
        assert app.lxmf_router is not None, "rebuild path dead in 0.31.8"
    finally:
        if saved is not None:
            sys.modules["LXMF"] = saved
        else:
            sys.modules.pop("LXMF", None)
        if saved_appdir is not None:
            kc._app_data_dir = saved_appdir


def test_blob_upload_retries_timeout_then_posts():
    """0.31.8 (boot.log receipt): every attach died at the first blob_put
    that straddled the ~5 s SAF-picker socket gap ([Errno 103] -> RNS
    reconnect), then fell through to the legacy inline post, which also
    failed ("failed with status: 07"). Chunked uploads are offset-addressed
    and idempotent, so the flow retries; a success posts by reference and
    the legacy inline post is never reached.
    """

    class _Fakes(object):
        pass

    app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    calls = {"blob": [], "post_v1": [], "legacy": [], "sleep": [],
             "up_active": []}

    main_screen = _Fakes()
    main_screen._current_channel = "general"
    main_screen.ids = _Fakes()
    main_screen.ids.message_grid = object()
    main_screen.ids.msg_input = _Fakes()

    class _Root(object):
        def get_screen(self, *_a, **_k):
            return main_screen

    app.root = _Root()
    app._channel_cid = lambda cid: cid
    app._set_status = lambda msg: None
    app.my_hash = "aa" * 16
    app.username = "tester"
    app._add_to_history = lambda m: None
    app._save_history = lambda: None
    app._render_message = lambda *a, **k: None
    app._save_attachment_blob = lambda blob, name: "/tmp/fake"

    class _API(object):
        link = object()  # plain stub: liveness unknown -> not dead

        def blobs_put(self, name, mime, blob):
            calls["blob"].append(name)
            calls["up_active"].append(app._up_active)
            if len(calls["blob"]) < 3:
                return {"ok": 0, "error": "timeout"}
            return {"ok": 1, "blob_id": "b1"}

        def post_v1(self, cid, text, attachments=None):
            calls["post_v1"].append(attachments)
            return {"ok": 1, "msg_id": "m1",
                    "attachment": {"path": "/file/attachments/x.bin"}}

        def post(self, cid, text, attachments=None):
            calls["legacy"].append(True)
            return {"ok": 1}

    app.guild_api = _API()

    real_threading = kc.threading
    real_time = kc.time

    import types as _t

    shim_threading = _t.ModuleType("threading")

    class _SyncThread(object):
        def __init__(self, target=None, daemon=False, *a, **k):
            self._target = target

        def start(self):
            self._target()

    shim_threading.Thread = _SyncThread
    shim_time = _t.ModuleType("time")

    shim_time.time = lambda *_a, **_k: real_time.time()
    shim_time.sleep = lambda s: calls["sleep"].append(s)
    kc.threading = shim_threading
    kc.time = shim_time
    try:
        app._send_with_attachment("hi", "f.bin", "application/octet-stream",
                                  b"data", kc.ATT_SERVER)
    finally:
        kc.threading = real_threading
        kc.time = real_time

    assert len(calls["blob"]) == 3, calls
    assert all(calls["up_active"]), "0.31.4 upload gate dropped on retry"
    assert len(calls["post_v1"]) == 1, calls
    assert calls["legacy"] == [], "legacy inline post ran on a server-stored hop"
    assert calls["sleep"], "no backoff between retries"
    for s in calls["sleep"]:
        assert 0 < s <= 8.0, calls["sleep"]


def test_blob_retry_never_inlines_big_blob_on_timeout():
    """Retries exhausted with the link still unresolvable -> the worker must
    NOT push the full blob inline (the receipted 7 MB failure mode); it
    surfaces a status instead.
    """

    app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    calls = {"blob": 0, "post_v1": 0, "legacy": 0}

    class _Fakes(object):
        pass

    main_screen = _Fakes()
    main_screen._current_channel = "general"
    main_screen.ids = _Fakes()

    class _Root(object):
        def get_screen(self, *_a, **_k):
            return main_screen

    app.root = _Root()
    app._channel_cid = lambda cid: cid
    app._set_status = lambda msg: calls.setdefault("status", []).append(msg)
    app.my_hash = "aa" * 16
    app.username = "tester"

    class _DeadLink(object):
        status = RNS.Link.CLOSED

    class _API(object):
        link = _DeadLink()

        def blobs_put(self, name, mime, blob):
            calls["blob"] += 1
            return {"ok": 0, "error": "timeout"}

        def post_v1(self, *a, **k):
            calls["post_v1"] += 1
            return {"ok": 0}

        def post(self, *a, **k):
            calls["legacy"] += 1
            return {"ok": 1}

    app.guild_api = _API()

    real_threading = kc.threading
    real_time = kc.time
    real_clock = kc.Clock

    class _NowClock(object):
        @staticmethod
        def schedule_once(cb, dt=0):
            cb(dt)

    shim = types.ModuleType("shim-threading")

    class _SyncThread(object):
        def __init__(self, target=None, daemon=False):
            self._target = target

        def start(self):
            self._target()

    shim.Thread = _SyncThread
    kc.threading = shim
    saves = []
    shim_time = types.ModuleType("shim-time")
    shim_time.time = lambda *_a, **_k: 0.0
    shim_time.sleep = saves.append
    kc.time = shim_time
    kc.Clock = _NowClock
    try:
        app._send_with_attachment("hi", "f.bin", "application/octet-stream",
                                  b"data", kc.ATT_SERVER)
    finally:
        kc.threading = real_threading
        kc.time = real_time
        kc.Clock = real_clock

    assert calls["blob"] == 1, "retried against a dead link"
    assert calls["legacy"] == 0, "inline blob pushed at a dead link"
    assert calls["post_v1"] == 0, calls
    assert any("timed out" in m for m in calls.get("status", [])), calls


def test_wait_path_mtu_bounded_and_uses_live_path():
    """0.31.8 (boot.log receipt): re-establishes ran with an invalidated
    path, RNS signed the DEFAULT 500 B MTU, the server confirmed 500 B and
    resources failed ("status: 07"). The establish path must wait, bounded,
    for a next-hop interface that exposes its hardware MTU.
    """

    seq = {"n": 0}
    waited = {"n": 0}

    def _fake_hw_mtu(_hash_bytes):
        seq["n"] += 1
        if seq["n"] < 3:
            return None
        return 8192

    real_transport = RNS.Transport
    real_time = kc.time
    shim_time = types.ModuleType("shim-time")
    shim_time.time = lambda *_a, **_k: real_time.time()
    shim_time.sleep = lambda s: waited.__setitem__("n", waited["n"] + 1)
    patched = type("ShimTransport", (), {})()
    patched.next_hop_interface_hw_mtu = staticmethod(_fake_hw_mtu)
    RNS.Transport = patched
    kc.time = shim_time
    try:
        ok = kc.sHadesClientApp._wait_path_mtu(bytes.fromhex("11" * 16),
                                               tries=5)
    finally:
        RNS.Transport = real_transport
        kc.time = real_time

    assert ok, "live path existed after 3 polls; wait gave up early"
    assert seq["n"] == 3, seq
    assert waited["n"] == 2, waited


def test_wait_path_mtu_bounded_gives_up():
    """No path ever arrives -> bounded wait, then False (never worse than
    the old unconditional establish; the establish attempts still run)."""

    waited = {"n": 0}

    def _no_path(_h):
        return None

    real_transport = RNS.Transport
    real_time = kc.time
    shim_time = types.ModuleType("shim-time")
    shim_time.time = lambda *_a, **_k: real_time.time()
    shim_time.sleep = lambda s: waited.__setitem__("n", waited["n"] + 1)
    patched = type("ShimTransport", (), {})()
    patched.next_hop_interface_hw_mtu = staticmethod(_no_path)
    RNS.Transport = patched
    kc.time = shim_time
    try:
        ok = kc.sHadesClientApp._wait_path_mtu(bytes.fromhex("22" * 16),
                                               tries=3)
    finally:
        RNS.Transport = real_transport
        kc.time = real_time

    assert ok is False, "no path arrived"
    assert waited["n"] == 3, waited


def main():
    test_link_dead_reported_and_signalled()
    test_link_raise_reported_and_signalled()
    test_plain_object_link_untouched()
    test_heal_never_nulls_healthy_router()
    test_heal_rebuild_only_when_router_gone()
    test_blob_upload_retries_timeout_then_posts()
    test_blob_retry_never_inlines_big_blob_on_timeout()
    test_wait_path_mtu_bounded_and_uses_live_path()
    test_wait_path_mtu_bounded_gives_up()
    print("PASS: 9  FAIL: 0")


if __name__ == "__main__":
    main()
