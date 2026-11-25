"""Real audio device I/O for the voice engine (build-order item 5).

Two pieces, both daemon-like, both called from the engine's own loops:

- MicSource: a ring buffer filled by PyAudio's stream callback and
  drained 20ms-frame-at-a-time by the engine's pacer (10Hz x
  PACKET_FRAMES calls/second, one frame per call).
- SpeakerSink: called from the engine's player loop with decoded PCM;
  a drain thread keeps the device stream fed so the player loop never
  blocks on a C call.

No device: every constructor raises VoiceDeviceError with the reason,
so the client can present "voice unavailable" instead of half-wired.
"""
import collections
import threading
import time

import voice_codec
import pyaudio

# must equal app_voice.AUDIO_RATE / PACKET_FRAMES pacing; 20ms frames
FRAME_MS = 20
# Frame geometry rides voice_codec, not the mic rate: the engine's
# source contract is one codec-rate frame per poll (960 samples at
# 48 kHz, 1920 bytes). The first cut used 16 kHz mic defaults and a
# 640-byte frame, which every codec.encode call would have rejected;
# the device gate's sine loopback never caught it because its source
# generates 48 kHz frames itself.
FRAMES_PER_CHUNK = 960          # 48 kHz * 0.02 s (bytes: *2, s16 mono)
FRAME_BYTES = FRAMES_PER_CHUNK * 2


class VoiceDeviceError(Exception):
    pass


class MicSource:
    """One 20ms frame (640B) per poll(); captures continuously."""

    def __init__(self, device=None, rate=voice_codec.SAMPLE_RATE,
                 channels=voice_codec.CHANNELS):
        self.pa = pyaudio.PyAudio()
        spec = _pick(self.pa, want_output=False, device=device)
        self._ring = collections.deque()   # 20ms frames as bytes
        self._ring_lock = threading.Lock()
        self._drop = 0
        self._stream = self.pa.open(
            format=pyaudio.paInt16, channels=channels, rate=rate,
            input=True, output=False, input_device_index=spec,
            frames_per_buffer=FRAMES_PER_CHUNK,
            stream_callback=self._cb)
        self.rate, self.channels = rate, channels
        self.frames_read = 0

    def _cb(self, in_data, frame_count, time_info, status_flags):
        with self._ring_lock:
            if len(self._ring) > 50:
                # the ring is bounded; a stalled pacer loses the oldest
                # frame, the newest always survive
                self._ring.popleft()
                self._drop += 1
            self._ring.append(in_data)
        return (None, pyaudio.paContinue)

    def poll(self):
        with self._ring_lock:
            if self._ring:
                self.frames_read += 1
                return self._ring.popleft()
        return None

    def close(self):
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        self.pa.terminate()


class SpeakerSink:
    """Queues decoded PCM; one drain thread writes the device stream.
    __call__(pcm) is the engine sink callback."""

    def __init__(self, device=None, rate=voice_codec.SAMPLE_RATE,
                 channels=voice_codec.CHANNELS):
        self.pa = pyaudio.PyAudio()
        spec = _pick(self.pa, want_output=True, device=device)
        self._stream = self.pa.open(
            format=pyaudio.paInt16, channels=channels, rate=rate,
            input=False, output=True, output_device_index=spec,
            frames_per_buffer=FRAMES_PER_CHUNK)
        self.rate, self.channels = rate, channels
        self._q = collections.deque()
        self._lock = threading.Lock()
        self._played_frames = 0
        self._bytes_in = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def __call__(self, pcm):
        if not pcm:
            return
        self._bytes_in += len(pcm)
        with self._lock:
            self._q.append(bytes(pcm))

    def _drain(self):
        while not self._stop.is_set():
            with self._lock:
                pcm = self._q.popleft() if self._q else None
            if pcm is None:
                # keep the device stream rolling (silence = PLC output)
                self._stream.write(b"\x00" * FRAME_BYTES,
                                   exception_on_underflow=False)
                time.sleep(0.01)
                continue
            self._played_frames += len(pcm) // FRAME_BYTES
            self._stream.write(pcm, exception_on_underflow=False)

    def stats(self):
        return {"bytes_in": self._bytes_in,
                "frames_played": self._played_frames}

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        self.pa.terminate()


def _pick(pa, want_output, device=None):
    if device is not None:
        info = pa.get_device_info_by_index(device)
        if (info["maxOutputChannels"] if want_output
                else info["maxInputChannels"]) < 1:
            raise VoiceDeviceError(
                "device %d has no %s channels"
                % (device, "output" if want_output else "input"))
        return device
    # default first: PyAudio resolves the system default either way
    try:
        idx = (pyaudio.PyAudio.get_default_output_device_info(pa)
               if want_output else
               pyaudio.PyAudio.get_default_input_device_info(pa))["index"]
        return int(idx)
    except Exception as e:
        raise VoiceDeviceError("no usable %s device: %r"
                               % ("speaker" if want_output else "microphone", e))


def bind(engine, mic=None, speaker=None):
    """Wire source/sink callbacks into a VoiceEngine. Either side may
    be omitted (a listen-only seat passes mic=None; a speaker seat that
    stays muted never calls the sink)."""
    if mic is not None:
        engine.attach_source(mic.poll)
    if speaker is not None:
        engine.attach_sink(speaker)
    return mic, speaker


class RNSLinkShell:
    """The VoiceEngine's link-like view of a live RNS.Link. alive reads
    the link status so pre-establish sends are skipped; send pushes the
    engine's raw packet down the link."""

    def __init__(self, link):
        self._link = link
        self.remote_dest = None

    @property
    def alive(self):
        import RNS
        try:
            return getattr(self._link, "status", None) == RNS.Link.ACTIVE
        except Exception:
            return False

    def send(self, body):
        self._link.send(body)

    def teardown(self):
        try:
            self._link.teardown()
        except Exception:
            pass


class VoiceLane:
    """One client's RNS voice lane: an announced listening destination
    plus a dial factory, both feeding one VoiceEngine."""

    APP_NAME = "shades"
    ASPECT = "voice"

    def __init__(self, identity):
        self.identity = identity
        self._listener = None
        self._dest = None
        self.engine = None

    def open_listener(self, engine, announce=True):
        """IN destination on the client identity; raw voice packets from
        any peer's link feed engine.accept_inbound."""
        import RNS
        self.engine = engine
        self._dest = RNS.Destination(
            self.identity, RNS.Destination.IN, RNS.Destination.SINGLE,
            self.APP_NAME, self.ASPECT)

        def on_link(link):
            shell = RNSLinkShell(link)
            try:
                ri = link.get_remote_identity()
                if ri is not None:
                    shell.remote_dest = RNS.Destination.hash(ri).hex()
                    engine.accept_inbound(shell.remote_dest, shell)
            except Exception:
                pass

        self._dest.set_link_established_callback(on_link)
        if announce:
            self._dest.announce(app_data=self._dest.hash)
        return self._dest.hash.hex() if hasattr(self._dest.hash, "hex") \
            else self._dest.hash

    def close_listener(self):
        try:
            self._dest.teardown()
        except Exception:
            pass
        self._dest = None
        self._listener = None

    def dial_factory(self, engine):
        def make(dest_hex):
            import RNS
            if not dest_hex:
                return None
            dest_bytes = bytes.fromhex(dest_hex)
            ri = RNS.Identity.recall(dest_bytes)
            if ri is None:
                # path not heard yet: the roster's dest hashes rely on
                # peers' voice announces; skip this tick, retry on the
                # next roster push
                return None
            out = RNS.Destination(ri, RNS.Destination.OUT,
                                  RNS.Destination.SINGLE,
                                  self.APP_NAME, self.ASPECT)
            link = RNS.Link(out)
            shell = RNSLinkShell(link)
            return shell
        return make
