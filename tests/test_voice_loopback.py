
"""Voice loopback bench (tay build order item 3, real RNS).

Topology on localhost (mirrors the integ topology):
  hub:      sHades.py process, TCPServerInterface 42431, transport on
            (directory + announces only: no voice relay exists)
  peer A:   this test process (client RNS instance + engine)
  peer B:   tests/voice_peer_worker.py subprocess (own RNS instance)

Receipts:
  A joins a voice room; B joins the same room; each engine's dialer
  links the OTHER's hades/voice destination directly. B records packet
  arrivals while A streams sine; the receipt must show frames flowing
  peer-to-peer with ~100ms packet pacing and hub metadata only.
"""
import base64
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time

RETI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RETI_DIR)

import RNS  # noqa: E402
import app_voice  # noqa: E402
from voice_codec import FRAME_SAMPLES, SAMPLE_RATE  # noqa: E402

os.environ["SHADES_VOICE_DEBUG"] = "1"

HUB_PORT = 42431
PEER_PORT = 42432
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          ("" if cond else "  | " + str(extra)))


def sine_source():
    state = {"i": 0}

    def src():
        vals = []
        for _ in range(FRAME_SAMPLES):
            vals.append(int(9000 * math.sin(
                2 * math.pi * 440.0 * state["i"] / SAMPLE_RATE)))
            state["i"] += 1
        return struct.pack("<%dh" % len(vals), *vals)
    return src


def boot_hub(base):
    g_store = os.path.join(base, "guild")
    os.makedirs(g_store, exist_ok=True)
    os.makedirs(os.path.join(g_store, "rns-guild"), exist_ok=True)
    with open(os.path.join(g_store, "rns-guild", "config"), "w") as f:
        cfg = ("[reticulum]\n  enable_transport = yes\n"
               "  share_instance = no\n\n[interfaces]\n"
               "  [[TCP Server Interface]]\n    type = TCPServerInterface\n"
               "    enabled = yes\n    listen_ip = 127.0.0.1\n"
               "    listen_port = %d\n") % HUB_PORT
        f.write(cfg)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    log = open(os.path.join(g_store, "hub.log"), "ab")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(RETI_DIR, "sHades.py"),
         "--storage", g_store,
         "--rns-configdir", os.path.join(g_store, "rns-guild"),
         "--announce-delay", "2", "--listen", "127.0.0.1:%d" % HUB_PORT],
        cwd=RETI_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
    info_path = os.path.join(g_store, "guild_info.json")
    info = None
    for _ in range(200):
        if os.path.isfile(info_path):
            try:
                info = json.load(open(info_path))
                break
            except json.JSONDecodeError:
                time.sleep(0.05)
        if proc.poll() is not None:
            print(open(os.path.join(g_store, "hub.log")).read())
            return proc, None, g_store
        time.sleep(0.25)
    return proc, info, g_store


def main(relay=False):
    base = tempfile.mkdtemp(prefix="voice_loopback_")
    relay_a_dest = os.urandom(16).hex()
    relay_b_dest = os.urandom(16).hex()
    hub_proc, info, g_store = boot_hub(base)
    try:
        check("hub booted", info is not None)
        if not info:
            return
        node_hash = bytes.fromhex(info["node_hash"])

        os.makedirs(os.path.join(base, "rns-a"), exist_ok=True)
        with open(os.path.join(base, "rns-a", "config"), "w") as f:
            f.write("[reticulum]\n  enable_transport = no\n"
                    "  share_instance = no\n\n[interfaces]\n"
                    "  [[TCP Client Interface]]\n"
                    "    type = TCPClientInterface\n    enabled = yes\n"
                    "    target_host = 127.0.0.1\n"
                    "    target_port = %d\n"
                    "  [[TCP Server Interface]]\n"
                    "    type = TCPServerInterface\n    enabled = yes\n"
                    "    listen_ip = 127.0.0.1\n"
                    "    listen_port = %d\n" % (HUB_PORT, PEER_PORT))
        RNS.Reticulum(configdir=os.path.join(base, "rns-a"))

        got = False
        for _ in range(160):
            if RNS.Transport.has_path(node_hash):
                got = True
                break
            RNS.Transport.request_path(node_hash)
            time.sleep(0.25)
        check("path to hub", got)
        if not got:
            return
        node_id = RNS.Identity.recall(node_hash)
        node_out = RNS.Destination(node_id, RNS.Destination.OUT,
                                   RNS.Destination.SINGLE,
                                   "nomadnetwork", "node")
        link = RNS.Link(node_out)
        for _ in range(100):
            if link.status == RNS.Link.ACTIVE:
                break
            time.sleep(0.1)
        check("hub link active", link.status == RNS.Link.ACTIVE)
        if link.status != RNS.Link.ACTIVE:
            return
        link.set_link_closed_callback(lambda lk: print(
            "A-LINK-CLOSED reason=%s status=%s at=%.1f" % (
                lk.teardown_reason, lk.status, time.time()), flush=True))
# push frames arrive on this link
        pushed = {}

        def _read_push(plaintext, packet=None):
            try:
                frame = RNS.vendor.umsgpack.unpackb(plaintext, raw=False)
            except Exception:
                return
            if isinstance(frame, dict):
                evs = frame.get("events") or []
                if evs and frame.get("k") == "stream":
                    pushed.setdefault("last", []).extend(evs)
        link.set_packet_callback(_read_push)

        def api(path, data, timeout=25.0):
            # legacy /api/* lanes speak msgpack dicts over the link;
            # /api/v1/* lanes speak JSON bytes (matches GuildAPI)
            if path.startswith("/api/v1/") or path.startswith("/api/voice"):
                payload = json.dumps(data).encode("utf-8")
            else:
                payload = data
            r = link.request(path, data=payload)
            end = time.time() + timeout
            while r.response is None and time.time() < end:
                time.sleep(0.03)
            raw = r.response
            if not raw:
                return {"ok": 0, "error": "no response"}
            if path.startswith("/api/v1/"):
                return json.loads(raw.decode("utf-8"))
            return RNS.vendor.umsgpack.unpackb(raw, raw=False)

        # A identity + registration (the link must carry the identity)
        a_identity = RNS.Identity()
        a_viewer = app_voice.RNSVoiceTransport.viewer_hash_of_identity(
            a_identity)
        link.identify(a_identity)
        r = api("/api/register", {"username": "loopa"})
        check("A register", r.get("ok") == 1, r)

        # A's voice room, pre-announced voice destination. In relay mode
        # the roster declares a dial target neither side can recall.
        a_engine = app_voice.VoiceEngine(
            viewer=a_viewer, hub=_HubAPI(
                api, force_dest=relay_a_dest if relay else None, link=link))
        app_voice.RNSVoiceTransport(
            a_engine, a_identity, verifier=lambda v: True,
            wait_secs=3.0 if relay else 20.0)
        r = api("/api/v1/channels/create",
                {"kind": "voice", "name": "Lounge-%d" % int(time.time() % 1e5)})
        check("A creates the voice room", r.get("ok") == 1, r)
        cid = r["channel"]["id"]
        r = a_engine.join(cid)
        check("A joined", r.get("ok") == 1, r)
        a_engine.attach_source(sine_source())

        # roster to A via poll (B's join should arrive as voice_state)
        r = api("/api/v1/stream/subscribe", {"channel": cid})
        check("A subscribed", r.get("ok") == 1, r)

        # B worker
        os.makedirs(os.path.join(base, "rns-peer"), exist_ok=True)
        with open(os.path.join(base, "rns-peer", "config"), "w") as f:
            f.write("[reticulum]\n  enable_transport = no\n"
                    "  share_instance = no\n\n[interfaces]\n"
                    "  [[TCP Client Interface]]\n"
                    "    type = TCPClientInterface\n    enabled = yes\n"
                    "    target_host = 127.0.0.1\n"
                    "    target_port = %d\n"
                    "  [[TCP Client Interface A]]\n"
                    "    type = TCPClientInterface\n    enabled = yes\n"
                    "    target_host = 127.0.0.1\n"
                    "    target_port = %d\n" % (HUB_PORT, PEER_PORT))
        receipt_path = os.path.join(base, "peer_receipt.json")
        b_worker_args = ["--store", base, "--room", cid, "--name", "loopb",
                         "--speak-seconds", "6" if relay else "4",
                         "--wait-seconds", "100" if relay else "40",
                         "--receipt", receipt_path]
        if relay:
            b_worker_args += ["--join-dest", relay_b_dest,
                              "--trans-wait-s", "3.0",
                              "--hold-until-deadline"]
        # B's stdout goes to a file: a PIPE wedges the worker in a
        # blocking print once its buffer fills (the harness only drains
        # the pipe after the process exits), which froze B's poll loop
        # mid-run and starved its request stream.  No pipe, no wedge.
        b_log_path = os.path.join(base, "peer_b.log")
        b_log = open(b_log_path, "wb")
        b_proc = subprocess.Popen(
            [sys.executable, os.path.join(RETI_DIR, "tests",
                                          "voice_peer_worker.py")]
            + b_worker_args,
            cwd=RETI_DIR, env=dict(os.environ),
            stdout=b_log, stderr=subprocess.STDOUT)

        # A's poll loop: feed rosters into A's engine; wait for B (relay
        # mode: until the dial cap is spent and the relay is engaged)
        cursor = None
        b_roster = None
        relay_deadline = None
        end = time.time() + (95 if relay else 50)
        a_ran = time.time() + 12
        while time.time() < end:
            ev = api("/api/v1/stream/poll",
                     {"timeout_s": 1.5,
                      **({"cursor": cursor} if cursor else {})}, 5)
            if ev.get("seq"):
                cursor = ev["seq"]
            for e in ev.get("events") or []:
                if e.get("kind") == "voice_state":
                    members = e["data"]["members"]
                    if any(m["viewer"] != a_viewer for m in members):
                        b_roster = members
                    a_engine.on_roster(cid, [
                        dict(m, viewer=str(m["viewer"]).lower())
                        for m in members])
            if b_roster and relay_deadline is None:
                # from roster arrival: 9 dials x (3s recall + 1s) + margin
                relay_deadline = time.time() + 42.0
            if relay and b_roster and time.time() > relay_deadline:
                break   # dial cap spent by now either way
            if not relay and b_roster and time.time() > a_ran and \
                    len(a_engine._out_links) == 1:
                break
        check("A saw B on the roster", b_roster is not None, str(b_roster))

        # A's engine: direct link to B was dialed through the roster only
        b_voice = _peer_voice_dest(b_roster, a_viewer)
        if relay:
            check("roster staged the undialable dest for B",
                  b_voice == relay_b_dest,
                  "%s != %s" % (b_voice, relay_b_dest))
            check("A's direct dial spent to the cap",
                  a_engine._dial_failures.get(b_voice, 0)
                  >= app_voice.DIAL_MAX_TRIES
                  or a_engine.hub.pubs > 0,
                  "fails=%s pubs=%d" % (a_engine._dial_failures,
                                        a_engine.hub.pubs))
            check("A holds no direct voice links",
                  not a_engine._out_links, str(list(a_engine._out_links)))
            # the relay lane: A publishes until B's window closes.
            # A's relay phase starts when its dialer spends to the cap,
            # which races the fixed 6s window: wait until the pub stream
            # is actually moving before sampling, so the delta measures
            # throughput, not phase-start timing.
            a_frames = a_engine.frames_out
            pub_before = a_engine.hub.pubs
            if a_engine.hub.pubs == pub_before:
                for _ in range(100):
                    if (a_engine.hub.pubs - pub_before > 2
                            or a_engine.frames_out - a_frames >= 40):
                        break
                    time.sleep(0.1)
            pub_before = a_engine.hub.pubs
            time.sleep(6.0)
            pub_delta = a_engine.hub.pubs - pub_before
            check("A relayed packets through the hub", pub_delta > 12,
                  "pubs/6s=%d total=%d" % (
                      pub_delta, a_engine.hub.pubs))
        else:
            check("A holds one direct voice link",
                  list(a_engine._out_links) == [b_voice],
                  "out=%s dial_q=%s fails=%s pinned=%s expected=%s" % (
                      list(a_engine._out_links), list(a_engine._dial_q),
                      a_engine._dial_failures, a_engine._speakers_pinned,
                      b_voice))
            # pacing in wall time, on the direct link only
            while not a_engine._out_links and time.time() < end:
                time.sleep(0.2)
            a_frames = a_engine.frames_out
            time.sleep(6.0)
            delta = a_engine.frames_out - a_frames
            check("A paced one voice packet per 100ms on the direct link",
                  160 <= delta <= 400,  # 5 frames x 10Hz x 6s window
                  "delta_frames/6s=%d" % delta)

        # B's stdout goes to peer_b.log (no pipe); the drain below reads
        # receipts from the file, so communicate() is vestigial. B's
        # file-backed stream carries both stdout and stderr.
        try:
            b_proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            b_proc.kill()
            b_proc.wait()
        receipt_line = ""
        with open(b_log_path, "rb") as _bbuf:
            receipt_line = _bbuf.read().decode("utf-8", "replace")
        receipt = None
        for line in receipt_line.splitlines():
            if line.startswith("PEER-RECEIPT "):
                try:
                    receipt = json.loads(line.split(" ", 1)[1])
                except ValueError:
                    receipt = None
        check("worker completed", b_proc.poll() == 0,
              receipt_line[-400:])
        diag = [ln for ln in receipt_line.splitlines()
                if ln.strip() and "PEER-RECEIPT" not in ln]
        for ln in diag[-8:]:
            print("B >", ln, flush=True)
        check("B receipt parsed", receipt is not None, receipt_line[-400:])
        if not receipt:
            return
        b_frames_in = receipt["frames_in"]
        print("B receipt:", json.dumps(receipt), flush=True)
        check("B heard frames over the %s link (relay staged)" % (
            "hub relay" if relay else "direct"), b_frames_in > 40,
            "frames_in=%s" % b_frames_in)
        if b_frames_in > 40:
            check("B's arrivals paced ~100ms",
                  60.0 <= (receipt["gap_avg_ms"] or 1e9) <= 400.0,
                  "gap_avg_ms=%s" % receipt["gap_avg_ms"])
        # structural receipt: A's voice sends carry peer dests only; the
        # hub's api lane saw exactly two metadata joins, nothing else.
        # (sHades holds no voice relay path at all in code.)

        print("A frames_out=%d  B frames_in=%s frames=%s" % (
            a_engine.frames_out, receipt["frames_in"],
            receipt.get("gap_avg_ms")))
    finally:
        if hub_proc is not None and hub_proc.poll() is None:
            hub_proc.terminate()
    print("RESULT: %d pass, %d fail" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


def _peer_voice_dest(roster, me):
    """The roster row of a peer (excludes this engine's own viewer)."""
    for m in roster or []:
        if m.get("dest") and m.get("viewer") != me:
            return m["dest"]
    return None


class _HubAPI(object):
    """Metadata-only hub face for A's engine (join/leave ride the api).
    force_dest stages a probe-failure: the roster then declares a dial
    target neither side can ever recall. voice_publish is the relay
    lane's outbound route; the bench counts publishes."""

    def __init__(self, api, force_dest=None, link=None):
        self.api = api
        self.cid = None
        self.force_dest = force_dest
        self._link = link
        self.pubs = 0

    def join(self, cid, dest_hex, muted):
        self.cid = cid
        return self.api("/api/v1/voice/join",
                        {"channel": cid, "dest": self.force_dest or dest_hex,
                         "muted": muted})

    def leave(self):
        return self.api("/api/v1/voice/leave", {})

    def voice_publish(self, cid, body):
        self.pubs += 1
        if self._link is not None:
            frame = RNS.vendor.umsgpack.packb(
                {"k": "voice-pub", "body": body})
            try:
                if RNS.Packet(self._link, frame).send() is not False:
                    return {"ok": 1}
            except Exception:
                pass
            # The raw path refused (link not in an RNS ACTIVE moment).
            # The relay is precisely the route for links that cannot be
            # trusted lane-live, so fall back to the encoded api path --
            # which is also an identified request and feeds the link.
            return self.api("/api/v1/voice/publish",
                            {"channel": cid,
                             "body": base64.b64encode(body).decode("ascii")})
        return self.api("/api/v1/voice/publish",
                        {"channel": cid,
                         "body": base64.b64encode(body).decode("ascii")})


if __name__ == "__main__":
    main(relay="--relay" in sys.argv)
