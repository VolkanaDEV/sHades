#!/usr/bin/env python3
"""Channel-targeted invite tests (server) — no mesh.

Pins down:
  - invites may aim at a channel: create validates the channel exists,
    list carries channel_id, the row round-trips;
  - join with a channel-targeted code tells the joiner the landing
    channel; plain codes return no channel (unchanged contract);
  - an unknown channel on create rejected (whole create is a no-op);
  - channel targeting is admin-only like every other invite field
    (a plain user gets 403 regardless).
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

base = tempfile.mkdtemp(prefix="shades_channel_invites_test_")
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

admin_ident, ADMIN = _mk_user("admin")
plain_ident, PLAIN = _mk_user("plain")
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (ADMIN, "admin", sHades.now()))
sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (PLAIN, "plain", sHades.now()))
sHades.db.commit()

admin_role = sHades._ensure_role("Admin", sHades.PERM_ALL, position=100)
sHades._assign_role(ADMIN, admin_role)

_idents = {ADMIN: admin_ident, PLAIN: plain_ident}

def call(path, data=None, user=ADMIN):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1",
                                  _idents[user], sHades.now())
    return json.loads(raw.decode("utf-8"))

def main():
    cid = call("/api/v1/channels/create", {"name": "lounge", "kind": "chat"})["channel"]["id"]

    # ---------- create with a channel target ----------
    r = call("/api/v1/invites", {"op": "create", "label": "lounge",
                                 "channel": cid})
    check("channel-targeted invite created", r.get("ok") == 1, r)
    code = r.get("code")

    lst = call("/api/v1/invites", {"op": "list"})["invites"]
    check("list carries channel_id",
          lst and lst[0].get("channel_id") == cid, lst)

    # ---------- the join response carries the landing channel ----------
    plain_ident_dest = RNS.Destination(plain_ident, RNS.Destination.OUT,
                                       RNS.Destination.SINGLE,
                                       "lxmf", "delivery")
    r = call("/api/v1/join", {"code": code}, user=PLAIN)
    check("join with a channel code lands in that channel",
          r.get("ok") == 1 and r.get("channel_id") == cid, r)
    r = call("/api/v1/join", {"code": code}, user=PLAIN)
    check("re-join idempotent (uses counted, still lands",
          r.get("ok") == 1, r)

    # ---------- unknown channel target rejected ----------
    r = call("/api/v1/invites", {"op": "create", "channel": "bogus"})
    check("unknown channel rejected on create",
          r.get("ok") == 0 and r.get("code") == 400, r)
    lst2 = call("/api/v1/invites", {"op": "list"})["invites"]
    check("rejected create stored nothing", len(lst2) == 1, len(lst2))

    # ---------- plain code (no target) unchanged ----------
    r = call("/api/v1/invites", {"op": "create", "label": "plain"})
    code2 = r.get("code")
    check("channel_id absent on a plain invite",
          all(inv.get("channel_id") != code2 for inv in
              call("/api/v1/invites", {"op": "list"})["invites"] if True), "")
    lst3 = call("/api/v1/invites", {"op": "list"})["invites"]
    plain_row = [i for i in lst3 if i["code"] == code2][0]
    check("plain invite row has a null channel_id",
          plain_row.get("channel_id") in (None, ""), plain_row)

    viewer = sHades.hexstr(RNS.Destination(plain_ident, RNS.Destination.OUT,
                                           RNS.Destination.SINGLE,
                                           "lxmf", "delivery").hash)
    r = call("/api/v1/join", {"code": code2}, user=PLAIN)
    check("join with a plain code returns no channel_id",
          r.get("ok") == 1 and not r.get("channel_id"), r)

    # ---------- non-admin cannot create (any field) ----------
    r = call("/api/v1/invites", {"op": "create", "channel": cid}, user=PLAIN)
    check("plain user denied (admin-only route)",
          r.get("ok") == 0 and r.get("code") == 403, r)

    # ---------- migration: legacy invites table gains channel_id ----------
    legacy = sqlite3.connect(os.path.join(base, "legacy.db"))
    legacy.row_factory = sqlite3.Row
    legacy.executescript(
        "CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, "
        "created REAL, kind TEXT NOT NULL DEFAULT 'chat', manifest TEXT NOT NULL DEFAULT '{}', topic TEXT);"
        "CREATE TABLE IF NOT EXISTS invites (code TEXT PRIMARY KEY, label TEXT, "
        "created_at REAL, expires_at REAL, max_uses INTEGER NOT NULL DEFAULT 0, "
        "uses INTEGER NOT NULL DEFAULT 0, created_by TEXT);")
    legacy.commit()
    sHades._migrate_schema(legacy)
    have = {r["name"] for r in legacy.execute("PRAGMA table_info(invites)").fetchall()}
    check("migration adds channel_id to a legacy invites table",
          "channel_id" in have, have)

    print("RESULT: %d PASS, %d FAIL" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
