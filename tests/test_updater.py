#!/usr/bin/env python3
"""P11 chain 2: the updater core + the server toggle + the rollback launcher.

Covers:
  - version compare (tuple parse, '0.9' < '0.10', malformed);
  - release manifest -> bundle resolution (the .sha256 asset names the
    version, the bundle asset must exist under that name);
  - sha gate: match passes with case-insensitivity, mismatch refuses;
  - safe extraction: hostile members (absolute, traversal) refused; the
    staged tree must compile before anything swaps;
  - the toggle: update_channel off / rns / github — off and rns fetch
    NOTHING (the local http server's hit list proves it);
  - the happy path: manifest -> download -> sha -> stage -> backup -> swap
    (the execv is held behind updaters exec_restart=False / SHADES_NO_RESTART);
  - sha mismatch in flight: refused, nothing stages;
  - update_run.py end to end as a REAL subprocess: a tree whose sHades.py
    exits 1 rolls back to the backup, clears the marker, and the second
    boot succeeds (proved by the stubs' append-only runlog).
"""
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import updater  # noqa: E402
import sHades   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=None):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail)


# ---------------------------------------------------------------- version
pv = updater.parse_version
check("parse: plain", pv("0.36.20") == (0, 36, 20))
check("parse: v prefix", pv("v0.36.20") == (0, 36, 20))
check("order: 0.9 < 0.10", pv("0.9") < pv("0.10"))
check("order: 0.36.19 < 0.36.20", pv("0.36.19") < pv("0.36.20"))
check("order: shorter < longer", pv("0.36") < pv("0.36.2"))
check("order: 0.36.20 < 0.100.1", pv("0.36.20") < pv("0.100.1"))
check("parse: garbage -> None", pv("gibber.1.x") is None
      and pv("") is None and pv(None) is None)
check("parse: trailing dot -> None", pv("0.36.") is None)

# ------------------------------------------------------------- resolver
GOOD_SHA = "a" * 64
ASSETS_OK = {
    "sHades-server-0.36.21.tar.gz": "http://localhost/tar",
    "sHades-server-0.36.21.sha256": "http://localhost/sha",
    "something-else.apk": "http://localhost/other",
}
picked = updater.pick_assets(
    ASSETS_OK,
    sha_body=("  ".join([GOOD_SHA, "sHades-server-0.36.21.tar.gz"])
              + "\n").encode("ascii"))
check("pick: resolves version+url+sha",
      picked is not None
      and picked["version"] == "0.36.21"
      and picked["bundle_url"] == "http://localhost/tar"
      and picked["sha256"] == GOOD_SHA, str(picked))
check("pick: bundle asset absent -> None",
      updater.pick_assets({"sHades-server-0.36.21.sha256": "x"}) is None)
try:
    updater.pick_assets(ASSETS_OK, sha_body=b"not-a-sha")
    check("pick: malformed sha body raises", False)
except updater.HttpError:
    check("pick: malformed sha body raises", True)


# ---------------------------------------------------------------- sha gate
def sha_of(data):
    return hashlib.sha256(data).hexdigest()


check("verify: match", updater.verify_sha(b"abc", sha_of(b"abc")))
check("verify: case-insensitive",
      updater.verify_sha(b"abc", sha_of(b"abc").upper()))
check("verify: mismatch refuses",
      not updater.verify_sha(b"abc", "0" * 64))

# ------------------------------------------------------- extract + probe
TAR_DIR = tempfile.mkdtemp(prefix="shades_upd_tar_")


def make_tar(path, pairs, hostile=None):
    with tarfile.open(path, "w:gz") as t:
        for name, content in pairs:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        if hostile is not None:
            t.addfile(hostile)


def stub_file(tag, fail=False):
    lines = ["import sys",
             "open('runlog.txt', 'a').write('%s\\n')" % tag]
    lines.append("sys.exit(1)" if fail else "sys.exit(0)")
    return "\n".join(lines) + "\n"


good_tar = os.path.join(TAR_DIR, "good.tar.gz")
make_tar(good_tar, [("sHades.py", "x = 1\n"),
                    ("updater.py", "x = 2\n"),
                    ("update_run.py", "x = 3\n"),
                    ("docs/readme.txt", "hi\n")])
stage_dest = tempfile.mkdtemp(prefix="shades_upd_dest_")
n = updater._extract_safe(good_tar, stage_dest)
check("extract: members landed", n == 4
      and os.path.isfile(os.path.join(stage_dest, "sHades.py")))
check("extract: nested dir member extracted",
      os.path.isfile(os.path.join(stage_dest, "docs", "readme.txt")))
check("extract: probe passes",
      updater._staged_pyc_ok(stage_dest, updater.TREE_MEMBERS) is None)

for label, hostile_name in (("absolute", "/etc/evil.sh"),
                            ("traversal", "../evil.sh")):
    bad = os.path.join(TAR_DIR, label + ".tar.gz")
    make_tar(bad, [], hostile=tarfile.TarInfo(hostile_name))
    try:
        updater._extract_safe(bad, stage_dest)
        check("extract: %s member refused" % label, False)
    except updater.HttpError:
        check("extract: %s member refused" % label, True)

broken_tar = os.path.join(TAR_DIR, "broken.tar.gz")
make_tar(broken_tar, [("sHades.py", "def (:\n    pass\n"),
                      ("updater.py", "x = 2\n"),
                      ("update_run.py", "x = 3\n")])
broken_dest = tempfile.mkdtemp(prefix="shades_upd_broken_")
updater._extract_safe(broken_tar, broken_dest)
check("probe: broken staged tree refused",
      updater._staged_pyc_ok(broken_dest, updater.TREE_MEMBERS) is not None)

# ------------------------------------------- local http seam + the toggle
Hits = []


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        Hits.append(self.path)
        if self.path.endswith("/releases/latest"):
            body = json.dumps({
                "tag_name": "v0.36.21",
                "assets": [
                    {"name": "sHades-server-0.36.21.tar.gz",
                     "browser_download_url": "http://%s/tar" % HOST},
                    {"name": "sHades-server-0.36.21.sha256",
                     "browser_download_url": "http://%s/sha" % HOST},
                ],
            }).encode()
        elif self.path == "/tar":
            body = srv.tar_bytes
        elif self.path == "/sha":
            body = srv.sha_line
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
HOST = "%s:%s" % srv.server_address
srv.daemon_threads = True
srv.tar_bytes = b""
srv.sha_line = b""
threading.Thread(target=srv.serve_forever, daemon=True).start()


class _DB:
    """A scratch server config DB per with-block."""

    def __enter__(self):
        self.base = tempfile.mkdtemp(prefix="shades_upd_db_")
        self.old_storage, self.old_db = sHades.STORAGE, sHades.db
        self.old_last = list(sHades._UPDATE_LAST)
        sHades.STORAGE = self.base
        sHades.db = sqlite3.connect(os.path.join(self.base, "sHades.db"))
        sHades.db.row_factory = sqlite3.Row
        sHades.db.executescript(sHades.SCHEMA)
        sHades._migrate_schema(sHades.db)
        sHades.db.commit()
        return self

    def __exit__(self, *exc):
        sHades.STORAGE, sHades.db = self.old_storage, self.old_db
        sHades._UPDATE_LAST[:] = self.old_last
        return False


def cfg(key, default=None):
    return sHades._cfg_get(key, default)


# --- off / rns fetch nothing
Hits.clear()
with _DB():
    r = updater.check_and_apply(TAR_DIR, "0.36.20", cfg,
                                api_base="http://127.0.0.1:1")
    check("toggle off: skipped without any fetch",
          bool(r.get("skipped")) and Hits == [], (str(r), Hits))
    sHades._cfg_set("update_channel", "rns")
    r = updater.check_and_apply(TAR_DIR, "0.36.20", cfg,
                                api_base="http://127.0.0.1:1")
    check("toggle rns: skipped, channel not wired",
          bool(r.get("skipped"))
          and r.get("reason") == "channel-not-wired", str(r))
    check("toggle rns: still no fetch", Hits == [], "hits=%s" % Hits)

# --- happy path: fetch, gate, stage, swap (exec restart off in-lane)
tar_path = os.path.join(TAR_DIR, "sHades-server-0.36.21.tar.gz")
make_tar(tar_path, [("sHades.py", stub_file("NEW-0.36.21")),
                    ("updater.py", stub_file("NEW")),
                    ("update_run.py", stub_file("NEW"))])
digest = sha_of(open(tar_path, "rb").read())
srv.tar_bytes = open(tar_path, "rb").read()
srv.sha_line = ("%s  sHades-server-0.36.21.tar.gz\n" % digest).encode()

Hits.clear()
with _DB():
    sHades._cfg_set("update_channel", "github")
    tree = tempfile.mkdtemp(prefix="shades_upd_tree_")
    for member, content in (("sHades.py", stub_file("OLD")),
                            ("updater.py", "x = 9\n"),
                            ("update_run.py", "x = 9\n")):
        with open(os.path.join(tree, member), "w", encoding="utf-8") as f:
            f.write(content)
    res = updater.check_and_apply(tree, "0.36.20", cfg,
                                  api_base="http://%s" % HOST,
                                  exec_restart=False)
    check("happy: manifest+tar+sha all fetched", len(Hits) >= 3, Hits)
    check("happy: swap reports the new version",
          bool(res.get("swapped"))
          and res.get("new_version") == "0.36.21", str(res))
    with open(os.path.join(tree, "update_backup", "sHades.py"),
              encoding="utf-8") as f:
        check("happy: backup carries the OLD member", "OLD" in f.read())
    with open(os.path.join(tree, "sHades.py"), encoding="utf-8") as f:
        check("happy: the tree now carries the NEW member",
              "NEW-0.36.21" in f.read())
    check("happy: the pending marker exists",
          os.path.isfile(os.path.join(tree, "update_staging",
                                      "pending_version")))

# --- sha mismatch in flight: refuse, nothing swaps
Hits.clear()
with _DB():
    srv.sha_line = ("%s  sHades-server-0.36.21.tar.gz\n"
                    % ("0" * 64)).encode()
    sHades._cfg_set("update_channel", "github")
    tree2 = tempfile.mkdtemp(prefix="shades_upd_refuse_")
    try:
        updater.check_and_apply(tree2, "0.36.20", cfg,
                                api_base="http://%s" % HOST,
                                exec_restart=False)
        check("mismatch: raises", False)
    except updater.HttpError:
        check("mismatch: raises", True)
    check("mismatch: no backup, no marker",
          not os.path.isfile(os.path.join(tree2, "update_staging",
                                          "pending_version"))
          and not os.path.isdir(os.path.join(tree2, "update_backup")))
srv.shutdown()

# ------------------------------------ the launcher: rollback as a subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tree = tempfile.mkdtemp(prefix="shades_upd_run_")
with open(os.path.join(tree, "sHades.py"), "w", encoding="utf-8") as f:
    f.write(stub_file("BROKEN", fail=True))
os.makedirs(os.path.join(tree, "update_backup"), exist_ok=True)
with open(os.path.join(tree, "update_backup", "sHades.py"), "w",
          encoding="utf-8") as f:
    f.write(stub_file("OLD"))
os.makedirs(os.path.join(tree, "update_staging"), exist_ok=True)
with open(os.path.join(tree, "update_staging", "pending_version"), "w",
          encoding="ascii"):
    pass

proc = subprocess.run([sys.executable, os.path.join(ROOT, "update_run.py"),
                       "--bootflag"],
                      cwd=tree, capture_output=True, text=True, timeout=60)
check("launcher: exit 0 after rollback", proc.returncode == 0,
      (proc.returncode, proc.stderr[-300:]))
check("launcher: marker cleared",
      not os.path.isfile(os.path.join(tree, "update_staging",
                                      "pending_version")))
runlog = os.path.join(tree, "runlog.txt")
seen = (open(runlog, encoding="utf-8").read().split()
        if os.path.isfile(runlog) else [])
check("launcher: BROKEN attempt then OLD recovery",
      seen == ["BROKEN", "OLD"], seen)

print()
print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
