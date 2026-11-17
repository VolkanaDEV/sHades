#!/usr/bin/env python3
"""Founder role tests (v0.31.6): the first member of a server is the creator.

tay: "add roles. first person into a server (the creator) should be able to
add/delete channels and have founder role. Channels can be text, site (micron
pages), or a text stream."

Pins (handler level, no mesh):
  1. the FIRST register grants the Founder role (PERM_ALL: every manage bit)
     to exactly that member, positioned above Admin;
  2. a SECOND register grants nothing;
  3. re-registering (profile edit upsert) never re-grants or duplicates;
  4. the founder passes the existing channel gates: is_moderator (site/term
     create), has_perm MANAGE_CHANNEL (delete) -- no new gates added;
  5. a roleless member is denied site/term creation (the old rule stands);
  6. whoami exposes founder's admin/moderator flags for the client UI.
"""
import os
import sys
import json
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


base = tempfile.mkdtemp(prefix="shades_founder_test_")
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.STORAGE = base
sHades.DEBUG = False

def register_as(ident, username):
    """Run the real /api/register handler (wrap_api gets an unpacked dict)."""
    raw = sHades.API_ROUTES["/api/register"]("/api/register",
                                             {"username": username},
                                             "req", "LK", ident, sHades.now())
    return sHades.msgpack.unpackb(raw, raw=False)

def roles_of(user):
    rows = sHades.db.execute(
        "SELECT r.name, r.perms FROM user_roles ur JOIN roles r "
        "ON r.role_id = ur.role_id WHERE ur.user_hash = ?", (user,)).fetchall()
    return [(r["name"], r["perms"]) for r in rows]

def v1(path, data, ident):
    payload = json.dumps(data).encode("utf-8")
    raw = sHades.API_ROUTES[path](path, payload, "req", "LK", ident, sHades.now())
    return json.loads(raw.decode("utf-8"))



CREATOR = RNS.Identity()
SECOND = RNS.Identity()
H_C = sHades.hexstr(RNS.Destination(CREATOR, RNS.Destination.OUT,
                                    RNS.Destination.SINGLE,
                                    "lxmf", "delivery").hash)
H_S = sHades.hexstr(RNS.Destination(SECOND, RNS.Destination.OUT,
                                    RNS.Destination.SINGLE,
                                    "lxmf", "delivery").hash)

def main():
    # 1: the first register grants Founder
    res = register_as(CREATOR, "creator")
    check("first register ok", res.get("ok") == 1, res)
    frow = sHades.db.execute(
        "SELECT name, perms, position FROM roles WHERE name='Founder'").fetchone()
    check("Founder role exists", frow is not None)
    if frow:
        check("Founder carries every bit (PERM_ALL)",
              frow["perms"] == sHades.PERM_ALL, hex(frow["perms"]))
        check("Founder above Admin position (200 > 100)",
              frow["position"] > 100, frow["position"])
    check("Founder assigned to the first member",
          ("Founder", sHades.PERM_ALL) in roles_of(H_C), roles_of(H_C))

    # 4: the founder passes the existing channel gates (add/delete)
    r_chat = v1("/api/v1/channels/create", {"name": "general", "kind": "chat"}, CREATOR)
    check("founder creates a text (chat) channel", r_chat.get("ok") == 1, r_chat)
    r_site = v1("/api/v1/channels/create", {"name": "lounge", "kind": "site"}, CREATOR)
    check("founder creates a site (micron) channel", r_site.get("ok") == 1, r_site)
    r_term = v1("/api/v1/channels/create", {"name": "feed", "kind": "term"}, CREATOR)
    check("founder creates a text-stream (term) channel", r_term.get("ok") == 1, r_term)
    if r_term.get("ok"):
        r_del = v1("/api/v1/channels/delete", {"channel": r_term["channel"]["id"]}, CREATOR)
        check("founder deletes a channel", r_del.get("ok") == 1, r_del)

    # 2: the second register grants nothing
    res2 = register_as(SECOND, "joiner")
    check("second register ok", res2.get("ok") == 1, res2)
    check("no Founder for the second member", roles_of(H_S) == [], roles_of(H_S))

    # 3: re-register (profile edit) never duplicates the grant
    res3 = register_as(CREATOR, "creator")
    check("re-register stays ok", res3.get("ok") == 1, res3)
    nfounder = sHades.db.execute(
        "SELECT COUNT(*) AS n FROM roles WHERE name='Founder'").fetchone()["n"]
    check("no duplicate Founder role", nfounder == 1, nfounder)
    check("no duplicate assignment",
          len(roles_of(H_C)) == 1, roles_of(H_C))

    # 5: old rule stands -- a roleless member cannot create site/term
    r_denied = v1("/api/v1/channels/create", {"name": "denied", "kind": "site"}, SECOND)
    check("roleless member denied site create (old gate intact)",
          r_denied.get("ok") == 0, r_denied)

    # 6: whoami exposes the founder flags for the client UI
    me = v1("/api/v1/whoami", {}, CREATOR)
    check("founder whoami: is_admin + is_moderator",
          me.get("ok") and me.get("is_admin") and me.get("is_moderator"), me)
    names = [r.get("name") for r in (me.get("roles") or [])]
    check("whoami lists Founder", "Founder" in names, names)

    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
