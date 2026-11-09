#!/usr/bin/env python3
"""Join gate + invite codes tests (U6b) — no RNS mesh needed.

Covers:
  - policy: 'open' (default) needs no code; 'code' requires one;
  - /api/v1/join status vs join-with-code, invalid/expired/exhausted codes;
  - registration gate: /api/register is refused without a join when the
    policy is 'code', and existing members are never locked out;
  - /api/v1/invites admin CRUD (list/create/revoke) + admin gating.
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


base = tempfile.mkdtemp(prefix="shades_join_test_")
sHades.STORAGE = base


def mkid():
    i = RNS.Identity()
    return i, sHades.hexstr(RNS.Destination(i, RNS.Destination.OUT,
                                            RNS.Destination.SINGLE,
                                            "lxmf", "delivery").hash)


admin_ident, ADMIN = mkid()
new_ident, NEW = mkid()
member_ident, MEMBER = mkid()

sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()

for uh, name in ((ADMIN, "admin"), (NEW, "newbie"), (MEMBER, "member")):
    sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                      "VALUES (?, ?, ?)", (uh, name, sHades.now()))
sHades.db.commit()
admin_role = sHades._ensure_role("Admin", sHades.PERM_ALL, position=100)
sHades._assign_role(ADMIN, admin_role)
sHades.DEBUG = False


def call(path, data=None, ident=new_ident, legacy=False):
    if legacy:
        raw = sHades.API_ROUTES[path]("/x", data if data is not None else {},
                                      "req-1", "L1", ident, sHades.now())
        return sHades.msgpack.unpackb(raw)
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1", ident,
                                  sHades.now())
    return json.loads(raw.decode("utf-8"))


def main():
    # ---------- default policy is open ----------
    r = call("/api/v1/join", ident=new_ident)
    check("join status on an open guild",
          r.get("ok") == 1 and r.get("required") is False
          and r.get("policy") == "open", r)
    r = call("/api/register", {"username": "newbie2"}, ident=new_ident,
             legacy=True)
    check("register allowed on an open guild", r.get("ok") == 1, r)

    # ---------- admin-only invite management ----------
    r = call("/api/v1/invites", {"op": "list"}, ident=new_ident)
    check("invites list denied for a non-admin", r.get("code") == 403, r)
    r = call("/api/v1/invites", {"op": "create", "label": "friends"},
             ident=new_ident)
    check("invite create denied for a non-admin", r.get("code") == 403, r)
    r = call("/api/v1/invites", {"op": "create", "label": "friends"},
             ident=admin_ident)
    check("admin creates an invite", r.get("ok") == 1
          and len(r.get("code", "")) == 8, r)
    code = r["code"]
    r = call("/api/v1/invites", {"op": "list"}, ident=admin_ident)
    check("invite appears in the list",
          any(i["code"] == code for i in r.get("invites", [])), r)
    check("invite list carries uses/max_uses",
          all("uses" in i and "max_uses" in i for i in r.get("invites", [])),
          r)

    # ---------- switch the policy to 'code' ----------
    call("/api/v1/admin/config", {"key": "registration", "value": "code"},
         ident=admin_ident)
    # a genuinely new identity (never registered) is not joined
    stranger_ident, STRANGER = mkid()
    r = call("/api/v1/join", ident=stranger_ident)
    check("join status says a code is required", r.get("required") is True, r)
    check("not joined yet", r.get("joined") is False, r)

    # existing members are not locked out
    r = call("/api/register", {"username": "member2"}, ident=member_ident,
             legacy=True)
    check("existing member can still register", r.get("ok") == 1, r)
    # a brand new user is blocked
    fresh_ident, FRESH = mkid()
    r = call("/api/register", {"username": "fresh"}, ident=fresh_ident,
             legacy=True)
    check("register blocked without a join code",
          r.get("ok") == 0 and "join code required" in r.get("error", ""), r)

    # ---------- joining with codes ----------
    r = call("/api/v1/join", {"code": "BADCODE1"}, ident=fresh_ident)
    check("invalid code rejected", r.get("ok") == 0, r)
    r = call("/api/v1/join", {"code": code.lower()}, ident=fresh_ident)
    check("code is case-insensitive and joins",
          r.get("ok") == 1 and r.get("joined") is True, r)
    r = call("/api/register", {"username": "fresh"}, ident=fresh_ident,
             legacy=True)
    check("register allowed after joining", r.get("ok") == 1, r)
    r = call("/api/v1/join", ident=fresh_ident)
    check("join status now reports joined", r.get("joined") is True, r)

    # usage counter increments
    r = call("/api/v1/invites", {"op": "list"}, ident=admin_ident)
    entry = [i for i in r["invites"] if i["code"] == code][0]
    check("invite use counter incremented", entry["uses"] == 1, entry)

    # ---------- max uses ----------
    r = call("/api/v1/invites", {"op": "create", "max_uses": 1,
                                 "label": "one-shot"}, ident=admin_ident)
    one = r["code"]
    ident_a, _ = mkid()
    ident_b, _ = mkid()
    r = call("/api/v1/join", {"code": one}, ident=ident_a)
    check("first use of a one-shot code works", r.get("ok") == 1, r)
    r = call("/api/v1/join", {"code": one}, ident=ident_b)
    check("second use of an exhausted code is refused",
          r.get("ok") == 0 and "exhausted" in r.get("error", ""), r)

    # ---------- expiry ----------
    r = call("/api/v1/invites", {"op": "create", "expires_in": 1},
             ident=admin_ident)
    exp = r["code"]
    time.sleep(1.2)
    ident_c, _ = mkid()
    r = call("/api/v1/join", {"code": exp}, ident=ident_c)
    check("expired code refused", r.get("ok") == 0 and "expired" in r.get(
        "error", ""), r)

    # ---------- validation ----------
    r = call("/api/v1/invites", {"op": "create", "max_uses": -1},
             ident=admin_ident)
    check("negative max_uses rejected", r.get("code") == 400, r)
    r = call("/api/v1/invites", {"op": "create", "expires_in": 99999999},
             ident=admin_ident)
    check("out-of-range expires_in rejected", r.get("code") == 400, r)
    r = call("/api/v1/invites", {"op": "bogus"}, ident=admin_ident)
    check("unknown op rejected", r.get("code") == 400, r)
    r = call("/api/v1/join", {"code": "sh"}, ident=fresh_ident)
    check("malformed code rejected", r.get("code") == 400, r)

    # ---------- revoke ----------
    r = call("/api/v1/invites", {"op": "revoke", "code": code},
             ident=admin_ident)
    check("admin revokes an invite", r.get("ok") == 1, r)
    r = call("/api/v1/join", {"code": code}, ident=fresh_ident)
    check("revoked code no longer works", r.get("ok") == 0, r)
    r = call("/api/v1/invites", {"op": "revoke", "code": code},
             ident=admin_ident)
    check("revoking an unknown code -> 404", r.get("code") == 404, r)

    # ---------- audit ----------
    r = call("/api/v1/admin/audit", {"limit": 50}, ident=admin_ident)
    actions = [e["action"] for e in r.get("entries", [])]
    check("invite create audited", "invite.create" in actions, actions[:8])
    check("invite revoke audited", "invite.revoke" in actions, actions[:8])
    check("join audited", "join" in actions, actions[:8])

    # ---------- back to open ----------
    call("/api/v1/admin/config", {"key": "registration", "value": "open"},
         ident=admin_ident)
    r = call("/api/v1/join", ident=new_ident)
    check("policy back to open", r.get("required") is False, r)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
