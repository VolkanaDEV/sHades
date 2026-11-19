#!/usr/bin/env python3
"""Guild icon tests (server) — no mesh.

Pins down:
  - "guild_icon" is a server-config key riding the whitelist;
  - setting it accepts a stored blob_id (32-hex digest reachable by
    /api/v1/blobs/get) and an empty value (clears);
  - non-blob junk rejected and stored nothing;
  - /api/v1/whoami surfaces {guild: {name, icon, banner}} to
    every member;
  - the icon value round-trips: config read-back and whoami agree;
  - non-admin writes stay 403.
"""
import os, sys, json, sqlite3, tempfile, base64, hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))

base = tempfile.mkdtemp(prefix="shades_guild_icon_test_")
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
    # put a real blob the way single-shot blobs/put does, then reference it
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(120)
    r = call("/api/v1/blobs/put", {"name": "icon.png", "mime": "image/png",
                                   "data_b64": base64.b64encode(png).decode()})
    check("icon blob stored", r.get("ok") == 1, r)
    blob_id = r.get("blob_id")

    # ---------- whoami before any icon: guild present, icon empty ----------
    r = call("/api/v1/whoami", user=PLAIN)
    check("whoami carries the guild block",
          r.get("ok") == 1 and isinstance(r.get("guild"), dict), r)
    check("guild name flows through whoami", r["guild"].get("name"), r)
    check("no icon until one is set", not r["guild"].get("icon"), r)
    check("no banner until one is set (P5)",
          not r["guild"].get("banner"), r)

    # ---------- set the icon ----------
    r = call("/api/v1/admin/config", {"key": "guild_icon", "value": blob_id})
    check("admin sets guild_icon to a blob_id", r.get("ok") == 1, r)
    r = call("/api/v1/admin/config", {"key": "guild_icon"})
    check("config read-back agrees", r["config"]["guild_icon"] == blob_id, r)
    r = call("/api/v1/whoami", user=PLAIN)
    check("whoami surfaces the icon to members",
          r["guild"].get("icon") == blob_id, r)

    # ---------- fetch it back through the blob route ----------
    r = call("/api/v1/blobs/get", {"blob_id": blob_id}, user=PLAIN)
    check("members fetch the icon blob",
          r.get("ok") == 1 and base64.b64decode(r.get("data_b64") or "") == png, r)

    # ---------- junk rejected ----------
    for junk in ("not-a-blob", "0" * 64, os.path.join(base, "icon.png")):
        r = call("/api/v1/admin/config", {"key": "guild_icon", "value": junk})
        check("junk guild_icon rejected: %r" % junk[:16],
              r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/admin/config", {"key": "guild_icon", "value": "f" * 32})
    check("a 32-hex digest with no stored blob rejected",
          r.get("ok") == 0 and r.get("code") == 400, r)

    # ---------- clear ----------
    r = call("/api/v1/admin/config", {"key": "guild_icon", "value": ""})
    check("empty value clears the icon", r.get("ok") == 1, r)
    r = call("/api/v1/whoami", user=PLAIN)
    check("icon gone after clearing", not r["guild"].get("icon"), r)

    # ---------- permission ----------
    r = call("/api/v1/admin/config", {"key": "guild_icon", "value": blob_id},
             user=PLAIN)
    check("plain user denied", r.get("ok") == 0 and r.get("code") == 403, r)

    print("RESULT: %d PASS, %d FAIL" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
