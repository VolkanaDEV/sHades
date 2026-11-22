#!/usr/bin/env python3
"""Bot stream filtering + data path unit tests (Group 2) — no RNS mesh.

Pins down:
  - _resolve_bot: good key ok; wrong key / unknown bot / revoked /
    non-string credentials -> None (callers map None to 403, never 404);
  - _bot_event_allowed: kind -> bit mapping; the never-delivered kinds
    (term_data, channels_changed, channel_deleted, posts_purged) and
    unknown kinds -> False even with perms 1023; site_patch is SITE-gated
    (bit 512), not never-delivered;
  - subscribe accepts bot credentials in the data, authenticates AS the
    owner for VIEW (owner without VIEW -> 403; bad credentials -> 403,
    never 404), and records bot_id + bot_perms on the subscription;
  - SUB_BY_USER never holds a bot sub: bots are not users, presence must
    never list them, and a bot sub must not clobber the owner's own entry;
  - push filtering: a bot is only woken (ticket frame) for a batch holding
    at least one allowed event; the typing snapshot rides the ticket only
    with the TYPING bit; a reset ticket still goes out (control signal);
  - poll filtering: _stream_collect with bot perms never returns a
    disallowed event; a revoked bot polling -> 403 + sub dropped; gap
    reset still works for bots; the human path stays unfiltered;
  - owner-VIEW gating: owner loses VIEW -> sub drops on push, poll 403s;
  - bot post: POST bit, owner VIEW+POST, rate_ok keyed by bot (a bot's
    limit is independent of the owner's and vice versa), posted as the
    owner, audit bot_post (actor=owner, target=bot_id, detail=msg_id);
  - bot react: REACT bit, reaction stored + emitted as the owner,
    audit bot_react;
  - READ_CHANNELS: bot-only /api/v1/bots/channels + /api/v1/bots/channel_info
    see exactly the owner's VIEWable channels; without the bit -> 403;
  - last_seen touched on successful bot requests; revoked bots get 403
    everywhere on the data path.
"""
import base64
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail)


base = tempfile.mkdtemp(prefix="shades_bots_stream_test_")
sHades.STORAGE = base


def mkid():
    i = RNS.Identity()
    return i, sHades.hexstr(RNS.Destination(i, RNS.Destination.OUT,
                                            RNS.Destination.SINGLE,
                                            "lxmf", "delivery").hash)


owner_ident, OWNER = mkid()
other_ident, OTHER = mkid()
bot_ident, BOTLINK = mkid()
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                  "VALUES (?,?,?)", (OWNER, "owner", sHades.now()))
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                  "VALUES (?,?,?)", (OTHER, "other", sHades.now()))
sHades.db.commit()
sHades.DEBUG = False


def call(path, data=None, ident=owner_ident, link_id="L1"):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", link_id, ident,
                                  sHades.now())
    return json.loads(raw.decode("utf-8"))


def legacy(path, data, ident=owner_ident, link_id="L9"):
    raw = sHades.API_ROUTES[path]("/x", data, "r", link_id, ident, sHades.now())
    return sHades.msgpack.unpackb(raw)


def make_bot(name, perms):
    r = call("/api/v1/bots/create", {"name": name, "perms": perms})
    assert r.get("ok") == 1, r
    return r["bot"]["bot_id"], r["api_key"]


def bot_row(bot_id):
    return sHades.store.bot_get(sHades.db, sHades.db_lock, bot_id)


def reset_stream_state():
    with sHades.EVENT_COND:
        sHades.EVENT_LOG.clear()
        sHades.EVENT_SEQ = 0
    with sHades.SUB_LOCK:
        sHades.SUBSCRIPTIONS.clear()
        sHades.SUB_BY_USER.clear()
    with sHades.TYPING_LOCK:
        sHades.TYPING.clear()


def vault_hide(cid):
    with sHades.db_lock:
        sHades.db.execute("INSERT OR REPLACE INTO channel_overwrites "
                          "(channel_id, role_id, perms) VALUES (?, 0, 0)",
                          (cid,))
        sHades.db.commit()


def vault_show(cid):
    with sHades.db_lock:
        sHades.db.execute("DELETE FROM channel_overwrites "
                          "WHERE channel_id=? AND role_id=0", (cid,))
        sHades.db.commit()


def audit_rows(action):
    rows = sHades.store.list_audit(sHades.db, sHades.db_lock, 200)
    return [r for r in rows if r["action"] == action]


# Fake link + packet capture for the push path (mirrors test_streams.py):
# only the bot's link is "live", so only frames to the bot are recorded.
sent_frames = []


class _FakePacket:
    def __init__(self, link, data):
        sent_frames.append(bytes(data))

    def send(self):
        return True


class _LiveLink:
    link_id = "BL"
    status = RNS.Link.ACTIVE


RNS.Packet = _FakePacket
RNS.Transport.active_links.append(_LiveLink())


def frames():
    return [sHades.msgpack.unpackb(f) for f in sent_frames]


def bot_sub(bot_id, api_key, cid, link_id="BL", extra=None):
    data = {"channel": cid, "bot_id": bot_id, "api_key": api_key}
    if extra:
        data.update(extra)
    return call("/api/v1/stream/subscribe", data, ident=bot_ident,
                link_id=link_id)


def bot_poll(bot_id, api_key, link_id="BL", extra=None):
    data = {"timeout_s": 0}
    if extra:
        data.update(extra)
    return call("/api/v1/stream/poll", data, ident=bot_ident, link_id=link_id)


def main():
    BP = sHades.BOT_PERMS
    general = call("/api/v1/channels/create",
                   {"name": "general", "kind": "chat"})["channel"]["id"]
    vault = call("/api/v1/channels/create",
                 {"name": "vault", "kind": "chat"})["channel"]["id"]
    check("setup channels", bool(general) and bool(vault)
          and general != vault, (general, vault))

    # ---------- _resolve_bot ----------
    msg_bot, msg_key = make_bot("msg-only", BP["MSG"])
    row = sHades._resolve_bot(msg_bot, msg_key)
    check("_resolve_bot good key returns the row",
          row is not None and row["owner_hash"] == OWNER
          and row["perms"] == BP["MSG"], dict(row) if row else None)
    check("_resolve_bot wrong key -> None",
          sHades._resolve_bot(msg_bot, "sk-" + "0" * 64) is None)
    check("_resolve_bot unknown bot_id -> None",
          sHades._resolve_bot("e" * 32, msg_key) is None)
    check("_resolve_bot non-string credentials -> None",
          sHades._resolve_bot(123, msg_key) is None
          and sHades._resolve_bot(msg_bot, None) is None
          and sHades._resolve_bot(msg_bot, 42) is None)
    dead_bot, dead_key = make_bot("doomed", BP["MSG"])
    call("/api/v1/bots/revoke", {"bot_id": dead_bot})
    check("_resolve_bot revoked bot -> None",
          sHades._resolve_bot(dead_bot, dead_key) is None)
    check("revoked bot still has its row (revoke, not delete)",
          bot_row(dead_bot) is not None
          and bot_row(dead_bot)["revoked_at"] is not None)

    # ---------- _bot_event_allowed ----------
    kinds = {"msg": "MSG", "edit": "EDIT", "delete": "DELETE",
             "reaction": "REACTION", "presence": "PRESENCE",
             "typing": "TYPING"}
    for kind, bit in kinds.items():
        check("allowed: %s with its bit" % kind,
              sHades._bot_event_allowed(BP[bit], kind) is True)
        check("denied: %s without its bit" % kind,
              sHades._bot_event_allowed(0, kind) is False)
    for kind in ("site_patch", "term_data", "channels_changed",
                 "channel_deleted", "posts_purged", "bogus"):
        check("never delivered: %s even with perms 511" % kind,
              sHades._bot_event_allowed(511, kind) is False)

    # ---------- subscribe: bot auth matrix ----------
    reset_stream_state()
    r = bot_sub(msg_bot, "sk-" + "0" * 64, general)
    check("subscribe with wrong key -> 403", r.get("ok") == 0
          and r.get("code") == 403, r)
    r = bot_sub("e" * 32, msg_key, general)
    check("subscribe with unknown bot_id -> 403 (never 404)",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/stream/subscribe", {"channel": general, "bot_id": msg_bot},
             ident=bot_ident, link_id="BL")
    check("subscribe with bot_id but no api_key -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/stream/subscribe",
             {"channel": general, "api_key": msg_key},
             ident=bot_ident, link_id="BL")
    check("subscribe with api_key but no bot_id -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = bot_sub(dead_bot, dead_key, general)
    check("subscribe with revoked bot -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = bot_sub(msg_bot, msg_key, general)
    check("subscribe with good bot credentials -> ok",
          r.get("ok") == 1 and r.get("channel") == general
          and isinstance(r.get("cursor"), int), r)
    sub = sHades.SUBSCRIPTIONS.get("BL")
    check("bot sub records bot_id + perms, user_hash is the owner",
          sub is not None and sub.get("bot_id") == msg_bot
          and sub.get("bot_perms") == BP["MSG"]
          and sub.get("user_hash") == OWNER
          and sub.get("channel") == general, sub)
    check("bot sub is NOT in SUB_BY_USER",
          sHades.SUB_BY_USER.get(OWNER) is None, sHades.SUB_BY_USER)
    check("subscribe touches last_seen",
          bot_row(msg_bot)["last_seen"] is not None, bot_row(msg_bot))

    # ---------- push filtering: MSG-only bot ----------
    sent_frames.clear()
    mid = sHades.ingest(general, OWNER, "hello push")
    check("msg event wakes the MSG-only bot (1 ticket)",
          len(frames()) == 1 and frames()[0].get("ticket") is True
          and frames()[0].get("channel") == general, frames())
    check("ticket carries no bodies and no typing snapshot w/o TYPING bit",
          "events" not in frames()[0] and frames()[0].get("typing") == [],
          frames())
    legacy("/api/edit", {"msg": mid, "content": "hello push edited"})
    call("/api/v1/react", {"msg": mid, "emoji": "+1"})
    legacy("/api/delete", {"msg": mid})
    check("edit/reaction/delete do not wake the MSG-only bot",
          len(frames()) == 1, len(frames()))
    call("/api/v1/typing", {"channel": general, "active": True})
    call("/api/v1/typing", {"channel": general, "active": False})
    sHades._emit_event(None, "presence", {"users": []}, guild_wide=True)
    sHades._emit_event(general, "site_patch", {"path": "/x.mu"})
    sHades._emit_event(general, "term_data", {"line": "hi"})
    sHades._emit_event(None, "channels_changed", {"order": []}, guild_wide=True)
    sHades._emit_event(general, "channel_deleted", {"channel": general})
    sHades._emit_event(general, "posts_purged", {"author_hash": OWNER})
    sHades._emit_event(general, "bogus", {})
    check("typing/presence/site_patch/term_data/channels_changed/"
          "channel_deleted/posts_purged/bogus never wake it",
          len(frames()) == 1, len(frames()))
    poll = bot_poll(msg_bot, msg_key)
    check("push tickets never consume events: the msg is still pollable",
          [e["kind"] for e in poll.get("events", [])] == ["msg"]
          and poll["events"][0]["data"]["msg"]["content"] == "hello push",
          poll)
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")
    check("unsubscribe drops the bot sub",
          "BL" not in sHades.SUBSCRIPTIONS)

    # ---------- typing snapshot gating ----------
    call("/api/v1/typing", {"channel": general, "active": True})
    r = bot_sub(msg_bot, msg_key, general)
    check("MSG-only bot's subscribe response hides the typing snapshot",
          r.get("ok") == 1 and r.get("typing") == [], r)
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")
    type_bot, type_key = make_bot("typer", BP["MSG"] | BP["TYPING"])
    r = bot_sub(type_bot, type_key, general)
    check("MSG|TYPING bot's subscribe response carries the typist",
          r.get("ok") == 1
          and [t["user_hash"] for t in r.get("typing", [])] == [OWNER], r)
    sent_frames.clear()
    sHades.ingest(general, OWNER, "typing still on")
    check("push frame to a TYPING bot carries the typing snapshot",
          len(frames()) == 1
          and [t["user_hash"] for t in frames()[0].get("typing", [])]
          == [OWNER], frames())
    call("/api/v1/typing", {"channel": general, "active": False})
    check("typing-stop event wakes a TYPING bot, snapshot now empty",
          len(frames()) == 2 and frames()[1].get("typing") == [], frames())
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")

    # ---------- all receive bits, and perms 511 still never gets the rest --
    all_bot, all_key = make_bot("all", 63)
    reset_stream_state()
    r = bot_sub(all_bot, all_key, general)
    check("all-bits bot subscribes", r.get("ok") == 1, r)
    sent_frames.clear()
    for kind in ("msg", "edit", "delete", "reaction", "presence", "typing"):
        sHades._emit_event(general, kind, {})
    check("all-bits bot is woken once per allowed kind",
          len(frames()) == 6, len(frames()))
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")
    full_bot, full_key = make_bot("full", 511)
    reset_stream_state()
    r = bot_sub(full_bot, full_key, general)
    check("perms-511 bot subscribes", r.get("ok") == 1, r)
    sent_frames.clear()
    for kind in ("site_patch", "term_data", "channels_changed",
                 "channel_deleted", "posts_purged", "bogus"):
        sHades._emit_event(general, kind, {})
    check("perms-511 bot is never woken for never-delivered kinds",
          len(frames()) == 0, len(frames()))
    sHades._emit_event(general, "msg", {})
    check("perms-511 bot is still woken for a msg",
          len(frames()) == 1, len(frames()))
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")

    # ---------- poll filtering ----------
    reset_stream_state()
    r = bot_sub(msg_bot, msg_key, general)
    check("poll setup: bot subscribed", r.get("ok") == 1, r)
    mid = sHades.ingest(general, OWNER, "poll me")
    legacy("/api/edit", {"msg": mid, "content": "poll me edited"})
    call("/api/v1/react", {"msg": mid, "emoji": "+1"})
    call("/api/v1/typing", {"channel": general, "active": True})
    call("/api/v1/typing", {"channel": general, "active": False})
    sHades._emit_event(None, "presence", {"users": []}, guild_wide=True)
    sHades._emit_event(general, "site_patch", {"path": "/x.mu"})
    poll = bot_poll(msg_bot, msg_key)
    kinds = [e["kind"] for e in poll.get("events", [])]
    check("poll returns only the allowed kind",
          poll.get("ok") == 1 and kinds == ["msg"], poll)
    check("poll event carries the message",
          poll["events"][0]["data"]["msg"]["content"] == "poll me", poll)
    check("poll cursor advances to the delivered event",
          poll.get("cursor") == poll["events"][0]["seq"], poll)
    poll2 = bot_poll(msg_bot, msg_key)
    check("drained bot poll returns no events",
          poll2.get("ok") == 1 and poll2.get("events") == [], poll2)
    # perms 0: subscribes fine, receives nothing
    zero_bot, zero_key = make_bot("zero", 0)
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")
    r = bot_sub(zero_bot, zero_key, general)
    check("perms-0 bot subscribes", r.get("ok") == 1, r)
    sHades.ingest(general, OWNER, "nothing for zero")
    poll = bot_poll(zero_bot, zero_key)
    check("perms-0 bot poll returns no events",
          poll.get("ok") == 1 and poll.get("events") == [], poll)
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")
    # gap -> reset still works for bots
    reset_stream_state()
    old_max = sHades.EVENT_LOG_MAX
    sHades.EVENT_LOG_MAX = 2
    r = bot_sub(msg_bot, msg_key, general, extra={"cursor": 0})
    for i in range(5):
        sHades.ingest(general, OWNER, "overflow %d" % i)
    poll = bot_poll(msg_bot, msg_key, extra={"cursor": 0})
    sHades.EVENT_LOG_MAX = old_max
    check("bot poll with a cursor behind the ring -> reset",
          poll.get("reset") is True and poll.get("events") == []
          and isinstance(poll.get("seq"), int), poll)
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")
    # revoked bot polling -> 403 + sub dropped
    rev_bot, rev_key = make_bot("revpoll", BP["MSG"])
    bot_sub(rev_bot, rev_key, general)
    call("/api/v1/bots/revoke", {"bot_id": rev_bot})
    poll = bot_poll(rev_bot, rev_key)
    check("revoked bot polling -> 403",
          poll.get("ok") == 0 and poll.get("code") == 403, poll)
    check("revoked bot's sub is dropped by the poll",
          "BL" not in sHades.SUBSCRIPTIONS)
    # human path stays unfiltered
    reset_stream_state()
    call("/api/v1/stream/subscribe", {"channel": general}, link_id="HL")
    mid = sHades.ingest(general, OWNER, "human sees all")
    legacy("/api/edit", {"msg": mid, "content": "human sees edit"})
    call("/api/v1/react", {"msg": mid, "emoji": "+1"})
    poll = call("/api/v1/stream/poll", {"timeout_s": 0}, link_id="HL")
    kinds = [e["kind"] for e in poll.get("events", [])]
    check("human poll still returns every kind",
          kinds == ["msg", "edit", "reaction"], poll)
    call("/api/v1/stream/unsubscribe", {}, link_id="HL")

    # ---------- owner-VIEW gating ----------
    reset_stream_state()
    vault_hide(vault)
    r = bot_sub(msg_bot, msg_key, vault)
    check("owner without VIEW: bot subscribe -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/stream/subscribe", {"channel": vault}, link_id="HL")
    check("owner without VIEW: human subscribe -> 403 (sanity)",
          r.get("ok") == 0 and r.get("code") == 403, r)
    vault_show(vault)
    r = bot_sub(msg_bot, msg_key, vault)
    check("owner with VIEW: bot subscribe -> ok", r.get("ok") == 1, r)
    vault_hide(vault)
    sent_frames.clear()
    sHades._emit_event(vault, "msg", {"msg": {"content": "hidden"}})
    check("owner loses VIEW: push drops the bot sub, no ticket",
          "BL" not in sHades.SUBSCRIPTIONS and len(frames()) == 0,
          (sHades.SUBSCRIPTIONS, len(frames())))
    vault_show(vault)
    bot_sub(msg_bot, msg_key, vault)
    vault_hide(vault)
    poll = bot_poll(msg_bot, msg_key)
    check("owner loses VIEW: bot poll -> 403",
          poll.get("ok") == 0 and poll.get("code") == 403, poll)
    check("owner loses VIEW: poll drops the sub",
          "BL" not in sHades.SUBSCRIPTIONS)
    vault_show(vault)

    # ---------- bot post ----------
    post_bot, post_key = make_bot("poster", BP["POST"] | BP["MSG"])
    no_post_bot, no_post_key = make_bot("nopost", BP["MSG"])
    r = call("/api/v1/bots/post",
             {"bot_id": no_post_bot, "api_key": no_post_key,
              "channel": general, "content": "nope"},
             ident=bot_ident, link_id="BL")
    check("bot without POST bit -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/bots/post",
             {"bot_id": post_bot, "api_key": "sk-" + "0" * 64,
              "channel": general, "content": "nope"},
             ident=bot_ident, link_id="BL")
    check("bot post with wrong key -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/bots/post",
             {"bot_id": post_bot, "api_key": post_key,
              "channel": "f" * 32, "content": "nope"},
             ident=bot_ident, link_id="BL")
    check("bot post to unknown channel -> 404",
          r.get("ok") == 0 and r.get("code") == 404, r)
    r = call("/api/v1/bots/post",
             {"bot_id": post_bot, "api_key": post_key,
              "channel": general, "content": "   "},
             ident=bot_ident, link_id="BL")
    check("bot post with empty content -> 400",
          r.get("ok") == 0 and r.get("code") == 400, r)
    vault_hide(vault)
    r = call("/api/v1/bots/post",
             {"bot_id": post_bot, "api_key": post_key,
              "channel": vault, "content": "hidden"},
             ident=bot_ident, link_id="BL")
    check("bot post where the owner lacks VIEW -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    vault_show(vault)
    r = call("/api/v1/bots/post",
             {"bot_id": post_bot, "api_key": post_key,
              "channel": general, "content": "bot says hi"},
             ident=bot_ident, link_id="BL")
    check("bot post ok", r.get("ok") == 1 and r.get("msg_id"), r)
    post_mid = r.get("msg_id")
    row = sHades.store.message_by_msg_id(sHades.db, sHades.db_lock, post_mid)
    check("bot post is stored as the owner",
          row is not None and row["author_hash"] == OWNER
          and row["content"] == "bot says hi", dict(row) if row else None)
    check("bot post writes audit bot_post (actor=owner, target=bot_id)",
          any(a["actor"] == OWNER and a["target"] == post_bot
              and a["detail"] == post_mid for a in audit_rows("bot_post")),
          audit_rows("bot_post"))
    check("bot post touches last_seen",
          bot_row(post_bot)["last_seen"] is not None)
    # attachments: base64 in, stored row out
    blob = base64.b64encode(b"hello attachment").decode()
    r = call("/api/v1/bots/post",
             {"bot_id": post_bot, "api_key": post_key, "channel": general,
              "content": "with file",
              "attachments": [{"name": "hello.txt", "mime": "text/plain",
                               "data": blob}]},
             ident=bot_ident, link_id="BL")
    check("bot post with a base64 attachment ok",
          r.get("ok") == 1 and r.get("msg_id"), r)
    atts = sHades.store.message_attachments(sHades.db, sHades.db_lock,
                                            r.get("msg_id"))
    check("attachment stored with name/mime",
          len(atts) == 1 and atts[0]["name"] == "hello.txt"
          and atts[0]["mime"] == "text/plain", atts)
    r = call("/api/v1/bots/post",
             {"bot_id": post_bot, "api_key": post_key, "channel": general,
              "content": "bad file",
              "attachments": [{"name": "x", "mime": "text/plain",
                               "data": "!!!not-base64!!!"}]},
             ident=bot_ident, link_id="BL")
    check("bot post with bad base64 attachment -> 400",
          r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/bots/post",
             {"bot_id": post_bot, "api_key": post_key, "channel": general,
              "content": "too many",
              "attachments": [{"name": "x", "mime": "text/plain",
                               "data": blob}] * 5},
             ident=bot_ident, link_id="BL")
    check("bot post with 5 attachments -> 400",
          r.get("ok") == 0 and r.get("code") == 400, r)
    # rate limit: keyed by bot, independent of the owner's window
    reset_stream_state()
    rate_bot, rate_key = make_bot("ratey", BP["POST"])
    ok = 0
    for i in range(10):
        r = call("/api/v1/bots/post",
                 {"bot_id": rate_bot, "api_key": rate_key,
                  "channel": general, "content": "rate %d" % i},
                 ident=bot_ident, link_id="BL")
        ok += 1 if r.get("ok") == 1 else 0
    check("bot posts 10 within the window", ok == 10, ok)
    r = call("/api/v1/bots/post",
             {"bot_id": rate_bot, "api_key": rate_key,
              "channel": general, "content": "rate 10"},
             ident=bot_ident, link_id="BL")
    check("11th bot post -> 429",
          r.get("ok") == 0 and r.get("code") == 429, r)
    r = legacy("/api/post", {"channel": general, "content": "owner still can"})
    check("bot at its limit does not block the owner",
          r.get("ok") == 1, r)
    for i in range(9):
        r = legacy("/api/post", {"channel": general,
                                 "content": "owner %d" % i})
    check("owner posts 9 more (10 in window)", r.get("ok") == 1, r)
    r = legacy("/api/post", {"channel": general, "content": "owner 10"})
    check("11th owner post -> 429",
          r.get("ok") == 0 and r.get("code") == 429, r)
    rate_bot2, rate_key2 = make_bot("ratey2", BP["POST"])
    r = call("/api/v1/bots/post",
             {"bot_id": rate_bot2, "api_key": rate_key2,
              "channel": general, "content": "fresh bot still can"},
             ident=bot_ident, link_id="BL")
    check("owner at its limit does not block a fresh bot",
          r.get("ok") == 1, r)
    # revoked bot cannot post
    call("/api/v1/bots/revoke", {"bot_id": post_bot})
    r = call("/api/v1/bots/post",
             {"bot_id": post_bot, "api_key": post_key,
              "channel": general, "content": "revoked"},
             ident=bot_ident, link_id="BL")
    check("revoked bot post -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)

    # ---------- bot react ----------
    react_bot, react_key = make_bot("reacter", BP["REACT"])
    no_react_bot, no_react_key = make_bot("noreact", BP["MSG"])
    mid = sHades.ingest(general, OWNER, "react target")
    r = call("/api/v1/bots/react",
             {"bot_id": no_react_bot, "api_key": no_react_key,
              "msg": mid, "emoji": "+1"}, ident=bot_ident, link_id="BL")
    check("bot without REACT bit -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/bots/react",
             {"bot_id": react_bot, "api_key": react_key,
              "msg": "f" * 32, "emoji": "+1"}, ident=bot_ident, link_id="BL")
    check("bot react to unknown message -> 404",
          r.get("ok") == 0 and r.get("code") == 404, r)
    r = call("/api/v1/bots/react",
             {"bot_id": react_bot, "api_key": react_key,
              "msg": mid, "emoji": "not an emoji!!!"},
             ident=bot_ident, link_id="BL")
    check("bot react with a bad emoji -> 400",
          r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/bots/react",
             {"bot_id": react_bot, "api_key": react_key,
              "msg": mid, "emoji": "+1"}, ident=bot_ident, link_id="BL")
    check("bot react ok, active", r.get("ok") == 1
          and r.get("active") is True, r)
    rx = sHades.store.message_reactions(sHades.db, sHades.db_lock, mid)
    check("reaction stored as the owner",
          len(rx) == 1 and rx[0]["user_hash"] == OWNER
          and rx[0]["emoji"] == "+1", rx)
    with sHades.EVENT_COND:
        ring = list(sHades.EVENT_LOG.get(general, []))
    rev = [e for _, e in ring if e["kind"] == "reaction"]
    check("reaction event emitted as the owner",
          rev and rev[-1]["data"]["user_hash"] == OWNER
          and rev[-1]["data"]["emoji"] == "+1", rev)
    r = call("/api/v1/bots/react",
             {"bot_id": react_bot, "api_key": react_key,
              "msg": mid, "emoji": "+1"}, ident=bot_ident, link_id="BL")
    check("bot react toggles off", r.get("ok") == 1
          and r.get("active") is False, r)
    check("bot react writes audit bot_react (actor=owner, target=bot_id)",
          any(a["actor"] == OWNER and a["target"] == react_bot
              and a["detail"] == mid for a in audit_rows("bot_react")),
          audit_rows("bot_react"))
    vault_mid = sHades.ingest(vault, OWNER, "vault target")
    vault_hide(vault)
    r = call("/api/v1/bots/react",
             {"bot_id": react_bot, "api_key": react_key,
              "msg": vault_mid, "emoji": "+1"}, ident=bot_ident, link_id="BL")
    check("bot react where the owner lacks VIEW -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    vault_show(vault)
    call("/api/v1/bots/revoke", {"bot_id": react_bot})
    r = call("/api/v1/bots/react",
             {"bot_id": react_bot, "api_key": react_key,
              "msg": mid, "emoji": "+1"}, ident=bot_ident, link_id="BL")
    check("revoked bot react -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)

    # ---------- READ_CHANNELS ----------
    read_bot, read_key = make_bot("reader", BP["READ_CHANNELS"])
    no_read_bot, no_read_key = make_bot("noread", BP["MSG"])
    for path, args in (
            ("/api/v1/bots/channels", {}),
            ("/api/v1/bots/channel_info", {"channel": general})):
        r = call(path, dict(args, bot_id=no_read_bot, api_key=no_read_key),
                 ident=bot_ident, link_id="BL")
        check("%s without READ_CHANNELS -> 403" % path,
              r.get("ok") == 0 and r.get("code") == 403, r)
        r = call(path, dict(args, bot_id=read_bot, api_key="sk-" + "0" * 64),
                 ident=bot_ident, link_id="BL")
        check("%s with wrong key -> 403" % path,
              r.get("ok") == 0 and r.get("code") == 403, r)
    seen_before = bot_row(read_bot)["last_seen"]
    r = call("/api/v1/bots/channels",
             {"bot_id": read_bot, "api_key": read_key},
             ident=bot_ident, link_id="BL")
    human = call("/api/v1/channels/list")
    check("bot channels ok, same ids as the owner's human list",
          r.get("ok") == 1
          and {c["id"] for c in r.get("channels", [])}
          == {c["id"] for c in human.get("channels", [])}, r)
    check("bot channel entries carry can_post + unread",
          all("can_post" in c and "unread" in c
              for c in r.get("channels", [])), r)
    check("channels call touches last_seen",
          bot_row(read_bot)["last_seen"] is not None
          and bot_row(read_bot)["last_seen"] >= (seen_before or 0))
    vault_hide(vault)
    r = call("/api/v1/bots/channels",
             {"bot_id": read_bot, "api_key": read_key},
             ident=bot_ident, link_id="BL")
    check("bot channels hides the channel the owner cannot view",
          r.get("ok") == 1
          and all(c["id"] != vault for c in r.get("channels", [])), r)
    vault_show(vault)
    r = call("/api/v1/bots/channels",
             {"bot_id": read_bot, "api_key": read_key},
             ident=bot_ident, link_id="BL")
    check("bot channels shows it again once the owner can view",
          r.get("ok") == 1
          and any(c["id"] == vault for c in r.get("channels", [])), r)
    r = call("/api/v1/bots/channel_info",
             {"bot_id": read_bot, "api_key": read_key, "channel": general},
             ident=bot_ident, link_id="BL")
    human_info = call("/api/v1/channels/info", {"channel": general})
    check("bot channel_info matches the human shape",
          r.get("ok") == 1 and r.get("channel", {}).get("id") == general
          and r.get("channel", {}).get("name")
          == human_info.get("channel", {}).get("name")
          and r.get("channel", {}).get("kind") == "chat", r)
    r = call("/api/v1/bots/channel_info",
             {"bot_id": read_bot, "api_key": read_key, "channel": "f" * 32},
             ident=bot_ident, link_id="BL")
    check("bot channel_info on unknown channel -> 404",
          r.get("ok") == 0 and r.get("code") == 404, r)
    call("/api/v1/bots/revoke", {"bot_id": read_bot})
    r = call("/api/v1/bots/channels",
             {"bot_id": read_bot, "api_key": read_key},
             ident=bot_ident, link_id="BL")
    check("revoked bot channels -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)

    # ---------- presence / SUB_BY_USER ----------
    reset_stream_state()
    r = bot_sub(msg_bot, msg_key, general)
    check("presence setup: bot subscribed", r.get("ok") == 1, r)
    check("bot sub leaves SUB_BY_USER empty for the owner",
          sHades.SUB_BY_USER.get(OWNER) is None)
    legacy("/api/register_node", {"node_hash": "1" * 32}, link_id="HL")
    pres = call("/api/v1/presence", {})
    me = [u for u in pres.get("users", []) if u["user_hash"] == OWNER]
    check("owner is online once the node registers",
          len(me) == 1, pres)
    check("bot sub does not set the owner's active_channel",
          me and me[0].get("active_channel") is None, pres)
    check("the bot's own link hash never appears in presence",
          all(u["user_hash"] != BOTLINK for u in pres.get("users", [])),
          pres)
    r = call("/api/v1/stream/subscribe", {"channel": vault}, link_id="HL2")
    check("owner human-subscribes to vault", r.get("ok") == 1, r)
    check("human sub lands in SUB_BY_USER",
          (sHades.SUB_BY_USER.get(OWNER) or {}).get("channel") == vault)
    pres = call("/api/v1/presence", {})
    me = [u for u in pres.get("users", []) if u["user_hash"] == OWNER]
    check("presence shows the HUMAN subscription's channel",
          me and me[0].get("active_channel") == vault, pres)
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")
    r = bot_sub(msg_bot, msg_key, general)
    check("bot re-subscribes after the human sub", r.get("ok") == 1, r)
    check("bot sub does not clobber the owner's SUB_BY_USER entry",
          (sHades.SUB_BY_USER.get(OWNER) or {}).get("channel") == vault
          and (sHades.SUB_BY_USER.get(OWNER) or {}).get("link_id") == "HL2")
    pres = call("/api/v1/presence", {})
    me = [u for u in pres.get("users", []) if u["user_hash"] == OWNER]
    check("presence still shows the human channel, not the bot's",
          me and me[0].get("active_channel") == vault, pres)
    call("/api/v1/stream/unsubscribe", {}, link_id="HL2")
    check("human unsubscribe clears SUB_BY_USER",
          sHades.SUB_BY_USER.get(OWNER) is None)
    call("/api/v1/stream/unsubscribe", {}, ident=bot_ident, link_id="BL")

    # ---------- revoked bot drops on push + 403s everywhere ----------
    doom_bot, doom_key = make_bot("doom", BP["MSG"] | BP["POST"]
                                  | BP["REACT"] | BP["READ_CHANNELS"])
    reset_stream_state()
    r = bot_sub(doom_bot, doom_key, general)
    check("doom bot subscribes", r.get("ok") == 1, r)
    call("/api/v1/bots/revoke", {"bot_id": doom_bot})
    sent_frames.clear()
    sHades.ingest(general, OWNER, "after the revoke")
    check("revoked bot gets no push and its sub is dropped",
          len(frames()) == 0 and "BL" not in sHades.SUBSCRIPTIONS,
          (len(frames()), sHades.SUBSCRIPTIONS))
    r = bot_sub(doom_bot, doom_key, general)
    check("revoked bot cannot subscribe",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/bots/post",
             {"bot_id": doom_bot, "api_key": doom_key,
              "channel": general, "content": "doom"},
             ident=bot_ident, link_id="BL")
    check("revoked bot cannot post",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/bots/react",
             {"bot_id": doom_bot, "api_key": doom_key,
              "msg": mid, "emoji": "+1"}, ident=bot_ident, link_id="BL")
    check("revoked bot cannot react",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/bots/channels",
             {"bot_id": doom_bot, "api_key": doom_key},
             ident=bot_ident, link_id="BL")
    check("revoked bot cannot list channels",
          r.get("ok") == 0 and r.get("code") == 403, r)

    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


main()
