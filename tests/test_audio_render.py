#!/usr/bin/env python3
"""Script-audio rendering tests — fake runner + fake audio drainer.

Covers _add_site_script's audio path (mirroring the GPU drain):
a script whose run queues audio drains it on the worker thread,
saves the WAV, lands an "audio: N notes, X.X s" line with drainer
notes, auto-plays it on landing, and offers a stop button; the
button (and leaving the page) stops playback via the AudioRow stop
idiom. A failing script produces no audio, an empty queue drains
nothing, editor preview never executes, and animated blocks drain
audio only on the frame-0 landing — a later frame never restarts
the melody.

Threading/Clock are captured, not run (the test_site_script_render
pattern): the test drives workers and scheduled landings by hand.
Run under xvfb-run (real Kivy widgets).
"""
import io
import os
import sys
import types
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_site  # noqa: E402
import kivy_client as kc  # noqa: E402
import micron  # noqa: E402
import sanguis_script  # noqa: E402

PASS, FAIL, SKIP = [], [], []
CID = "c" * 64
SCRIPT_SRC = ("cell main() {\n  let n = audio_note(440.0, 0.5, "
              "\"square\", 0.5);\n  output n;\n}")
PAGE = "> Demo\n\n`=sanguis\n" + SCRIPT_SRC + "\n`=\n\ntrailing text"
ANIM_SRC = ("cell main() {\n  let h = gpu_open();\n"
            "  gpu_dispatch(h, frame);\n  gpu_close(h);\n"
            "  output frame;\n}")
ANIM_PAGE = "> Anim\n\n`=sanguis anim\n" + ANIM_SRC + "\n`=\n\ntrailing text"
AUDIO_QUEUE = [["audio_note", 440.0, 0.5, "square", 0.5],
               ["audio_note", 660.0, 0.5, "square", 0.5]]
DEMO_QUEUE = [["audio_note", 293.66, 0.22, "square", 0.5, 0.0,
               0.01, 0.08],
              ["audio_note", 349.23, 0.22, "square", 0.5, 0.22,
               0.01, 0.08],
              ["audio_note", 440.0, 0.22, "square", 0.5, 0.44,
               0.01, 0.08],
              ["audio_note", 587.33, 0.22, "square", 0.5, 0.66,
               0.01, 0.08]]
FAKE_WAV = b"RIFF....WAVEfake-wav-bytes"


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def skip(name, reason):
    SKIP.append(name)
    print("SKIP  " + name + "  | " + reason)


class _FakeMedia(object):
    """Stand-in for a playing sound: records stop/release calls."""

    def __init__(self):
        self.stopped = 0
        self.released = 0

    def stop(self):
        self.stopped += 1

    def release(self):
        self.released += 1


class _CaptureThread(object):
    """Thread stand-in: records targets, runs nothing until told."""
    targets = []

    def __init__(self, target=None, daemon=False, **k):
        self._target = target
        _CaptureThread.targets.append(target)

    def start(self):
        pass


class _CaptureClock(object):
    """Clock stand-in: records (callback, delay) pairs, runs none."""
    callbacks = []

    @staticmethod
    def schedule_once(fn, dt=0):
        _CaptureClock.callbacks.append((fn, dt))

    @staticmethod
    def unschedule(fn):
        _CaptureClock.callbacks = [c for c in _CaptureClock.callbacks
                                   if c[0] is not fn]


def visual_texts(container):
    """Widget texts in visual (top-to-bottom) order."""
    out = []
    for w in reversed(container.children):
        t = getattr(w, "text", None)
        if isinstance(t, str):
            out.append(t)
    return out


def make_app():
    main = types.SimpleNamespace(
        _current_channel="wiki",
        ids=types.SimpleNamespace(message_grid=None, scroll=None))

    def get_screen(name):
        if name == "main":
            return main
        raise KeyError(name)

    root = types.SimpleNamespace(get_screen=get_screen)
    app = types.SimpleNamespace(
        root=root, guild_api=None,
        _site_current=(CID, "/index.mu"), _site_stack=[],
        _site_forward=[], _site_theme={}, _site_pending_scroll=1.0,
        _msg_widgets=[], _skeleton_cid=None, _site_src_text=None,
        status=[], saved_blobs=[], _audio_media=None, play_calls=[])
    for name in ("_micron_blocks_into", "_micron_label",
                 "_add_site_script", "_land_site_script",
                 "_script_result_widgets", "_script_audio_row",
                 "_script_bindings", "_script_page_path",
                 "_save_gpu_image", "_save_audio_wav",
                 "_run_page_script", "_drain_page_gpu",
                 "_drain_page_audio", "_anim_maybe_start",
                 "_anim_start", "_anim_alive", "_anim_tick",
                 "_land_anim_frame"):
        setattr(app, name, types.MethodType(
            getattr(kc.sHadesClientApp, name), app))
    app._hex6_to_rgba = kc.sHadesClientApp._hex6_to_rgba
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID
    app._save_attachment_blob = lambda blob, name: (
        app.saved_blobs.append((blob, name)) or "/tmp/fake-audio.wav")

    def fake_play(path):
        app.play_calls.append(path)
        media = _FakeMedia()
        app._audio_media = media
        app._last_media = media
        return media

    app._play_audio = fake_play
    return app


def render(app, page_text=PAGE, media_ok=True):
    from app_site import SitePage
    page = SitePage()
    app._micron_blocks_into(page, micron.micron_parse(page_text),
                            CID, media_ok=media_ok)
    return page


def run_workers():
    """Run every captured worker target, then clear the list."""
    for target in list(_CaptureThread.targets):
        if target is not None:
            target()
    _CaptureThread.targets = []


def pop_callback():
    """Take the earliest scheduled (callback, delay) pair."""
    return _CaptureClock.callbacks.pop(0)


def drive_all():
    """Run every captured worker, then every scheduled landing."""
    run_workers()
    for fn, _dt in list(_CaptureClock.callbacks):
        fn(0)
    _CaptureClock.callbacks = []


def reset():
    _CaptureThread.targets = []
    _CaptureClock.callbacks = []


def find_audio_row(page):
    """The landed script-audio stop row, if any (recursive: the row
    is a direct child of the page container)."""
    for w in page.children:
        if getattr(w, "_is_script_audio_row", False):
            return w
    return None


def button_label(btn):
    fancy = getattr(btn, "_fancy_label", None)
    if fancy is not None:
        return fancy.text
    return getattr(btn, "text", None)


def main():
    real_clock, real_thread = app_site.Clock, app_site.threading.Thread
    real_avail = sanguis_script.scripting_available
    app_site.Clock = _CaptureClock
    app_site.threading.Thread = _CaptureThread
    sanguis_script.scripting_available = lambda: True
    try:
        # ---- 0. run_script end to end through the REAL bch+interp.bc
        res = sanguis_script.run_script(SCRIPT_SRC)
        check("run_script result always carries an audio field",
              isinstance(res, dict) and "audio" in res, res)
        check("run_script failure shapes carry an empty audio list",
              sanguis_script._failure("x").get("audio") == []
              and sanguis_script._normalize(
                  {"ok": 1, "result": 2, "audio": "junk"}).get("audio")
              == [], sanguis_script._failure("x"))
        if (res.get("ok") == 1.0
                and res.get("audio") == [["audio_note", 440.0, 0.5,
                                          "square", 0.5]]):
            check("real runner: audio_note queues the exact record",
                  True)
        elif "unknown function: audio_note" in str(res.get("result")):
            skip("real runner: audio_note queues the exact record",
                 "interpreter predates the audio builtins (%s)"
                 % (res.get("result"),))
        else:
            check("real runner: audio_note queues the exact record",
                  False, res)

        # ---- 1. ok script + audio queue -> drain, note, stop, play
        reset()
        app = make_app()
        audio_drains = []

        def fake_audio_drain(queue):
            audio_drains.append(list(queue))
            return {"wav": FAKE_WAV, "duration": 1.0, "count": 2,
                    "handled": ["audio_note"], "unhandled": [],
                    "notes": ["drained 2 notes"]}

        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 2.0, "out": ["played"],
            "gpu": [], "audio": list(AUDIO_QUEUE)}
        app._drain_page_audio = fake_audio_drain
        app._drain_page_gpu = lambda queue: (_ for _ in ()).throw(
            AssertionError("gpu drainer must not run with no gpu queue"))
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        row = find_audio_row(page)
        check("audio drainer receives the script's audio queue",
              audio_drains == [AUDIO_QUEUE], audio_drains)
        check("audio note line lands with count + duration",
              any("audio: 2 notes, 1.0 s" in t for t in texts), texts)
        check("audio drainer notes land with the result",
              any(t == "[b]2[/b]" for t in texts)
              and any("drained 2 notes" in t for t in texts), texts)
        check("wav saved as script-audio.wav bytes",
              len(app.saved_blobs) == 1
              and app.saved_blobs[0][0] == FAKE_WAV
              and app.saved_blobs[0][1] == "script-audio.wav",
              [(len(b), n) for b, n in app.saved_blobs])
        check("stop control row exists after the audio line",
              row is not None, texts)
        check("stop button is labelled stop",
              row is not None and button_label(row._stop_button)
              == "stop", button_label(row._stop_button)
              if row is not None else None)
        check("auto-play invoked with the saved wav path",
              app.play_calls == ["/tmp/fake-audio.wav"],
              app.play_calls)
        check("row counts as started once playback began",
              row is not None and row._audio_started is True,
              getattr(row, "_audio_started", None)
              if row is not None else None)

        # ---- 1b. unhandled audio commands land as a note ----
        reset()
        app = make_app()

        def unhandled_drain(queue):
            return {"wav": None, "duration": 0.0, "count": 0,
                    "handled": [], "unhandled": ["audio_zap"],
                    "notes": []}

        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 1.0, "out": [],
            "gpu": [], "audio": [["audio_zap", 1.0]]}
        app._drain_page_audio = unhandled_drain
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("unhandled audio commands land as a visible note",
              any("unhandled audio commands: audio_zap" in t
                  for t in texts), texts)
        check("no wav, no stop row when nothing was synthesized",
              find_audio_row(page) is None
              and app.play_calls == [], app.play_calls)

        # ---- 2. failing script: no audio at all ----
        reset()
        app = make_app()
        drain_calls = []
        app._run_page_script = lambda s, b: {
            "ok": 0.0, "result": "type error: boom",
            "out": [], "gpu": [], "audio": list(AUDIO_QUEUE)}
        app._drain_page_audio = lambda queue: drain_calls.append(
            list(queue)) or (_ for _ in ()).throw(
            AssertionError("audio drainer must not run on failure"))
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("failing script: audio drainer never called",
              drain_calls == [], drain_calls)
        check("failing script: no audio line lands",
              not any("audio:" in t for t in texts), texts)
        check("failing script: no stop row lands",
              find_audio_row(page) is None, texts)
        check("failing script: error block still lands",
              any("script error:" in t and "type error" in t
                  for t in texts), texts)

        # ---- 3. empty audio queue: nothing drains, nothing lands
        reset()
        app = make_app()
        drain_calls = []
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 1.0, "out": [],
            "gpu": [], "audio": []}
        app._drain_page_audio = lambda queue: drain_calls.append(
            list(queue)) or (_ for _ in ()).throw(
            AssertionError("audio drainer must not run empty"))
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("empty queue: audio drainer never called",
              drain_calls == [], drain_calls)
        check("empty queue: no audio line, no stop row",
              not any("audio:" in t for t in texts)
              and find_audio_row(page) is None, texts)
        check("empty queue: result still lands",
              any(t == "[b]1[/b]" for t in texts), texts)
        # Old-runner shape: no "audio" key at all.
        reset()
        app = make_app()
        drain_calls = []
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 1.0, "out": [], "gpu": []}
        app._drain_page_audio = lambda queue: drain_calls.append(
            list(queue)) or (_ for _ in ()).throw(
            AssertionError("audio drainer must not run without audio"))
        page = render(app)
        drive_all()
        check("runner without an audio field: drainer never called",
              drain_calls == []
              and not any("audio:" in t for t in visual_texts(page)),
              (drain_calls, visual_texts(page)))

        # ---- 4. editor preview never executes ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = lambda s, b: calls.append((s, b)) or {
            "ok": 1.0, "result": 1.0, "out": [],
            "gpu": [], "audio": list(AUDIO_QUEUE)}
        page = render(app, media_ok=False)
        texts = visual_texts(page)
        check("preview shows the script source as code",
              any("audio_note" in t for t in texts), texts)
        check("preview starts no worker thread",
              _CaptureThread.targets == [], _CaptureThread.targets)
        check("preview never calls the runner", calls == [], calls)
        check("preview schedules no landing",
              _CaptureClock.callbacks == [], _CaptureClock.callbacks)

        # ---- 5. anim block: frame 0 drains audio once, later
        # frames never drain it again (and never re-play it) ----
        reset()
        app = make_app()
        from PIL import Image
        audio_drains = []
        gpu_drains = []
        play_calls = []
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 1.0, "out": [],
            "gpu": [["gpu_open", 1.0]],
            "audio": list(AUDIO_QUEUE)}

        def anim_audio_drain(queue):
            audio_drains.append(list(queue))
            return {"wav": FAKE_WAV, "duration": 1.0, "count": 2,
                    "handled": ["audio_note"], "unhandled": [],
                    "notes": []}

        def anim_gpu_drain(queue):
            gpu_drains.append(list(queue))
            return {"image": Image.new("RGB", (4, 3),
                                       color=(255, 0, 0)),
                    "notes": ["GPU backend: CPU"],
                    "handled": ["gpu_open"], "unhandled": [],
                    "backend": "cpu"}

        app._drain_page_audio = anim_audio_drain
        app._drain_page_gpu = anim_gpu_drain
        orig_play = app._play_audio

        def counting_play(path):
            play_calls.append(path)
            return orig_play(path)

        app._play_audio = counting_play
        page = render(app, ANIM_PAGE)
        run_workers()
        pop_callback()[0](0)  # frame-0 landing, loop starts
        emb = [w for w in page.children
               if isinstance(w, app_site.MessageEmbed)]
        state = getattr(emb[0], "_anim_state", None) if emb else None
        check("anim: frame 0 drains the audio queue once",
              audio_drains == [AUDIO_QUEUE], audio_drains)
        check("anim: frame 0 auto-plays once",
              play_calls == ["/tmp/fake-audio.wav"], play_calls)
        check("anim: loop still starts on the GPU image",
              state is not None and state["frame"] == 0, state)
        check("anim: audio line lands on frame 0",
              any("audio: 2 notes, 1.0 s" in t
                  for t in visual_texts(page)), visual_texts(page))
        pop_callback()[0](0)  # tick: frame 1 worker starts
        run_workers()  # frame 1 re-runs the script
        pop_callback()[0](0)  # frame-1 landing swaps the texture
        check("anim: frame 1 re-run does NOT drain audio again",
              len(audio_drains) == 1, audio_drains)
        check("anim: frame 1 re-run does NOT auto-play again",
              len(play_calls) == 1, play_calls)
        check("anim: GPU still drains once per frame",
              len(gpu_drains) == 2, gpu_drains)
        check("anim: frame 1 landed (loop kept running)",
              state is not None and state["landed_frames"] == 1,
              state)

        # ---- 6. stop button stops playback; leaving stops it too
        reset()
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 2.0, "out": [],
            "gpu": [], "audio": list(AUDIO_QUEUE)}
        app._drain_page_audio = fake_audio_drain
        page = render(app)
        drive_all()
        row = find_audio_row(page)
        media = app._last_media
        check("stop: playback is running before the press",
              row is not None and row._audio_started is True
              and app._audio_media is media
              and media.stopped == 0, (media.stopped
                                       if media else None))
        row._stop_button.dispatch("on_release")
        check("stop: pressing stop calls stop() on the media",
              media.stopped == 1, media.stopped)
        check("stop: pressing stop calls release() and clears media",
              media.released == 1 and app._audio_media is None,
              (media.released, app._audio_media))
        check("stop: the row label resets to audio stopped",
              row._audio_label.text == "audio stopped",
              row._audio_label.text)
        check("stop: the button label resets to stopped",
              button_label(row._stop_button) == "stopped",
              button_label(row._stop_button))
        # Stop-on-leave: a fresh landing, then re-render the page.
        reset()
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 2.0, "out": [],
            "gpu": [], "audio": list(AUDIO_QUEUE)}
        app._drain_page_audio = fake_audio_drain
        page = render(app)
        drive_all()
        row = find_audio_row(page)
        media = app._last_media
        check("leave: playback is running before the re-render",
              row is not None and row._audio_started is True
              and media.stopped == 0, media.stopped)
        page.clear_widgets()  # re-render drops the row's parent
        check("leave: re-render stops playback",
              media.stopped == 1 and app._audio_media is None,
              (media.stopped, app._audio_media))

        # ---- 7. drainer failure modes degrade, never crash ----
        reset()
        app = make_app()

        def boom(queue):
            raise RuntimeError("no audio here")

        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 7.0, "out": [],
            "gpu": [], "audio": list(AUDIO_QUEUE)}
        app._drain_page_audio = boom
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("drainer raising lands a visible note, result kept",
              any(t == "[b]7[/b]" for t in texts)
              and any("Audio output unavailable: no audio here" in t
                      for t in texts), texts)
        check("drainer raising: no stop row without a wav",
              find_audio_row(page) is None
              and app.play_calls == [], app.play_calls)
        # Non-dict drain result degrades to empty.
        reset()
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 7.0, "out": [],
            "gpu": [], "audio": list(AUDIO_QUEUE)}
        app._drain_page_audio = lambda queue: "not-a-dict"
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("non-dict drain result degrades, result kept",
              any(t == "[b]7[/b]" for t in texts), texts)
        # Missing audio_drain module -> the same visible note.
        reset()
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 7.0, "out": [],
            "gpu": [], "audio": list(AUDIO_QUEUE)}
        app._drain_page_audio = types.MethodType(
            kc.sHadesClientApp._drain_page_audio, app)
        page = render(app)
        real_mod = sys.modules.get("audio_drain")
        sys.modules["audio_drain"] = None
        try:
            drive_all()
        finally:
            if real_mod is None:
                sys.modules.pop("audio_drain", None)
            else:
                sys.modules["audio_drain"] = real_mod
        texts = visual_texts(page)
        check("missing audio_drain module lands a visible note",
              any(t == "[b]7[/b]" for t in texts)
              and any("Audio output unavailable:" in t for t in texts),
              texts)

        # ---- 8. real drainer integration (skips if not present) --
        try:
            import audio_drain  # noqa: F401
            have_drainer = True
        except ImportError:
            have_drainer = False
        if not have_drainer:
            skip("real drainer: demo melody drains to a WAV",
                 "audio_drain module not present")
            skip("real drainer: WAV decodes to 0.88 s, count 4",
                 "audio_drain module not present")
        else:
            reset()
            app = make_app()
            app._run_page_script = lambda s, b: {
                "ok": 1.0, "result": 4.0,
                "out": ["d-minor arpeggio: 4 notes"],
                "gpu": [], "audio": list(DEMO_QUEUE)}
            app._drain_page_audio = types.MethodType(
                kc.sHadesClientApp._drain_page_audio, app)
            page = render(app)
            drive_all()
            texts = visual_texts(page)
            check("real drainer: demo melody drains to a WAV",
                  len(app.saved_blobs) == 1
                  and app.saved_blobs[0][0][:4] == b"RIFF"
                  and app.saved_blobs[0][1] == "script-audio.wav"
                  and any("audio: 4 notes, 0.9 s" in t for t in texts),
                  texts)
            wav = wave.open(io.BytesIO(app.saved_blobs[0][0]), "rb")
            dur = wav.getnframes() / wav.getframerate()
            check("real drainer: WAV decodes to 0.88 s, count 4",
                  abs(dur - 0.88) < 0.01
                  and wav.getnchannels() == 1
                  and wav.getsampwidth() == 2, dur)
            check("real drainer: auto-play invoked with the wav",
                  app.play_calls == ["/tmp/fake-audio.wav"],
                  app.play_calls)
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread
        sanguis_script.scripting_available = real_avail

    print("PASS audio render: %d checks, %d skipped, %d failed"
          % (len(PASS), len(SKIP), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
