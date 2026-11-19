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

    print("PASS site seed suite: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
