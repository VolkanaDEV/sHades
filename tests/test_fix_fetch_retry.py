#!/usr/bin/env python3
"""H6: intermittent read timeouts must retry, and a later success must
clear the sticky error status.

Choice (documented): the retry lives in GuildAPI._request /
_request_json, behind a whitelist of idempotent read paths
(/api/messages, /api/v1/channels/list, /api/v1/whoami). One change
covers all three read paths; non-idempotent posts are never retried
because their paths are not whitelisted. Only a "timeout" error
retries (a dead link reports "link dead" and goes to re-establish
instead). _fetch_messages clears a stale "fetch failed" status when
a later fetch succeeds.

  (a) GuildAPI level, fake link: messages() fails once with a timeout
      then succeeds -- one retry, messages returned
  (b) same fake link: channels_list() and whoami() retry the same way
  (c) same fake link: post() is NEVER retried -- one link call even
      when it times out
  (d) app level, real GuildAPI + fake link: a pre-set "fetch failed"
      status is cleared and the messages render after the retry

Run: xvfb-run -a python tests/test_fix_fetch_retry.py
"""
import json
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-fix-fetch-retry-")
os.environ["HOME"] = TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402
import app_settings  # noqa: E402
from guild_api import GuildAPI  # noqa: E402
from RNS.vendor import umsgpack as msgpack  # noqa: E402

kc._app_data_dir = lambda: TMP
app_settings._app_data_dir = lambda: TMP

PASS, FAIL = [], []
MSGS = [{"msg_id": "retry-%d" % n, "author_hash": "c" * 32,
         "username": "carol", "content": "retried hello %d" % n,
         "ts": 1700000000.0 + n, "channel_id": "x" * 64}
        for n in range(2)]


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


class _NoResponse:
    """A receipt that never answers: _request treats a receipt with
    no `response` attribute as an instant timeout (no 10s wait)."""
    pass


class _Response:
    def __init__(self, payload):
        self.response = payload


class FakeLink:
    """link.request() stub: per-path scripted receipts, call counts."""

    def __init__(self):
        self.calls = {}  # path -> count
        self.status = None  # _check_dead: None means "not known dead"

    def request(self, path, data=None):
        self.calls[path] = self.calls.get(path, 0) + 1
        n = self.calls[path]
        if path == "/api/messages":
            if n == 1:
                return _NoResponse()
            return _Response(msgpack.packb(
                {"ok": 1, "messages": MSGS, "updates": []}))
        if path == "/api/v1/channels/list":
            if n == 1:
                return _NoResponse()
            return _Response(json.dumps(
                {"ok": 1, "channels": []}).encode("utf-8"))
        if path == "/api/v1/whoami":
            if n == 1:
                return _NoResponse()
            return _Response(json.dumps(
                {"ok": 1, "is_admin": False,
                 "is_moderator": False, "roles": []}).encode("utf-8"))
        if path == "/api/v1/receipts":
            return _Response(json.dumps({"ok": 1}).encode("utf-8"))
        if path == "/api/post":
            return _NoResponse()
        return _NoResponse()


def test_api_level():
    link = FakeLink()
    api = GuildAPI(None, link)
    res = api.messages("x" * 64)
    check("messages() retries a timeout and succeeds",
          res.get("ok") == 1 and len(res.get("messages", [])) == 2, res)
    check("messages() needed exactly 2 link calls (1 retry)",
          link.calls.get("/api/messages") == 2,
          link.calls.get("/api/messages"))
    res = api.channels_list()
    check("channels_list() retries a timeout and succeeds",
          res.get("ok") == 1, res)
    check("channels_list() needed exactly 2 link calls (1 retry)",
          link.calls.get("/api/v1/channels/list") == 2,
          link.calls.get("/api/v1/channels/list"))
    res = api.whoami()
    check("whoami() retries a timeout and succeeds",
          res.get("ok") == 1, res)
    check("whoami() needed exactly 2 link calls (1 retry)",
          link.calls.get("/api/v1/whoami") == 2,
          link.calls.get("/api/v1/whoami"))
    res = api.post("x" * 64, "never retry me")
    check("post() still fails on timeout", not res.get("ok"), res)
    check("post() is NEVER retried (1 link call)",
          link.calls.get("/api/post") == 1, link.calls.get("/api/post"))


class FetchRetryProbe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        self.root.current = "main"
        self.guild_api = GuildAPI(None, FakeLink())
        self._set_status("fetch failed: timeout")
        self._fetch_messages("general")
        # Poll for the render instead of a fixed delay: the worker +
        # Clock hop lands in ~1s on a fast box but software-GL layout
        # under Xvfb can starve the worker for a few seconds.
        self._check_deadline = time.time() + 10.0
        Clock.schedule_once(self.wait_render, 0.5)

    def wait_render(self, dt):
        if sorted(self._msg_widgets) == ["retry-0", "retry-1"] \
                or time.time() > self._check_deadline:
            self.check(dt)
        else:
            Clock.schedule_once(self.wait_render, 0.5)

    def check(self, dt):
        rendered = sorted(self._msg_widgets)
        check("messages render after the retry",
              rendered == ["retry-0", "retry-1"], rendered)
        try:
            status = self.root.get_screen("connect").ids.status.text
        except Exception as e:
            status = "<unreadable: %r>" % (e,)
        check("error status is cleared on the next success",
              status == "", repr(status))
        print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)), flush=True)
        self.stop()


def _watchdog():
    time.sleep(45)
    print("WATCHDOG: app did not exit; forcing exit", flush=True)
    os._exit(2)


if __name__ == "__main__":
    try:
        test_api_level()
    except Exception as e:
        check("api-level tests raised", False, repr(e))
    if FAIL:
        print()
        print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
        sys.exit(1)
    threading.Thread(target=_watchdog, daemon=True).start()
    Window.size = (1280, 720)
    probe = FetchRetryProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
