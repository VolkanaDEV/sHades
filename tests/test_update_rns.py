#!/usr/bin/env python3
"""P11d lane: the RNS software-channel carrier on fake transport.

The receipt-lane recipe: REAL code drives - the serve gate, the serve
routes (fingerprint + chunk slicing), and updater.check_and_apply's
rns branch with its sha gate and staging/swap - while the transport is
a fake RNS module whose Link.request() runs the REAL route functions
and msgpack-packs their dicts, exactly like the wire carries them.

Covers tay's priced contract (plan_admin.md arrow 6):
  - the serve gate: an unpinned, non-admin identity is refused; the
    pinned peer's identity is served the real fingerprint;
  - the pull: the carrier links to the pin, identifies, reads
    serve_info, pulls the chunks, and the assembled bytes are
    byte-identical to the served tar;
  - the sha gate: bytes-in-flight that disagree with the peer's sha256
    refuse before anything stages;
  - happy path: check_and_apply(update_channel=rns, rns_fetch=carrier)
    stages and swaps into a temp tree (restart held off) and marks the
    carrier;
  - the pin-less pull refuses before any request leaves; an unheard
    pin refuses as no-bundle;
  - the rns toggle with NO fetch hook stays pending and fetches
    nothing.
"""
import base64
import os
import sys
import tempfile
import traceback
import types

import msgpack

os.environ.setdefault("SHADES_NO_RESTART", "1")
os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_CONSOLE", "1")

REAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REAL_ROOT)

import RNS as _real_rns  # noqa: E402
import sHades  # noqa: E402
import updater  # noqa: E402

PIN_HEX = bytes(range(32)).hex()
PIN_HASH = bytes.fromhex(PIN_HEX)
OTHER_HASH = bytes(range(32, 64))
REAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REAL_ROOT)

FAKE_CFG = {"update_channel_peer": PIN_HEX}

PASS, FAIL = [], []


def check(name, cond, extra=None):
    (PASS if cond else FAIL).append(name)
    extra_s = "" if extra is None else ("   %s" % (repr(extra)[:200],))
    print(("PASS" if cond else "FAIL") + ("  " + name) + extra_s, flush=True)


class _FakeDestinationClass(object):
    OUT = 1
    SINGLE = 2

    def __init__(self, *a, **k):
        self.args = a
        self.aspects = a[3:] if len(a) > 3 else k


class _FakeIdentity(object):
    def __init__(self, hash_bytes, name):
        self.hash = hash_bytes
        self.name = name


class FakeReceipt(object):
    def __init__(self, payload):
        self.response = msgpack.packb(payload)


class Serving(object):
    """The fake serving peer behind the link: REAL routes, fake wire."""

    def __init__(self, version_stamp="0.36.26", wrong_sha=False,
                 mangle=False):
        served = sHades.api_v1_update_serve_info(
            None, {}, 1, "fake-link", _FakeIdentity(PIN_HASH, "peer"), 0)
        if not served.get("ok"):
            raise RuntimeError("serve_info refused: %r" % served)
        self.served = dict(served)
        self.served["version"] = version_stamp
        if wrong_sha:
            self.served["bundle_sha256"] = "0" * 64
        self.mangle = mangle
        self.chunks = 0
        self.identified = None
        self.payload = None


class _FakeLink(object):
    def __init__(self, serving):
        self._serving = serving
        self.identified = None
        self.status = 1

    def identify(self, identity):
        self.identified = identity

    def request(self, path, data=None):
        s = self._serving
        if path == "/api/v1/update/serve_info":
            return FakeReceipt(s.served)
        if path == "/api/v1/update/serve_chunk":
            res = sHades.api_v1_update_serve_chunk(
                None, data or {"offset": 0}, 1, "fake-link",
                _FakeIdentity(PIN_HASH, "peer"), 0)
            if not res.get("ok"):
                raise RuntimeError("serve_chunk refused: %r" % res)
            if s.mangle:
                chunk = bytearray(base64.b64decode(res["data_b64"]))
                chunk[0] ^= 0xFF
                res = dict(res)
                res["data_b64"] = base64.b64encode(
                    bytes(chunk)).decode("ascii")
            s.chunks += 1
            if s.payload is None:
                s.payload = bytearray()
            s.payload.extend(base64.b64decode(res["data_b64"]))
            return FakeReceipt(res)
        raise AssertionError("unexpected request %r" % path)


def make_fake_rns(version_stamp="0.36.26", wrong_sha=False):
    """A copy of the real RNS module with a fake Identity/Destination/
    Link trio wired to the fake serving peer. sHades keeps its real
    module reference; only _rns_pull's local `import RNS` resolves to
    the copy, so every other RNS touchpoint in this process stays
    real."""
    serving = Serving(version_stamp=version_stamp, wrong_sha=wrong_sha)
    link = _FakeLink(serving)
    nid = _FakeIdentity(PIN_HASH, "pinned-peer")

    class FakeIdentityModule(object):
        @staticmethod
        def recall(h):
            return nid if h == PIN_HASH else None

    fake = types.ModuleType("RNS")
    fake.__dict__.update(_real_rns.__dict__)
    fake.Identity = FakeIdentityModule
    fake.Destination = _FakeDestinationClass

    class FakeLinkClass(object):
        ACTIVE = 1

        def __new__(cls, nd, *a, **k):
            return link

    fake.Link = FakeLinkClass
    return fake, link, serving


def st_verdict():
    return updater.get_status()["verdict"]


def st_detail():
    return updater.get_status()["detail"]


def main():
    sHades._cfg_get = (lambda key, default=None:
                       FAKE_CFG.get(key, default))
    # the pull identifies with this server's destination; give it a fake
    # whose hash equals the pin so the REAL serving gate sees a trusted
    # peer
    sHades.node_destination = type(
        "Nd", (object,), {"identity": _FakeIdentity(PIN_HASH,
                                                    "serving-peer")})

    old_mod = sys.modules.get("RNS")
    try:
        # ------------------------------------------------------------ gates
        served = sHades.api_v1_update_serve_info(
            None, {}, 1, "fake-link", _FakeIdentity(PIN_HASH, "peer"), 0)
        check("gate: the pinned peer is served the real fingerprint",
              served.get("ok") == 1
              and served["bundle_name"]
              == "sHades-server-%s.tar.gz" % sHades.SERVER_VERSION
              and len(served["bundle_sha256"]) == 64
              and served["version"] == sHades.SERVER_VERSION,
              {k: served.get(k) for k in ("ok", "bundle_name")})

        stranger = sHades.api_v1_update_serve_info(
            None, {}, 1, "fake-link", _FakeIdentity(OTHER_HASH,
                                                    "stranger"), 0)
        check("gate: a stranger's identity is refused on serve_info",
              stranger.get("ok") == 0 and stranger.get("error") ==
              "peer not allowed", stranger)

        stranger_chunk = sHades.api_v1_update_serve_chunk(
            None, {"offset": 0}, 1, "fake-link",
            _FakeIdentity(OTHER_HASH, "stranger"), 0)
        check("gate: a stranger's identity is refused on serve_chunk",
              stranger_chunk.get("ok") == 0, stranger_chunk)

        # ------------------------------------------------------------ pull
        fake, link, serving = make_fake_rns()
        sys.modules["RNS"] = fake
        got = sHades._rns_pull()
        check("pull: the carrier returns the fetch dict",
              isinstance(got, dict)
              and got["version"] == "0.36.26"
              and got["bundle_name"]
              == "sHades-server-%s.tar.gz" % sHades.SERVER_VERSION
              and len(got["bundle_bytes"]) > 0
              and got["sha_source"] == "peer-only", got is not None)
        check("pull: the puller identified this server's identity on the "
              "link",
              link.identified is not None
              and link.identified.hash == PIN_HASH, link.identified)

        with open(sHades._SERVE_CACHE["path"], "rb") as f:
            served_bytes = f.read()
        check("pull: assembled bytes are byte-identical to the served tar",
              bytes(serving.payload or b"") == served_bytes,
              (len(serving.payload or b""), len(served_bytes)))

        # ------------------------------------------- happy path, end to end
        tmp_tree = tempfile.mkdtemp(prefix="shades-lane-tree-")
        for m in ("sHades.py", "updater.py", "update_run.py"):
            with open(os.path.join(tmp_tree, m), "w",
                      encoding="utf-8") as f:
                f.write("# placeholder %s\n" % m)
        res = updater.check_and_apply(
            tmp_tree, sHades.SERVER_VERSION,
            (lambda k, d=None: "rns" if k == "update_channel" else d),
            exec_restart=False, rns_fetch=sHades._rns_pull)
        check("happy path: check_and_apply(rns) stages and swaps, "
              "carrier=rns",
              res.get("swapped") is True and res.get("carrier") == "rns"
              and res.get("new_version") == "0.36.26",
              {k: res.get(k) for k in ("swapped", "carrier",
                                       "new_version")})

        swapped = open(os.path.join(tmp_tree, "sHades.py"), "rb").read()
        repo = open(os.path.join(REAL_ROOT, "sHades.py"), "rb").read()
        check("happy path: the swapped tree carries the served members "
              "and the backup kept the placeholders",
              swapped == repo and os.path.isfile(
                  os.path.join(tmp_tree, "update_backup", "sHades.py")),
              (len(swapped), len(repo)))

        # ----------------------------------------------- sha gate, in flight
        fake2, link2, serving2 = make_fake_rns(wrong_sha=True)
        sys.modules["RNS"] = fake2
        tmp_tree2 = tempfile.mkdtemp(prefix="shades-lane-tree2-")
        placeholders = {}
        for m in ("sHades.py", "updater.py", "update_run.py"):
            with open(os.path.join(tmp_tree2, m), "w",
                      encoding="utf-8") as f:
                f.write("# placeholder %s\n" % m)
            placeholders[m] = "# placeholder %s\n" % m
        res2 = updater.check_and_apply(
            tmp_tree2, sHades.SERVER_VERSION,
            (lambda k, d=None: "rns" if k == "update_channel" else d),
            exec_restart=False, rns_fetch=sHades._rns_pull)
        still = {m: open(os.path.join(tmp_tree2, m),
                         encoding="utf-8").read() for m in placeholders}
        check("sha gate: bytes disagreeing with the peer's sha256 refuse "
              "before staging",
              res2.get("swapped") is None
              and st_verdict() == "refused"
              and all(still[m] == placeholders[m] for m in still),
              (res2.get("reason"), st_verdict(), st_detail()))

        # ---------------------------------------------- P11e: the artifact pin
        real_sha = serving.served["bundle_sha256"]
        FAKE_CFG["update_channel_shapin"] = real_sha
        fake3, link3, serving3 = make_fake_rns()
        sys.modules["RNS"] = fake3
        got3 = sHades._rns_pull()
        check("P11e: with the artifact pin set, a matching peer serves and "
              "the authority is the pin",
              isinstance(got3, dict) and got3["sha256"] == real_sha
              and got3["sha_source"] == "pin", got3 is not None)

        fake4, link4, serving4 = make_fake_rns()
        sys.modules["RNS"] = fake4
        FAKE_CFG["update_channel_shapin"] = "ab" * 32
        got4 = sHades._rns_pull()
        check("P11e: a served fingerprint disagreeing with the artifact pin "
              "refuses before any bytes move",
              got4 is None and st_verdict() == "refused"
              and serving4.chunks == 0
              and "pinned artifact sha" in st_detail(), st_detail())

        fake5, link5, serving5 = make_fake_rns()
        serving5.mangle = True
        sys.modules["RNS"] = fake5
        FAKE_CFG["update_channel_shapin"] = real_sha
        got5 = sHades._rns_pull()
        check("P11e: bytes mangled in flight refuse against the pin, not "
              "the peer's claim",
              got5 is None and st_verdict() == "refused"
              and serving5.chunks > 0
              and "pinned artifact" in st_detail(), st_detail())

        # ------------------------------------------ P11f: the trusted hub tier
        FAKE_CFG["update_channel_shapin"] = ""
        FAKE_CFG["update_trusted_hub"] = PIN_HEX  # == the pin: tier 1
        fake6, link6, serving6 = make_fake_rns()
        sys.modules["RNS"] = fake6
        got6 = sHades._rns_pull()
        check("P11f: the pinned peer matching the trusted hub is the sole "
              "sha oracle (trusted-hub source)",
              isinstance(got6, dict)
              and got6["sha_source"] == "trusted-hub", got6 is not None)
        FAKE_CFG["update_trusted_hub"] = ""

        # ------------------------------------------------------ pin hygiene
        sys.modules["RNS"] = old_mod
        FAKE_CFG["update_channel_shapin"] = ""
        saved_pin = FAKE_CFG["update_channel_peer"]
        FAKE_CFG["update_channel_peer"] = ""
        got_no_pin = sHades._rns_pull()
        check("pull: pin-less refuses before any request",
              got_no_pin is None and st_verdict() == "refused"
              and "not a 64-hex pin" in st_detail(), st_detail())

        FAKE_CFG["update_channel_peer"] = "aa" * 32
        got_unknown = sHades._rns_pull()
        check("pull: an unheard pin refuses as no-bundle (recall None)",
              got_unknown is None and st_verdict() == "no-bundle",
              st_detail())
        FAKE_CFG["update_channel_peer"] = saved_pin

        # ---------------------------------------------------- no hook wired
        res3 = updater.check_and_apply(
            tmp_tree2, sHades.SERVER_VERSION,
            (lambda k, d=None: "rns" if k == "update_channel" else d),
            exec_restart=False)
        check("toggle: rns with no wired fetch hook stays pending",
              res3.get("skipped") is True
              and res3.get("reason") == "no-rns-fetch", res3)

        # ------------------------------------------ P11f: the boot guard
        guard_tree = tempfile.mkdtemp(prefix="shades-guard-")
        os.makedirs(os.path.join(guard_tree, "update_backup"))
        os.makedirs(os.path.join(guard_tree, "update_staging"))
        with open(os.path.join(guard_tree, "sHades.py"), "w",
                  encoding="utf-8") as f:
            f.write("half-swapped")
        with open(os.path.join(guard_tree, "update_backup", "sHades.py"),
                  "w", encoding="utf-8") as f:
            f.write("previous tree")
        with open(os.path.join(
                guard_tree, "update_staging", "pending_version"), "w",
                encoding="ascii") as f:
            f.write("sHades-server-0.36.26.tar.gz")
        restored = sHades._update_boot_guard(guard_tree)
        now = open(os.path.join(guard_tree, "sHades.py"),
                   encoding="utf-8").read()
        marker_gone = not os.path.isfile(os.path.join(
            guard_tree, "update_staging", "pending_version"))
        check("P11f: a direct boot under a pending marker restores the "
              "backup and clears the marker",
              restored == 1 and now == "previous tree" and marker_gone
              and st_verdict() == "rolled-back", (restored, now))
        check("P11f: a clean boot touches nothing",
              sHades._update_boot_guard(guard_tree) is None, None)
    except Exception:
        traceback.print_exc()
        FAIL.append("lane crashed")
    finally:
        sys.modules["RNS"] = old_mod
    print("RESULT: %d PASS, %d FAIL" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
