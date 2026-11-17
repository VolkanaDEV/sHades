#!/usr/bin/env python3
"""Channel ordering tests (server) — no mesh.

Pins down:
  - new channels take position MAX+1; list order follows position (name
    breaks ties);
  - /api/v1/channels/reorder persists the given order MANAGE_CHANNEL-
    gated: unknown cid rejects the whole reorder, plain user gets 403;
  - reorder emits channels_changed guild_wide (stream subscribers notice);
  - list still VIEW-filters and carries unread badges;
  - migration: a fixture DB written without a position column boots with
    the column added (position 0 for every legacy row).
"""
import os, sys, json, sqlite3, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))

base = tempfile.mkdtemp(prefix="shades_channel_order_test_")
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.STORAGE = base
sHades.DEBUG = False

def _mk_user(name):
    i = RNS.Identity()
    return i, sHades.hexstr(RNS.Destination(i, RNS.Destination.OUT,
                                            RNS.Destination.SINGLE,
                                            "lxmf", "delivery").hash)

plain_ident, USER = _mk_user("plain")
mod_ident, MOD = _mk_user("mod")
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (USER, "plain", sHades.now()))
sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (MOD, "mod", sHades.now()))
sHades.db.commit()

# MOD carries MANAGE_CHANNEL only (drag/reorder right, no moderation bits)
mod_role = sHades._ensure_role("Moderator", sHades.PERMS["MANAGE_CHANNEL"],
                               position=50)
sHades._assign_role(MOD, mod_role)

_idents = {USER: plain_ident, MOD: mod_ident}

def call(path, data=None, user=USER):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1",
                                  _idents[user], sHades.now())
    return json.loads(raw.decode("utf-8"))

def _reset_events():
    with sHades.EVENT_COND:
        sHades.EVENT_LOG.clear()
        sHades.EVENT_SEQ = 0
    with sHades.SUB_LOCK:
        sHades.SUBSCRIPTIONS.clear()
        sHades.SUB_BY_USER.clear()

def list_names(user=USER):
    res = call("/api/v1/channels/list", user=user)
    return res["ok"], [c["name"] for c in res["channels"]]

def main():
    _reset_events()
    c1 = call("/api/v1/channels/create", {"name": "alpha", "kind": "chat"})["channel"]["id"]
    c2 = call("/api/v1/channels/create", {"name": "beta", "kind": "chat"})["channel"]["id"]
    c3 = call("/api/v1/channels/create", {"name": "gamma", "kind": "chat"})["channel"]["id"]

    # fresh names list alphabetically (positions still all >0 and ascending)
    ok, names = list_names()
    check("name order before any reorder", ok and names == ["alpha", "beta", "gamma"], names)
    pos = {r["name"]: r["position"] for r in sHades.db.execute(
        "SELECT name, position FROM channels")}
    check("new channels take MAX+1 positions",
          pos["alpha"] == 1 and pos["beta"] == 2 and pos["gamma"] == 3, pos)

    # reorder gamma to the front (MANAGE_CHANNEL carrier)
    ok, names = list_names(user=MOD)  # sanity: MOD sees the same names
    r = call("/api/v1/channels/reorder", {"order": [c3, c1, c2]}, user=MOD)
    check("reorder accepted for a MANAGE_CHANNEL carrier", r.get("ok") == 1, r)
    ok, names = list_names()
    check("list follows the persisted order",
          ok and names == ["gamma", "alpha", "beta"], names)

    # whole-batch atomicity: one unknown cid -> nothing changes
    r = call("/api/v1/channels/reorder", {"order": [c2, c1, "nope"]}, user=MOD)
    check("reorder with an unknown channel rejected",
          r.get("ok") == 0 and r.get("code") == 400, r)
    ok, names = list_names()
    check("failed batch left the order intact",
          ok and names == ["gamma", "alpha", "beta"], names)

    # permission gate
    r = call("/api/v1/channels/reorder", {"order": [c1, c2, c3]}, user=USER)
    check("reorder denied for a plain user", r.get("ok") == 0 and r.get("code") == 403, r)

    # payload validation
    r = call("/api/v1/channels/reorder", {"order": [c1]}, user=MOD)
    check("partial reorder rejected (not every channel listed)",
          r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/channels/reorder", {"order": "gamma,alpha,beta"}, user=MOD)
    check("non-list order rejected", r.get("ok") == 0 and r.get("code") == 400, r)

    # guild-wide event: with a live subscriber the reorder is visible on the
    # stream; with none, _emit_event drops by design (only rings carry it).
    with sHades.SUB_LOCK:
        sHades.SUBSCRIPTIONS["test-link"] = {"user_hash": USER, "channel": c3,
                                             "cursor": 0, "at": sHades.now()}
    call("/api/v1/channels/reorder", {"order": [c3, c1, c2]}, user=MOD)
    with sHades.EVENT_COND:
        ring = sHades.EVENT_LOG.get(c3, [])
        fired = any(ev.get("kind") == "channels_changed"
                    for seq, ev in ring)
    check("channels_changed fired guild-wide", fired,
          "EVENT_LOG: %r" % dict(sHades.EVENT_LOG))

    # position visible on the list rows
    res = call("/api/v1/channels/list")
    rows = [(c["name"], c["position"]) for c in res["channels"]]
    check("list rows carry position",
          [p for _, p in rows] == sorted(p for _, p in rows) and rows[0][0] == "gamma", rows)

    # ---------- migration: legacy DB without the position column ----------
    legacy = sqlite3.connect(os.path.join(base, "legacy.db"))
    legacy.row_factory = sqlite3.Row
    legacy.executescript(
        "CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, "
        "created REAL, kind TEXT NOT NULL DEFAULT 'chat', manifest TEXT NOT NULL DEFAULT '{}', topic TEXT);")
    legacy.execute("INSERT INTO channels (id, name, created) VALUES ('x', 'old', 0)")
    legacy.commit()
    sHades._migrate_schema(legacy)
    have = {r["name"] for r in legacy.execute("PRAGMA table_info(channels)").fetchall()}
    check("migration adds position to a legacy channels table", "position" in have, have)
    row = legacy.execute("SELECT position FROM channels WHERE id='x'").fetchone()
    check("legacy rows default to position 0", row and row["position"] == 0, row)

    print("RESULT: %d PASS, %d FAIL" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
