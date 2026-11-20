#!/usr/bin/env python3
"""P11 arrow 5: the client updater.

Covers:
  - parse_version (the client-local mirror, same order rules);
  - update_decision: only the github toggle offers, only strictly-newer,
    unparseable versions refuse to offer;
  - newest_client_asset: newest arm64 apk strictly-newer than the running
    one, with its .sha256 companion REQUIRED;
  - parse_sha_file: 'hex name' and lone-hex accepted, garbage refused;
  - download_with_dm via the lane seam: sha mismatch refuses BEFORE any
    install-shaped call, match returns the bytes;
  - install_intent via the seam: asserts path + the apk mime;
  - save_candidate via the seam: parks the verified bytes;
  - the server half: whoami's guild payload carries app_version ==
    SERVER_VERSION + the update_channel key, flipping with the config;
  - the app half: _apply_update_hint drives the strip property from the
    pure decision (fake app object, no widget tree).
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import client_update  # noqa: E402
import sqlite3        # noqa: E402
import sHades         # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=None):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail)


pv = client_update.parse_version
check("parse: plain", pv("0.36.20") == (0, 36, 20))
check("parse: v prefix", pv("v0.36.22") == (0, 36, 22))
check("order: 0.9 < 0.10", pv("0.9") < pv("0.10"))
check("order: shorter < longer", pv("0.36") < pv("0.36.2"))
check("parse: garbage -> None", pv("") is None and pv(None) is None
      and pv("x.1") is None and pv("0.36.") is None)

dec = client_update.update_decision
check("decision: off -> None", dec("0.36.20", "0.36.22", "off") is None)
check("decision: rns -> None", dec("0.36.20", "0.36.22", "rns") is None)
check("decision: equal -> None", dec("0.36.22", "0.36.22", "github")
      is None)
check("decision: ahead -> None", dec("0.36.23", "0.36.22", "github")
      is None)
check("decision: behind offers",
      dec("0.36.20", "0.36.22", "github") == (0, 36, 22))
check("decision: garbage server -> None",
      dec("0.36.20", "nonsense", "github") is None)

ASSETS = [
    {"name": "shades-0.36.19-arm64-v8a-debug.apk", "browser_download_url":
     "http://h/apk19"},
    {"name": "shades-0.36.21-arm64-v8a-debug.apk", "browser_download_url":
     "http://h/apk21"},
    {"name": "shades-0.36.21-arm64-v8a-debug.apk.sha256",
     "browser_download_url": "http://h/sha21"},
    {"name": "shades-0.36.20-arm64-v8a-debug.apk", "browser_download_url":
     "http://h/apk20"},
    {"name": "shades-0.36.22-arm64-v8a-debug.apk",
     "browser_download_url": "http://h/apk22"},
    {"name": "shades-0.36.22-arm64-v8a-debug.apk.sha256",
     "browser_download_url": "http://h/sha22"},
    {"name": "sHades-server-0.36.22.tar.gz", "browser_download_url":
     "http://h/bundle"},
]
pick = client_update.newest_client_asset(ASSETS)
check("pick: newest arm64 + sha companion",
      pick and pick["apk_name"] == "shades-0.36.22-arm64-v8a-debug.apk"
      and pick["apk_url"] == "http://h/apk22"
      and pick["sha_url"] == "http://h/sha22"
      and pick["version"] == "0.36.22", str(pick))
pick_l = client_update.newest_client_asset(ASSETS, since="0.36.20")
check("pick: since-gate skips <= running", pick_l
      and pick_l["apk_name"] == "shades-0.36.22-arm64-v8a-debug.apk")
check("pick: since at newest -> None",
      client_update.newest_client_asset(ASSETS, since="0.36.22") is None)
check("pick: sha companion missing -> None",
      client_update.newest_client_asset([ASSETS[0], ASSETS[3]]) is None)
try:
    client_update.newest_client_asset(ASSETS, since="gibberish")
    check("pick: unparseable running version raises", False)
except client_update.HttpError:
    check("pick: unparseable running version raises", True)

psf = client_update.parse_sha_file
check("sha body: hex + name", psf(b"a" * 64 + b"  shades-x.apk\n")
      == "a" * 64)
check("sha body: lone hex", psf(("b" * 64).encode()) == "b" * 64)
try:
    psf(b"garbage")
    check("sha body: garbage raises", False)
except client_update.HttpError:
    check("sha body: garbage raises", True)

SHA_OK = hashlib.sha256(b"apkbytes").hexdigest()
inst_calls, write_calls = [], []


def fake_installer(path, mime):
    inst_calls.append((path, mime))
    return True


def fake_writer(blob, name, dest):
    write_calls.append((dest, name))
    return os.path.join(dest, name)


try:
    client_update.download_with_dm("u", "n.apk", "z" * 64,
                                   sha_bytes=b"apkbytes")
    check("dm: sha mismatch refuses", False)
except client_update.HttpError as e:
    check("dm: sha mismatch refuses", "disagrees" in str(e), str(e))
check("dm: mismatch ran no installer", inst_calls == [])
blob = client_update.download_with_dm("u", "n.apk", SHA_OK,
                                      sha_bytes=b"apkbytes")
check("dm: match returns bytes", blob == b"apkbytes")

path = client_update.install_intent("/cache/x.apk", "x.apk",
                                    installer=fake_installer)
check("install seam: path + apk mime asserted",
      inst_calls and inst_calls[0] == ("/cache/x.apk",
                                       "application/vnd.android."
                                       "package-archive"), inst_calls)
p2 = client_update.save_candidate(b"apkbytes", "x.apk", "/tmp",
                                  writer=fake_writer)
check("save seam: parks via writer",
      write_calls == [("/tmp", "x.apk")] and p2.endswith("x.apk"))

# ------------------------------------------------------- the server half
import json    # noqa: E402
import tempfile  # noqa: E402

base = tempfile.mkdtemp(prefix="shades_upd_cli_")
old_storage, old_db = sHades.STORAGE, sHades.db
sHades.STORAGE = base

import RNS  # noqa: E402

_rid = RNS.Identity()
RVIEWER = sHades.hexstr(RNS.Destination(
    _rid, RNS.Destination.OUT, RNS.Destination.SINGLE,
    "lxmf", "delivery").hash)


def call(path):
    payload = json.dumps({}).encode("utf-8")
    return json.loads(sHades.API_ROUTES[path](
        "/api/v1/x", payload, "req-1", "L1", _rid,
        sHades.now()).decode("utf-8"))


sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at)"
                  " VALUES (?, ?, ?)", (RVIEWER, "viewer", sHades.now()))
sHades.db.commit()
try:
    res = call("/api/v1/whoami")
    g = res.get("guild") or {}
    check("whoami: guild carries app_version == SERVER_VERSION",
          g.get("app_version") == sHades.SERVER_VERSION, g)
    check("whoami: guild carries update_channel",
          g.get("update_channel") == "off", g)
    sHades._cfg_set("update_channel", "github")
    res = call("/api/v1/whoami")
    check("whoami: toggle flip is visible",
          res["guild"].get("update_channel") == "github")
finally:
    sHades.STORAGE, sHades.db = old_storage, old_db

# ---------------------------------------------------------- the app half
import kivy_client  # noqa: E402  (mock-kivy import; jnius-free)


class _FakeApp:
    client_update_version = ""
    _v = "0.36.20"
    _apply_update_hint = kivy_client.sHadesClientApp._apply_update_hint

    def _client_ver(self):
        return self._v


def hint(my, server, channel):
    app = _FakeApp()
    app._v = my
    app._apply_update_hint(server, channel)
    return app.client_update_version


check("hint: behind on github -> strip set", hint("0.36.20", "0.36.22",
                                                  "github") == "0.36.22")
check("hint: off -> strip cleared", hint("0.36.20", "0.36.22", "off")
      == "")
check("hint: current -> strip cleared", hint("0.36.22", "0.36.22",
                                             "github") == "")

print()
print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
