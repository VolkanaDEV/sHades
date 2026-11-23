
"""Voice loopback worker: boots one client, joins a voice room, streams
sine through the p2p engine, records per-packet arrivals, writes a
receipt json. Run by tests/test_voice_loopback.py (which runs the other
peer in-process)."""
import json
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RNS  # noqa: E402
from RNS.vendor import umsgpack as msgpack  # noqa: E402
import app_voice  # noqa: E402
from voice_codec import FRAME_SAMPLES, SAMPLE_RATE  # noqa: E402


def parse_args(argv):
    args = {"store": None, "name": "peer", "speak_s": 6.0, "room": None,
            "wait_s": 30.0, "receipt": None, "hub_ip": "127.0.0.1",
            "hub_port": 42431}
    argv = list(argv)
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if key in ("store", "name", "receipt", "room"):
                args[key] = argv[i + 1]
            elif key in ("speak_s", "wait_s"):
                args[key] = float(argv[i + 1])
            elif key == "hub_port":
                args[key] = int(argv[i + 1])
            i += 2
        else:
            i += 1
    return args


class BenchEngine(app_voice.VoiceEngine):
    """Records per-packet arrival gaps for the receipt."""

    def __init__(self, **kw):
        app_voice.VoiceEngine.__init__(self, **kw)
        self.arrivals = []
        self._last_arrival = None

    def packet_received(self, speaker_dest_hex, body):
        t = time.time()
        if self._last_arrival is not None:
            self.arrivals.append(t - self._last_arrival)
        self._last_arrival = t
        app_voice.VoiceEngine.packet_received(self, speaker_dest_hex, body)


def sine_source(name):
    """Phase-continuous 440 Hz sine, one 20 ms frame per call."""
    state = {"i": 0}

    def src():
        vals = []
        for _ in range(FRAME_SAMPLES):
            vals.append(int(9000 * math.sin(
                2 * math.pi * 440.0 * state["i"] / SAMPLE_RATE))
                if state["i"] < 10 ** 9 else 0)
            state["i"] += 1
        return struct.pack("<%dh" % len(vals), *vals)
    return src


def main():
    args = parse_args(sys.argv[1:])
    store = args["store"]
    cfg_dir = os.path.join(store, "rns-peer")

    RNS.Reticulum(configdir=cfg_dir)   # boots the instance

    # hub identity from guild_info.json (written at hub boot)
    hub_store = os.path.join(store, "guild") if os.path.isdir(
        os.path.join(store, "guild")) else store
    with open(os.path.join(hub_store, "guild_info.json")) as f:
        info = json.load(f)
    node_hash = bytes.fromhex(info["node_hash"])

    got_path = False
    for _ in range(160):
        if RNS.Transport.has_path(node_hash):
            got_path = True
            break
        RNS.Transport.request_path(node_hash)
        time.sleep(0.25)
    assert got_path, "no path to hub node"
    node_id = RNS.Identity.recall(node_hash)
    node_out = RNS.Destination(node_id, RNS.Destination.OUT,
                               RNS.Destination.SINGLE,
                               "nomadnetwork", "node")
    link = RNS.Link(node_out)
    for _ in range(100):
        if link.status == RNS.Link.ACTIVE:
            break
        time.sleep(0.1)
    assert link.status == RNS.Link.ACTIVE, "hub link inactive"

    # the hub derives the user hash from the identified identity
    identity = RNS.Identity()
    link.identify(identity)
    viewer = app_voice.RNSVoiceTransport.viewer_hash_of_identity(identity)

    def api(path, data):
        # legacy /api/* lanes speak msgpack dicts; /api/v1/* lanes speak
        # JSON bytes (matching GuildAPI)
        if path.startswith("/api/v1/"):
            payload = json.dumps(data).encode("utf-8")
        else:
            payload = data
        r = link.request(path, data=payload)
        for _ in range(250):
            if r.response is not None:
                break
            time.sleep(0.03)
        raw = r.response
        if not raw:
            return {"ok": 0, "error": "no response"}
        if path.startswith("/api/v1/"):
            return json.loads(raw.decode("utf-8"))
        return msgpack.unpackb(raw, raw=False)

    r = api("/api/register", {"username": args["name"]})
    assert r.get("ok"), r
    cid = args["room"]
    assert cid, "worker needs --room"

    engine = BenchEngine(viewer=viewer, hub=_HubFace(api, cid))
    transport = app_voice.RNSVoiceTransport(engine, identity)
    engine.attach_source(sine_source(args["name"]))
    joined = engine.join(cid)
    assert joined.get("ok"), joined
    # live-stream semantics: events before the subscribe never replay,
    # so the join response is the roster seed
    if joined.get("members"):
        engine.on_roster(cid, joined["members"])

    # roster rides the stream (poll loop, no push wiring in the worker)
    r = api("/api/v1/stream/subscribe", {"channel": cid})
    assert r.get("ok"), r
    if isinstance(r.get("members"), list):
        engine.on_roster(cid, r["members"])
    cursor = None
    peer_seen = False
    deadline = time.time() + args["wait_s"]
    speak_until = time.time() + args["speak_s"]
    while time.time() < deadline:
        ev = api("/api/v1/stream/poll", {"timeout_s": 2,
                                         **({"cursor": cursor} if cursor else {})})
        if ev.get("seq"):
            cursor = ev["seq"]
        elif ev.get("cursor"):
            cursor = ev["cursor"]
        for e in ev.get("events") or []:
            if e.get("kind") == "voice_state":
                members = e["data"]["members"]
                if any(m["viewer"] != viewer for m in members):
                    peer_seen = True
                engine.on_roster(cid, [
                    dict(m, viewer=str(m["viewer"]).lower())
                    for m in members])
        if time.time() > speak_until and peer_seen:
            break
    # let the tail drain
    time.sleep(0.5)
    if not engine.frames_in and engine.arrivals:
        # voice-path diagnostic: packets arrived but no frames decoded;
        # the demux early-return reason lives in the body/key state
        print("B-DIAG packets=%d frames_in=0 jitter_keys=%s arrivals=%s"
              % (len(engine.arrivals), list(engine._jitter)[:3],
                 [round(g, 3) for g in engine.arrivals[:4]]), flush=True)
    arrivals = [a for a in engine.arrivals if a > 0]
    gaps = list(arrivals)
    gaps.sort()
    # gaps are absolute packet timestamps until the second packet, so
    # drop the first; with fewer than 3 packets the gap stats mean
    # nothing and stay None
    if len(gaps) >= 3:
        gaps = gaps[1:]
        receipt = {
            "ok": 1,
            "viewer": viewer,
            "dest": transport.dest_hash_hex,
            "packets_received": len(engine.arrivals),
            "frames_in": engine.frames_in,
            "frames_out": engine.frames_out,
            "plc_frames": engine.plc_frames,
            "gap_avg_ms": round(1000.0 * sum(gaps) / len(gaps), 1),
            "gap_p95_ms": round(1000.0 * gaps[int(len(gaps) * 0.95)], 1),
            "gap_max_ms": round(1000.0 * gaps[-1], 1),
            "peer_seen": peer_seen,
        }
    else:
        receipt = {
            "ok": 0, "no_audio": 1,
            "packets_received": len(engine.arrivals),
            "frames_in": engine.frames_in,
            "frames_out": engine.frames_out,
            "plc_frames": engine.plc_frames,
            "peer_seen": peer_seen,
        }
    if args["receipt"]:
        with open(args["receipt"], "w") as f:
            json.dump(receipt, f)
    print("PEER-RECEIPT " + json.dumps(receipt))
    sys.exit(0)


class _HubFace(object):
    """Metadata only: join/leave to the hub over the api link."""

    def __init__(self, api, cid):
        self.api = api
        self.cid = cid

    def join(self, cid, dest_hex, muted):
        return self.api("/api/v1/voice/join",
                        {"channel": self.cid, "dest": dest_hex,
                         "muted": bool(muted)})

    def leave(self):
        return self.api("/api/v1/voice/leave", {})


if __name__ == "__main__":
    main()
