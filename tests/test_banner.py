#!/usr/bin/env python3
"""Server banner (P5, plan_admin v1.1) — no mesh.

Pins down:
  - "banner" is a server-config key riding the whitelist with the same
    validation shape as guild_icon (stored blob_id or empty);
  - whoami surfaces {guild: {..., banner}} to every member;
  - junk values rejected, plain users 403, flips audited;
  - the key stays settable in ephemeral mode (the blobs metadata row
    plus the _blob_fname DB fallback carry the reference);
  - kv side: the sidebar strip keeps the letter-tile fallback block;
  - client side: the ingest engine stores banner bytes under
    banners/<host>_<blob head>.<ext> and records the servers entry
    (functional headless probe).
"""
import os
import sys
import json
import sqlite3
import tempfile
import time
import base64

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          ("" if cond else ("  | " + str(extra))))


base = tempfile.mkdtemp(prefix="shades_banner_test_")
sHades.STORAGE = base
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.DEBUG = False


def _mk_user(name):
    i = RNS.Identity()
    h = sHades.hexstr(RNS.Destination(i, RNS.Destination.OUT,
                                      RNS.Destination.SINGLE,
                                      "lxmf", "delivery").hash)
    return i, h


admin_ident, ADMIN = _mk_user("admin")
plain_ident, PLAIN = _mk_user("plain")
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                  "VALUES (?,?,?)", (ADMIN, "admin", sHades.now()))
sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, "
                  "registered_at) VALUES (?,?,?)", (PLAIN, "plain",
                                                    sHades.now()))
sHades.db.commit()
admin_role = sHades._ensure_role("Admin", sHades.PERM_ALL, position=100)
sHades._assign_role(ADMIN, admin_role)

_idents = {ADMIN: admin_ident, PLAIN: plain_ident}


def call(path, data=None, user=ADMIN):
    payload = json.dumps(data or {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1",
                                  _idents[user], sHades.now())
    return json.loads(raw.decode("utf-8"))


def main():
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(90)
    r = call("/api/v1/blobs/put", {"name": "banner.png", "mime": "image/png",
                                   "data_b64": base64.b64encode(png)
                                   .decode()})
    check("banner blob stored", r.get("ok") == 1, r)
    blob_id = r.get("blob_id")

    # ---------- default: no banner ----------
    r = call("/api/v1/whoami", user=PLAIN)
    check("whoami carries banner=None by default",
          r.get("ok") == 1 and not r["guild"].get("banner"), r)

    # ---------- set + read back + whoami ----------
    r = call("/api/v1/admin/config", {"key": "banner", "value": blob_id})
    check("admin sets banner to a blob_id", r.get("ok") == 1, r)
    r = call("/api/v1/admin/config", {"key": "banner"})
    check("config read-back agrees", r["config"]["banner"] == blob_id, r)
    r = call("/api/v1/whoami", user=PLAIN)
    check("whoami surfaces the banner to members",
          r["guild"].get("banner") == blob_id, r)

    # ---------- audit ----------
    r = call("/api/v1/admin/audit")
    row = [e for e in r.get("entries", []) if e["action"] == "config.set"
           and e["target"] == "banner"]
    check("banner write audited", bool(row), r.get("entries", [])[-2:])

    # ---------- junk rejected ----------
    for junk in ("not-a-blob", "0" * 64, os.path.join(base, "banner.png")):
        r = call("/api/v1/admin/config", {"key": "banner", "value": junk})
        check("junk banner rejected: %r" % junk[:14],
              r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/admin/config", {"key": "banner", "value": "f" * 32})
    check("a 32-hex digest with no stored blob rejected",
          r.get("ok") == 0 and r.get("code") == 400, r)

    # ---------- permission ----------
    r = call("/api/v1/admin/config", {"key": "banner", "value": blob_id},
             user=PLAIN)
    check("plain user denied", r.get("ok") == 0 and r.get("code") == 403, r)

    # ---------- ephemeral interplay ----------
    r = call("/api/v1/admin/config", {"key": "attachments",
                                      "value": "ephemeral"})
    check("flip to ephemeral", r.get("ok") == 1, r)
    png2 = b"\x89PNG\r\n\x1a\n" + os.urandom(40)
    r = call("/api/v1/blobs/put", {"name": "b2.png",
                                   "data_b64": base64.b64encode(png2)
                                   .decode()})
    check("banner blob 2 stored (RAM in ephemeral mode)",
          r.get("ok") == 1, r)
    bid2 = r.get("blob_id")
    r = call("/api/v1/admin/config", {"key": "banner", "value": bid2})
    check("banner stays settable in ephemeral mode (RAM blob + "
          "metadata-row fallback)", r.get("ok") == 1, r)
    call("/api/v1/admin/config", {"key": "attachments", "value": "saved"})

    # ---------- clear ----------
    r = call("/api/v1/admin/config", {"key": "banner", "value": ""})
    check("empty value clears the banner", r.get("ok") == 1, r)
    r = call("/api/v1/whoami", user=PLAIN)
    check("banner gone after clearing", not r["guild"].get("banner"), r)

    # ---------- kv contract: strip block + letter-tile fallback ----------
    kv_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "kv_md.kv")
    kv = open(kv_path, encoding="utf-8").read()
    # EOL law, portable across runners (repo bytes are LF; a Windows
    # checkout may show CRLF via autocrlf): consistent terminators only
    raw_kv = open(kv_path, "rb").read()
    crlf, lf_only = bytes((13, 10)), bytes((10,))
    cr, lf = raw_kv.count(crlf), raw_kv.count(lf_only)

    assert cr == lf or cr == 0, "kv eol law (consistent terminators)"
    check("kv has the banner strip container", "id: banner_strip" in kv)
    check("kv has the letter-tile fallback label", "id: banner_letter"
          in kv and "app.banner_letter or 'S'" in kv)
    check("kv strip opacity rides the banner property",
          "opacity: 1 if app.banner_channel_path else 0" in kv
          and "opacity: 0 if app.banner_channel_path else 1" in kv)
    check("kv strip keeps the original Image id", "id: banner_channel"
          in kv)

    # ---------- client lane: ingest engine, headless functional probe --
    py_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "kivy_client.py")
    py = open(py_path, encoding="utf-8").read()
    check("kivy_client has the shared ingest engine",
          "def _fetch_server_blob" in py)
    check("kivy_client has the banner ingest wrapper",
          "def _ingest_banner" in py and '"banners"' in py
          and '"banner_blob"' in py)
    check("kivy_client whoami chain schedules the banner ingest",
          "self._ingest_banner(" in py and 'banner_blob = (guild.get(' in py)
    check("sidebar refresh rides the strip properties",
          "self.banner_channel_path = (bpath" in py
          and "self.banner_letter = (" in py)

    import kivy_client as kc

    class FakeBannerApi:
        def __init__(self, data):
            self.data = data

        def blobs_get(self, blob_id):
            return {"ok": 1,
                    "data_b64": base64.b64encode(self.data).decode()}

    app = kc.sHadesClientApp()
    app.guild_api = FakeBannerApi(png)
    app._servers = [{"host": "bannerhost:123", "display_name": "BH"}]
    app._active_server_idx = 0
    app._save_servers = lambda: None
    app._refresh_server_sidebar = lambda: None
    app._ingest_banner(blob_id, app._servers[0])
    srv = app._servers[0]
    deadline = time.time() + 5.0
    while time.time() < deadline and "banner_path" not in srv:
        time.sleep(0.05)
    check("ingest stored the banner file", os.path.isfile(
        srv.get("banner_path", "")), srv)
    check("ingest recorded the server entry keys",
          srv.get("banner_blob") == blob_id
          and os.path.dirname(srv["banner_path"]) == os.path.join(
              kc._app_data_dir(), "banners"), srv)
    check("ingest filename shape <host>_<blob head>.<ext>",
          os.path.basename(srv.get("banner_path", "")) ==
          "bannerhost_123_%s.png" % blob_id[:12],
          srv.get("banner_path"))

    print("RESULT: %d PASS, %d FAIL" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
