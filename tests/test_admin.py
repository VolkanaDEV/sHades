#!/usr/bin/env python3
"""Admin panel + audit log + slowmode tests (U8) — no RNS mesh needed.

Covers:
  - admin gating: panel endpoints need the ADMIN role (a moderator is NOT
    enough), and the ADMIN role short-circuits;
  - config: read all, read one, write one, unknown key rejected,
    registration/slowmode value validation;
  - audit: every privileged mutation lands a row with actor/action/target,
    newest first, admin-only viewer;
  - whitelists: /admin/mods add/remove rewrites moderators.txt and
    admin.txt atomically, and adding a moderator does not make them admin;
  - slowmode: N seconds between posts per user, admin-exempt.
"""
import os
import sys
import json
import sqlite3
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail)


base = tempfile.mkdtemp(prefix="shades_admin_test_")
sHades.STORAGE = base


def mkid():
    i = RNS.Identity()
    return i, sHades.hexstr(RNS.Destination(i, RNS.Destination.OUT,
                                            RNS.Destination.SINGLE,
                                            "lxmf", "delivery").hash)


admin_ident, ADMIN = mkid()
mod_ident, MOD = mkid()
plain_ident, PLAIN = mkid()

sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()

for uh, name in ((ADMIN, "admin"), (MOD, "mod"), (PLAIN, "plain")):
    sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                      "VALUES (?, ?, ?)", (uh, name, sHades.now()))
sHades.db.commit()

# role seeding: admin gets ADMIN, mod gets the moderator role only
sHades.load_role_seeds(base)
admin_role = sHades._ensure_role("Admin", sHades.PERM_ALL, position=100)
sHades._assign_role(ADMIN, admin_role)
mod_role = sHades._ensure_role("Moderator",
                               sHades.PERMS["MANAGE_MESSAGES"] |
                               sHades.PERMS["MANAGE_CHANNEL"], position=50)
sHades._assign_role(MOD, mod_role)
sHades.DEBUG = False


def call(path, data=None, ident=plain_ident, legacy=False):
    if legacy:
        raw = sHades.API_ROUTES[path]("/x", data if data is not None else {},
                                      "req-1", "L1", ident, sHades.now())
        return sHades.msgpack.unpackb(raw)
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1", ident,
                                  sHades.now())
    return json.loads(raw.decode("utf-8"))


def main():
    P = sHades.PERMS

    # ---------- admin gating ----------
    r = call("/api/v1/admin/config", ident=plain_ident)
    check("config denied for a plain user", r.get("code") == 403, r)
    r = call("/api/v1/admin/config", ident=mod_ident)
    check("config denied for a moderator (ADMIN only)", r.get("code") == 403, r)
    r = call("/api/v1/admin/config", ident=admin_ident)
    check("config allowed for the admin", r.get("ok") == 1, r)
    r = call("/api/v1/admin/audit", ident=mod_ident)
    check("audit denied for a moderator", r.get("code") == 403, r)
    r = call("/api/v1/admin/mods", {"op": "list"}, ident=mod_ident)
    check("mods denied for a moderator", r.get("code") == 403, r)
    r = call("/api/v1/admin/mods", {"op": "list"}, ident=admin_ident)
    check("mods list allowed for the admin", r.get("ok") == 1, r)

    # ---------- config read/write ----------
    cfg = call("/api/v1/admin/config", ident=admin_ident)["config"]
    check("config carries the defaults",
          cfg.get("registration") == "open" and cfg.get("slowmode") == "0", cfg)
    r = call("/api/v1/admin/config", {"key": "guild_name", "value": "TestGuild"},
             ident=admin_ident)
    check("admin writes a config value", r.get("ok") == 1, r)
    r = call("/api/v1/admin/config", {"key": "guild_name"}, ident=admin_ident)
    check("config read-back returns the new value",
          r["config"]["guild_name"] == "TestGuild", r)
    r = call("/api/v1/admin/config", {"key": "bogus", "value": "x"},
             ident=admin_ident)
    check("unknown config key rejected", r.get("code") == 400, r)
    r = call("/api/v1/admin/config", {"key": "registration", "value": "weird"},
             ident=admin_ident)
    check("registration value validated", r.get("code") == 400, r)
    r = call("/api/v1/admin/config", {"key": "slowmode", "value": "abc"},
             ident=admin_ident)
    check("slowmode value validated", r.get("code") == 400, r)
    r = call("/api/v1/admin/config", {"key": "slowmode", "value": "9999"},
             ident=admin_ident)
    check("slowmode range validated", r.get("code") == 400, r)
    r = call("/api/v1/admin/config", {"key": "slowmode", "value": "30"},
             ident=admin_ident)
    check("slowmode accepted", r.get("ok") == 1, r)

    # ---------- audit rows ----------
    r = call("/api/v1/admin/audit", ident=admin_ident)
    check("audit is admin-only and readable", r.get("ok") == 1, r)
    entries = r.get("entries", [])
    check("config change was audited",
          any(e["action"] == "config.set" and e["target"] == "guild_name"
              for e in entries), entries[:3])
    check("audit rows carry the actor",
          all(e["actor"] for e in entries), entries[:3])
    check("audit is newest first",
          [e["ts"] for e in entries] == sorted([e["ts"] for e in entries],
                                               reverse=True), entries[:3])

    # ---------- privileged mutations are audited ----------
    cid = call("/api/v1/channels/create", {"name": "adminchan"},
               ident=admin_ident)["channel"]["id"]
    r = call("/api/v1/admin/audit", ident=admin_ident)
    check("channel create audited",
          any(e["action"] == "channel.create" for e in r["entries"]),
          r["entries"][:3])
    r = call("/api/v1/mod/ban", {"user": PLAIN, "reason": "spam"},
             ident=admin_ident)
    check("ban succeeds for the admin", r.get("ok") == 1, r)
    r = call("/api/v1/admin/audit", ident=admin_ident)
    check("ban audited",
          any(e["action"] == "mod.banned" and e["target"] == PLAIN
              for e in r["entries"]), r["entries"][:3])
    # a role assignment is audited too
    rid = sHades._ensure_role("tmp", P["POST"])
    r = call("/api/v1/roles/assign", {"role_id": rid, "user": PLAIN},
             ident=admin_ident)
    check("role assign ok", r.get("ok") == 1, r)
    r = call("/api/v1/admin/audit", ident=admin_ident)
    check("role assign audited",
          any(e["action"] == "role.assign" for e in r["entries"]),
          r["entries"][:3])

    # ---------- whitelist management ----------
    r = call("/api/v1/admin/mods", {"op": "add", "user": PLAIN},
             ident=admin_ident)
    check("admin adds a moderator", r.get("ok") == 1, r)
    check("moderator list carries the new hash", PLAIN in r.get("mods", []), r)
    check("adding a moderator does NOT make them admin",
          PLAIN not in r.get("admins", []), r)
    modfile = os.path.join(base, "moderators.txt")
    check("moderators.txt rewritten",
          PLAIN in open(modfile).read(), modfile)
    r = call("/api/v1/admin/mods", {"op": "remove", "user": PLAIN},
             ident=admin_ident)
    check("admin removes the moderator", PLAIN not in r.get("mods", []), r)
    check("moderators.txt rewritten on removal",
          PLAIN not in open(modfile).read(), modfile)
    r = call("/api/v1/admin/mods", {"op": "admin_add", "user": MOD},
             ident=admin_ident)
    check("admin_add promotes to admin", MOD in r.get("admins", []), r)
    check("admin.txt rewritten",
          MOD in open(os.path.join(base, "admin.txt")).read(), base)
    check("admin_add grants the ADMIN role",
          sHades.has_role_perm(MOD, sHades.PERMS["ADMIN"]), MOD)
    r = call("/api/v1/admin/config", ident=mod_ident)
    check("promoted user can now reach the panel", r.get("ok") == 1, r)
    r = call("/api/v1/admin/mods", {"op": "admin_remove", "user": MOD},
             ident=admin_ident)
    check("admin_remove works", MOD not in r.get("admins", []), r)
    r = call("/api/v1/admin/mods", {"op": "bogus"}, ident=admin_ident)
    check("unknown op rejected", r.get("code") == 400, r)
    r = call("/api/v1/admin/mods", {"op": "add", "user": "nothex"},
             ident=admin_ident)
    check("bad hash rejected", r.get("code") == 400, r)

    # ---------- slowmode ----------
    # use a fresh, un-banned, un-privileged user
    slow_ident, SLOW = mkid()
    sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                      "VALUES (?, ?, ?)", (SLOW, "slowpoke", sHades.now()))
    sHades.db.commit()
    call("/api/v1/admin/config", {"key": "slowmode", "value": "5"},
         ident=admin_ident)
    sHades._last_post_at.clear()
    mid = sHades.ingest(cid, SLOW, "first post")
    check("first post passes slowmode", mid is not None, mid)
    mid = sHades.ingest(cid, SLOW, "second post immediately")
    check("second post inside the window is dropped", mid is None, mid)
    sHades._last_post_at[SLOW] = time.time() - 10
    mid = sHades.ingest(cid, SLOW, "post after the window")
    check("post allowed once the window passes", mid is not None, mid)
    # an admin is exempt from slowmode
    sHades._last_post_at.clear()
    check("admin posts under slowmode",
          sHades.ingest(cid, ADMIN, "admin one") is not None
          and sHades.ingest(cid, ADMIN, "admin two") is not None)
    call("/api/v1/admin/config", {"key": "slowmode", "value": "0"},
         ident=admin_ident)
    mid = sHades.ingest(cid, SLOW, "slowmode off")
    mid2 = sHades.ingest(cid, SLOW, "slowmode off again")
    check("slowmode 0 disables the gate", mid is not None and mid2 is not None,
          (mid, mid2))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
