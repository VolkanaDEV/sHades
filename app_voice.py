
"""Voice p2p engine (tay build order item 3): the hub is the directory.

The roster (join/leave/mute flags/peer voice-destination hashes) rides
the guild push path; audio frames ride DIRECT peer links:
speaker -> listener RNS.Link packets, no hub relay, no app-level forward
on the hub. This module is kivy-free so the bench can drive it.

Structure:
  VoiceEngine   one client's voice stack: its own hades/voice inbound
                destination (speakers accept listener links), outbound
                links to each roster peer (listener side), the encode
                pacer (N frames per outbound packet per listener), the
                per-speaker jitter buffer + PLC decoder feeding a sink.
Transport is injected (send_packet/receive hooks) so the unit bench can
run the whole loop with a fake and the client with real RNS.
"""

import threading
import time

try:
    import voice_codec as vc
except Exception:  # pragma: no cover - keeps lint import paths sane
    vc = None

PACKET_FRAMES = 5          # 5 x 20ms = 100ms per outbound packet
PACKET_INTERVAL_S = 0.1
JITTER_MAX_PACKETS = 6     # per-speaker holdback (~600ms ceiling)


class FakeLink(object):
    """Unit-test stand-in for an RNS.Link. Records sends, optionally
    drops whole packets (loss lane), and feeds receive through the
    engine's demux exactly like the real packet callback would."""

    def __init__(self, engine, peer, loss=0.0):
        self.engine = engine
        self.peer = peer
        self.loss = loss
        self.sends = 0
        self.dropped = 0
        self.alive = True

    def send(self, body):
        import random
        if not self.alive or random.random() < self.loss:
            self.dropped += 1
            return
        self.sends += 1
        self.engine._deliver(self.peer, body)

    def teardown(self):
        self.alive = False


class VoiceEngine(object):
    """One client in one voice room. kivy-free; wires are injected.

    identity      the client's RNS.Identity (destination announced by
                  the wiring layer; the bench runs without RNS)
    hub           metadata-only hub face: join(cid, dest, muted),
                  leave(), and nothing else -- no frame ever goes here
    viewer        this client's roster hash (lxmf delivery hash)
    """

    def __init__(self, identity=None, viewer=None, hub=None):
        if vc is None:
            raise RuntimeError("voice_codec missing")
        self.codec = vc.VoiceCodec()
        self.identity = identity
        self.viewer = viewer
        self.hub = hub
        self.room_cid = None
        self.muted = False
        self._dest_hex = None
        self._source = None       # cb -> 960-sample pcm bytes (or None)
        self._sink = None         # cb(pcm bytes)
        self._send = None         # cb(peer_dest_hex, body) transport hook
        self._verify_member = None  # cb(identity_or_viewer) -> bool
        self._out_links = {}      # peer dest hex -> FakeLink-like
        self._jitter = {}         # speaker dest hex -> packet deque
        self._speakers_pinned = set()
        self._pacer = None
        self._player = None
        self._pacing = threading.Event()
        self._playing = threading.Event()
        self._out_seq = 0
        self.frames_in = 0
        self.frames_out = 0
        self.plc_frames = 0
        self.relayed_frames = 0   # hub forwards: never moves (receipt)
        self.last_played_at = 0.0

    # ---- wiring -------------------------------------------------------

    def attach_source(self, cb):
        self._source = cb

    def attach_sink(self, cb):
        self._sink = cb

    def attach_transport(self, send_cb):
        """send_cb(peer_dest_hex, body): the real layer pushes the packet
        down an open RNS.Link to that peer; the bench's FakeTransport."""
        self._send = send_cb

    def attach_verifier(self, cb):
        """cb(viewer_hash) -> True if that member is in the roster now."""
        self._verify_member = cb

    def dest_hex(self):
        return self._dest_hex

    def set_dest(self, dest_hex):
        self._dest_hex = dest_hex

    # ---- room lifecycle -----------------------------------------------

    def join(self, cid, muted=False):
        """Join the room: announce self, register with the hub (metadata
        only), start pacing + playback loops."""
        if self.hub is not None:
            r = self.hub.join(cid, self._dest_hex, muted)
            if not r or not r.get("ok"):
                return r
        self.room_cid = cid
        self.muted = muted
        self.start()
        return {"ok": 1, "room": cid}

    def leave(self):
        self.stop()
        cid = self.room_cid
        self.room_cid = None
        self._out_links.clear()
        self._jitter.clear()
        if self.hub is not None:
            self.hub.leave()
        return {"ok": 1, "left": cid}

    def set_muted(self, muted):
        self.muted = muted
        return muted

    # ---- roster (push path) --------------------------------------------

    def on_roster(self, cid, members):
        """From the room's voice_state push. Peers = roster minus self,
        each with its own declared voice destination."""
        if cid != self.room_cid:
            return
        alive = set()
        for m in members:
            if m.get("viewer") == self.viewer or not m.get("dest"):
                continue
            dest = m["dest"]
            alive.add(dest)
            if dest not in self._out_links:
                link = FakeLink(self, dest)
                self._out_links[dest] = link
                self._jitter[dest] = {
                    "buf": [], "last": time.time()}
        for dest in list(self._out_links):
            if dest not in alive:
                self._out_links.pop(dest).teardown()
                self._jitter.pop(dest, None)
        # pinned speakers: expected-active peers, so playback gaps in
        # their streams raise PLC frames instead of bare silence
        self._speakers_pinned = alive.copy()

    # ---- speaker side ---------------------------------------------------

    def _pacer_loop(self):
        while not self._pacing.wait(PACKET_INTERVAL_S):
            if self.room_cid is None or self.muted or self._source is None:
                continue
            frames = []
            for _ in range(PACKET_FRAMES):
                pcm = self._source()
                if pcm:
                    frames.append(self.codec.encode(pcm))
                    self.frames_out += 1
            if not frames:
                continue
            body = vc.encode_packet(self.codec, frames)
            body = self._out_seq.to_bytes(2, "big") + body
            self._out_seq = (self._out_seq + 1) & 0xFFFF
            for peer, link in list(self._out_links.items()):
                try:
                    if not link.alive:
                        continue
                    self._send(peer, body) if self._send else link.send(body)
                except Exception:
                    pass

    # ---- listener side --------------------------------------------------

    def packet_received(self, speaker_dest_hex, body):
        """Demux: unpack, hold in the per-speaker jitter, feed the pump."""
        if len(body) < 2:
            return
        seq = int.from_bytes(body[:2], "big")
        try:
            frames = vc.decode_packet(body[2:])
        except ValueError:
            return
        j = self._jitter.get(speaker_dest_hex)
        if j is None:
            return
        j["buf"].append((seq, time.time(), frames))
        if len(j["buf"]) > JITTER_MAX_PACKETS:
            j["buf"].pop(0)   # a clogged peer loses its oldest packet
        self.frames_in += len(frames)

    def _deliver(self, peer, body):
        """FakeLink's receive side; the real layer routes packets on its
        inbound links here."""
        self.packet_received(peer, body)

    def _player_loop(self):
        while not self._playing.wait(0.02):
            if self.room_cid is None or self._sink is None:
                continue
            now = time.time()
            for dest, j in list(self._jitter.items()):
                buf = j["buf"]
                if buf:
                    seq, at, frames = buf[0]
                    if now - at < 0.06:
                        continue   # holdback window stays a window
                    buf.pop(0)
                    for frame in frames:
                        try:
                            self._sink(self.codec.decode(frame))
                        except Exception:
                            pass
                # gap: the speaker had no fresh packet -> PLC frame
                if not buf and self._speakers_pinned and \
                        now - j["last"] > 0.12:
                    try:
                        self._sink(self.codec.decode(None))
                        self.plc_frames += 1
                    except Exception:
                        pass
                    j["last"] = now
                elif buf:
                    j["last"] = now

    # ---- loops -----------------------------------------------------------

    def start(self):
        if self._pacer is None:
            self._pacing = threading.Event()
            self._pacer = threading.Thread(
                target=self._pacer_loop, daemon=True, name="voice-pacer")
            self._pacer.start()
        if self._player is None:
            self._playing = threading.Event()
            self._player = threading.Thread(
                target=self._player_loop, daemon=True, name="voice-player")
            self._player.start()

    def stop(self):
        self._out_seq = 0
        if self._pacer is not None:
            self._pacing.set()
            self._pacer = None
        if self._player is not None:
            self._playing.set()
            self._player = None
