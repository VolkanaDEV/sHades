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


def main():
    test_link_dead_reported_and_signalled()
    test_link_raise_reported_and_signalled()
    test_plain_object_link_untouched()
    print("PASS: 3  FAIL: 0")


if __name__ == "__main__":
    main()
