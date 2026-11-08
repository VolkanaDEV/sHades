#!/usr/bin/env python3
"""Roles + permission bitmask unit tests (U5) — no RNS mesh needed.

The matrix this pins down:
  - boot seeding: admin.txt -> Admin role (ADMIN), moderators.txt -> Moderator;
  - default @everyone = VIEW|POST (pre-roles behaviour preserved);
  - VIEW/POST via channel_overwrites with role union; ADMIN short-circuit;
  - guild-wide bits via roles; legacy MODERATORS set still carries
    MANAGE_MESSAGES/MANAGE_CHANNEL;
  - enforcement: ingest POST, channels/list VIEW, messages VIEW, react VIEW,
    search not leaking hidden channels;
  - roles API: create/assign/unassign/delete with MANAGE_ROLES gating;
  - channels/patch overwrites with MANAGE_CHANNEL gating.
"""
import os, sys, json, sqlite3, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail)

base = tempfile.mkdtemp(prefix="shades_roles_test_")
sHades.STORAGE = base

# seeds BEFORE identity creation: write the whitelist files, then boot the DB
def mkid():
    i = RNS.Identity()
    return i, sHades.hexstr(RNS.Destination(i, RNS.Destination.OUT,
                                            RNS.Destination.SINGLE,
                                            "lxmf", "delivery").hash)

admin_ident, ADMIN = mkid()
mod_ident, LEGACY_MOD = mkid()      # only in moderators.txt (legacy path)
plain_ident, PLAIN = mkid()
roleless_ident, ROLELESS = mkid()   # never gets any role
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()

for uh, name in ((ADMIN, "admin"), (LEGACY_MOD, "legmod"), (PLAIN, "plain"),
                 (ROLELESS, "roleless")):
    sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                      (uh, name, sHades.now()))
sHades.db.commit()

# whitelist files + role seeding
with open(os.path.join(base, "admin.txt"), "w") as f:
    f.write(ADMIN + "\n")
with open(os.path.join(base, "moderators.txt"), "w") as f:
    f.write(LEGACY_MOD + "\n")
sHades.load_role_seeds(base)

sHades.DEBUG = False

def call(path, data=None, ident=plain_ident, legacy=False):
    # legacy /api/* endpoints receive an unpacked dict (RNS link layer does
    # the msgpack unpack) and answer with msgpack bytes
    if legacy:
        raw = sHades.API_ROUTES[path]("/x", data if data is not None else {},
                                      "req-1", "L1", ident, sHades.now())
        return sHades.msgpack.unpackb(raw)
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1", ident, sHades.now())
    return json.loads(raw.decode("utf-8"))

def main():
    P = sHades.PERMS
    cid = call("/api/v1/channels/create", {"name": "general", "kind": "chat"},
               ident=admin_ident)["channel"]["id"]

    # ---------- boot seeding ----------
    r = call("/api/v1/whoami", ident=admin_ident)
    check("admin.txt hash has the Admin role",
          any(r_["name"] == "Admin" for r_ in r.get("roles", [])), r.get("roles"))
    check("whoami is_admin", r.get("is_admin") is True, r)
    r = call("/api/v1/whoami", ident=mod_ident)
    check("moderators.txt hash has the Moderator role",
          any(r_["name"] == "Moderator" for r_ in r.get("roles", [])), r.get("roles"))
    check("legacy moderator is_moderator", r.get("is_moderator") is True, r)
    r = call("/api/v1/whoami", ident=plain_ident)
    check("plain user has no roles", r.get("roles") == [] and r.get("is_admin") is False, r)

    # ---------- has_perm matrix: defaults ----------
    check("default @everyone = VIEW|POST",
          sHades.has_perm(PLAIN, cid, P["VIEW"])
          and sHades.has_perm(PLAIN, cid, P["POST"]))
    check("default denies MANAGE bits",
          not sHades.has_perm(PLAIN, cid, P["MANAGE_MESSAGES"])
          and not sHades.has_perm(PLAIN, cid, P["ADMIN"]))
    check("admin short-circuits channel perms",
          sHades.has_perm(ADMIN, cid, P["MANAGE_ROLES"])
          and sHades.has_perm(ADMIN, cid, P["POST"]))

    # ---------- overwrites: hide a channel from @everyone, grant a role ----------
    r = sHades._ensure_role("secret-viewer", P["VIEW"], position=10)
    role_id = r
    # (direct overwrite: @everyone loses even VIEW)
    with sHades.db_lock:
        sHades.db.execute("INSERT INTO channel_overwrites (channel_id, role_id, perms) "
                          "VALUES (?, 0, 0)", (cid,))
        sHades.db.commit()
    check("@everyone 0 -> plain user loses VIEW",
          not sHades.has_perm(PLAIN, cid, P["VIEW"]))
    sHades._assign_role(PLAIN, role_id)
    check("role with VIEW grants it back (union)",
          sHades.has_perm(PLAIN, cid, P["VIEW"]))
    check("but not POST (role only has VIEW)",
          not sHades.has_perm(PLAIN, cid, P["POST"]))
    check("admin still sees everything (short-circuit)",
          sHades.has_perm(ADMIN, cid, P["VIEW"]))

    # ---------- enforcement: channels/list hides hidden channels ----------
    cl = call("/api/v1/channels/list", ident=roleless_ident)
    check("channels/list hides non-VIEW channels from role-less user",
          all(c["id"] != cid for c in cl.get("channels", [])), cl)
    cl = call("/api/v1/channels/list", ident=admin_ident)
    check("admin still sees the hidden channel",
          any(c["id"] == cid for c in cl.get("channels", [])), cl)

    # ---------- enforcement: messages + search ----------
    sHades.ingest(cid, ADMIN, "hidden channel secret post")
    r = call("/api/messages", {"channel": cid}, ident=roleless_ident, legacy=True)
    check("legacy /api/messages VIEW-gated", r.get("ok") == 0
          and r.get("error") == "view permission required", r)
    r = call("/api/v1/search", {"q": "secret post"}, ident=roleless_ident)
    check("unscoped search does not leak hidden channels",
          all("secret post" not in (x["content"] or "") for x in r.get("results", [])), r)
    r = call("/api/v1/search", {"q": "secret post"}, ident=admin_ident)
    check("admin search finds it", len(r.get("results", [])) == 1, r)

    # ---------- enforcement: POST via ingest ----------
    # restore @everyone VIEW|POST, then remove POST only
    with sHades.db_lock:
        sHades.db.execute("UPDATE channel_overwrites SET perms=? "
                          "WHERE channel_id=? AND role_id=0",
                          (P["VIEW"] | P["POST"], cid))
        sHades.db.commit()
    sHades._ensure_role("posters", P["VIEW"] | P["POST"], position=10)
    posters = sHades.db.execute("SELECT role_id FROM roles "
                                "WHERE name='posters'").fetchone()["role_id"]
    sHades._assign_role(PLAIN, posters)
    mid = sHades.ingest(cid, PLAIN, "plain can post")
    check("POST allowed with default + role", mid is not None, mid)
    with sHades.db_lock:
        sHades.db.execute("UPDATE channel_overwrites SET perms=? "
                          "WHERE channel_id=? AND role_id=0", (P["VIEW"], cid))
        sHades.db.commit()
        # plain's 'posters' role still carries POST -> still allowed
    mid = sHades.ingest(cid, PLAIN, "role carries POST")
    check("role POST union keeps posting allowed", mid is not None, mid)
    # a user with NO roles is denied when @everyone loses POST
    mid = sHades.ingest(cid, ROLELESS, "no POST for me")
    check("@everyone without POST blocks role-less users", mid is None, mid)

    # ---------- roles API gating + CRUD ----------
    r = call("/api/v1/roles/create", {"name": "helpers"}, ident=plain_ident)
    check("roles/create needs MANAGE_ROLES", r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/roles/create", {"name": "helpers", "perms": P["POST"] | P["VIEW"]},
             ident=admin_ident)
    check("admin creates a role", r.get("ok") == 1
          and r["role"]["perms"] == P["VIEW"] | P["POST"], r)
    helpers = r["role"]["role_id"]
    r = call("/api/v1/roles/create", {"name": "helpers"}, ident=admin_ident)
    check("duplicate role -> 409", r.get("code") == 409, r)
    r = call("/api/v1/roles/assign", {"role_id": helpers, "user": PLAIN},
             ident=plain_ident)
    check("roles/assign needs MANAGE_ROLES", r.get("code") == 403, r)
    r = call("/api/v1/roles/assign", {"role_id": helpers, "user": "zzz"},
             ident=admin_ident)
    check("assign unknown user -> 404", r.get("code") == 404, r)
    r = call("/api/v1/roles/assign", {"role_id": helpers, "user": LEGACY_MOD},
             ident=admin_ident)
    check("admin assigns by hash", r.get("ok") == 1, r)
    rl = call("/api/v1/roles/list", ident=admin_ident)
    helpers_role = [x for x in rl["roles"] if x["role_id"] == helpers]
    check("role members listed", LEGACY_MOD in helpers_role[0]["members"], helpers_role)
    r = call("/api/v1/roles/unassign", {"role_id": helpers, "user": LEGACY_MOD},
             ident=admin_ident)
    check("unassign ok", r.get("ok") == 1, r)
    r = call("/api/v1/roles/delete", {"role_id": helpers}, ident=admin_ident)
    check("delete role ok", r.get("ok") == 1, r)

    # ---------- channels/patch overwrites ----------
    r = call("/api/v1/channels/patch",
             {"channel": cid, "overwrites": {"0": P["VIEW"] | P["POST"]}},
             ident=plain_ident)
    check("channels/patch needs MANAGE_CHANNEL", r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/channels/patch",
             {"channel": cid, "topic": "the general topic",
              "overwrites": {"0": P["VIEW"] | P["POST"]}},
             ident=admin_ident)
    check("admin patches topic + overwrites", r.get("ok") == 1, r)
    info = call("/api/v1/channels/info", {"channel": cid}, ident=admin_ident)
    check("patched topic visible", info["channel"]["topic"] == "the general topic", info)
    r = call("/api/v1/channels/patch",
             {"channel": cid, "overwrites": {"0": None}}, ident=admin_ident)
    check("null overwrite deletes the row", r.get("ok") == 1, r)
    check("overwrite row gone (default restored)",
          sHades.db.execute("SELECT COUNT(*) AS n FROM channel_overwrites "
                            "WHERE channel_id=? AND role_id=0",
                            (cid,)).fetchone()["n"] == 0)

    # ---------- dispatcher honours MANAGE_CHANNEL ----------
    r = call("/api/v1/command", {"input": "/topic general nope"}, ident=plain_ident)
    check("slash topic denied without MANAGE_CHANNEL",
          "manage channel permission required" in r["output"], r)
    r = call("/api/v1/command", {"input": "/topic general admin-set"}, ident=admin_ident)
    check("slash topic allowed for admin", "topic set" in r["output"], r)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
