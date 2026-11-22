#!/usr/bin/env python3
"""Bot registry + management endpoints unit tests (Group 1) — no RNS mesh.

Pins down:
  - create returns bot + one-time api key + gateway, and stores ONLY the
    SHA-256 hash of the key (raw key nowhere in the row);
  - the key is never retrievable via list/info (no key_hash, no raw key);
  - create/update validation: name 1-64 chars, perms an int bitmask 0-1023;
  - ownership: list shows only the caller's bots; info/update/revoke on
    another owner's bot -> 403; unknown bot_id -> 404;
  - update renames + changes perms; revoke sets revoked_at, stays listed
    as revoked, and is idempotent;
  - audit rows exist for create / revoke / perm-change, actor = owner hash,
    target = bot_id;
  - migrate_schema adds the bots table to a DB created from a SCHEMA
    without it (pre-bots database).
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail)


base = tempfile.mkdtemp(prefix="shades_bots_test_")
sHades.STORAGE = base


def mkid():
    i = RNS.Identity()
    return i, sHades.hexstr(RNS.Destination(i, RNS.Destination.OUT,
                                            RNS.Destination.SINGLE,
                                            "lxmf", "delivery").hash)


owner_ident, OWNER = mkid()
other_ident, OTHER = mkid()
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()

# Guild row as boot would save it, so the create gateway is verifiable.
NODE_HASH = "a" * 32
DELIVERY_HASH = "b" * 32
sHades.store.save_guild(sHades.db, sHades.db_lock, sHades.GUILD_ID,
                        "sHades Hub", NODE_HASH, DELIVERY_HASH, "c" * 32)

sHades.DEBUG = False


def call(path, data=None, ident=owner_ident):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1", ident,
                                  sHades.now())
    return json.loads(raw.decode("utf-8"))


def audit_rows(action):
    rows = sHades.store.list_audit(sHades.db, sHades.db_lock, 100)
    return [r for r in rows if r["action"] == action]


def main():
    # ---------- create: shape, key format, gateway ----------
    r = call("/api/v1/bots/create", {"name": "helper", "perms": 1 | 64})
    check("create ok", r.get("ok") == 1, r)
    bot = r.get("bot") or {}
    check("bot_id is 32 hex chars",
          bool(re.match(r"^[0-9a-f]{32}$", bot.get("bot_id", ""))), bot)
    check("create bot fields",
          bot.get("name") == "helper" and bot.get("owner_hash") == OWNER
          and bot.get("perms") == 65 and bot.get("revoked_at") is None
          and bot.get("last_seen") is None
          and isinstance(bot.get("created_at"), float), bot)
    key = r.get("api_key", "")
    check("api key is sk- + 64 hex chars",
          bool(re.match(r"^sk-[0-9a-f]{64}$", key)), key[:10] + "...")
    gw = r.get("gateway") or {}
    check("gateway node/delivery from guilds row",
          gw.get("node_hash") == NODE_HASH
          and gw.get("delivery_hash") == DELIVERY_HASH, gw)
    check("gateway endpoints",
          gw.get("endpoints") == {"subscribe": "/api/v1/stream/subscribe",
                                  "poll": "/api/v1/stream/poll",
                                  "post": "/api/v1/bots/post",
                                  "react": "/api/v1/bots/react",
                                  "channels": "/api/v1/bots/channels",
                                  "channel_info": "/api/v1/bots/channel_info",
                                  "site_get": "/api/v1/bots/site_get",
                                  "site_manifest": "/api/v1/bots/site_manifest"}, gw)
    check("bot dict has no key material",
          "key_hash" not in bot and "api_key" not in bot, bot)
    bot_id = bot["bot_id"]

    # ---------- only the hash is stored ----------
    row = sHades.store.bot_get(sHades.db, sHades.db_lock, bot_id)
    check("stored key_hash is sha256 of the returned key",
          row is not None
          and row["key_hash"] == hashlib.sha256(key.encode()).hexdigest(), row)
    check("raw key is not in the stored row",
          row is not None
          and all(key != str(row[k]) for k in row.keys())
          and row["key_hash"] != key, dict(row) if row else None)

    # ---------- key never retrievable via list/info ----------
    r = call("/api/v1/bots/list")
    dumped = json.dumps(r)
    check("list never returns key material",
          "sk-" not in dumped and "key_hash" not in dumped, dumped[:200])
    r = call("/api/v1/bots/info", {"bot_id": bot_id})
    check("info never returns key material",
          r.get("ok") == 1 and "sk-" not in json.dumps(r)
          and "key_hash" not in json.dumps(r), r)
    check("info returns the same bot",
          r.get("bot", {}).get("bot_id") == bot_id
          and r["bot"]["name"] == "helper", r)

    # ---------- create validation ----------
    for label, args, why in (
            ("unknown bits 1024", {"name": "x", "perms": 1024}, "1024"),
            ("negative perms", {"name": "x", "perms": -1}, "-1"),
            ("string perms", {"name": "x", "perms": "7"}, "str"),
            ("bool perms", {"name": "x", "perms": True}, "bool"),
            ("missing perms", {"name": "x"}, "missing"),
            ("missing name", {"perms": 1}, "missing"),
            ("empty name", {"name": "   ", "perms": 1}, "blank"),
            ("name over 64 chars", {"name": "n" * 65, "perms": 1}, "65")):
        r = call("/api/v1/bots/create", args)
        check("create rejects %s -> 400" % label,
              r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/bots/create", {"name": "n" * 64, "perms": 1023})
    check("create accepts 64-char name and perms 1023", r.get("ok") == 1, r)
    max_bot = (r.get("bot") or {}).get("bot_id")
    r = call("/api/v1/bots/create", {"name": "zero", "perms": 0})
    check("create accepts perms 0", r.get("ok") == 1, r)
    zero_bot = (r.get("bot") or {}).get("bot_id")

    # ---------- list is owner-scoped ----------
    r = call("/api/v1/bots/create", {"name": "theirs", "perms": 1},
             ident=other_ident)
    other_bot = (r.get("bot") or {}).get("bot_id")
    check("other owner can create", r.get("ok") == 1, r)
    r = call("/api/v1/bots/list")
    mine = [b["bot_id"] for b in r.get("bots", [])]
    check("list returns only the caller's bots",
          sorted(mine) == sorted([bot_id, max_bot, zero_bot]), mine)
    r = call("/api/v1/bots/list", ident=other_ident)
    check("other owner lists only their bot",
          [b["bot_id"] for b in r.get("bots", [])] == [other_bot], r)

    # ---------- ownership: 403 / unknown: 404 ----------
    for path in ("/api/v1/bots/info", "/api/v1/bots/update",
                 "/api/v1/bots/revoke"):
        r = call(path, {"bot_id": other_bot})
        check("%s on another owner's bot -> 403" % path,
              r.get("ok") == 0 and r.get("code") == 403, r)
        r = call(path, {"bot_id": "f" * 32})
        check("%s on unknown bot_id -> 404" % path,
              r.get("ok") == 0 and r.get("code") == 404, r)
        r = call(path, {})
        check("%s with missing bot_id -> 400" % path,
              r.get("ok") == 0 and r.get("code") == 400, r)

    # ---------- update ----------
    r = call("/api/v1/bots/update", {"bot_id": bot_id, "name": "renamed",
                                     "perms": 3})
    check("update renames + changes perms",
          r.get("ok") == 1 and r["bot"]["name"] == "renamed"
          and r["bot"]["perms"] == 3, r)
    r = call("/api/v1/bots/update", {"bot_id": bot_id, "perms": 1024})
    check("update rejects unknown perms bits -> 400",
          r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/bots/update", {"bot_id": bot_id, "name": ""})
    check("update rejects empty name -> 400",
          r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/bots/update", {"bot_id": bot_id})
    check("update with nothing to change -> 400",
          r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/bots/info", {"bot_id": bot_id})
    check("failed updates left the bot unchanged",
          r["bot"]["name"] == "renamed" and r["bot"]["perms"] == 3, r)

    # ---------- revoke: sets revoked_at, stays listed, idempotent ----------
    r = call("/api/v1/bots/revoke", {"bot_id": zero_bot})
    check("revoke ok with revoked_at set",
          r.get("ok") == 1 and r["bot"]["revoked_at"] is not None, r)
    first_revoked = r["bot"]["revoked_at"]
    r = call("/api/v1/bots/list")
    revoked = [b for b in r.get("bots", []) if b["bot_id"] == zero_bot]
    check("revoked bot stays listed as revoked",
          len(revoked) == 1 and revoked[0]["revoked_at"] == first_revoked, r)
    r = call("/api/v1/bots/revoke", {"bot_id": zero_bot})
    check("revoke is idempotent (same revoked_at)",
          r.get("ok") == 1 and r["bot"]["revoked_at"] == first_revoked, r)

    # ---------- audit ----------
    created = audit_rows("bot_create")
    check("audit has bot_create rows for this owner",
          any(a["actor"] == OWNER and a["target"] == bot_id for a in created),
          created)
    revoked_a = audit_rows("bot_revoke")
    check("audit has bot_revoke with actor=owner, target=bot_id",
          any(a["actor"] == OWNER and a["target"] == zero_bot
              for a in revoked_a), revoked_a)
    updated = audit_rows("bot_update")
    check("audit has bot_update for the perm change",
          any(a["actor"] == OWNER and a["target"] == bot_id
              and "perms" in (a["detail"] or "") for a in updated), updated)

    # ---------- store helpers directly ----------
    check("bot_list_by_owner is owner-scoped (store level)",
          len(sHades.store.bot_list_by_owner(sHades.db, sHades.db_lock,
                                             OTHER)) == 1)
    check("bot_get returns None for unknown id",
          sHades.store.bot_get(sHades.db, sHades.db_lock, "e" * 32) is None)
    touched = sHades.store.bot_touch_last_seen(sHades.db, sHades.db_lock,
                                               bot_id, 1234.5)
    check("bot_touch_last_seen records last_seen",
          touched is not None and touched["last_seen"] == 1234.5, touched)
    check("bot_touch_last_seen skips revoked bots",
          sHades.store.bot_touch_last_seen(sHades.db, sHades.db_lock,
                                           zero_bot, 9999.0)["last_seen"] is None)

    # ---------- migrate_schema on a pre-bots DB ----------
    old_path = os.path.join(base, "old.db")
    old_conn = sqlite3.connect(old_path)
    old_conn.row_factory = sqlite3.Row
    old_schema = sHades.SCHEMA.replace(sHades.store.BOTS_DDL, "")
    old_conn.executescript(old_schema)
    old_conn.commit()
    has = old_conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='bots'").fetchone()
    check("simulated old DB has no bots table", has is None)
    sHades._migrate_schema(old_conn)
    old_conn.commit()
    has = old_conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='bots'").fetchone()
    check("migrate_schema adds the bots table", has is not None)
    lock = threading.Lock()
    row = sHades.store.bot_create(old_conn, lock, "d" * 32, OWNER, "migrated",
                                  "h" * 64, 1, 1.0)
    check("bots table is usable after migration",
          row is not None and row["name"] == "migrated", row)
    # migrate is idempotent: running it again must not error or drop data.
    sHades._migrate_schema(old_conn)
    old_conn.commit()
    check("second migrate keeps the migrated bot",
          sHades.store.bot_get(old_conn, lock, "d" * 32) is not None)
    old_conn.close()

    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


main()
