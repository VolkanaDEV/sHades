
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
            "hub_port": 42431, "join_dest": None, "trans_wait_s": 20.0,
            "fake_peer": None, "hold": False}
    argv = list(argv)
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            # the harness passes --wait-seconds; the parser's float list
            # only knows wait_s, so B ran on the 30s default every run
            if key == "wait_seconds":
                key = "wait_s"
            if key in ("store", "name", "receipt", "room", "join_dest",
                       "fake_peer") or key == "hold_until_deadline":
                if key == "hold_until_deadline":
                    args["hold"] = True
                else:
                    args[key] = argv[i + 1]
            elif key in ("speak_s", "wait_s", "trans_wait_s"):
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
    import faulthandler
    import threading as _th

    def _dump():
        faulthandler.dump_traceback(file=sys.stderr)
        t = _th.Timer(40.0, _dump)
        t.daemon = True
        t.start()

    t0 = _th.Timer(35.0, _dump)
    t0.daemon = True
    t0.start()
    args = parse_args(sys.argv[1:])
    store = args["store"]
    cfg_dir = os.path.join(store, "rns-peer")

    RNS.Reticulum(configdir=cfg_dir,
                  loglevel=int(os.environ.get("SHADES_PEER_RNS_LOGLEVEL", 3)))

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
    link.set_link_closed_callback(lambda lk: print(
        "B-LINK-CLOSED reason=%s status=%s at=%.1f" % (
            lk.teardown_reason, lk.status, time.time()), flush=True))

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

    # relay lane: hub "voice" push frames ride this api link raw; the
    # speaker's voice destination tags the frame (direct wins over the
    # relay inside the engine)
    def _on_voice_frame(plaintext, packet=None):
        try:
            frame = msgpack.unpackb(plaintext, raw=False)
        except Exception:
            return
        if isinstance(frame, dict) and frame.get("k") == "voice":
            engine.packet_relayed(frame["dest"], frame["body"])

    link.set_packet_callback(_on_voice_frame)

    engine = BenchEngine(viewer=viewer, hub=_HubFace(
        api, cid, link=link, join_dest=args["join_dest"]))
    transport = app_voice.RNSVoiceTransport(engine, identity,
                                            wait_secs=args["trans_wait_s"])
    engine.attach_source(sine_source(args["name"]))
    joined = engine.join(cid)
    if not joined.get("ok"):
        # bench rooms live one boot: create and retry once
        created = api("/api/v1/channels/create",
                      {"kind": "voice", "name": "Bisect-%s" % args["name"]})
        if created.get("ok"):
            cid = created["channel"]["id"]
            engine.hub.cid = cid
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
    if args["fake_peer"]:
        # the dying scenario's shape: one unreachable roster peer whose
        # dial probe floods the hub with path requests for 3s/attempt
        engine.on_roster(cid, [{"viewer": "fakepeer",
                                "dest": args["fake_peer"],
                                "muted": False}])
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
        if time.time() > speak_until and peer_seen and not args["hold"]:
            break
    # let the tail drain
    time.sleep(0.5)
    if not engine.frames_in and engine.arrivals:
        # voice-path diagnostic: packets arrived but no frames decoded;
        # the demux early-return reason lives in the body/key state
        print("B-DIAG packets=%d frames_in=0 jitter_keys=%s arrivals=%s"
              % (len(engine.arrivals), list(engine._jitter)[:3],
                 [round(g, 3) for g in engine.arrivals[:4]]), flush=True)
    arrivals = list(engine.arrivals)
    # every entry the wrapper records is an inter-arrival delta (the
    # first callback appends nothing), so the stats run on the raw
    # order: no sorting and no first-entry drop
    if len(arrivals) >= 2:
        receipt = {
            "ok": 1,
            "viewer": viewer,
            "dest": transport.dest_hash_hex,
            "packets_received": len(engine.arrivals),
            "frames_in": engine.frames_in,
            "frames_out": engine.frames_out,
            "plc_frames": engine.plc_frames,
            "gap_avg_ms": round(1000.0 * sum(arrivals) / len(arrivals), 1),
            "gap_p95_ms": round(1000.0 *
                                sorted(arrivals)[int(len(arrivals) * 0.95)], 1),
            "gap_max_ms": round(1000.0 * max(arrivals), 1),
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
    """Metadata only: join/leave to the hub over the api link; the
    relay lane publishes as a raw voice-pub packet on the same link
    (no request table, no response wait) with the request route as
    fallback when no link is in hand. join_dest lets a bench scenario
    replace the roster's dial target for this peer."""

    def __init__(self, api, cid, link=None, join_dest=None):
        self.api = api
        self.cid = cid
        self._link = link
        self._join_dest = join_dest

    def join(self, cid, dest_hex, muted):
        return self.api("/api/v1/voice/join",
                        {"channel": self.cid,
                         "dest": self._join_dest or dest_hex,
                         "muted": bool(muted)})

    def leave(self):
        return self.api("/api/v1/voice/leave", {})

    def voice_publish(self, cid, body):
        if self._link is not None:
            frame = msgpack.packb({"k": "voice-pub", "body": body})
            try:
                RNS.Packet(self._link, frame).send()
            except Exception as e:
                if app_voice.VOICE_DEBUG:
                    print("publish send failed: %s" % e, flush=True)
            return {"ok": 1}
        import base64
        return self.api("/api/v1/voice/publish",
                        {"channel": self.cid,
                         "body": base64.b64encode(body).decode("ascii")})


if __name__ == "__main__":
    main()
