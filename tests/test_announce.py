"""P8: announce channel kind -- server contract lane.

The gates: the kind creates cleanly; the open default does NOT confer
POST on an announce channel (moderators always; explicit @everyone
overwrite or role-perm grants only); channels_list stamps can_post so
the composer's truth rides the read the client already fetches; a
granted post still emits the msg event to the channel ring (push and
catchup machinery unchanged).
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

import RNS  # noqa: E402  (mesh identity shapes; path set above)
import sHades  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=None):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail)


base = tempfile.mkdtemp(prefix="shades_announce_test_")
sHades.STORAGE = base


def mkid():
    i = RNS.Identity()
    return i, sHades.hexstr(RNS.Destination(i, RNS.Destination.OUT,
                                            RNS.Destination.SINGLE,
                                            "lxmf", "delivery").hash)


admin_ident, ADMIN = mkid()
mod_ident, MOD = mkid()
plain_ident, PLAIN = mkid()
grant_ident, GRANT = mkid()

sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()

for uh, name in ((ADMIN, "admin"), (MOD, "mod"), (PLAIN, "plain"),
                 (GRANT, "announcer")):
    sHades.db.execute("INSERT INTO users (dest_hash, username, "
                      "registered_at) VALUES (?, ?, ?)",
                      (uh, name, sHades.now()))
sHades.db.commit()

sHades.load_role_seeds(base)
admin_role = sHades._ensure_role("Admin", sHades.PERM_ALL, position=100)
sHades._assign_role(ADMIN, admin_role)
mod_role = sHades._ensure_role(
    "Moderator", sHades.PERMS["MANAGE_MESSAGES"] |
    sHades.PERMS["MANAGE_CHANNEL"], position=50)
sHades._assign_role(MOD, mod_role)
announcer_role = sHades._ensure_role("Announcer",
                                     sHades.PERMS["POST"], position=40)
sHades._assign_role(GRANT, announcer_role)
sHades.DEBUG = False


def call(path, data=None, ident=plain_ident, legacy=False):
    if legacy:
        import msgpack
        raw = sHades.API_ROUTES[path]("/x", data if data is not None else {},
                                      "req-1", "L1", ident, sHades.now())
        return msgpack.unpackb(raw)
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1",
                                  ident, sHades.now())
    return json.loads(raw.decode("utf-8"))


# ---------- 1. the kind creates through the real route ----------
r = call("/api/v1/channels/create", {"name": "announcements",
                                     "kind": "announce"},
         ident=admin_ident)
check("announce kind accepted by channels/create",
      r.get("ok") == 1 and r["channel"]["kind"] == "announce", r)
ANNC = r["channel"]["id"]
ANNC_NAME = r["channel"]["name"]

# ---------- 2. chat post-perm semantics unchanged (refactor guard) ----------
check("open default still confers POST on a chat channel",
      sHades.has_perm(PLAIN, ANNC, sHades.PERMS["VIEW"])
      is not None and
      sHades.ingest(sHades.ensure_channel("general"), PLAIN, "hi") is not None,
      "generic post must keep working for a roleless user")

# ---------- 3. the open default never confers POST on announce ----------
got = sHades.ingest(ANNC, PLAIN, "should not post")
check("roleless user denied on announce (no default POST)", got is None,
      got)

# ---------- 4. moderators always may announce ----------
got = sHades.ingest(ANNC, MOD, "mod announcement")
check("moderator may announce", got is not None, got)

# ---------- 5. an @everyone overwrite row carrying POST grants ----------
sHades.db.execute(
    "INSERT OR REPLACE INTO channel_overwrites (channel_id, role_id, perms)"
    " VALUES (?, 0, ?)",
    (ANNC, sHades.PERMS["VIEW"] | sHades.PERMS["POST"]))
sHades.db.commit()
got = sHades.ingest(ANNC, PLAIN, "granted via everyone overwrite")
check("@everyone overwrite POST grants announce", got is not None, got)

# ---------- 6. role-perm grant works without the @everyone row ----------
sHades.db.execute(
    "DELETE FROM channel_overwrites WHERE channel_id=? AND role_id=0",
    (ANNC,))
sHades.db.commit()
got = sHades.ingest(ANNC, GRANT, "granted via role perm")
check("role PERMS['POST'] grants announce", got is not None, got)
got = sHades.ingest(ANNC, PLAIN, "still denied after removing overwrite")
check("roleless user still denied without explicit grants", got is None,
      got)

# ---------- 7. channels_list stamps can_post (the composer's truth) ----------
cl = call("/api/v1/channels/list", ident=admin_ident)
mine = {c["name"]: c for c in cl.get("channels", [])}
check("admin can_post True on announce",
      mine.get(ANNC_NAME, {}).get("can_post") is True, mine.get(ANNC_NAME))
cl = call("/api/v1/channels/list", ident=plain_ident)
mine = {c["name"]: c for c in cl.get("channels", [])}
check("plain can_post False on announce",
      mine.get(ANNC_NAME, {}).get("can_post") is False,
      mine.get(ANNC_NAME))
check("plain can_post True on a chat channel",
      mine.get("general", {}).get("can_post") is True,
      mine.get("general"))
clmod = call("/api/v1/channels/list", ident=mod_ident)
mmod = {c["name"]: c for c in clmod.get("channels", [])}
check("moderator can_post True on announce",
      mmod.get(ANNC_NAME, {}).get("can_post") is True,
      mmod.get(ANNC_NAME))

# ---------- 8. push/catchup unchanged: granted post emits the msg event ----------
ring = sHades.EVENT_LOG.get(ANNC)
kinds = [ev["kind"] for _, ev in list(ring)] if ring else []
check("announce posts emit the msg event to the channel ring",
      "msg" in kinds, str(kinds)[:200])

# ---------- 9. the message lands in the ledger and the catchup view ----------
r = call("/api/messages", {"channel": ANNC}, ident=plain_ident, legacy=True)
texts = [m.get("content") for m in r.get("messages", [])]
check("readers still see the announcement history",
      "mod announcement" in texts, str(texts)[:200])

print()
print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
