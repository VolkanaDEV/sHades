#!/usr/bin/env python3
"""Trust model step 1: signed release manifests (verify-and-log / enforce).

Covers the verifier (release_manifest.py) and its wiring into
updater.check_and_apply's github path under update_manifest_policy:
  - off      : legacy sha256 path, manifest assets never fetched
  - log      : bad/missing manifest -> proceed on legacy sha (warn noted)
  - enforce  : bad/missing manifest -> refuse, nothing swaps
  - enforce  : good manifest + good bytes -> proceeds to apply
  - anti-rollback: manifest min_version above the running version refuses
"""
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey)
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import release_manifest  # noqa: E402
import updater  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=None):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


def sha256(b):
    return hashlib.sha256(b).hexdigest()


KEY = Ed25519PrivateKey.generate()
PUB_RAW = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
PUB_B64 = __import__("base64").b64encode(PUB_RAW).decode()
PUB_HEX = PUB_RAW.hex()
PUB_PEM = KEY.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()

NOW = time.time()


def manifest_bytes(version="0.37.0", min_version="0.0.0",
                   channel="general", bundle_name="sHades-server-0.37.0.tar.gz",
                   bundle_sha="00" * 32, size=123,
                   issued=None, expires=None):
    m = {"version": version, "min_version": min_version,
         "channel": channel,
         "issued_at": issued or time.strftime(
             "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60)),
         "expires_at": expires or time.strftime(
             "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 3600)),
         "artifact": {"name": bundle_name, "sha256": bundle_sha,
                      "size": size}}
    return json.dumps(m, sort_keys=True).encode()


def sign(mb):
    return KEY.sign(mb)


def verify(mb, sig, **kw):
    return release_manifest.verify_manifest(
        mb, __import__("base64").b64encode(sig).decode(), PUB_B64, **kw)


# ------------------------------------------------ module: key loading
for label, mat in (("b64", PUB_B64), ("hex", PUB_HEX), ("pem", PUB_PEM)):
    try:
        release_manifest.load_public_key(mat)
        check("key load: %s" % label, True)
    except Exception as e:
        check("key load: %s" % label, False, e)
try:
    release_manifest.load_public_key("a" * 43)  # not 32 bytes decoded
    check("key load: garbage refused", False)
except release_manifest.ManifestError:
    check("key load: garbage refused", True)

# --------------------------------------------- module: verify happy path
MB = manifest_bytes()
SIG = sign(MB)
m = verify(MB, SIG, current_version="0.36.25", channel="general")
check("verify: happy path", m["version"] == "0.37.0")
check("verify: sha recorded", m["artifact"]["sha256"] == "00" * 32)

# tampered manifest
try:
    verify(MB.replace(b"0.37.0", b"0.37.1"), SIG,
           current_version="0.36.25")
    check("verify: tampered manifest refused", False)
except release_manifest.ManifestError:
    check("verify: tampered manifest refused", True)

# wrong key
KEY2 = Ed25519PrivateKey.generate()
pub2 = __import__("base64").b64encode(KEY2.public_key().public_bytes(
    Encoding.Raw, PublicFormat.Raw)).decode()
try:
    release_manifest.verify_manifest(MB, __import__(
        "base64").b64encode(SIG).decode(), pub2)
    check("verify: wrong key refused", False)
except release_manifest.ManifestError:
    check("verify: wrong key refused", True)

# expired / future-issued / min_version / channel mismatch
mb = manifest_bytes(expires=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(NOW - 10)))
try:
    verify(mb, sign(mb), current_version="0.36.25")
    check("verify: expired refused", False)
except release_manifest.ManifestError:
    check("verify: expired refused", True)

mb = manifest_bytes(issued=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(NOW + 86400 * 2)))
try:
    verify(mb, sign(mb), current_version="0.36.25")
    check("verify: future-issued refused", False)
except release_manifest.ManifestError:
    check("verify: future-issued refused", True)

mb = manifest_bytes(min_version="0.40.0")
try:
    verify(mb, sign(mb), current_version="0.36.25")
    check("verify: below min_version refused", False)
except release_manifest.ManifestError:
    check("verify: below min_version refused", True)

mb = manifest_bytes(channel="nightly")
try:
    verify(mb, sign(mb), current_version="0.36.25", channel="general")
    check("verify: channel mismatch refused", False)
except release_manifest.ManifestError:
    check("verify: channel mismatch refused", True)

# artifact check
data = b"payload"
mb = manifest_bytes(bundle_sha=sha256(data), size=len(data))
parsed = verify(mb, sign(mb), current_version="0.36.25")
try:
    release_manifest.check_artifact(parsed, data)
    check("artifact: size+sha ok", True)
except release_manifest.ManifestError:
    check("artifact: size+sha ok", False)
try:
    release_manifest.check_artifact(parsed, data + b"!")
    check("artifact: size mismatch refused", False)
except release_manifest.ManifestError:
    check("artifact: size mismatch refused", True)

# ------------------- integration: updater github path, policy gates
GOOD_TAR = None
TAR_DIR = tempfile.mkdtemp(prefix="shades_mf_tar_")


def make_tar(path):
    buf = [("sHades.py", "x = 1\n"), ("updater.py", "x = 2\n"),
           ("update_run.py", "x = 3\n")]
    with tarfile.open(path, "w:gz") as t:
        for name, content in buf:
            d = content.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(d)
            t.addfile(ti, io.BytesIO(d))
    return open(path, "rb").read()


TAR_PATH = os.path.join(TAR_DIR, "bundle.tar.gz")
BUNDLE = make_tar(TAR_PATH)
BUNDLE_SHA = sha256(BUNDLE)
BUNDLE_NAME = "sHades-server-0.37.0.tar.gz"
M_BYTES = manifest_bytes(bundle_name=BUNDLE_NAME, bundle_sha=BUNDLE_SHA,
                         size=len(BUNDLE))
M_SIG = sign(M_BYTES)

Hits = []


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        Hits.append(self.path)
        body = None
        if self.path.endswith("/releases/latest"):
            assets = [
                {"name": BUNDLE_NAME,
                 "browser_download_url": "http://%s/bundle" % HOST},
                {"name": "sHades-server-0.37.0.sha256",
                 "browser_download_url": "http://%s/sha" % HOST},
                {"name": "sHades-server-0.37.0.manifest.json",
                 "browser_download_url": "http://%s/manifest" % HOST},
                {"name": "sHades-server-0.37.0.manifest.json.sig",
                 "browser_download_url": "http://%s/sig" % HOST},
            ]
            body = json.dumps({"tag_name": "v0.37.0",
                               "assets": assets}).encode()
        elif self.path == "/bundle":
            body = BUNDLE
        elif self.path == "/sha":
            body = (BUNDLE_SHA + "  " + BUNDLE_NAME + "\n").encode()
        elif self.path == "/manifest":
            body = srv.manifest_bytes
        elif self.path == "/sig":
            body = srv.sig_bytes
        if body is None:
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
srv.manifest_bytes = M_BYTES
srv.sig_bytes = __import__("base64").b64encode(M_SIG).decode().encode()
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()


def make_cfg(policy, key=PUB_B64):
    vals = {"update_channel": "github",
            "update_repo": "x/y",
            "update_manifest_policy": policy,
            "update_manifest_key": key,
            "update_release_channel": "general"}
    return lambda k, default=None: vals.get(k, default)


def run_case(policy, manifest_bytes_, sig_bytes, key=PUB_B64):
    Hits.clear()
    srv.manifest_bytes = manifest_bytes_
    srv.sig_bytes = sig_bytes
    tree = tempfile.mkdtemp(prefix="shades_mf_tree_")
    try:
        updater.check_and_apply(tree, "0.36.25", make_cfg(policy, key),
                                exec_restart=False,
                                api_base="http://%s" % HOST)
        return "applied"
    except updater.HttpError as e:
        return "refused: %s" % e


# enforce + good manifest -> applies
r = run_case("enforce", M_BYTES,
             __import__("base64").b64encode(M_SIG).decode().encode())
check("enforce: good manifest applies", r == "applied", r)

# enforce + tampered manifest -> refused, nothing swapped
r = run_case("enforce", M_BYTES.replace(b"0.37.0", b"0.37.9"),
             __import__("base64").b64encode(M_SIG).decode().encode())
check("enforce: tampered manifest refused", r.startswith("refused"), r)

# enforce + missing assets (empty manifest body simulates 404 -> fetch fails)
srv.manifest_bytes = b""
srv.sig_bytes = b""
r = run_case("enforce", b"", b"")
check("enforce: missing manifest refused", r.startswith("refused"), r)

# log + bad manifest -> proceeds on legacy sha
r = run_case("log", M_BYTES.replace(b"0.37.0", b"0.37.9"),
             __import__("base64").b64encode(M_SIG).decode().encode())
check("log: bad manifest proceeds (legacy sha)", r == "applied", r)

# off -> manifest never fetched
Hits.clear()
srv.manifest_bytes = M_BYTES
srv.sig_bytes = __import__("base64").b64encode(M_SIG).decode().encode()
tree = tempfile.mkdtemp(prefix="shades_mf_off_")
updater.check_and_apply(tree, "0.36.25", make_cfg("off"),
                        exec_restart=False, api_base="http://%s" % HOST)
check("off: manifest assets never fetched",
      not any(h.endswith("manifest") or h.endswith("manifest.json")
              or h.endswith(".sig") for h in Hits), str(Hits))

# enforce + manifest names a different artifact -> refused
M_OTHER = manifest_bytes(bundle_name="sHades-server-9.9.9.tar.gz",
                         bundle_sha=BUNDLE_SHA, size=len(BUNDLE))
M_OTHER_SIG = sign(M_OTHER)
r = run_case("enforce", M_OTHER,
             __import__("base64").b64encode(M_OTHER_SIG).decode().encode())
check("enforce: artifact-name mismatch refused", r.startswith("refused"), r)

print()
print("release_manifest: %d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
