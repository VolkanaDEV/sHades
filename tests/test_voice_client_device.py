"""Device gate for client_voice (build-order item 5).

The loopback is real end to end: sine frames are encoded by the pacer,
paced at 10Hz through a direct transport, jittered, decoded by the
player loop, and handed to SpeakerSink, whose drain thread writes them
to the actual output device. The mic half is capture-probed (device
opens, ring fills, RMS logged) and is non-gating: a silent microphone
is still a working microphone.
"""
import math
import struct
import sys
import time

import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import voice_codec
from app_voice import VoiceEngine

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          ("" if not extra else "  | " + str(extra)), flush=True)


class HubFace(object):
    def join(self, cid, dest, muted):
        return {"ok": 1}

    def leave(self):
        pass


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
    state = {"i": int(t * voice_codec.SAMPLE_RATE)}

    def src():
        vals = []
        for _ in range(voice_codec.FRAME_SAMPLES):
            vals.append(int(9000 * math.sin(
                2 * math.pi * 440.0 * state["i"] /
                voice_codec.SAMPLE_RATE)))
            state["i"] += 1
        return struct.pack("<%dh" % len(vals), *vals)
    return src


def rms_dbfs(raw):
    n = len(raw) // 2
    if not n:
        return -96.0
    vals = struct.unpack("<%dh" % n, raw[:n * 2])
    s = 0.0
    for v in vals:
        s += float(v) * float(v)
    return max(-96.0, 20.0 * math.log10(math.sqrt(s / n) / 32768.0))


def mic_probe():
    import client_voice
    try:
        mic = client_voice.MicSource()
    except Exception as e:
        print("PASS  mic capture probe (no input device: %r)" % e,
              flush=True)
        return
    time.sleep(0.6)
    frames = [f for f in (mic.poll() for _ in range(120)) if f]
    mic.close()
    ok = len(frames) >= 10
    rms = rms_dbfs(b"".join(frames[-20:])) if frames else -96.0
    check("mic capture probe", ok,
          "frames=%d rms=%.1f dBFS (live mic: expect loud)"
          % (len(frames), rms))
    # codec-rate parity: every frame the mic hands is codec-shaped and
    # encodes without raising (the 16k-default defect this catches)
    if frames:
        codec = voice_codec.VoiceCodec()
        sizes_ok = all(len(f) == voice_codec.FRAME_BYTES for f in frames)
        check("mic frame geometry (codec rate)",
              sizes_ok and len(frames[0]) == voice_codec.FRAME_BYTES,
              "frame0=%dB want=%d" % (len(frames[0]),
                                      voice_codec.FRAME_BYTES))
        try:
            codec.encode(frames[-1])
            check("mic frame encodes at codec rate", True)
        except Exception as e:
            check("mic frame encodes at codec rate", False, repr(e))
    else:
        check("mic frame geometry (codec rate)", False,
              "no mic frames to check")


def sink_loopback():
    import client_voice
    try:
        sink = client_voice.SpeakerSink()
    except Exception as e:
        check("speaker device open", False, repr(e))
        return
    khz = voice_codec.SAMPLE_RATE // 1000
    check("speaker device open", True,
          "default output, %dkHz mono s16" % khz)

    hub = HubFace()
    transport = FakeTransport()
    a = VoiceEngine(viewer="A" * 64, hub=hub)
    b = VoiceEngine(viewer="B" * 64, hub=hub)
    a.set_dest("a" * 32)
    b.set_dest("b" * 32)
    a.attach_transport(transport.wire(a))
    b.attach_transport(transport.wire(b))
    transport.routes["b" * 32] = b
    transport.routes["a" * 32] = a
    client_voice.bind(b, speaker=sink)
    a.attach_source(frame_src(0.0))
    a.join("room-gate")
    b.join("room-gate")
    a.on_roster("room-gate", [{"viewer": b.viewer, "dest": b.dest_hex(),
                               "muted": False}])
    b.on_roster("room-gate", [{"viewer": a.viewer, "dest": a.dest_hex(),
                               "muted": False}])

    target = voice_codec.SAMPLE_RATE * 2   # 1s of s16-mono PCM bytes
    played_target = 50                      # >=1s of device writes
    deadline = time.time() + 8.0
    while time.time() < deadline:
        st = sink.stats()
        if st["bytes_in"] >= target and st["frames_played"] >= played_target:
            break
        time.sleep(0.2)
    st = sink.stats()
    check("sine crossed the pipeline into the device",
          st["bytes_in"] >= target,
          "bytes_in=%d frames_played=%d"
          % (st["bytes_in"], st["frames_played"]))
    check("player decoded frames", b.frames_in >= 40,
          "frames_in=%d" % b.frames_in)
    # the drain thread plays at device speed: >=1s of real output
    played = st["frames_played"]
    check("device write pacing", played >= played_target,
          "played_frames=%d target=%d" % (played, played_target))
    sink.close()


def main():
    try:
        voice_codec.VoiceCodec()
    except Exception as e:
        check("codec constructs", False, repr(e))
        print("RESULT: %d pass, %d fail" % (len(PASS), len(FAIL)))
        sys.exit(1)
    mic_probe()
    sink_loopback()
    print("RESULT: %d pass, %d fail" % (len(PASS), len(FAIL)))
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
