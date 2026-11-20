#!/usr/bin/env python3
"""Server lane: fresh site channels seed a default template /index.mu
(tay's device receipt 0.36.10 era: a new site channel rendered
"loading site..." forever because no resource existed to fetch).
Checks: the seed file exists with parseable micron content, site/get
serves it, chat channels stay unseeded, an overwrite republishes
cleanly, and a duplicate create neither rewrites nor reseeds."""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402
import micron  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


base = tempfile.mkdtemp(prefix="shades_sites_test_")
sHades.STORAGE = base
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.DEBUG = False
sHades.node_destination = None   # lanes carry no mesh node


def _mk_user():
    i = RNS.Identity()
    h = sHades.hexstr(RNS.Destination(i, RNS.Destination.OUT,
                                      RNS.Destination.SINGLE,
                                      "lxmf", "delivery").hash)
    return i, h


ident, H = _mk_user()
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                  "VALUES (?,?,?)", (H, "admin", sHades.now()))
sHades.db.commit()
role = sHades._ensure_role("Admin", sHades.PERM_ALL, position=100)
sHades._assign_role(H, role)


def call(path, data=None):
    payload = json.dumps(data or {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1",
                                  ident, sHades.now())
    return json.loads(raw.decode("utf-8"))


def main():
    import base64
    cid = None

    # 1. fresh site create seeds /index.mu under the channel root
    r = call("/api/v1/channels/create", {"name": "wiki", "kind": "site"})
    check("site create ok", r.get("ok") == 1, r)
    if r.get("ok"):
        cid = r["channel"]["id"]
        fpath = sHades._site_file_path(cid, "index.mu")
        check("seed file exists", fpath is not None and os.path.isfile(fpath),
              fpath)
        if os.path.isfile(fpath or ""):
            raw = open(fpath, "rb").read()
            check("seed carries the template header",
                  raw.startswith(b"#!c=0\n>"), raw[:40])
            text = raw.decode("utf-8")
            check("template parses as micron",
                  any(b.get("type") == "heading"
                      for b in micron.micron_parse(text)))
            # client default index matches the client-side fallback literal
            check("site/get serves the seed",
                  call("/api/v1/site/get",
                       {"channel": cid, "path": "/index.mu"}).get("ok") == 1)

    # 2. a chat channel leaves the site roots untouched
    r2 = call("/api/v1/channels/create", {"name": "general", "kind": "chat"})
    if r2.get("ok"):
        root2 = os.path.join(sHades._site_root(), r2["channel"]["id"])
        check("chat channel not seeded", not os.path.isdir(root2))

    # 3. the template is ordinary content: site/put overwrites it
    if r.get("ok"):
        new_mu = b"#!c=0\n> Owned page\n"
        r3 = call("/api/v1/site/put",
                  {"channel": cid, "path": "/index.mu",
                   "data_b64": base64.b64encode(new_mu).decode("ascii")})
        check("site put ok", r3.get("ok") == 1, r3)
        got = call("/api/v1/site/get", {"channel": cid, "path": "/index.mu"})
        check("overwrite wins over the template",
              got.get("ok") == 1
              and base64.b64decode(got.get("data_b64") or "") == new_mu)

        # 4. a duplicate create answers 409 and never rewrites the resource
        before = open(sHades._site_file_path(cid, "index.mu"), "rb").read()
        r4 = call("/api/v1/channels/create", {"name": "wiki", "kind": "site"})
        check("duplicate create 409",
              r4.get("ok") == 0 and r4.get("code") == 409, r4)
        after = open(sHades._site_file_path(cid, "index.mu"), "rb").read()
        check("no reseed on duplicate create", after == before)

    # 5. P7b: the channel-scoped SITE_EDIT bit gates site writes
    #    (moderator OR the bit; the bit rides channel_overwrites and
    #    works through the @everyone row, a role overwrite, or a role's
    #    own guild perms; MANAGE_* carriers pass without the bit).
    if r.get("ok"):
        ident2, H2 = _mk_user()
        sHades.db.execute("INSERT INTO users (dest_hash, username, "
                          "registered_at) VALUES (?,?,?)",
                          (H2, "editor", sHades.now()))
        sHades.db.commit()

        def call_as(the_ident, path, data=None):
            payload = json.dumps(data or {}).encode("utf-8")
            raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-9", "L1",
                                          the_ident, sHades.now())
            return json.loads(raw.decode("utf-8"))

        edit_put = {"channel": cid, "path": "/editor.mu",
                    "data_b64": base64.b64encode(
                        b"#!c=0\n> Editor page\n").decode("ascii")}
        r5a = call_as(ident2, "/api/v1/site/put", edit_put)
        check("no-bit member cannot site/put",
              r5a.get("ok") == 0 and r5a.get("code") == 403, r5a)

        grant = call("/api/v1/channels/patch",
                     {"channel": cid, "overwrites":
                      {"0": sHades.PERM_ALL}})
        check("@everyone overwrite accepts 127 (PERM_ALL widened)",
              grant.get("ok") == 1, grant)
        too_high = call("/api/v1/channels/patch",
                        {"channel": cid, "overwrites": {"0": 128}})
        check("overwrite above PERM_ALL rejected",
              too_high.get("ok") == 0 and too_high.get("code") == 400,
              too_high)
        r5b = call_as(ident2, "/api/v1/site/put", edit_put)
        check("@everyone SITE_EDIT grants site/put", r5b.get("ok") == 1, r5b)
        r5b2 = call_as(ident2, "/api/v1/site/manifest_put",
                       {"channel": cid, "manifest": {"theme": "x"}})
        check("@everyone SITE_EDIT grants manifest_put",
              r5b2.get("ok") == 1, r5b2)

        revoke = call("/api/v1/channels/patch",
                      {"channel": cid, "overwrites": {"0": None}})
        check("@everyone overwrite deletable (null perms)",
              revoke.get("ok") == 1, revoke)
        ident3, H3 = _mk_user()
        sHades.db.execute("INSERT INTO users (dest_hash, username, "
                          "registered_at) VALUES (?,?,?)",
                          (H3, "plain", sHades.now()))
        sHades.db.commit()
        r5c = call_as(ident3, "/api/v1/site/put", edit_put)
        check("default (open VIEW/POST) does not carry SITE_EDIT",
              r5c.get("ok") == 0 and r5c.get("code") == 403, r5c)

        editors = sHades._ensure_role("Editors", sHades.PERMS["SITE_EDIT"],
                                      position=1)
        sHades._assign_role(H2, editors)
        r5d = call_as(ident2, "/api/v1/site/put", edit_put)
        check("role-carried SITE_EDIT grants site/put", r5d.get("ok") == 1, r5d)

        managers = sHades._ensure_role(
            "Managers", sHades.PERMS["MANAGE_MESSAGES"]
            | sHades.PERMS["MANAGE_CHANNEL"], position=2)
        sHades._assign_role(H3, managers)
        r5e = call_as(ident3, "/api/v1/site/put", edit_put)
        check("moderator-class roles pass without SITE_EDIT",
              r5e.get("ok") == 1, r5e)

        # 6. P7c: site/get publishes can_edit for the editor affordance
        r6a = call_as(ident2, "/api/v1/site/get",
                      {"channel": cid, "path": "/index.mu"})
        check("site/get stamps can_edit for a SITE_EDIT holder",
              r6a.get("ok") == 1 and r6a.get("can_edit") is True, r6a)
        ident4, H4 = _mk_user()
        sHades.db.execute("INSERT INTO users (dest_hash, username, "
                          "registered_at) VALUES (?,?,?)",
                          (H4, "reader", sHades.now()))
        sHades.db.commit()
        r6b = call_as(ident4, "/api/v1/site/get",
                      {"channel": cid, "path": "/index.mu"})
        check("site/get stamps can_edit=false for a plain reader",
              r6b.get("ok") == 1 and r6b.get("can_edit") is False, r6b)
        r6c = call_as(ident3, "/api/v1/site/get",
                      {"channel": cid, "path": "/index.mu"})
        check("site/get stamps can_edit for the moderator-class role",
              r6c.get("ok") == 1 and r6c.get("can_edit") is True, r6c)

    print("PASS site seed suite: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
