"""Audio queue drainer for sHades micron pages.

The interpreter (``interp.sanguis``, ``script_run``) never executes an
``audio_*`` call: it records it. Every call appends a command record
``[name, args...]`` (raw JSON values, unwrapped) to the audio queue.
This module is the host side of that contract: it drains the queue
after ``script_run`` returns and synthesizes 16-bit PCM WAV bytes
from it. It never plays anything -- synthesis only; playback is the
caller's job.

Sample rate: 22050 Hz. That is half the cost of 44100 Hz (half the
samples to synthesize and store per second) and plenty for chiptune
square / saw / triangle waves, whose harmonics live well below the
11 kHz Nyquist limit here; the WAV header carries the rate, so any
player handles the file. ``MAX_DURATION`` caps one drain at 30 s so
a hostile script cannot make the host synthesize unbounded audio.

Host-side contract -- one row per queued command name
-----------------------------------------------------
``audio_note(freq, dur, wave, vol, start, attack, decay)``
    Synthesize one pitched note. 2-7 args; defaults: ``wave`` "sine",
    ``vol`` 0.5, ``start`` the end time of the most recently queued
    note (sequential-melody default; 0.0 when no note queued yet),
    ``attack`` 0.01, ``decay`` 0.05. Waves: "sine", "square",
    "triangle", "saw". ``freq`` 20-20000 Hz, ``dur`` > 0, ``vol``
    0-1, ``start`` >= 0.
``audio_noise(dur, vol, start, attack, decay)``
    Synthesize white noise for ``dur`` seconds. 1-5 args; defaults
    ``vol`` 0.5, ``start`` the same sequential default, ``attack``
    0.01, ``decay`` 0.05. Noise is DETERMINISTIC: every drain seeds
    its PRNG identically (``random.Random(NOISE_SEED)``), so the
    same queue always produces byte-identical WAV bytes and tests
    are stable. Noise consumes the PRNG in queue order.

Validation: a hostile script must never blow up the mixer. Invalid
notes are SKIPPED with a note string, never raised: non-finite
values (NaN/inf, checked with ``math.isfinite`` on every numeric
arg), wrong JSON types (e.g. ``freq`` a string, ``wave`` a number),
arity outside the ranges above, ``freq`` outside 20-20000 Hz,
``dur`` <= 0, ``vol`` outside 0-1, ``start`` < 0, unknown wave
name. Clamped, not rejected: ``attack``/``decay`` into ``[0, dur]``;
if ``attack + decay > dur``, ``decay`` shrinks to ``dur - attack``.
Duration cap: a note starting at or after ``max_duration`` is
skipped with a note; a note extending past ``max_duration`` is
truncated to end at ``max_duration`` (kept, clipped).

Synthesis: per note, ``phase = freq * t_local`` (``t_local``
measured from the note's start); sine is ``sin(2*pi*phase)``;
square is 1.0 when ``sin >= 0`` else -1.0; triangle is
``2*abs(2*(phase - floor(phase + 0.5))) - 1``; saw is
``2*(phase - floor(phase)) - 1``. Envelope: linear attack ramp
0 -> 1 over the attack seconds, linear decay ramp 1 -> 0 over the
final decay seconds, gain 1.0 between. Notes are mixed by summing
into one float buffer at their start offsets. If the mix peak
exceeds 1.0 the whole buffer is scaled by ``0.95 / peak``
(normalize, never hard-clip); otherwise levels are untouched.
Quantized to 16-bit: ``int(round(s * 32767))``, clipped to
``[-32768, 32767]``. The WAV is built with the stdlib ``wave``
module over ``io.BytesIO`` (mono, 16-bit, ``sample_rate``).

``drain_audio_commands(queue, *, sample_rate=22050,
max_duration=30.0)`` returns ``{"wav": bytes or None, "duration":
float, "count": int, "handled": [names], "unhandled": [names],
"notes": [str]}``, mirroring ``gpu_drain.drain_gpu_commands``:
``"wav"`` is a complete WAV file or None when no playable note
survived; ``"duration"`` is the total audible length in seconds
(max end time of kept notes, 0.0 when none); ``"count"`` is the
number of notes actually synthesized; ``"handled"`` /
``"unhandled"`` list command names in first-appearance order.
The drainer never raises for any queue input: unknown command
names, ``None`` entries, non-list entries, a ``None``/non-list
queue, and an empty queue are reported in ``unhandled`` / ``notes``.
"""

import io
import math
import random
import struct
import wave

__all__ = [
    "drain_audio_commands",
    "SAMPLE_RATE",
    "MAX_DURATION",
    "IMPLEMENTED_COMMANDS",
    "WAVES",
]

#: Default sample rate (Hz); see the module docstring for the choice.
SAMPLE_RATE = 22050

#: Default cap on the total audible length of one drain (seconds).
MAX_DURATION = 30.0

#: Fixed PRNG seed: every drain seeds identically, so noise is
#: deterministic across runs (same queue -> byte-identical WAV).
NOISE_SEED = 12345

#: Valid pitched-note frequencies (Hz).
MIN_FREQ = 20.0
MAX_FREQ = 20000.0

#: Wave names ``audio_note`` understands.
WAVES = frozenset(["sine", "square", "triangle", "saw"])

#: Commands this drainer executes (the live subset).
IMPLEMENTED_COMMANDS = frozenset(["audio_note", "audio_noise"])


def _is_number(value):
    """True for a real int/float (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _wave_value(wave_name, phase):
    """One oscillator sample for ``wave_name`` at ``phase`` cycles."""
    sine = math.sin(2.0 * math.pi * phase)
    if wave_name == "sine":
        return sine
    if wave_name == "square":
        return 1.0 if sine >= 0.0 else -1.0
    if wave_name == "triangle":
        return 2.0 * abs(2.0 * (phase - math.floor(phase + 0.5))) - 1.0
    # "saw"
    return 2.0 * (phase - math.floor(phase)) - 1.0


def _envelope(t_local, dur, attack, decay):
    """Linear attack 0->1, sustain 1.0, linear decay 1->0."""
    if attack > 0.0 and t_local < attack:
        return t_local / attack
    if decay > 0.0 and t_local > dur - decay:
        return (dur - t_local) / decay
    return 1.0


class _Drainer:
    """State for one drain: validated notes, mix buffer, result lists."""

    def __init__(self, sample_rate, max_duration):
        self.sample_rate = sample_rate
        self.max_duration = max_duration
        self.notes = []
        self.handled = []
        self.unhandled = []
        self.kept = []  # note dicts, in queue order
        self.last_end = 0.0  # end time of the most recently kept note
        self.rng = random.Random(NOISE_SEED)

    def _note(self, text):
        self.notes.append(text)

    def _mark(self, name):
        if name in IMPLEMENTED_COMMANDS:
            if name not in self.handled:
                self.handled.append(name)
        elif name not in self.unhandled:
            self.unhandled.append(name)

    def run(self, queue):
        for index, entry in enumerate(queue):
            try:
                self._run_entry(index, entry)
            except Exception as exc:  # noqa: BLE001 - drain never raises
                self._note("entry %d: %s: %s; skipped"
                           % (index, type(exc).__name__, exc))
        return self._build_result()

    def _run_entry(self, index, entry):
        if entry is None:
            self._note("entry %d is None; skipped" % index)
            return
        if not isinstance(entry, (list, tuple)) or not entry:
            self._note("entry %d is not a command record %r; skipped"
                       % (index, entry))
            return
        name = entry[0]
        if not isinstance(name, str):
            self._note("entry %d has a non-string command name %r; skipped"
                       % (index, name))
            return
        args = list(entry[1:])
        self._mark(name)
        if name == "audio_note":
            self._cmd_note(args)
        elif name == "audio_noise":
            self._cmd_noise(args)
        else:
            self._note("%s: unknown audio command; ignored (no host "
                       "meaning defined)" % name)

    def _clamp_envelope(self, name, attack, decay, dur):
        """Clamp attack/decay into [0, dur]; shrink decay if they sum past dur."""
        attack = max(0.0, min(attack, dur))
        decay = max(0.0, min(decay, dur))
        if attack + decay > dur:
            decay = dur - attack
        return attack, decay

    def _check_common(self, name, args, start_pos, attack_pos, decay_pos,
                      dur_pos, vol_pos):
        """Validate the shared numeric args; return a dict or None (skipped).

        ``*_pos`` are the indices of start/attack/decay/dur/vol in
        ``args`` (dur/vol positions differ between the two commands).
        Missing trailing args take their defaults.
        """
        dur = args[dur_pos]
        if not _is_number(dur):
            self._note("%s: bad duration %r; skipped" % (name, dur))
            return None
        dur = float(dur)
        if dur <= 0.0:
            self._note("%s: duration %r <= 0; skipped" % (name, dur))
            return None
        vol = 0.5
        if len(args) > vol_pos:
            if not _is_number(args[vol_pos]):
                self._note("%s: bad volume %r; skipped" % (name, args[vol_pos]))
                return None
            vol = float(args[vol_pos])
            if vol < 0.0 or vol > 1.0:
                self._note("%s: volume %r outside 0..1; skipped"
                           % (name, vol))
                return None
        start = self.last_end
        if len(args) > start_pos:
            if not _is_number(args[start_pos]):
                self._note("%s: bad start %r; skipped" % (name, args[start_pos]))
                return None
            start = float(args[start_pos])
            if start < 0.0:
                self._note("%s: start %r < 0; skipped" % (name, start))
                return None
        attack = 0.01
        if len(args) > attack_pos:
            if not _is_number(args[attack_pos]):
                self._note("%s: bad attack %r; skipped"
                           % (name, args[attack_pos]))
                return None
            attack = float(args[attack_pos])
        decay = 0.05
        if len(args) > decay_pos:
            if not _is_number(args[decay_pos]):
                self._note("%s: bad decay %r; skipped"
                           % (name, args[decay_pos]))
                return None
            decay = float(args[decay_pos])
        attack, decay = self._clamp_envelope(name, attack, decay, dur)
        if start >= self.max_duration:
            self._note("%s: start %r at or past max_duration %r; skipped"
                       % (name, start, self.max_duration))
            return None
        end = min(start + dur, self.max_duration)
        return {"dur": end - start, "vol": vol, "start": start,
                "attack": attack, "decay": decay, "end": end}

    def _cmd_note(self, args):
        if len(args) < 2 or len(args) > 7:
            self._note("audio_note: needs 2..7 args, got %d; skipped"
                       % len(args))
            return
        if not _is_number(args[0]):
            self._note("audio_note: bad frequency %r; skipped" % (args[0],))
            return
        freq = float(args[0])
        if freq < MIN_FREQ or freq > MAX_FREQ:
            self._note("audio_note: frequency %r outside 20..20000 Hz; "
                       "skipped" % freq)
            return
        wave_name = "sine"
        if len(args) > 2:
            if not isinstance(args[2], str):
                self._note("audio_note: bad wave %r; skipped" % (args[2],))
                return
            wave_name = args[2]
            if wave_name not in WAVES:
                self._note("audio_note: unknown wave %r; skipped"
                           % wave_name)
                return
        # args: freq, dur, wave, vol, start, attack, decay
        common = self._check_common("audio_note", args, 4, 5, 6, 1, 3)
        if common is None:
            return
        common.update({"kind": "note", "freq": freq, "wave": wave_name})
        self.kept.append(common)
        self.last_end = common["end"]
        self._note("audio_note: %s %.1f Hz %.3f s at %.3f s vol %.2f"
                   % (wave_name, freq, common["dur"], common["start"],
                      common["vol"]))

    def _cmd_noise(self, args):
        if len(args) < 1 or len(args) > 5:
            self._note("audio_noise: needs 1..5 args, got %d; skipped"
                       % len(args))
            return
        # args: dur, vol, start, attack, decay
        common = self._check_common("audio_noise", args, 2, 3, 4, 0, 1)
        if common is None:
            return
        common.update({"kind": "noise"})
        self.kept.append(common)
        self.last_end = common["end"]
        self._note("audio_noise: %.3f s at %.3f s vol %.2f"
                   % (common["dur"], common["start"], common["vol"]))

    def _build_result(self):
        if not self.kept:
            return {"wav": None, "duration": 0.0, "count": 0,
                    "handled": self.handled, "unhandled": self.unhandled,
                    "notes": self.notes}
        duration = max(note["end"] for note in self.kept)
        total = int(round(duration * self.sample_rate))
        mix = [0.0] * total
        for note in self.kept:
            self._mix_note(mix, note)
        peak = max((abs(value) for value in mix), default=0.0)
        if peak > 1.0:
            scale = 0.95 / peak
            mix = [value * scale for value in mix]
            self._note("mix peak %.3f exceeded 1.0; normalized by %.4f"
                       % (peak, scale))
        samples = [max(-32768, min(32767, int(round(value * 32767))))
                   for value in mix]
        wav = self._encode_wav(samples)
        return {"wav": wav, "duration": duration, "count": len(self.kept),
                "handled": self.handled, "unhandled": self.unhandled,
                "notes": self.notes}

    def _mix_note(self, mix, note):
        start_idx = int(round(note["start"] * self.sample_rate))
        count = int(round(note["dur"] * self.sample_rate))
        end_idx = min(start_idx + count, len(mix))
        freq = note.get("freq")
        wave_name = note.get("wave")
        for idx in range(start_idx, end_idx):
            t_local = (idx - start_idx) / self.sample_rate
            if note["kind"] == "noise":
                value = self.rng.random() * 2.0 - 1.0
            else:
                value = _wave_value(wave_name, freq * t_local)
            env = _envelope(t_local, note["dur"], note["attack"],
                            note["decay"])
            mix[idx] += value * env * note["vol"]

    def _encode_wav(self, samples):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(struct.pack("<%dh" % len(samples),
                                             *samples))
        return buffer.getvalue()


def _sanitize_rate(value, default):
    """Positive finite int sample rate, or the default (with a note)."""
    if _is_number(value) and float(value) > 0.0:
        return int(value), None
    return default, ("sample_rate %r invalid; using %d"
                     % (value, default))


def _sanitize_duration(value, default):
    """Positive finite max duration, or the default (with a note)."""
    if _is_number(value) and float(value) > 0.0:
        return float(value), None
    return default, ("max_duration %r invalid; using %r"
                     % (value, default))


def drain_audio_commands(queue, *, sample_rate=SAMPLE_RATE,
                         max_duration=MAX_DURATION):
    """Drain a ``script_run`` audio queue into WAV bytes.

    Returns ``{"wav": bytes or None, "duration": float, "count": int,
    "handled": [names], "unhandled": [names], "notes": [str]}``.
    ``"wav"`` is a complete mono 16-bit WAV file at ``sample_rate``,
    or None when no playable note survived. ``"duration"`` is the
    total audible length in seconds (max end time of kept notes,
    0.0 when none) and ``"count"`` the number of notes actually
    synthesized. ``handled`` / ``unhandled`` list command names in
    first-appearance order. Never raises: garbage input is reported
    in ``notes`` / ``unhandled`` instead.
    """
    try:
        return _drain(queue, sample_rate=sample_rate,
                      max_duration=max_duration)
    except Exception as exc:  # noqa: BLE001 - the drainer never raises
        return {"wav": None, "duration": 0.0, "count": 0,
                "handled": [], "unhandled": [],
                "notes": ["drainer error: %s: %s"
                          % (type(exc).__name__, exc)]}


def _drain(queue, *, sample_rate, max_duration):
    sample_rate, rate_note = _sanitize_rate(sample_rate, SAMPLE_RATE)
    max_duration, dur_note = _sanitize_duration(max_duration, MAX_DURATION)
    drainer = _Drainer(sample_rate, max_duration)
    if rate_note is not None:
        drainer.notes.append(rate_note)
    if dur_note is not None:
        drainer.notes.append(dur_note)
    if queue is None:
        drainer.notes.append("queue is None; treated as empty")
        return drainer.run([])
    if not isinstance(queue, (list, tuple)):
        drainer.notes.append("queue is not a list (%r); ignored"
                             % (type(queue).__name__,))
        return drainer.run([])
    return drainer.run(list(queue))
