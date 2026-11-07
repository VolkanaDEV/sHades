#!/usr/bin/env python3
"""Term channel (guild console) unit tests — no RNS mesh needed.

Pins down:
  1. session lifecycle: open (kind-gated, capped), data, resize, close, quit;
  2. the line protocol: partial input buffers, CRLF handling, multiple lines
     per payload;
  3. the command table: help/who/channels/msg/info/presence/uptime/clear —
     and 'msg' landing in the real ledger through ingest;
  4. sanitisation: control chars (ANSI escapes) stripped from input and output;
  5. ownership (a stranger cannot drive or close a session), idle expiry,
     per-user session caps and the input rate limit;
  6. output rides term_data events on the channel stream.
"""
import os, sys, json, time, sqlite3, tempfile, base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

base = tempfile.mkdtemp(prefix="shades_term_test_")
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
                  (USER, "operator", sHades.now()))
sHades.db.commit()
sHades.MODERATORS.add(USER)

other = RNS.Identity()
OTHER = sHades.hexstr(RNS.Destination(other, RNS.Destination.OUT,
                                      RNS.Destination.SINGLE, "lxmf", "delivery").hash)
sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (OTHER, "stranger", sHades.now()))
sHades.db.commit()

def call(path, data=None, ident=identity, link_id="L1"):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", link_id, ident, sHades.now())
    return json.loads(raw.decode("utf-8"))

def b64(s):
    return base64.b64encode(s.encode() if isinstance(s, str) else s).decode()

def reset_stream():
    with sHades.EVENT_COND:
        sHades.EVENT_LOG.clear()
        sHades.EVENT_SEQ = 0
    with sHades.SUB_LOCK:
        sHades.SUBSCRIPTIONS.clear()
        sHades.SUB_BY_USER.clear()

def main():
    cid_chat = call("/api/v1/channels/create", {"name": "general", "kind": "chat"})["channel"]["id"]
    cid_term = call("/api/v1/channels/create", {"name": "console", "kind": "term"})["channel"]["id"]

    # ---------- open: kind gating ----------
    r = call("/api/v1/term/open", {"channel": cid_chat})
    check("term open on chat channel -> 404", r.get("ok") == 0 and r.get("code") == 404, r)
    r = call("/api/v1/term/open", {"channel": "f" * 64})
    check("term open on unknown channel -> 404", r.get("ok") == 0 and r.get("code") == 404, r)

    reset_stream()
    call("/api/v1/stream/subscribe", {"channel": cid_term})
    r = call("/api/v1/term/open", {"channel": cid_term})
    check("term open ok", r.get("ok") == 1 and r.get("session"), r)
    sid = r["session"]

    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    banner = [e for e in poll["events"] if e["kind"] == "term_data"]
    check("banner + prompt arrive as term_data events",
          banner and "guild console" in banner[0]["data"]["text"]
          and "> " in banner[0]["data"]["text"], poll)
    check("term_data carries the session id",
          banner and banner[0]["data"]["session"] == sid, poll)

    # ---------- line protocol ----------
    r = call("/api/v1/term/data", {"session": sid, "data_b64": b64("he")})
    check("partial line buffered (no execution yet)", r.get("ok") == 1, r)
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    check("no output event for a partial line",
          not [e for e in poll["events"] if e["kind"] == "term_data"], poll)
    r = call("/api/v1/term/data", {"session": sid, "data_b64": b64("llo\n")})
    check("newline completes the line", r.get("ok") == 1, r)
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    unk = [e for e in poll["events"] if e["kind"] == "term_data"]
    check("unknown command answered", unk and "unknown command: hello" in unk[0]["data"]["text"], poll)

    # CRLF + two commands in one payload
    call("/api/v1/term/data", {"session": sid, "data_b64": b64("uptime\r\nhelp\r\n")})
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    outs = [e["data"]["text"] for e in poll["events"] if e["kind"] == "term_data"]
    check("CRLF handled + both commands executed",
          any("up " in o for o in outs) and any("commands:" in o for o in outs), outs)

    # ---------- commands ----------
    call("/api/v1/term/data", {"session": sid, "data_b64": b64("who\n")})
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    who = [e["data"]["text"] for e in poll["events"] if e["kind"] == "term_data"]
    check("who lists the (pinging) operator", who and "nobody online" in who[0], who)
    sHades.API_ROUTES["/api/register_node"](
        "/api/register_node", {"node_hash": "1" * 32, "address": "10.0.0.1", "port": 29222},
        "r", "L9", identity, sHades.now())
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    call("/api/v1/term/data", {"session": sid, "data_b64": b64("who\n")})
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    who = [e["data"]["text"] for e in poll["events"] if e["kind"] == "term_data"]
    check("who lists the online operator by name",
          who and "operator" in who[0] and "node 111111111111" in who[0], who)

    r = call("/api/v1/term/data", {"session": sid, "data_b64": b64("msg general hello from the console\n")})
    check("msg command accepted", r.get("ok") == 1, r)
    msgs = sHades.db.execute("SELECT content FROM messages WHERE channel_id=?",
                             (cid_chat,)).fetchall()
    check("msg landed in the real ledger",
          any(m["content"] == "hello from the console" for m in msgs), [dict(m) for m in msgs])
    r = call("/api/v1/term/data", {"session": sid, "data_b64": b64("msg nosuch hi\n")})
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    err = [e["data"]["text"] for e in poll["events"] if e["kind"] == "term_data"]
    check("msg to unknown channel errors",
          any("unknown channel: #nosuch" in t for t in err), err)

    call("/api/v1/term/data", {"session": sid, "data_b64": b64("channels\ninfo general\npresence\n")})
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    outs = " ".join(e["data"]["text"] for e in poll["events"] if e["kind"] == "term_data")
    check("channels lists kinds", "#console (term)" in outs, outs[:200])
    check("info shows the channel", "#general  chat  seq" in outs, outs[:300])
    check("presence lists users", "operator" in outs, outs[:300])

    call("/api/v1/term/data", {"session": sid, "data_b64": b64("clear\n")})
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    clr = [e for e in poll["events"] if e["kind"] == "term_data"]
    check("clear emits a structured clear event",
          clr and clr[0]["data"].get("clear") is True
          and not any("\x1b" in e["data"]["text"] for e in clr), clr)

    # ---------- sanitisation ----------
    r = call("/api/v1/term/data", {"session": sid,
                                   "data_b64": b64("msg general \x1b[31mred\x07text\n")})
    msgs = sHades.db.execute("SELECT content FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT 1",
                             (cid_chat,)).fetchone()
    check("control chars stripped from console input",
          msgs["content"] == "redtext", repr(msgs["content"]))

    # ---------- ownership ----------
    r = call("/api/v1/term/data", {"session": sid, "data_b64": b64("who\n")}, ident=other)
    check("stranger cannot drive a session", r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/term/close", {"session": sid}, ident=other)
    check("stranger cannot close a session", r.get("ok") == 0 and r.get("code") == 403, r)
    check("session survived the stranger's close attempt",
          sid in sHades.TERM_SESSIONS)
    r = call("/api/v1/term/data", {"session": "deadbeef", "data_b64": b64("who\n")})
    check("unknown session -> 404", r.get("ok") == 0 and r.get("code") == 404, r)

    # ---------- resize ----------
    r = call("/api/v1/term/resize", {"session": sid, "rows": 50, "cols": 120})
    check("resize ok", r.get("ok") == 1, r)
    r = call("/api/v1/term/resize", {"session": sid, "rows": "abc"})
    check("bad resize rejected", r.get("ok") == 0 and r.get("code") == 400, r)

    # ---------- caps ----------
    sids = [sid]
    for i in range(sHades.TERM_MAX_PER_USER - 1):
        r = call("/api/v1/term/open", {"channel": cid_term})
        check(f"extra session {i + 2} ok", r.get("ok") == 1, r)
        sids.append(r["session"])
    r = call("/api/v1/term/open", {"channel": cid_term})
    check("per-user session cap enforced", r.get("ok") == 0 and r.get("code") == 429, r)
    for s in sids[1:]:
        call("/api/v1/term/close", {"session": s})

    # ---------- close + quit ----------
    r = call("/api/v1/term/close", {"session": sid})
    check("close ok", r.get("ok") == 1, r)
    r = call("/api/v1/term/data", {"session": sid, "data_b64": b64("who\n")})
    check("data after close -> 404", r.get("ok") == 0 and r.get("code") == 404, r)

    r = call("/api/v1/term/open", {"channel": cid_term})
    sid2 = r["session"]
    r = call("/api/v1/term/data", {"session": sid2, "data_b64": b64("quit\n")})
    check("quit closes the session", r.get("ok") == 1 and r.get("closed") is True, r)
    check("quit removed the session", sid2 not in sHades.TERM_SESSIONS)

    # ---------- idle expiry ----------
    old_ttl = sHades.TERM_IDLE_S
    sHades.TERM_IDLE_S = 0.3
    r = call("/api/v1/term/open", {"channel": cid_term})
    sid3 = r["session"]
    time.sleep(0.5)
    r = call("/api/v1/term/data", {"session": sid3, "data_b64": b64("who\n")})
    sHades.TERM_IDLE_S = old_ttl
    check("idle session purged", r.get("ok") == 0 and r.get("code") == 404, r)

    # ---------- input rate limit (keep last: exhausts the window) ----------
    r = call("/api/v1/term/open", {"channel": cid_term})
    sid4 = r["session"]
    codes = []
    for i in range(sHades.TERM_IN_RATE[0] + 2):
        r = call("/api/v1/term/data", {"session": sid4, "data_b64": b64(f"x{i}\n")})
        codes.append(r.get("code") if r.get("ok") == 0 else None)
    check("term input rate limited", 429 in codes, codes)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
