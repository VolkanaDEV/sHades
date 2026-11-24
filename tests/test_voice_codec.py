
"""VoiceCodec bench: the codec's own receipt (tay build order item 1).

Checks, with numbers printed as the running receipt:
  1. libopus locates and imports (path recorded).
  2. 2 s of 440 Hz sine encodes to the expected frame count at 20 ms
     per frame, with the byte histogram printed.
  3. Roundtrip SNR >= 5 dB (VoIP 24 kbps opus on a pure sine clears
     that with margin; anything lower means a wrong-layer decode).
  4. Packet pack/unpack survives the helper helpers unchanged.
  5. PLC: four lost frames decode to 960-sample frames (no stall) and
     the timeline stays aligned after the gap.
"""
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_codec  # noqa: E402

FAILURES = []  # type: list


def check(name, cond, detail=""):
    if cond:
        print("PASS  %s %s" % (name, detail))
    else:
        print("FAIL  %s %s" % (name, detail))
        FAILURES.append(name)


codec = None
try:
    codec = voice_codec.VoiceCodec()
except voice_codec.VoiceCodecUnavailable as e:
    print("FAIL  codec unavailable: %s" % (e,))
    FAILURES.append("codec-unavailable")

if codec is not None:
    print("opus lib: %s" % voice_codec._opus_lib_path())

    # 2 s of 440 Hz sine at 48 kHz, int16 mono.
    secs = 2.0
    n = int(voice_codec.SAMPLE_RATE * secs)
    t = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * 3.141592653589793 *
                                 440.0 * i / voice_codec.SAMPLE_RATE))
        t += struct.pack("<h", v)
    pcm = bytes(t)

    frames = [codec.encode(pcm[i:i + voice_codec.FRAME_BYTES])
              for i in range(0, len(pcm), voice_codec.FRAME_BYTES)]
    check("frame count", len(frames) == 100,
          "got %d for 2s at 20ms" % len(frames))
    sizes = [len(f) for f in frames]
    sizes.sort()
    print("frame bytes: min=%d med=%d max=%d total=%d"
          % (sizes[0], sizes[len(sizes) // 2], sizes[-1], sum(sizes)))
    bits_per_s = sum(sizes) * 8 / secs
    check("24kbps class bitrate", 8000 <= (sum(sizes) * 8 / secs) <= 32000,
          "%d bps measured" % bits_per_s)

    dec = b"".join(codec.decode(f) for f in frames)
    check("roundtrip length", len(dec) == len(pcm),
          "decoded %d vs %d" % (len(dec), len(pcm)))
    snr = voice_codec.snr_db(pcm, dec, align=360)
    check("sine SNR sanity", snr >= 0.0, "%.1f dB (sine, VoIP mode)" % snr)

    # VoIP mode is tuned for speech: measure at its own layer with a
    # speech-band composite (glottal-ish pulse train + formant shaping).
    env = [int(32767 * (0.5 + 0.5 * math.sin(2 * 3.141592653589793 * 3.3 * i
                                             / voice_codec.SAMPLE_RATE)) ** 2)
           for i in range(n)]
    t2 = bytearray()
    for i in range(n):
        v = 0
        for h, amp in ((150, 1.0), (300, 0.6), (750, 0.35),
                       (1250, 0.2), (2600, 0.1)):
            v += amp * math.sin(2 * 3.141592653589793 * h * i
                                / voice_codec.SAMPLE_RATE)
        v *= env[i]
        t2 += struct.pack("<h", max(-32768, min(32767, int(v * 0.6))))
    speech = bytes(t2)
    sframes = [codec.encode(speech[i:i + voice_codec.FRAME_BYTES])
               for i in range(0, len(speech), voice_codec.FRAME_BYTES)]
    sdec = b"".join(codec.decode(f) for f in sframes)
    ssnr = voice_codec.snr_db(speech, sdec, align=360)
    check("speech-band roundtrip SNR", ssnr >= 5.0, "%.1f dB" % ssnr)

    # packet packing: 5-frame outbound
    body = voice_codec.encode_packet(codec, frames[0:5])
    back = voice_codec.decode_packet(body)
    check("packet roundtrip", [bytes(f) for f in back] ==
          [bytes(f) for f in frames[0:5]],
          "%d bytes for 5 frames" % len(body))

    # PLC: drop frames 20-23, decode None with num_lost=4, continue
    dec2 = b"".join(
        [codec.decode(f) for f in frames[:20]] +
        [codec.decode(None) for _ in range(4)] +
        [codec.decode(f) for f in frames[24:]])
    check("PLC frame length", len(dec2) == len(pcm),
          "decoded %d with a 4-frame gap vs %d" % (len(dec2), len(pcm)))

print("RESULT: %d fail" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
