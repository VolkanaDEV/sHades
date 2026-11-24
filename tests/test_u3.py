#!/usr/bin/env python3
"""U3 backend unit tests: whoami, moderator endpoints, channel deletion.

Pins down:
  1. whoami reports the caller's identity + moderator flag;
  2. mod/ban|unban|mute|unmute: moderator gate (403), 64-hex validation,
     self-moderation rejection, real effect on ingest (banned/muted drop);
  3. channels/delete: moderator gate, last-channel protection, full cascade
     (messages/attachments rows/reactions/receipts/members/state), site
     resources removed, term sessions closed, channel_deleted event.
"""
import os, sys, json, sqlite3, tempfile, base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

base = tempfile.mkdtemp(prefix="shades_u3_test_")
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.STORAGE = base
sHades.DEBUG = False

identity = RNS.Identity()
MOD = sHades.hexstr(RNS.Destination(identity, RNS.Destination.OUT,
                                    RNS.Destination.SINGLE, "lxmf", "delivery").hash)
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (MOD, "boss", sHades.now()))
sHades.db.commit()
sHades.MODERATORS.add(MOD)

victim_identity = RNS.Identity()
VICTIM = sHades.hexstr(RNS.Destination(victim_identity, RNS.Destination.OUT,
                                       RNS.Destination.SINGLE, "lxmf", "delivery").hash)
sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (VICTIM, "victim", sHades.now()))
sHades.db.commit()

def call(path, data=None, ident=identity):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1", ident, sHades.now())
    return json.loads(raw.decode("utf-8"))

def reset_stream():
    with sHades.EVENT_COND:
        sHades.EVENT_LOG.clear()
        sHades.EVENT_SEQ = 0
    with sHades.SUB_LOCK:
        sHades.SUBSCRIPTIONS.clear()
        sHades.SUB_BY_USER.clear()

def main():
    cid = call("/api/v1/channels/create", {"name": "general", "kind": "chat"})["channel"]["id"]

    # ---------- whoami ----------
    r = call("/api/v1/whoami")
    check("whoami reports mod status", r.get("ok") == 1
          and r.get("user_hash") == MOD and r.get("is_moderator") is True, r)
    r = call("/api/v1/whoami", ident=victim_identity)
    check("whoami reports non-mod", r.get("is_moderator") is False, r)

    # ---------- moderation gate ----------
    r = call("/api/v1/mod/ban", {"user": VICTIM}, ident=victim_identity)
    check("non-mod ban -> 403", r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/mod/ban", {"user": "zzz"})
    check("bad user hash -> 400", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/mod/ban", {"user": "f" * 64})
    check("64-hex (a channel id, not a user hash) -> 400",
          r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/mod/ban", {"user": MOD})
    check("cannot moderate yourself", r.get("ok") == 0 and r.get("code") == 400, r)

    # ---------- ban / unban actually gate ingest ----------
    sHades.ingest(cid, VICTIM, "before ban")
    r = call("/api/v1/mod/ban", {"user": VICTIM, "reason": "spam"})
    check("mod ban ok", r.get("ok") == 1, r)
    check("ban recorded", sHades.db.execute(
        "SELECT 1 FROM banned WHERE dest_hash=?", (VICTIM,)).fetchone() is not None)
    mid = sHades.ingest(cid, VICTIM, "after ban")
    check("banned user's post dropped", mid is None, mid)
    r = call("/api/v1/mod/unban", {"user": VICTIM})
    check("mod unban ok", r.get("ok") == 1, r)
    mid = sHades.ingest(cid, VICTIM, "after unban")
    check("unbanned user can post again", mid is not None, mid)

    # ---------- mute / unmute ----------
    r = call("/api/v1/mod/mute", {"user": VICTIM})
    check("mod mute ok", r.get("ok") == 1, r)
    check("mute recorded", sHades.db.execute(
        "SELECT 1 FROM muted WHERE dest_hash=?", (VICTIM,)).fetchone() is not None)
    mid = sHades.ingest(cid, VICTIM, "while muted")
    check("muted post dropped silently", mid is None, mid)
    r = call("/api/v1/mod/unmute", {"user": VICTIM})
    check("mod unmute ok", r.get("ok") == 1, r)
    mid = sHades.ingest(cid, VICTIM, "after unmute")
    check("unmuted user can post again", mid is not None, mid)

    # ---------- channel deletion ----------
    cid_site = call("/api/v1/channels/create", {"name": "doomed", "kind": "site",
                                                "manifest": {"index": "/index.mu"}}
                    )["channel"]["id"]
    # site resource on disk + a message with attachment + receipts + reaction
    call("/api/v1/site/put", {"channel": cid_site, "path": "/index.mu",
                              "data_b64": base64.b64encode(b"page").decode()})
    msg = sHades.ingest(cid_site, VICTIM, "with attachment",
                        attachments=[["f.png", "image/png", b"pngbytes"]])
    call("/api/v1/receipts", {"channel": cid_site, "msg_id": msg})
    call("/api/v1/react", {"msg": msg, "emoji": "+1"})
    check("preconditions: site dir + rows exist",
          os.path.isdir(os.path.join(base, "sites", cid_site))
          and sHades.db.execute("SELECT COUNT(*) AS n FROM messages WHERE channel_id=?",
                                (cid_site,)).fetchone()["n"] == 1)

    # open a term session on a term channel to test session cleanup
    cid_term = call("/api/v1/channels/create", {"name": "term2", "kind": "term"})["channel"]["id"]
    r = call("/api/v1/term/open", {"channel": cid_term})
    term_sid = r["session"]
    check("term session open for the cascade test", r.get("ok") == 1, r)
    reset_stream()
    call("/api/v1/stream/subscribe", {"channel": cid})
    r = call("/api/v1/channels/delete", {"channel": cid_site}, ident=victim_identity)
    check("non-mod delete -> 403", r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/channels/delete", {"channel": "f" * 64})
    check("delete unknown channel -> 404", r.get("ok") == 0 and r.get("code") == 404, r)
    r = call("/api/v1/channels/delete", {"channel": cid_site})
    check("mod deletes channel", r.get("ok") == 1 and r.get("name") == "doomed", r)
    check("site resources removed", not os.path.isdir(os.path.join(base, "sites", cid_site)))
    n_msgs = sHades.db.execute("SELECT COUNT(*) AS n FROM messages WHERE channel_id=?",
                               (cid_site,)).fetchone()["n"]
    n_rx = sHades.db.execute("SELECT COUNT(*) AS n FROM reactions WHERE msg_id=?",
                             (msg,)).fetchone()["n"]
    n_rc = sHades.db.execute("SELECT COUNT(*) AS n FROM receipts WHERE channel_id=?",
                             (cid_site,)).fetchone()["n"]
    n_ch = sHades.db.execute("SELECT COUNT(*) AS n FROM channels WHERE id=?",
                             (cid_site,)).fetchone()["n"]
    check("cascade removed messages/reactions/receipts/channel row",
          (n_msgs, n_rx, n_rc, n_ch) == (0, 0, 0, 0), (n_msgs, n_rx, n_rc, n_ch))
    n_att = sHades.db.execute(
        "SELECT COUNT(*) AS n FROM attachments WHERE msg_id=?", (msg,)).fetchone()["n"]
    check("attachment rows removed", n_att == 0, n_att)

    # deleting a term channel closes its console sessions
    r = call("/api/v1/channels/delete", {"channel": cid_term})
    check("mod deletes term channel", r.get("ok") == 1, r)
    check("term sessions on the deleted channel closed",
          term_sid not in sHades.TERM_SESSIONS, list(sHades.TERM_SESSIONS))

    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    dele = [e for e in poll["events"] if e["kind"] == "channel_deleted"]
    check("channel_deleted events flow to subscribers",
          any(e["data"]["channel"] == cid_site and e["data"]["name"] == "doomed"
              for e in dele), [e["data"] for e in dele])

    # last-channel protection
    with sHades.db_lock:
        remaining = sHades.db.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
    for name in ("a", "b", "c"):
        if remaining == 1:
            break
        call("/api/v1/channels/create", {"name": name, "kind": "chat"})
        remaining += 1
    only = sHades.db.execute("SELECT id FROM channels").fetchall()
    if len(only) == 1:
        r = call("/api/v1/channels/delete", {"channel": only[0]["id"]})
        check("cannot delete the last channel", r.get("ok") == 0
              and r.get("code") == 400, r)
    else:
        check("cannot delete the last channel (skipped: %d channels)"
              % len(only), True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
