#!/usr/bin/env python3
"""Audio queue drainer tests -- imports audio_drain only, no Kivy.

Covers synthesis correctness at the sample level: sine/square/
triangle/saw waveforms, attack/decay envelopes, mixing of overlapping
notes (summing + normalization), the sequential-start default, the
30 s duration cap and param validation (NaN/inf, ranges, arity), the
WAV header, deterministic noise, and garbage input that never raises.
"""
import io
import math
import os
import struct
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_drain import drain_audio_commands  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def _samples(wav_bytes):
    """WAV bytes -> (params dict, list of int16 samples)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        params = {"rate": w.getframerate(), "frames": w.getnframes(),
                  "channels": w.getnchannels(),
                  "width": w.getsampwidth()}
        raw = w.readframes(w.getnframes())
    return params, list(struct.unpack("<%dh" % (len(raw) // 2), raw))


def _rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _peak(samples):
    return max((abs(s) for s in samples), default=0) / 32767.0


def main():
    # ---- 1. sine correctness: 440 Hz, 0.5 s, vol 1.0, no envelope ----
    res = drain_audio_commands(
        [["audio_note", 440.0, 0.5, "sine", 1.0, 0.0, 0.0, 0.0]])
    check("sine: wav bytes present", res["wav"] is not None)
    check("sine: duration 0.5", res["duration"] == 0.5, res["duration"])
    check("sine: count 1", res["count"] == 1, res["count"])
    check("sine: handled lists the command",
          res["handled"] == ["audio_note"], res["handled"])
    p, s = _samples(res["wav"])
    check("sine: WAV is mono 16-bit 22050 Hz",
          (p["rate"], p["channels"], p["width"]) == (22050, 1, 2), p)
    check("sine: 11025 frames for 0.5 s", p["frames"] == 11025, p)
    check("sine: sample[0] is 0 (phase 0)", s[0] == 0, s[0])
    # t = 1/(4*440) -> sample 13 (22050/1760 = 12.53); sin peak.
    check("sine: quarter-period sample near peak",
          abs(s[13] - 32767) / 32767.0 < 0.05, s[13])
    check("sine: peak is full scale", _peak(s) > 0.99, _peak(s))

    # ---- 2. square / triangle / saw ----
    res = drain_audio_commands(
        [["audio_note", 440.0, 0.5, "square", 1.0, 0.0, 0.0, 0.0]])
    p, s = _samples(res["wav"])
    check("square: hits both extremes",
          max(s) == 32767 and min(s) == -32767, (max(s), min(s)))
    check("square: flat top (samples 5..20 all max)",
          all(v == 32767 for v in s[5:20]), s[5:20])
    res = drain_audio_commands(
        [["audio_note", 440.0, 0.5, "triangle", 1.0, 0.0, 0.0, 0.0]])
    p, s = _samples(res["wav"])
    check("triangle: starts at -32767 (phase 0)", s[0] == -32767, s[0])
    check("triangle: peak reaches full scale", _peak(s) > 0.99, _peak(s))
    res = drain_audio_commands(
        [["audio_note", 440.0, 0.5, "saw", 1.0, 0.0, 0.0, 0.0]])
    p, s = _samples(res["wav"])
    check("saw: starts at -32767 (phase 0)", s[0] == -32767, s[0])
    check("saw: rises monotonically across the first period",
          s[13] < s[25] < s[38], (s[13], s[25], s[38]))
    check("saw: peak reaches full scale", _peak(s) > 0.99, _peak(s))

    # ---- 3. envelopes: attack ramp, decay ramp ----
    res = drain_audio_commands(
        [["audio_note", 440.0, 0.5, "sine", 1.0, 0.0, 0.1, 0.1]])
    p, s = _samples(res["wav"])
    # Sample 1115: t = 0.05057 s, phase 22.25 cycles -> sin = 1.0,
    # attack gain = 0.05057/0.1 = 0.506 -> ~16570.
    check("envelope: mid-attack sample at ~half gain",
          abs(s[1115] - 16570) / 32767.0 < 0.10, s[1115])
    mid = s[4000:6000]
    tail = s[-1103:]  # last 0.05 s: inside the 0.1 s decay ramp
    check("envelope: tail RMS well below middle RMS",
          _rms(tail) < 0.35 * _rms(mid),
          (_rms(tail), _rms(mid)))
    check("envelope: last sample is ~0 (decay reaches 0)",
          abs(s[-1]) < 1000, s[-1])

    # ---- 4. mixing: overlap sums, loud mix normalizes ----
    one = drain_audio_commands(
        [["audio_note", 440.0, 0.5, "sine", 0.25, 0.0, 0.0, 0.0]])
    _, s1 = _samples(one["wav"])
    check("mix: single vol-0.25 note peaks at 0.25",
          abs(_peak(s1) - 0.25) < 0.01, _peak(s1))
    pair = drain_audio_commands(
        [["audio_note", 440.0, 0.5, "sine", 0.25, 0.0, 0.0, 0.0],
         ["audio_note", 440.0, 0.5, "sine", 0.25, 0.0, 0.0, 0.0]])
    _, s2 = _samples(pair["wav"])
    check("mix: overlapping pair peaks at 0.5 (sums, no norm)",
          abs(_peak(s2) - 0.5) < 0.01, _peak(s2))
    loud = drain_audio_commands(
        [["audio_note", 440.0, 0.5, "sine", 1.0, 0.0, 0.0, 0.0],
         ["audio_note", 440.0, 0.5, "sine", 1.0, 0.0, 0.0, 0.0]])
    _, s3 = _samples(loud["wav"])
    check("mix: loud pair normalizes to 0.95 peak",
          abs(_peak(s3) - 0.95) < 0.01, _peak(s3))
    check("mix: normalization is noted",
          any("normalized" in n for n in loud["notes"]), loud["notes"])

    # ---- 5. sequential-start default: two 2-arg notes back to back ----
    res = drain_audio_commands([["audio_note", 440.0, 0.5],
                                ["audio_note", 440.0, 0.5]])
    check("seq: duration 1.0, count 2",
          (res["duration"], res["count"]) == (1.0, 2),
          (res["duration"], res["count"]))
    p, s = _samples(res["wav"])
    check("seq: 22050 frames", p["frames"] == 22050, p["frames"])
    half = len(s) // 2
    r1, r2 = _rms(s[:half]), _rms(s[half:])
    check("seq: both halves carry equal energy",
          abs(r1 - r2) / max(r1, 1.0) < 0.10, (r1, r2))

    # ---- 6. demo melody (demo-audio-page.mu): 4 square notes, 0.88 s ----
    res = drain_audio_commands([
        ["audio_note", 293.66, 0.22, "square", 0.5, 0.0, 0.01, 0.08],
        ["audio_note", 349.23, 0.22, "square", 0.5, 0.22, 0.01, 0.08],
        ["audio_note", 440.0, 0.22, "square", 0.5, 0.44, 0.01, 0.08],
        ["audio_note", 587.33, 0.22, "square", 0.5, 0.66, 0.01, 0.08]])
    check("demo: duration 0.88, count 4",
          (res["duration"], res["count"]) == (0.88, 4),
          (res["duration"], res["count"]))
    p, s = _samples(res["wav"])
    check("demo: 19404 frames", p["frames"] == 19404, p["frames"])

    # ---- 7. caps: 30 s total, late notes skipped ----
    res = drain_audio_commands([["audio_note", 440.0, 40.0]])
    check("cap: 40 s note truncated to 30.0",
          res["duration"] == 30.0, res["duration"])
    p, s = _samples(res["wav"])
    check("cap: 661500 frames for 30 s", p["frames"] == 661500, p["frames"])
    res = drain_audio_commands([["audio_note", 440.0, 0.5, "sine", 0.5,
                                 31.0, 0.0, 0.0]])
    check("cap: note starting at 31 s is skipped",
          res["wav"] is None and res["count"] == 0,
          (res["wav"] is not None, res["count"]))
    check("cap: skip is noted", len(res["notes"]) >= 1, res["notes"])

    # ---- 8. validation: bad params skipped with notes, never raised ----
    bad = [
        (["audio_note", 1.0, 0.5], "freq 1 Hz below range"),
        (["audio_note", 30000.0, 0.5], "freq 30 kHz above range"),
        (["audio_note", float("nan"), 0.5], "NaN freq"),
        (["audio_note", float("inf"), 0.5], "inf freq"),
        (["audio_note", 440.0, 0.0], "zero duration"),
        (["audio_note", 440.0, -1.0], "negative duration"),
        (["audio_note", 440.0, 0.5, "wobbly"], "unknown wave"),
        (["audio_note", 440.0, 0.5, "sine", 1.5], "vol above 1"),
        (["audio_note", 440.0, 0.5, "sine", 0.5, -1.0], "negative start"),
        (["audio_note", "440", 0.5], "string freq"),
        (["audio_note", 440.0], "too few args"),
        (["audio_note", 440.0, 0.5, "sine", 0.5, 0.0, 0.0, 0.0, 9.0],
         "too many args"),
        (["audio_noise", 0.5, 1.5], "noise vol above 1"),
        (["audio_noise"], "noise with no args"),
    ]
    for rec, label in bad:
        res = drain_audio_commands([rec])
        check("validate: %s skipped" % label,
              res["wav"] is None and res["count"] == 0
              and len(res["notes"]) >= 1,
              (res["count"], res["notes"]))
    # A bad note does not poison a good one queued after it.
    res = drain_audio_commands([["audio_note", 1.0, 0.5],
                                ["audio_note", 440.0, 0.5]])
    check("validate: good note after a bad one still drains",
          res["count"] == 1 and res["duration"] == 0.5,
          (res["count"], res["duration"]))

    # ---- 9. noise: deterministic, audible ----
    n1 = drain_audio_commands([["audio_noise", 0.5, 0.5, 0.0, 0.0, 0.0]])
    n2 = drain_audio_commands([["audio_noise", 0.5, 0.5, 0.0, 0.0, 0.0]])
    check("noise: two drains byte-identical", n1["wav"] == n2["wav"])
    _, sn = _samples(n1["wav"])
    check("noise: audible energy (RMS > 0.1)",
          _rms(sn) / 32767.0 > 0.1, _rms(sn) / 32767.0)
    check("noise: not a flat DC level",
          len(set(sn[:1000])) > 100, len(set(sn[:1000])))

    # ---- 10. garbage queues never raise ----
    res = drain_audio_commands(
        [None, "junk", ["audio_note"], ["nope_cmd", 1.0], 42])
    check("garbage: wav None, count 0",
          res["wav"] is None and res["count"] == 0,
          (res["wav"] is not None, res["count"]))
    check("garbage: bare audio_note marks handled",
          res["handled"] == ["audio_note"], res["handled"])
    check("garbage: unknown name lands in unhandled",
          res["unhandled"] == ["nope_cmd"], res["unhandled"])
    check("garbage: 5 notes recorded", len(res["notes"]) == 5,
          len(res["notes"]))
    for q, label in [(None, "None queue"), ([], "empty queue"),
                     ("not a list", "string queue"),
                     ([["audio_note", 440.0, 0.5]], "valid queue")]:
        try:
            res = drain_audio_commands(q)
            ok = True
        except Exception as e:  # noqa: BLE001 -- the point is no raise
            ok, res = False, e
        check("garbage: %s never raises" % label, ok, res)

    # ---- 11. unhandled ordering + mixed audio/gpu names ----
    res = drain_audio_commands([["gpu_open", 1.0],
                                ["audio_note", 440.0, 0.25],
                                ["nope_cmd", 1.0],
                                ["audio_note", 440.0, 0.25]])
    check("mixed: gpu_* record is unhandled here",
          "gpu_open" in res["unhandled"], res["unhandled"])
    check("mixed: unhandled order is first-appearance",
          res["unhandled"] == ["gpu_open", "nope_cmd"], res["unhandled"])
    check("mixed: audio note still drains",
          res["count"] == 2 and res["duration"] == 0.5,
          (res["count"], res["duration"]))
    check("mixed: handled lists only the known command",
          res["handled"] == ["audio_note"], res["handled"])

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
