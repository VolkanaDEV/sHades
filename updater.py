"""P11 chain 2: the shared updater core (sHades <-> server, later client).

Transport adapters: github (now), rns (arrow 6). The core is transport-free:
a fetcher returns a release manifest, pick_assets resolves the server bundle
from it, verify_sha gates the bytes, stage_and_swap installs them with a
kept backup, and the restart/rollback loop lives in update_run.py so a
bundle that cannot boot rolls itself back under the marker file.

Design law: no import of sHades here (sHades imports us) — the current
version, the config getter and the tree dir all arrive as arguments.
"""
import hashlib
import hmac
import json
import os
import py_compile
import shutil
import stat
import tarfile
import tempfile
import urllib.request

GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "sHades-updater"
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
HTTP_TIMEOUT = 20
# The members a server bundle must carry before anything swaps.
TREE_MEMBERS = ("sHades.py", "updater.py", "update_run.py")

_status = {"last_check": None, "verdict": "never", "detail": ""}


def get_status():
    return dict(_status)


def _note(verdict, detail=""):
    _status["verdict"] = verdict
    _status["detail"] = str(detail)[:400]
    return _status


# ---------------------------------------------------------------------------
# version compare
# ---------------------------------------------------------------------------

def parse_version(s):
    """'0.36.20' -> (0, 36, 20); 'v0.36.20' tolerated; garbage -> None."""
    if not isinstance(s, str):
        return None
    s = s.strip().lstrip("vV")
    if not s or not all(ch.isdigit() or ch == "." for ch in s):
        return None
    parts = s.split(".")
    if any(not p for p in parts):
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# github transport
# ---------------------------------------------------------------------------

class HttpError(Exception):
    pass


def _get_bytes(url, size_cap=MAX_BUNDLE_BYTES):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        if r.status != 200:
            raise HttpError("http %s for %s" % (r.status, url))
        out = bytearray()
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            out += chunk
            if len(out) > size_cap:
                raise HttpError("payload exceeds size cap")
    return bytes(out)


def fetch_latest_release(repo, api_base=None):
    """GET the latest release manifest: {'tag': str, 'assets': {name: url}}."""
    base = (api_base or GITHUB_API_BASE).rstrip("/")
    raw = _get_bytes("%s/repos/%s/releases/latest" % (base, repo))
    rel = json.loads(raw.decode("utf-8"))
    if not isinstance(rel, dict) or "tag_name" not in rel:
        raise HttpError("release manifest malformed")
    assets = {}
    for a in rel.get("assets") or []:
        name, url = a.get("name"), a.get("browser_download_url")
        if name and url:
            assets[name] = url
    return {"tag": rel["tag_name"], "assets": assets}


def pick_assets(assets, sha_body=None):
    """Resolve the server bundle: the sHades-server-<V>.sha256 asset names
    the version, its body's first line carries '<hex>  <fname>'; the
    bundle asset must exist under that name. Returns None when absent."""
    import re
    sha_names = sorted(n for n in assets
                       if re.match(r"^sHades-server-[0-9.]+\.sha256$", n))
    if not sha_names:
        return None
    sname = sha_names[-1]  # one server bundle per release; newest wins
    vmatch = re.match(r"^sHades-server-([0-9.]+)\.sha256$", sname)
    version = vmatch.group(1) if vmatch else None
    if version is None or parse_version(version) is None:
        raise HttpError("sha asset name carries no parseable version")
    fname = "sHades-server-%s.tar.gz" % version
    if fname not in assets:
        return None
    body = sha_body if sha_body is not None else _get_bytes(assets[sname])
    text = body.decode("utf-8", "replace")
    sha = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and "sHades-server-" in parts[1]:
            sha = parts[0]
            break
    if not sha or len(sha) != 64 or any(
            ch not in "0123456789abcdefABCDEF" for ch in sha):
        raise HttpError("sha body malformed")
    return {"version": version,
            "sha256": sha.lower(),
            "bundle_url": assets[fname],
            "bundle_name": fname}


# ---------------------------------------------------------------------------
# verify + stage + swap
# ---------------------------------------------------------------------------

def verify_sha(data, sha):
    got = hashlib.sha256(data).hexdigest()
    return hmac.compare_digest(got, sha.lower())


def _member_ok(name, ti):
    if not (name and not name.startswith("/") and name not in ("", ".")):
        return False
    if any(part == ".." for part in name.replace("\\", "/").split("/")):
        return False
    if ti.issym() or ti.islnk():
        return False  # no links in our bundles
    return not ti.ischr() and not ti.isblk() and not ti.isdev() and (
        ti.isreg() or ti.isdir())


def _extract_safe(tar_path, dest):
    n = 0
    with tarfile.open(tar_path, "r:gz") as tar:
        while True:
            ti = tar.next()
            if ti is None:
                break
            if not _member_ok(ti.name, ti):
                raise HttpError("unsafe bundle member: %s" % ti.name)
            if ti.isdir():
                os.makedirs(os.path.join(dest, ti.name), exist_ok=True)
                continue
            if ti.isreg():
                target = os.path.join(dest, ti.name)
                parent = os.path.dirname(target)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                src = tar.extractfile(ti)
                if src is None:
                    raise HttpError("member unreadable: %s" % ti.name)
                with open(target, "wb") as f:
                    shutil.copyfileobj(src, f, 65536)
                mode = (stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR) \
                    if ti.name.endswith(".sh") else (stat.S_IRUSR |
                                                     stat.S_IWUSR)
                os.chmod(target, mode)
                n += 1
    return n


def _staged_pyc_ok(dest, members):
    """A staged tree that cannot compile never swaps (boot-failure guard)."""
    cdir = tempfile.mkdtemp(prefix="sHades-probe-")
    try:
        for m in members:
            path = os.path.join(dest, m)
            if not os.path.isfile(path):
                return "missing member %s" % m
            try:
                py_compile.compile(
                    path, cfile=os.path.join(cdir, "probe.pyc"), doraise=True)
            except py_compile.PyCompileError as e:
                return "staged %s does not compile: %s" % (m, str(e)[:200])
    finally:
        shutil.rmtree(cdir, ignore_errors=True)
    return None


def stage_and_swap(bundle_path, sha, tree_dir):
    """Extract -> compile-probe -> keep a staging dir + marker for the
    swap. Returns a status dict; raises HttpError on any refusal."""
    data = open(bundle_path, "rb").read()
    if not verify_sha(data, sha):
        _note("refused", "downloaded bundle sha disagrees with the .sha256")
        raise HttpError("sha mismatch refusing to stage")
    with tarfile.open(bundle_path, "r:gz"):
        pass  # openability check (bad gzip dies here, not at extract)
    stage = tempfile.mkdtemp(prefix="sHades-stage-")
    try:
        _extract_safe(bundle_path, stage)
        bad = _staged_pyc_ok(stage, TREE_MEMBERS)
        if bad:
            _note("refused", bad)
            raise HttpError("staged tree failed the import probe: %s" % bad)
        staging = os.path.join(tree_dir, "update_staging")
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        os.makedirs(staging, exist_ok=True)
        shutil.move(stage, os.path.join(staging, "next"))
        stage = None  # moved; do not rmtree
        marker = os.path.join(staging, "pending_version")
        with open(marker, "w", encoding="ascii") as f:
            f.write(os.path.basename(bundle_path))
        _note("staged", os.path.basename(bundle_path))
        return {"staged": True,
                "staging": os.path.join(staging, "next"),
                "marker": marker}
    finally:
        if stage:
            shutil.rmtree(stage, ignore_errors=True)


def apply_update(bundle_path, sha, tree_dir, argv=None, exec_restart=True):
    """Swap the staged members into the tree (+ backup), then hand the
    process over to update_run.py which owns restart + rollback."""
    st = stage_and_swap(bundle_path, sha, tree_dir)
    staging = st["staging"]
    backup = os.path.join(tree_dir, "update_backup")
    os.makedirs(backup, exist_ok=True)
    for m in TREE_MEMBERS:
        src = os.path.join(staging, m)
        if not os.path.isfile(src):
            raise HttpError("staged tree lacks %s" % m)
        old = os.path.join(tree_dir, m)
        if os.path.isfile(old):
            shutil.copy2(old, os.path.join(backup, m))
        shutil.copy2(src, old)  # copy: staging stays for the rollback leg
    _note("swapped", "backup at %s" % backup)
    if exec_restart:
        _handover(tree_dir, argv)
    return {"swapped": True, "backup": backup, "staging": staging}


def _handover(tree_dir, argv):
    if os.environ.get("SHADES_NO_RESTART") == "1":
        _note("held", "SHADES_NO_RESTART=1, no execv")
        return
    runner = os.path.join(tree_dir, "update_run.py")
    args = list(argv if argv is not None else sys_argv())
    os.environ["SHADES_PIDFILE"] = ""  # exec'd process re-writes its own
    os.execv(sys_executable(), [sys_executable(), runner] + args)


def sys_executable():
    import sys
    return sys.executable


def sys_argv():
    import sys
    return sys.argv


def check_and_apply(tree_dir, current_version, cfg_get, argv=None,
                    exec_restart=True, api_base=None, rns_fetch=None):
    """Full round: toggle gate -> manifest -> compare -> download ->
    sha -> stage -> swap -> handover. Returns a status dict either way.
    The github channel fetches the manifest itself; the rns channel
    (P11d) hands fetching to the rns_fetch callable and reuses this
    tail verbatim: verify -> apply. The core stays transport-free."""
    import time
    _status["last_check"] = time.time()
    ch = (cfg_get("update_channel", "off") or "off").strip().lower()
    if ch == "off":
        _note("off", "update_channel is off")
        return {"skipped": True, "reason": "off"}
    if ch == "rns":
        if rns_fetch is None:
            _note("pending", "rns channel selected but no fetch hook wired")
            return {"skipped": True, "reason": "no-rns-fetch"}
        picked = rns_fetch()
        if picked is None:
            return {"skipped": True, "reason": "no-bundle"}
        cur = parse_version(current_version)
        new = parse_version(picked["version"])
        if cur is None or new is None:
            raise HttpError("unparseable version pair")
        if new <= cur:
            _note("current", "%s is the newest server bundle"
                  % picked["version"])
            return {"skipped": True, "reason": "current",
                    "newest": picked["version"]}
        tmp = tempfile.mkdtemp(prefix="sHades-dl-rns-")
        bp = os.path.join(tmp, picked["bundle_name"])
        with open(bp, "wb") as f:
            f.write(picked["bundle_bytes"])
        try:
            res = apply_update(bp, picked["sha256"], tree_dir, argv=argv,
                               exec_restart=exec_restart)
            res["new_version"] = picked["version"]
            res["carrier"] = "rns"
            return res
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if ch != "github":
        _note("pending", "channel %r not wired yet (rns rides arrow 6)" % ch)
        return {"skipped": True, "reason": "channel-not-wired"}
    release = fetch_latest_release(cfg_get("update_repo",
                                           "eldeucebag/sHades") or
                                   "eldeucebag/sHades", api_base=api_base)
    picked = pick_assets(release["assets"])
    if picked is None:
        _note("no-bundle", "release carries no server bundle")
        return {"skipped": True, "reason": "no-bundle"}
    cur = parse_version(current_version)
    new = parse_version(picked["version"])
    if cur is None or new is None:
        raise HttpError("unparseable version pair")
    if new <= cur:
        _note("current", "%s is the newest server bundle" % picked["version"])
        return {"skipped": True, "reason": "current",
                "newest": picked["version"]}
    data = _get_bytes(picked["bundle_url"])
    if not verify_sha(data, picked["sha256"]):
        _note("refused", "bundle bytes disagree with the .sha256")
        raise HttpError("sha mismatch on download")
    tmp = tempfile.mkdtemp(prefix="sHades-dl-")
    bp = os.path.join(tmp, picked["bundle_name"])
    with open(bp, "wb") as f:
        f.write(data)
    try:
        res = apply_update(bp, picked["sha256"], tree_dir, argv=argv,
                           exec_restart=exec_restart)
        res["new_version"] = picked["version"]
        res["manifest_tag"] = release["tag"]
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
