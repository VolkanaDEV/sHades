#!/usr/bin/env python3
"""Slash-command dispatcher unit tests (U4) — no RNS mesh needed.

The dispatcher is shared by the term console and /api/v1/command; these tests
exercise it through the JSON endpoint. Pins down:
  1. the ephemeral contract: output returns to the caller, nothing is posted;
  2. moderation commands by name or hash, with gating and self-guards;
  3. create/delete/rename/topic through the dispatcher incl. permission and
     collision errors;
  4. rate limiting, sanitisation, and quit/clear semantics in slash context.
"""
import os, sys, json, sqlite3, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

base = tempfile.mkdtemp(prefix="shades_cmd_test_")
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

other = RNS.Identity()
OTHER = sHades.hexstr(RNS.Destination(other, RNS.Destination.OUT,
                                      RNS.Destination.SINGLE, "lxmf", "delivery").hash)
sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (OTHER, "guest user", sHades.now()))
sHades.db.commit()

def call(path, data=None, ident=identity):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1", ident, sHades.now())
    return json.loads(raw.decode("utf-8"))

def slash(text, ident=identity):
    return call("/api/v1/command", {"input": text}, ident=ident)

def main():
    cid = call("/api/v1/channels/create", {"name": "general", "kind": "chat"})["channel"]["id"]

    # ---------- ephemeral contract ----------
    sHades.ingest(cid, MOD, "marker message")
    before = sHades.db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    r = slash("/who")
    check("slash returns ok + output", r.get("ok") == 1 and isinstance(r.get("output"), str), r)
    after = sHades.db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    check("command output is ephemeral (no message posted)", after == before, (before, after))
    r = slash("/nosuchthing")
    check("unknown command answered in output", "unknown command" in r["output"], r)
    r = slash("")
    check("empty input -> 400", r.get("ok") == 0 and r.get("code") == 400, r)

    # ---------- moderation by name (with a space) and by hash ----------
    sHades.ingest(cid, OTHER, "guest speaking")
    r = slash("/mute guest user", ident=other)
    check("non-mod mute denied in output", "moderator required" in r["output"], r)
    r = slash("/mute guest user")
    check("mod mutes by name (spaces ok)", "muted guest user" in r["output"], r)
    mid = sHades.ingest(cid, OTHER, "while muted")
    check("muted user dropped", mid is None, mid)
    r = slash("/unmute guest user")
    check("unmute by name", "unmuted guest user" in r["output"], r)
    mid = sHades.ingest(cid, OTHER, "after unmute")
    check("unmuted user posts again", mid is not None, mid)
    r = slash("/ban " + OTHER)
    check("ban by 32-hex hash", "banned" in r["output"], r)
    check("ban recorded", sHades.db.execute(
        "SELECT 1 FROM banned WHERE dest_hash=?", (OTHER,)).fetchone() is not None)
    r = slash("/unban guest user")
    check("unban by name", "unbanned" in r["output"], r)
    r = slash("/mute nobodyhere")
    check("unknown user reported", "unknown user" in r["output"], r)
    r = slash("/ban boss", ident=other)
    check("non-mod ban denied", "moderator required" in r["output"], r)

    # ---------- create / delete through the dispatcher ----------
    r = slash("/create lounge site", ident=other)
    check("non-mod site create denied", "moderator required" in r["output"], r)
    r = slash("/create lounge site")
    check("mod creates site via slash", "created #lounge" in r["output"], r)
    lounge = sHades.db.execute("SELECT id FROM channels WHERE name='lounge'").fetchone()["id"]
    r = slash("/create lounge")
    check("duplicate create denied", "channel exists" in r["output"], r)
    r = slash("/create bad_name!!")
    check("invalid name denied", "invalid channel name" in r["output"], r)
    r = slash("/create lounge nope")
    check("invalid kind denied", "kind must be one of" in r["output"], r)

    # ---------- rename / topic ----------
    r = slash("/rename lounge lounge2")
    check("mod renames", "renamed #lounge -> #lounge2" in r["output"], r)
    cl = call("/api/v1/channels/list")
    names = [c["name"] for c in cl["channels"]]
    check("rename visible in channels/list", "lounge2" in names and "lounge" not in names, names)
    r = slash("/rename lounge2 general")
    check("rename collision denied", "name already taken" in r["output"], r)
    r = slash("/rename lounge2 x", ident=other)
    check("non-mod rename denied", "moderator required" in r["output"], r)
    r = slash("/topic lounge2 welcome to the new lounge")
    check("mod sets topic", "topic set" in r["output"], r)
    info = call("/api/v1/channels/info", {"channel": lounge})
    check("topic visible in channels/info",
          info["channel"]["topic"] == "welcome to the new lounge", info["channel"])
    r = slash("/topic lounge2")
    check("empty topic clears", "topic cleared" in r["output"], r)
    r = slash("/rename", ident=other)
    check("garbage rename usage handled", "usage" in r["output"] or "moderator" in r["output"], r)

    # ---------- delete through the dispatcher ----------
    r = slash("/delete lounge2")
    check("mod deletes via slash", "deleted #lounge2" in r["output"], r)
    cl = call("/api/v1/channels/list")
    check("deleted channel gone", all(c["name"] != "lounge2" for c in cl["channels"]), cl)
    r = slash("/delete nosuch")
    check("delete unknown channel", "unknown channel" in r["output"], r)

    # ---------- clear / quit in slash context ----------
    r = slash("/clear")
    check("clear returns the clear flag", r.get("clear") is True and r.get("output") == "", r)
    r = slash("/quit")
    check("quit in slash context is inert", r.get("ok") == 1 and r.get("quit") is True, r)

    # ---------- sanitisation + rate limit (keep last) ----------
    r = slash("/msg general \x1b[31mred\x07text")
    msgs = sHades.db.execute("SELECT content FROM messages ORDER BY id DESC LIMIT 1").fetchone()
    check("ANSI stripped from slash input", msgs["content"] == "redtext",
          repr(msgs["content"]))
    codes = []
    for i in range(sHades.TERM_IN_RATE[0] + 2):
        r = slash("/uptime")
        codes.append(r.get("code") if r.get("ok") == 0 else None)
    check("slash commands rate limited", 429 in codes, codes)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
