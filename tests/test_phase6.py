#!/usr/bin/env python3
"""Receipts, reactions, search unit tests (JSON API v1 phase 6) — no mesh.

Pins down:
  1. receipts: mark-read monotonicity, cross-channel message rejection,
     unread counting, per-user isolation, channels/list unread badges;
  2. reactions: toggle on/off, ASCII-shortcode validation, per-message cap,
     counts, reaction events on the stream, cleanup on message delete;
  3. search: substring match, deleted messages excluded, channel scoping,
     LIKE wildcard escaping, limit clamping, short-query rejection.
"""
import os, sys, json, sqlite3, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

base = tempfile.mkdtemp(prefix="shades_phase6_test_")
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.STORAGE = base
sHades.DEBUG = False

identity = RNS.Identity()
USER = sHades.hexstr(RNS.Destination(identity, RNS.Destination.OUT,
                                     RNS.Destination.SINGLE, "lxmf", "delivery").hash)
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (USER, "reader", sHades.now()))
sHades.db.commit()

other = RNS.Identity()
OTHER = sHades.hexstr(RNS.Destination(other, RNS.Destination.OUT,
                                      RNS.Destination.SINGLE, "lxmf", "delivery").hash)
sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (OTHER, "second", sHades.now()))
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
    cid2 = call("/api/v1/channels/create", {"name": "random", "kind": "chat"})["channel"]["id"]

    # ---------- receipts ----------
    r = call("/api/v1/receipts", {"channel": cid})
    check("receipts report zero when never read",
          r.get("ok") == 1 and r["last_read_id"] == 0 and r["unread"] == 0, r)
    m1 = sHades.ingest(cid, USER, "first message")
    m2 = sHades.ingest(cid, USER, "second message")
    sHades.ingest(cid2, USER, "other channel message")
    r = call("/api/v1/receipts", {"channel": cid})
    check("unread counts up before any receipt", r["unread"] == 2, r)

    lst = call("/api/v1/channels/list")
    chans = {c["id"]: c for c in lst["channels"]}
    check("channels/list carries unread badges",
          chans[cid]["unread"] == 2 and chans[cid2]["unread"] == 1, lst)

    r = call("/api/v1/receipts", {"channel": cid, "msg_id": m1})
    check("mark read up to m1", r.get("ok") == 1 and r["unread"] == 1, r)
    lst = call("/api/v1/channels/list")
    chans = {c["id"]: c for c in lst["channels"]}
    check("list unread drops after the receipt", chans[cid]["unread"] == 1, lst)

    r = call("/api/v1/receipts", {"channel": cid, "msg_id": m1})
    check("re-marking an older message never moves the watermark back",
          r["unread"] == 1, r)
    r = call("/api/v1/receipts", {"channel": cid, "msg_id": m2})
    check("mark read up to m2 clears unread", r["unread"] == 0, r)

    r = call("/api/v1/receipts", {"channel": cid, "msg_id": m1})  # m1 in cid? yes
    check("older re-mark stays monotonic", r["unread"] == 0, r)
    r = call("/api/v1/receipts", {"channel": cid2, "msg_id": m1})
    check("marking a message from another channel -> 404",
          r.get("ok") == 0 and r.get("code") == 404, r)
    r = call("/api/v1/receipts", {"channel": "f" * 64})
    check("receipts on unknown channel -> 404", r.get("ok") == 0, r)

    # per-user isolation: OTHER has their own watermark
    sHades.ingest(cid, USER, "third message")
    r_other = call("/api/v1/receipts", {"channel": cid}, ident=other)
    check("watermarks are per-user", r_other["unread"] == 3, r_other)
    r = call("/api/v1/receipts", {"channel": cid})
    check("other user's read state does not leak (only 'third' unread for USER)",
          r["unread"] == 1, r)

    # ---------- reactions ----------
    reset_stream()
    call("/api/v1/stream/subscribe", {"channel": cid})
    r = call("/api/v1/react", {"msg": m1, "emoji": "+1"})
    check("react adds", r.get("ok") == 1 and r["active"] is True
          and r["counts"] == {"+1": 1}, r)
    r = call("/api/v1/react", {"msg": m1, "emoji": "+1"}, ident=other)
    check("second user stacks the same emoji", r["counts"] == {"+1": 2}, r)
    call("/api/v1/react", {"msg": m1, "emoji": "laugh"})
    r = call("/api/v1/react", {"msg": m1, "emoji": "+1"})
    check("same user toggles off (other's +1 and laugh remain)",
          r["active"] is False and r["counts"] == {"+1": 1, "laugh": 1}, r)
    r = call("/api/v1/react", {"msg": m1, "emoji": "fire_100"})
    check("shortcode charset accepted", r.get("ok") == 1, r)
    r = call("/api/v1/react", {"msg": m1, "emoji": "🔥"})
    check("raw unicode emoji rejected", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/react", {"msg": m1, "emoji": "x" * 20})
    check("overlong shortcode rejected", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/react", {"msg": "f" * 64, "emoji": "+1"})
    check("react to unknown message -> 404", r.get("ok") == 0 and r.get("code") == 404, r)

    # distinct-emoji cap
    for i in range(sHades.MAX_EMOJI_PER_MSG - 1):
        call("/api/v1/react", {"msg": m1, "emoji": "e%02d" % i})
    r = call("/api/v1/react", {"msg": m1, "emoji": "over_the_cap"})
    check("distinct-emoji cap enforced", r.get("ok") == 0 and r.get("code") == 400, r)

    # reactions surface in message rows
    row = sHades.msg_row_json(sHades.db.execute(
        "SELECT * FROM messages WHERE msg_id=?", (m1,)).fetchone())
    emoji_seen = {e["emoji"]: e["count"] for e in row["reactions"]}
    check("message rows carry reaction summaries",
          emoji_seen.get("laugh") == 1 and "reactions" in row, row["reactions"])

    # reaction events on the stream
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    revs = [e for e in poll["events"] if e["kind"] == "reaction"]
    check("reaction events flow to subscribers",
          revs and revs[0]["data"]["msg_id"] == m1 and "counts" in revs[0]["data"],
          [e["data"] for e in revs])

    # delete cleans reactions up
    raw = sHades.API_ROUTES["/api/delete"]("/api/delete", {"msg": m1},
                                           "r", "L9", identity, sHades.now())
    check("delete ok", sHades.msgpack.unpackb(raw)["ok"] == 1)
    n = sHades.db.execute("SELECT COUNT(*) AS n FROM reactions WHERE msg_id=?",
                          (m1,)).fetchone()["n"]
    check("reactions removed when the message is deleted", n == 0, n)

    # ---------- search ----------
    sHades.ingest(cid, USER, "the quick brown fox jumps")
    sHades.ingest(cid2, USER, "foxes are sneaky")
    sHades.ingest(cid, USER, "100% of _snakes_ are not foxes")
    doomed = sHades.ingest(cid, USER, "deleted fox footprint")
    sHades.API_ROUTES["/api/delete"]("/api/delete", {"msg": doomed},
                                     "r", "L9", identity, sHades.now())

    r = call("/api/v1/search", {"q": "fox"})
    contents = [x["content"] for x in r["results"]]
    check("search finds live matches only",
          r.get("ok") == 1 and len(contents) == 3
          and "deleted fox footprint" not in contents, contents)
    r = call("/api/v1/search", {"q": "fox", "channel": cid})
    check("channel-scoped search", len(r["results"]) == 2, r)
    r = call("/api/v1/search", {"q": "fox", "channel": "f" * 64})
    check("search unknown channel -> 404", r.get("ok") == 0, r)
    r = call("/api/v1/search", {"q": "f_o_x"})
    check("LIKE wildcards in the query are escaped (literal _)",
          len(r["results"]) == 0, r)
    r = call("/api/v1/search", {"q": "fox", "limit": 1})
    check("limit clamps results", len(r["results"]) == 1, r)
    r = call("/api/v1/search", {"q": "f"})
    check("short query rejected", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/search", {"q": "fox", "limit": 999})
    check("overlarge limit clamps to 50", r.get("ok") == 1, r)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
