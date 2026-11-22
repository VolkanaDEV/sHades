#!/usr/bin/env python3
"""Guild API client — the RNS-link wrapper for the sHades guild node.

Carved out of kivy_client.py (client split, step 1). GuildAPI is a thin
wrapper around an RNS Link: it speaks the guild's /api/* endpoints
(msgpack) for messages, state, avatars and attachments. It takes only
what it needs — the reticulum instance, the link, and optional callbacks
(on_error, on_dead, log) — and never touches the UI, so it stays
import-safe for unit tests (only stdlib + RNS).

kivy_client.py keeps a thin re-export (`from guild_api import GuildAPI`)
so existing names keep working: kc.GuildAPI, and the two construction
sites in sHadesClientApp (which pass log=dlog).
"""

import base64
import json
import time

import RNS
from RNS.vendor import umsgpack as msgpack


class GuildAPI:
    """Thin wrapper around an RNS Link to the sHades guild node."""

    def __init__(self, reticulum, link, on_error=None, on_dead=None,
                 log=None):
        self.reticulum = reticulum
        self.link = link
        self.on_error = on_error or (lambda *a: None)
        # log is injected (kivy_client passes dlog): guild_api must
        # not import kivy_client for its DEBUG-gated logger.
        self._log = log or (lambda *a: None)
        # v0.31.2 link self-heal: called when a request lands on a link
        # that is no longer ACTIVE (or raises) -- the app re-establishes
        # and re-primes in one place instead of dying until restart.
        self.on_dead = on_dead or (lambda *a: None)

    def _check_dead(self):
        st = getattr(self.link, "status", None)
        if st is not None and st != RNS.Link.ACTIVE:
            try:
                self.on_dead(st)
            except Exception:
                pass
            return True
        return False

    # H6: idempotent reads retried on timeout (once or twice, short
    # backoff). A burst-timeout (~5-10% under load) must not surface
    # as a sticky "fetch failed" with no retry, and a POST must never
    # be re-issued -- it is not idempotent. The whitelist is the
    # complete set of retried paths; everything else (posts, edits,
    # deletes, register, avatar upload, ...) attempts exactly once.
    _READ_RETRY_PATHS = frozenset((
        "/api/messages",          # _fetch_messages
        "/api/v1/channels/list",  # _refresh_channels_from_guild
        "/api/v1/whoami",         # _fetch_whoami
    ))
    _READ_RETRIES = 2
    _READ_RETRY_BACKOFF = (0.3, 0.6)

    def _request(self, path, data, timeout=10):
        if path in self._READ_RETRY_PATHS:
            for attempt in range(self._READ_RETRIES + 1):
                res = self._request_once(path, data, timeout)
                if res.get("ok") or res.get("error") != "timeout":
                    return res
                if attempt < self._READ_RETRIES:
                    time.sleep(self._READ_RETRY_BACKOFF[attempt])
            return res
        return self._request_once(path, data, timeout)

    def _request_once(self, path, data, timeout=10):
        if not isinstance(data, dict):
            data = {}
        if self._check_dead():
            return {"ok": 0, "error": "link dead"}
        try:
            receipt = self.link.request(path, data=data)
        except Exception as e:
            print("request on link failed: %r" % (e,))
            try:
                self.on_dead("raise")
            except Exception:
                pass
            return {"ok": 0, "error": "link dead"}
        # link.request() may return a Receipt object (with .response) or
        # True/False (bool) depending on RNS version. Handle both.
        if isinstance(receipt, bool):
            if not receipt:
                return {"ok": 0, "error": "request failed"}
            # v0.26: the old fallback RE-ISSUED the same call with empty
            # data "to poll for a response" -- which double-sent posts
            # and re-ran side-effecting calls (register, avatar). A bool
            # receipt carries nothing pollable; report unconfirmed and
            # let the caller decide.
            return {"ok": 1, "unconfirmed": True}
        deadline = time.time() + timeout
        while (hasattr(receipt, "response")
               and receipt.response is None
               and time.time() < deadline):
            time.sleep(0.05)
        if not hasattr(receipt, "response") or receipt.response is None:
            return {"ok": 0, "error": "timeout"}
        try:
            return msgpack.unpackb(receipt.response, raw=False)
        except Exception as e:
            return {"ok": 0, "error": "bad response: %s" % e}

    def state(self, channel):
        return self._request("/api/state", {"channel": channel})

    def messages(self, channel, since_id=None, since_ts=None, before=None, limit=50):
        data = {"channel": channel, "limit": limit}
        if since_id is not None:
            data["since_id"] = since_id
        if since_ts is not None:
            data["since_ts"] = since_ts
        if before is not None:
            data["before"] = before
        return self._request("/api/messages", data)

    def post(self, channel, content, attachments=None):
        data = {"channel": channel, "content": content}
        if attachments:
            data["attachments"] = attachments
        return self._request("/api/post", data)

    def edit(self, msg_id, content):
        return self._request("/api/edit", {"msg": msg_id, "content": content})

    def delete(self, msg_id):
        return self._request("/api/delete", {"msg": msg_id})

    def register_node(self, node_hash):
        """Register this client's node destination hash with the guild index.

        v0.29: a ONE-SHOT directory write at connect (and on reconnect).
        Presence is not this call any more: the guild derives presence from
        link liveness (RNS keepalives and teardowns), so the client holds no
        heartbeat of its own. RNS is cryptographically addressed — members
        reach a node by its destination hash (Identity.recall + RNS.Link),
        never by ip:port, so address/port left the protocol. No identity is
        asserted in the payload — the guild derives the user hash from the
        identity this link was identified with, so it cannot be forged.
        """
        return self._request("/api/register_node", {"node_hash": node_hash})

    def lookup_node(self, node_hash):
        return self._request("/api/lookup_node", {"node_hash": node_hash})

    def register(self, username, profile_node=""):
        return self._request("/api/register",
                             {"username": username, "profile_node": profile_node})

    def avatar_upload(self, blob, timeout=30):
        # v0.26: timeout raised from 10s -- the avatar payload moves as
        # an RNS resource transfer and slower links need the headroom.
        return self._request("/api/avatar", {"avatar": blob}, timeout=timeout)

    def avatar_get(self, user_hash):
        return self._request("/api/avatar_get", {"user": user_hash})

    def nodes(self):
        """Fetch the current active node registry from the guild."""
        return self._request("/api/nodes", {})

    def presence(self):
        """Fetch the active user list — who is currently pinging the guild.

        Stale clients are absent from the response, not flagged, so a caller
        that mirrors this list is pruned automatically.
        """
        return self._request("/api/presence", {})

    # ---- JSON API v1 (json bytes over the same RNS link; no HTTP anywhere) ----

    def _request_json(self, path, data, timeout=10.0):
        if path in self._READ_RETRY_PATHS:
            for attempt in range(self._READ_RETRIES + 1):
                res = self._request_json_once(path, data, timeout)
                if res.get("ok") or res.get("error") != "timeout":
                    return res
                if attempt < self._READ_RETRIES:
                    time.sleep(self._READ_RETRY_BACKOFF[attempt])
            return res
        return self._request_json_once(path, data, timeout)

    def _request_json_once(self, path, data, timeout=10.0):
        payload = json.dumps(data if data is not None else {}).encode("utf-8")
        if self._check_dead():
            return {"ok": 0, "error": "link dead"}
        try:
            receipt = self.link.request(path, data=payload)
        except Exception as e:
            print("json request on link failed: %r" % (e,))
            try:
                self.on_dead("raise")
            except Exception:
                pass
            return {"ok": 0, "error": "link dead"}
        # link.request() may return a Receipt object or True/False depending
        # on the RNS version (see _request). Re-issue with the SAME payload.
        if isinstance(receipt, bool):
            if not receipt:
                return {"ok": 0, "error": "request failed"}
            receipt = self.link.request(path, data=payload)
        deadline = time.time() + timeout
        while (hasattr(receipt, "response")
               and receipt.response is None
               and time.time() < deadline):
            time.sleep(0.05)
        if not hasattr(receipt, "response") or receipt.response is None:
            # v0.31.1 diag: a deadline without a response means different
            # things -- "the wire transfer died" (receipt concluded FAILED)
            # vs "still moving, we gave up early" (receipt unsettled; the
            # resource outlives us client-side and RNS's watchdog owns
            # it). Distinguish, do not guess.
            try:
                st = getattr(receipt, "status", None)
                if st is not None:
                    return {"ok": 0, "error": "timeout",
                            "receipt_state": str(st)}
            except Exception:
                pass
            return {"ok": 0, "error": "timeout"}
        try:
            return json.loads(receipt.response.decode("utf-8"))
        except Exception as e:
            return {"ok": 0, "error": "bad response: %s" % e}

    def channels_list(self):
        return self._request_json("/api/v1/channels/list", {})

    def channels_create(self, name, kind="chat", manifest=None):
        data = {"name": name, "kind": kind}
        if manifest is not None:
            data["manifest"] = manifest
        return self._request_json("/api/v1/channels/create", data)

    def channels_reorder(self, order):
        return self._request_json("/api/v1/channels/reorder",
                                  {"order": order})

    def channels_info(self, channel):
        return self._request_json("/api/v1/channels/info", {"channel": channel})

    def stream_subscribe(self, channel, cursor=None):
        data = {"channel": channel}
        if cursor is not None:
            data["cursor"] = cursor
        return self._request_json("/api/v1/stream/subscribe", data)

    def stream_poll(self, cursor=None, timeout_s=4.0):
        data = {"timeout_s": timeout_s}
        if cursor is not None:
            data["cursor"] = cursor
        # server may hold the poll for timeout_s; allow RTT on top
        return self._request_json("/api/v1/stream/poll", data, timeout=timeout_s + 8)

    def stream_unsubscribe(self):
        return self._request_json("/api/v1/stream/unsubscribe", {})

    def typing(self, channel, active=True):
        return self._request_json("/api/v1/typing",
                                  {"channel": channel, "active": bool(active)})

    def presence_v1(self):
        """Active users plus the channel each one is streaming."""
        return self._request_json("/api/v1/presence", {})

    def blobs_put(self, name, mime, data, on_progress=None):
        """Upload one content-addressed blob (v1). Returns
        {ok, blob_id, size, path} — reference blob_id from post_v1.

        Chunked automatically (v0.29-c, tay: "The payload should be maximum
        of 10mb & automatically chunked, not timer based"): init -> byte-
        offset slices -> finish, the loop driven by each ack's next_offset.
        Retries rewrite the same offset, so they cannot corrupt the stream.
        Payloads above the limit fail fast with a named error.

        v0.32 mobile receipts: uploads timed out on the phone while the
        identical code sat fine on desktop (receipt: tay, "Neither one
        works"). The chunk deadline now matches the download doctrine
        (15s), and a receipt timeout shrinks the chunk and resumes from
        the last acked offset -- the server stores offset-addressed bytes
        (seek+write), so resuming rewrites bytes it already has instead of
        corrupting anything. Bounded: six shrink steps, then up to three
        retries at the floor; an aged-out staged handle re-inits once."""
        size = len(data)
        if size > 10 * 1024 * 1024:
            return {"ok": 0, "error": "payload too large: limit is 10 MB",
                    "size": size}
        res = self._request_json("/api/v1/blobs/put/init",
                                 {"name": name, "mime": mime, "size": size},
                                 timeout=15)
        if not res.get("ok"):
            res.setdefault("phase", "init")
            return res
        upload_id = res["upload_id"]
        step = int(res.get("chunk_size") or (256 * 1024))
        offset = 0
        fails = 0
        restarted = False
        while offset < size:
            piece = data[offset:offset + step]
            body = {
                "upload_id": upload_id, "offset": offset,
                "data_b64": base64.b64encode(piece).decode("ascii"),
            }
            if on_progress:
                on_progress(offset, size)
            res = self._request_json("/api/v1/blobs/put/chunk", body,
                                     timeout=15)
            if not res.get("ok"):
                err = res.get("error", "chunk failed")
                # the staged handle aged out mid-upload (600s idle law):
                # one deterministic restart, offsets driven anew
                if err == "unknown upload_id" and not restarted:
                    restarted = True
                    res2 = self._request_json(
                        "/api/v1/blobs/put/init",
                        {"name": name, "mime": mime, "size": size},
                        timeout=15)
                    if res2.get("ok"):
                        upload_id = res2["upload_id"]
                        continue
                # receipt timeout on a slow/high-RTT pipe: shrink the chunk
                # and resume from the last acked offset (bounded)
                if err == "timeout" and fails < 6 and step > 32 * 1024:
                    fails += 1
                    step = max(32 * 1024, step // 2)
                    res["phase"] = "chunk@%d/%d" % (offset, step)
                    continue
                if err == "timeout" and fails < 3:
                    fails += 1
                    continue
                print("blobs_put FAIL off=%s step=%s err=%s state=%s"
                      % (offset, step, err, res.get("receipt_state")))
                res.setdefault("error", err or "chunk failed")
                res["phase"] = "chunk@%d" % offset
                return res
            fails = 0
            offset = int(res.get("next_offset") or (offset + len(piece)))
        res = self._request_json("/api/v1/blobs/put/finish",
                                 {"upload_id": upload_id}, timeout=15)
        if not res.get("ok"):
            res.setdefault("error", "finish failed")
            res["phase"] = "finish"
        elif on_progress:
            on_progress(size, size)
        return res

    def post_v1(self, channel, content, attachments=None):
        """JSON chat post; attachments are [{blob_id, name?, mime?}] refs or
        inline {name, mime, data_b64} dicts."""
        data = {"channel": channel, "content": content}
        if attachments:
            data["attachments"] = attachments
        return self._request_json("/api/v1/post", data, timeout=30)

    def blobs_get(self, blob_id, offset=0, length=None):
        """Chunked blob read; returns {data_b64, total, offset, eof}."""
        data = {"blob_id": blob_id, "offset": offset}
        if length is not None:
            data["len"] = length
        return self._request_json("/api/v1/blobs/get", data)

    def att_get(self, path, offset=0, length=None):
        """Chunked attachment read (v0.31). Returns {data_b64, total,
        offset, eof} for a /file/attachments/... path — the same slice
        machinery as blobs_get, keyed by the path the server puts in
        message metadata."""
        data = {"path": path, "offset": offset}
        if length is not None:
            data["len"] = length
        return self._request_json("/api/v1/att/get", data, timeout=15)

    def site_get(self, channel, path, offset=0, length=None):
        """Chunked site resource read (micron pages, images, media)."""
        data = {"channel": channel, "path": path, "offset": offset}
        if length is not None:
            data["len"] = length
        return self._request_json("/api/v1/site/get", data)

    def site_put(self, channel, path, data_b64, mime=None):
        """Store one site resource (P7b: moderator or channel SITE_EDIT)."""
        data = {"channel": channel, "path": path, "data_b64": data_b64}
        if mime:
            data["mime"] = mime
        return self._request_json("/api/v1/site/put", data, timeout=15)

    def site_manifest(self, channel):
        return self._request_json("/api/v1/site/manifest", {"channel": channel})

    def term_open(self, channel):
        """Open a guild console session on a term channel."""
        return self._request_json("/api/v1/term/open", {"channel": channel},
                                  timeout=15)

    def term_data(self, session, text):
        """Feed one line (or fragment) of console input."""
        return self._request_json("/api/v1/term/data", {
            "session": session,
            "data_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        })

    def term_close(self, session, timeout=10.0):
        return self._request_json("/api/v1/term/close",
                                  {"session": session}, timeout=timeout)

    def receipts(self, channel, msg_id=None):
        """Read watermark: mark up to msg_id, or report {last_read_id, unread}."""
        data = {"channel": channel}
        if msg_id:
            data["msg_id"] = msg_id
        return self._request_json("/api/v1/receipts", data)

    def react(self, msg_id, emoji):
        """Toggle a reaction (ASCII shortcode)."""
        return self._request_json("/api/v1/react",
                                  {"msg": msg_id, "emoji": emoji})

    def search(self, q, channel=None, limit=None):
        data = {"q": q}
        if channel:
            data["channel"] = channel
        if limit:
            data["limit"] = limit
        return self._request_json("/api/v1/search", data)

    def whoami(self):
        return self._request_json("/api/v1/whoami", {})

    def mod_ban(self, user_hash, reason=""):
        return self._request_json("/api/v1/mod/ban",
                                  {"user": user_hash, "reason": reason})

    def mod_unban(self, user_hash):
        return self._request_json("/api/v1/mod/unban", {"user": user_hash})

    def mod_mute(self, user_hash, reason=""):
        return self._request_json("/api/v1/mod/mute",
                                  {"user": user_hash, "reason": reason})

    def mod_unmute(self, user_hash):
        return self._request_json("/api/v1/mod/unmute", {"user": user_hash})

    def channels_delete(self, channel):
        return self._request_json("/api/v1/channels/delete", {"channel": channel})

    def command(self, line):
        """Slash-command passthrough: runs the shared console dispatcher,
        returns the output ephemerally."""
        return self._request_json("/api/v1/command", {"input": line})

    # ---- roles (U5) ----
    def roles_list(self):
        return self._request_json("/api/v1/roles/list", {})

    def roles_create(self, name, perms=0, color=None, position=0):
        return self._request_json("/api/v1/roles/create",
                                  {"name": name, "perms": perms,
                                   "color": color, "position": position})

    def roles_delete(self, role_id):
        return self._request_json("/api/v1/roles/delete", {"role_id": role_id})

    def roles_assign(self, role_id, user):
        return self._request_json("/api/v1/roles/assign",
                                  {"role_id": role_id, "user": user})

    def roles_unassign(self, role_id, user):
        return self._request_json("/api/v1/roles/unassign",
                                  {"role_id": role_id, "user": user})

    # ---- admin panel (U8) ----
    def admin_config(self, key=None, value=None):
        data = {}
        if key is not None:
            data["key"] = key
        if value is not None:
            data["value"] = value
        return self._request_json("/api/v1/admin/config", data)

    def admin_audit(self, limit=100):
        return self._request_json("/api/v1/admin/audit", {"limit": limit})

    def member_moderation(self, limit=50):
        return self._request_json("/api/v1/moderation/public", {"limit": limit})

    def message_edits(self, msg_id):
        return self._request_json("/api/v1/messages/edits", {"msg": msg_id})

    def account_export(self, after=0, limit=100):
        return self._request_json("/api/v1/account/export",
                                  {"after": after, "limit": limit})

    def account_delete_posts(self):
        return self._request_json("/api/v1/account/delete-posts",
                                  {"confirm": "DELETE MY POSTS"})

    def account_leave(self):
        return self._request_json("/api/v1/account/leave",
                                  {"confirm": "LEAVE GUILD"})

    def catchup(self):
        return self._request_json("/api/v1/catchup", {})

    def admin_mods(self, op="list", user=None):
        data = {"op": op}
        if user:
            data["user"] = user
        return self._request_json("/api/v1/admin/mods", data)

    # ---- join gate (U6b) ----
    def join(self, code=None):
        """Join status (no code) or join with an invite code."""
        data = {}
        if code:
            data["code"] = code
        return self._request_json("/api/v1/join", data)

    def invites(self, op="list", code=None, label=None, max_uses=None,
                expires_in=None, channel=None):
        data = {"op": op}
        if code:
            data["code"] = code
        if label is not None:
            data["label"] = label
        if max_uses is not None:
            data["max_uses"] = max_uses
        if expires_in is not None:
            data["expires_in"] = expires_in
        if channel:
            data["channel"] = channel
        return self._request_json("/api/v1/invites", data)

    def fetch_file(self, path):
        """Fetch raw bytes from a /file/... request handler on the guild."""
        self._log(f"fetch_file: requesting {path}")
        receipt = self.link.request(path, data={})
        # link.request() may return a Receipt object (with .response) or
        # True/False (bool) depending on RNS version. Handle both.
        if isinstance(receipt, bool):
            if not receipt:
                self._log(f"fetch_file: link.request returned False for {path}")
                return None
            receipt = self.link.request(path, data={})
        deadline = time.time() + 15
        while receipt.response is None and time.time() < deadline:
            time.sleep(0.05)
        if receipt.response is None:
            self._log(f"fetch_file: timeout for {path}")
            return None
        self._log(f"fetch_file: received {len(receipt.response)} bytes for {path}")
        return receipt.response
