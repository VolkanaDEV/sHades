#!/usr/bin/env python3
"""Bot micron-page hooks (SITE bit 512) — no RNS mesh.

Pins down:
  - _bot_event_allowed: site_patch is allowed only with the SITE bit
    (512); perms 511 (the old full mask) still gets nothing; perms 1023
    gets site_patch but never term_data / channels_changed /
    channel_deleted / posts_purged / bogus;
  - hook push: a real site_put (moderator publish) wakes a SITE bot's
    push ticket on the site channel, and a real manifest_put does too;
    a MSG-only bot subscribed to the same channel is never woken;
  - hook poll: the SITE bot's poll carries the site_patch events (with
    path on put, manifest on manifest_put); the MSG bot's poll never
    carries them;
  - /api/v1/bots/site_get: SITE bit + owner VIEW; chunked read identical
    to the human endpoint (same content, total, eof, mime, can_edit);
    403 without the bit / with a wrong key / for a revoked bot / when
    the owner lost VIEW; 404 for non-site / unknown channels and
    missing resources; last_seen touched;
  - /api/v1/bots/site_manifest: same SITE + owner-VIEW gate; 404 for
    non-site channels; the human endpoint stays ungated;
  - create accepts the SITE bit (perms 512);
  - the human site_get / site_manifest handlers are unchanged by the
    _site_read_for / _site_manifest_for extraction (same responses).
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


base = tempfile.mkdtemp(prefix="shades_bots_site_hooks_test_")
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
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                  "VALUES (?,?,?)", (OWNER, "owner", sHades.now()))
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                  "VALUES (?,?,?)", (OTHER, "other", sHades.now()))
sHades.db.commit()
sHades.DEBUG = False
sHades.MODERATORS.add(OWNER)


def call(path, data=None, ident=owner_ident, link_id="L1"):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", link_id, ident,
                                  sHades.now())
    return json.loads(raw.decode("utf-8"))


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


def bot_sub(bot_id, api_key, cid, link_id="BL"):
    return call("/api/v1/stream/subscribe",
                {"channel": cid, "bot_id": bot_id, "api_key": api_key},
                ident=bot_ident, link_id=link_id)


def bot_poll(link_id="BL"):
    return call("/api/v1/stream/poll", {"timeout_s": 0},
                ident=bot_ident, link_id=link_id)


def bot_unsub(link_id="BL"):
    return call("/api/v1/stream/unsubscribe", {},
                ident=bot_ident, link_id=link_id)


def b64(b):
    return base64.b64encode(b).decode("ascii")


def main():
    BP = sHades.BOT_PERMS
    wiki = call("/api/v1/channels/create",
                {"name": "wiki", "kind": "site"})["channel"]["id"]
    general = call("/api/v1/channels/create",
                   {"name": "general", "kind": "chat"})["channel"]["id"]
    check("setup channels", bool(wiki) and bool(general)
          and wiki != general, (wiki, general))

    # ---------- _bot_event_allowed: SITE gates site_patch ----------
    check("allowed: site_patch with SITE bit",
          sHades._bot_event_allowed(BP["SITE"], "site_patch") is True)
    check("denied: site_patch with old full mask 511",
          sHades._bot_event_allowed(511, "site_patch") is False)
    check("allowed: site_patch with full mask 1023",
          sHades._bot_event_allowed(1023, "site_patch") is True)
    for kind in ("term_data", "channels_changed", "channel_deleted",
                 "posts_purged", "bogus"):
        check("never delivered: %s even with perms 1023" % kind,
              sHades._bot_event_allowed(1023, kind) is False)

    # ---------- create accepts the SITE bit ----------
    site_bot, site_key = make_bot("wiki-watcher", BP["SITE"])
    check("create accepts perms 512",
          bot_row(site_bot)["perms"] == BP["SITE"], bot_row(site_bot))
    msg_bot, msg_key = make_bot("msg-only", BP["MSG"])

    # ---------- hook push: a real publish wakes the SITE bot ----------
    reset_stream_state()
    r = bot_sub(site_bot, site_key, wiki)
    check("SITE bot subscribes to wiki", r.get("ok") == 1, r)
    sub = sHades.SUBSCRIPTIONS.get("BL")
    check("bot sub records SITE perms",
          sub is not None and sub.get("bot_perms") == BP["SITE"], sub)
    page = b"# wiki\nhello hooks\n"
    sent_frames.clear()
    r = call("/api/v1/site/put",
             {"channel": wiki, "path": "/index.mu", "data_b64": b64(page)})
    check("moderator publishes a page", r.get("ok") == 1, r)
    check("publish wakes the SITE bot (1 ticket, wiki channel)",
          len(frames()) == 1 and frames()[0].get("ticket") is True
          and frames()[0].get("channel") == wiki, frames())
    bot_unsub()
    reset_stream_state()
    r = bot_sub(msg_bot, msg_key, wiki)
    check("MSG bot subscribes to wiki", r.get("ok") == 1, r)
    sent_frames.clear()
    r = call("/api/v1/site/put",
             {"channel": wiki, "path": "/second.mu", "data_b64": b64(b"two\n")})
    check("second publish ok", r.get("ok") == 1, r)
    check("publish never wakes the MSG-only bot", len(frames()) == 0,
          len(frames()))
    bot_unsub()

    # ---------- hook poll: SITE bot sees the patch, MSG bot does not ----------
    reset_stream_state()
    r = bot_sub(site_bot, site_key, wiki)
    check("poll setup: SITE bot subscribed", r.get("ok") == 1, r)
    call("/api/v1/site/put",
         {"channel": wiki, "path": "/third.mu", "data_b64": b64(b"three\n")})
    poll = bot_poll()
    kinds = [e["kind"] for e in poll.get("events", [])]
    check("SITE poll carries the site_patch",
          poll.get("ok") == 1 and kinds == ["site_patch"], poll)
    check("site_patch carries the published path",
          poll["events"][0]["data"].get("path") == "/third.mu", poll)
    r = call("/api/v1/site/manifest_put",
             {"channel": wiki, "manifest": {"theme": "midnight"}})
    check("manifest_put ok", r.get("ok") == 1, r)
    poll = bot_poll()
    kinds = [e["kind"] for e in poll.get("events", [])]
    check("manifest change also arrives as site_patch",
          kinds == ["site_patch"]
          and poll["events"][0]["data"].get("manifest") == {"theme": "midnight"},
          poll)
    bot_unsub()
    reset_stream_state()
    r = bot_sub(msg_bot, msg_key, wiki)
    check("poll setup: MSG bot subscribed", r.get("ok") == 1, r)
    call("/api/v1/site/put",
         {"channel": wiki, "path": "/fourth.mu", "data_b64": b64(b"four\n")})
    poll = bot_poll()
    check("MSG poll never carries a site_patch",
          poll.get("ok") == 1 and poll.get("events") == [], poll)
    bot_unsub()

    # ---------- /api/v1/bots/site_get ----------
    def site_get(bot_id, key, **kw):
        d = {"bot_id": bot_id, "api_key": key, "channel": wiki,
             "path": "/index.mu"}
        d.update(kw)
        return call("/api/v1/bots/site_get", d, ident=bot_ident, link_id="BL")

    r = site_get(site_bot, site_key)
    check("SITE bot reads the published page",
          r.get("ok") == 1 and base64.b64decode(r["data_b64"]) == page
          and r.get("total") == len(page) and r.get("eof") is True
          and r.get("mime") == "text/micron"
          and r.get("can_edit") is True, r)
    r = site_get(site_bot, site_key, offset=2, len=5)
    check("chunked read slices the page",
          r.get("ok") == 1 and base64.b64decode(r["data_b64"]) == page[2:7]
          and r.get("offset") == 2 and r.get("eof") is False, r)
    r = site_get(msg_bot, msg_key)
    check("no SITE bit -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = site_get(site_bot, "sk-" + "0" * 64)
    check("wrong key -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = site_get(site_bot, site_key, channel=general)
    check("chat channel -> 404",
          r.get("ok") == 0 and r.get("code") == 404, r)
    r = site_get(site_bot, site_key, channel="f" * 64)
    check("unknown channel -> 404",
          r.get("ok") == 0 and r.get("code") == 404, r)
    r = site_get(site_bot, site_key, path="/missing.mu")
    check("missing resource -> 404 (can_edit still rides it)",
          r.get("ok") == 0 and r.get("code") == 404
          and r.get("can_edit") is True, r)
    check("site_get touches last_seen",
          bot_row(site_bot)["last_seen"] is not None, bot_row(site_bot))
    vault_hide(wiki)
    r = site_get(site_bot, site_key)
    check("owner without VIEW -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    vault_show(wiki)
    r = site_get(site_bot, site_key)
    check("owner VIEW restored -> ok again", r.get("ok") == 1, r)
    doomed, doomed_key = make_bot("doomed-site", BP["SITE"])
    call("/api/v1/bots/revoke", {"bot_id": doomed})
    r = site_get(doomed, doomed_key)
    check("revoked bot cannot read pages",
          r.get("ok") == 0 and r.get("code") == 403, r)

    # ---------- human handlers unchanged by the extraction ----------
    r = call("/api/v1/site/get",
             {"channel": wiki, "path": "/index.mu"})
    check("human site_get still reads the page",
          r.get("ok") == 1 and base64.b64decode(r["data_b64"]) == page, r)
    r = call("/api/v1/site/manifest", {"channel": wiki})
    check("human site_manifest still reads the manifest",
          r.get("ok") == 1 and r.get("manifest") == {"theme": "midnight"}, r)

    # ---------- /api/v1/bots/site_manifest ----------
    def site_manifest(bot_id, key, **kw):
        d = {"bot_id": bot_id, "api_key": key, "channel": wiki}
        d.update(kw)
        return call("/api/v1/bots/site_manifest", d,
                    ident=bot_ident, link_id="BL")

    r = site_manifest(site_bot, site_key)
    check("SITE bot reads the manifest",
          r.get("ok") == 1 and r.get("manifest") == {"theme": "midnight"}, r)
    r = site_manifest(msg_bot, msg_key)
    check("manifest: no SITE bit -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    r = site_manifest(site_bot, site_key, channel=general)
    check("manifest: chat channel -> 404",
          r.get("ok") == 0 and r.get("code") == 404, r)
    vault_hide(wiki)
    r = site_manifest(site_bot, site_key)
    check("manifest: owner without VIEW -> 403",
          r.get("ok") == 0 and r.get("code") == 403, r)
    vault_show(wiki)
    r = site_manifest(site_bot, site_key)
    check("manifest: owner VIEW restored -> ok again",
          r.get("ok") == 1, r)

    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


main()
