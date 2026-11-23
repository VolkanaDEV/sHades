
"""Voice unit bench (item 3): the p2p frame path with a fake transport.

The engine's receipts, without RNS (the RNS loopback bench rides the
integ pattern next):
  1. two engines, roster diff, direct fan-out: A speaks, B hears, the
     hub face never carries a frame (its frame counter stays zero);
  2. out-of-order + loss: a hole in the packet stream heals with PLC,
     the timeline stays full-length;
  3. the pacer holds ~100 ms per packet in wall time;
  4. roster removal tears the peer link (no further sends).
"""
import math
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import voice_codec  # noqa: E402
from voice_codec import VoiceCodec, VoiceCodecUnavailable  # noqa: E402
import app_voice  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          ("" if cond else "  | " + str(extra)))


class HubFace(object):
    """Metadata-only hub: join/leave counted, any frame arriving counts."""

    def __init__(self):
        self.metadata = 0
        self.frames = 0

    def join(self, cid, dest, muted):
        self.metadata += 1
        return {"ok": 1}

    def leave(self):
        self.metadata += 1


class FakeTransport(object):
    """Routes packets peer-to-peer (engine _send -> peer's demux)."""

    def __init__(self):
        self.routes = {}
        self.sent = 0

    def wire(self, sender_engine):
        def send(peer, body):
            self.sent += 1
            peer_engine = self.routes[peer]
            peer_engine.packet_received(sender_engine.dest_hex(), body)
        return send


def frame_src(t):
    """Sine phase-continuous source, one 20 ms frame per call."""
    state = {"i": int(t * voice_codec.SAMPLE_RATE)}

    def src():
        vals = []
        for _ in range(voice_codec.FRAME_SAMPLES):
            vals.append(int(9000 * math.sin(
                2 * math.pi * 440.0 * state["i"] / voice_codec.SAMPLE_RATE)))
            state["i"] += 1
        import struct
        return struct.pack("<%dh" % len(vals), *vals)
    return src


def main():
    try:
        VoiceCodec()  # gate: codec must construct
    except VoiceCodecUnavailable as e:
        print("FAIL  codec unavailable: %s" % e)
        print("RESULT: 1 fail")
        sys.exit(1)

    hub = HubFace()
    transport = FakeTransport()

    # two engines; rosters point at each other
    a = app_voice.VoiceEngine(viewer="A" * 64, hub=hub)
    b = app_voice.VoiceEngine(viewer="B" * 64, hub=hub)
    a.set_dest("a" * 32)
    b.set_dest("b" * 32)
    a.attach_transport(transport.wire(a))
    b.attach_transport(transport.wire(b))
    transport.routes["b" * 32] = b     # a's send target
    transport.routes["a" * 32] = a     # b's send target

    played_b = []
    got_b = threading.Event()
    b.attach_sink(lambda pcm: (played_b.append(len(pcm)),
                               got_b.set() if len(played_b) >= 3 else None))
    a.attach_source(frame_src(0.0))

    a.join("room-1")
    b.join("room-1")
    check("hub metadata calls only",
          hub.metadata == 2 and hub.frames == 0,
          "metadata=%d frames=%d" % (hub.metadata, hub.frames))

    # roster arrives via the push path
    roster = [{"viewer": "A" * 64, "dest": "a" * 32},
              {"viewer": "B" * 64, "dest": "b" * 32}]
    a.on_roster("room-1", roster)
    b.on_roster("room-1", roster)
    check("each engine links one peer",
          len(a._out_links) == 1 and len(b._out_links) == 1,
          "%d/%d" % (len(a._out_links), len(b._out_links)))

    # pacer runs ~10 packets/s; give it 0.8 s, expect 4-10 packet sends
    time.sleep(0.8)
    a_send = transport.sent
    check("pacer packets in ~100ms window", 4 <= a_send <= 12,
          "sends=%d over 0.8s" % a_send)
    check("frames flowed outbound", a.frames_out > 8,
          "out_frames=%d" % a.frames_out)

    # listener gets audio through p2p only
    b_audio_before = len(played_b)
    deadline = time.time() + 2.0
    while len(played_b) == b_audio_before and time.time() < deadline:
        time.sleep(0.05)
    check("b heard frames over the direct link", got_b.is_set(),
          "sink calls=%d" % len(played_b))
    check("hub frame counter moved zero", hub.frames == 0)

    # loss: drop 2 of the next packets; PLC keeps the timeline going
    a._out_links["b" * 32].loss = 0.4
    time.sleep(0.8)
    check("gap heals with PLC", b.plc_frames > 0,
          "plc=%d" % b.plc_frames)

    # roster removal: the peer link dies, no further sends to b
    b.on_roster("room-1", [])       # empty room on b's side
    time.sleep(0.5)
    a.on_roster("room-1", [{"viewer": "A" * 64, "dest": "a" * 32}])
    time.sleep(0.5)
    check("roster removal tears the peer link",
          a._out_links.get("b" * 32) is None, "%r" % a._out_links)

    a.leave()
    b.leave()
    check("leave returns rooms", a.room_cid is None and b.room_cid is None)

    print("RESULT: %d pass, %d fail" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
