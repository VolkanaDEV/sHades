#!/usr/bin/env python3
"""Animated micron script blocks — frame loop tests.

Covers the `` `=sanguis anim `` opener: the parser flags the block,
the renderer re-runs it once per frame with a `frame` binding
(integer, starting at 0), drains each frame's GPU queue, and swaps
the new image into the landed embed's texture in place. The loop
skips ticks while a frame is still draining (never two runs at
once), stops when the page is left or re-rendered, and stops with a
visible note when a frame fails. One-shot blocks are untouched: no
`frame` binding, no loop. The CPU backend animates too (a fake
drainer reporting backend "cpu", plus the real drainer in-harness,
which falls back to CPU on the Kivy thread).

Threading/Clock are captured, not run (the test_site_script_render
pattern): the test drives workers and scheduled landings by hand, so
the frame interleavings can be staged exactly. Run under xvfb-run
(real Kivy widgets + CoreImage texture swaps).
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_site  # noqa: E402
import kivy_client as kc  # noqa: E402
import micron  # noqa: E402
import sanguis_script  # noqa: E402

PASS, FAIL = [], []
CID = "c" * 64
ANIM_SRC = ("cell main() {\n  let h = gpu_open();\n"
            "  gpu_upload_f32(h, [-0.75, 0.0, 3.0, 48.0, 0.0, 1.0]);\n"
            "  gpu_dispatch(h, frame);\n  gpu_close(h);\n"
            "  output frame;\n}")
ANIM_PAGE = "> Anim\n\n`=sanguis anim\n" + ANIM_SRC + "\n`=\n\ntrailing text"
PLAIN_SRC = "cell main() {\n  output 1.0;\n}"
PLAIN_PAGE = "> Plain\n\n`=sanguis\n" + PLAIN_SRC + "\n`=\n\ntrailing text"


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


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
        status=[], saved_blobs=[])
    for name in ("_micron_blocks_into", "_micron_label",
                 "_add_site_script", "_land_site_script",
                 "_script_result_widgets", "_script_bindings",
                 "_script_page_path", "_save_gpu_image",
                 "_run_page_script", "_drain_page_gpu",
                 "_anim_maybe_start", "_anim_start", "_anim_alive",
                 "_anim_tick", "_land_anim_frame"):
        setattr(app, name, types.MethodType(
            getattr(kc.sHadesClientApp, name), app))
    app._hex6_to_rgba = kc.sHadesClientApp._hex6_to_rgba
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID
    app._save_attachment_blob = lambda blob, name: (
        app.saved_blobs.append((blob, name)) or "/tmp/fake-gpu.png")
    return app


def render(app, page_text=ANIM_PAGE, media_ok=True):
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


def reset():
    _CaptureThread.targets = []
    _CaptureClock.callbacks = []


def find_embed(page):
    embeds = [w for w in page.children
              if isinstance(w, app_site.MessageEmbed)]
    return embeds[0] if embeds else None


def anim_state(page):
    emb = find_embed(page)
    return getattr(emb, "_anim_state", None) if emb is not None else None


def main():
    real_clock, real_thread = app_site.Clock, app_site.threading.Thread
    real_avail = sanguis_script.scripting_available
    app_site.Clock = _CaptureClock
    app_site.threading.Thread = _CaptureThread
    sanguis_script.scripting_available = lambda: True
    try:
        # ---- 0. parser: the anim opener flags the block ----
        blocks = micron.micron_parse(ANIM_PAGE)
        scripts = [b for b in blocks if b["type"] == "script"]
        check("anim opener parses to a script block with anim True",
              len(scripts) == 1 and scripts[0].get("anim") is True
              and scripts[0]["text"] == ANIM_SRC, scripts)
        blocks = micron.micron_parse(PLAIN_PAGE)
        scripts = [b for b in blocks if b["type"] == "script"]
        check("plain opener parses to a script block without anim",
              len(scripts) == 1 and "anim" not in scripts[0], scripts)
        blocks = micron.micron_parse("`=sanguis anim\ncell main() {")
        check("unterminated anim block is a code block, never runs",
              len(blocks) == 1 and blocks[0]["type"] == "code", blocks)
        blocks = micron.micron_parse("`=sanguis foo\ncell main() {\n`=\n")
        check("unknown opener suffix is not a script block",
              not any(b["type"] == "script" for b in blocks), blocks)

        # ---- 0b. runner: the frame binding arrives as a number ----
        # (real bch + rebuilt interp.bc; this is the bindings-file
        # path, not just script_run's literal bindings.)
        res = sanguis_script.run_script(
            "cell main() {\n  output frame * 2.0;\n}",
            [["frame", 3.0]])
        check("runner: frame binding is numeric (3 * 2 = 6)",
              res.get("ok") == 1.0 and res.get("result") == 6.0, res)
        res = sanguis_script.run_script(
            "cell main() {\n  let h = gpu_open();\n"
            "  gpu_dispatch(h, frame);\n  output h;\n}",
            [["frame", 5.0]])
        check("runner: frame reaches gpu_dispatch in the queue",
              res.get("ok") == 1.0
              and res.get("gpu") == [["gpu_open", 1.0],
                                     ["gpu_dispatch", 1.0, 5.0]], res)

        # ---- 1. one-shot block: no frame binding, no loop ----
        reset()
        app = make_app()
        calls = []

        def one_shot_run(source, bindings):
            calls.append((source, bindings))
            return {"ok": 1.0, "result": 1.0, "out": [],
                    "gpu": [["gpu_open", 1.0]]}

        from PIL import Image
        app._run_page_script = one_shot_run
        app._drain_page_gpu = lambda queue: {
            "image": Image.new("RGB", (4, 3), color=(255, 0, 0)),
            "notes": [], "handled": ["gpu_open"], "unhandled": [],
            "backend": "cpu"}
        page = render(app, PLAIN_PAGE)
        run_workers()
        check("one-shot: worker runs once with no frame binding",
              len(calls) == 1
              and calls[0][1] == [["page", "/index.mu"],
                                  ["gpu_present", 1.0],
                                  ["channel", "wiki"]], calls)
        run_callback = pop_callback()
        run_callback[0](0)
        check("one-shot: embed lands, no animation state attached",
              find_embed(page) is not None
              and anim_state(page) is None, visual_texts(page))
        check("one-shot: embed still uses the file source",
              find_embed(page).source == "/tmp/fake-gpu.png",
              find_embed(page).source)
        check("one-shot: no tick scheduled after landing",
              _CaptureClock.callbacks == [], _CaptureClock.callbacks)

        # ---- 2. the loop advances frame by frame ----
        reset()
        app = make_app()
        calls = []
        drains = []
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]

        def anim_run(source, bindings):
            calls.append((source, bindings))
            return {"ok": 1.0, "result": 1.0, "out": [],
                    "gpu": [["gpu_open", 1.0], ["gpu_dispatch", 1.0, 0.0]]}

        def anim_drain(queue):
            drains.append(list(queue))
            i = len(drains) - 1
            return {"image": Image.new(
                        "RGB", (4, 3), color=colors[i % len(colors)]),
                    "notes": ["GPU backend: CPU"],
                    "handled": ["gpu_open", "gpu_dispatch"],
                    "unhandled": [], "backend": "cpu"}

        app._run_page_script = anim_run
        app._drain_page_gpu = anim_drain
        page = render(app)
        check("anim: placeholder inserted while frame 0 runs",
              any("running script" in t for t in visual_texts(page)),
              visual_texts(page))
        run_workers()
        check("anim: frame 0 runs with the frame binding at 0.0",
              len(calls) == 1
              and calls[0][1][-1] == ["frame", 0.0], calls)
        pop_callback()[0](0)  # frame-0 landing
        state = anim_state(page)
        emb0 = find_embed(page)
        check("anim: loop state starts on the landed embed",
              state is not None and state["frame"] == 0
              and state["landed_frames"] == 0
              and state["busy"] is False, state)
        check("anim: frame 0 lands texture-only, no source to load",
              emb0 is not None and not emb0.source
              and emb0.texture is not None
              and getattr(emb0, "_anim_source", None)
              == "/tmp/fake-gpu.png",
              (emb0.source if emb0 is not None else None,
               getattr(emb0, "_anim_source", None)
               if emb0 is not None else None))
        check("anim: first tick scheduled at the target interval",
              len(_CaptureClock.callbacks) == 1
              and 0.05 < _CaptureClock.callbacks[0][1] <= 0.1,
              _CaptureClock.callbacks)
        # Frame 1: tick -> worker -> landing swaps the texture.
        pop_callback()[0](0)
        check("anim: tick starts a worker, run not yet called",
              len(calls) == 1 and len(_CaptureThread.targets) == 1,
              (len(calls), len(_CaptureThread.targets)))
        run_workers()
        check("anim: frame 1 runs with the frame binding at 1.0",
              len(calls) == 2
              and calls[1][1][-1] == ["frame", 1.0], calls)
        check("anim: one drain per run, no duplicates",
              len(drains) == 2, len(drains))
        pop_callback()[0](0)  # frame-1 landing
        emb = find_embed(page)
        tex1 = emb.texture.pixels if emb.texture is not None else None
        check("anim: frame 1 swaps the embed texture in place",
              emb.texture is not None and tex1 is not None
              and state["landed_frames"] == 1
              and state["frame"] == 1, state)
        # Frame 2: the texture changes again, page not re-rendered.
        pop_callback()[0](0)
        run_workers()
        check("anim: frame 2 runs with the frame binding at 2.0",
              len(calls) == 3
              and calls[2][1][-1] == ["frame", 2.0], calls)
        pop_callback()[0](0)  # frame-2 landing
        tex2 = emb.texture.pixels if emb.texture is not None else None
        check("anim: frame 2 texture differs from frame 1",
              tex2 is not None and tex2 != tex1
              and state["landed_frames"] == 2, state)
        check("anim: page keeps its heading and trailing text",
              any("Anim" in t for t in visual_texts(page))
              and any("trailing text" in t
                      for t in visual_texts(page)), visual_texts(page))
        check("anim: result text stays frame 0's (only the image moves)",
              any(t == "[b]1[/b]" for t in visual_texts(page)),
              visual_texts(page))

        # ---- 3. busy tick is skipped, never a second run ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = anim_run
        app._drain_page_gpu = anim_drain
        # (anim_run/anim_drain close over the section-2 lists; reset
        # them so the counts below are this section's.)
        calls = []
        drains = []
        page = render(app)
        run_workers()
        pop_callback()[0](0)  # frame-0 landing, loop started
        state = anim_state(page)
        pop_callback()[0](0)  # tick: frame 1 worker starts, busy
        check("busy: frame 1 worker started, busy set",
              state["busy"] is True and len(calls) == 1
              and len(_CaptureThread.targets) == 1,
              (len(calls), len(_CaptureThread.targets)))
        app._anim_tick(state)  # a second tick while frame 1 drains
        check("busy: second tick starts no run",
              len(calls) == 1 and len(_CaptureThread.targets) == 1,
              (len(calls), len(_CaptureThread.targets)))
        check("busy: second tick schedules a retry, not a run",
              len(_CaptureClock.callbacks) == 1, _CaptureClock.callbacks)
        run_workers()  # frame 1 runs; landing scheduled behind retry
        check("busy: frame 1 ran once the worker was driven",
              len(calls) == 2 and len(_CaptureThread.targets) == 0,
              len(calls))
        pop_callback()[0](0)  # the retry tick: still busy, skips again
        check("busy: retry tick while frame 1 lands starts no run",
              len(calls) == 2, len(calls))
        pop_callback()[0](0)  # the frame-1 landing
        check("busy: frame 1 still lands after the skipped ticks",
              state["landed_frames"] == 1 and state["busy"] is False,
              state)

        # ---- 4. loop stops when the page is left ----
        reset()
        app = make_app()
        calls = []
        drains = []
        app._run_page_script = anim_run
        app._drain_page_gpu = anim_drain
        page = render(app)
        run_workers()
        pop_callback()[0](0)  # frame-0 landing, loop started
        state = anim_state(page)
        app._site_current = (CID, "/other.mu")  # navigated away
        pop_callback()[0](0)  # the pending tick
        check("leave: tick after navigation starts no run",
              len(calls) == 1 and len(_CaptureThread.targets) == 0,
              (len(calls), len(_CaptureThread.targets)))
        check("leave: loop is stopped, no tick rescheduled",
              state["stopped"] is True
              and _CaptureClock.callbacks == [], _CaptureClock.callbacks)

        # ---- 4b. loop stops when the page is re-rendered ----
        reset()
        app = make_app()
        calls = []
        drains = []
        app._run_page_script = anim_run
        app._drain_page_gpu = anim_drain
        page = render(app)
        run_workers()
        pop_callback()[0](0)  # frame-0 landing, loop started
        state = anim_state(page)
        page.clear_widgets()  # re-render drops the old container
        pop_callback()[0](0)  # the pending tick
        check("re-render: tick after the embed leaves starts no run",
              len(calls) == 1 and state["stopped"] is True, calls)

        # ---- 5. CPU fallback still animates (fake drainer) ----
        reset()
        app = make_app()
        calls = []
        drains = []
        app._run_page_script = anim_run
        app._drain_page_gpu = anim_drain  # reports backend "cpu"
        page = render(app)
        run_workers()
        pop_callback()[0](0)
        state = anim_state(page)
        pop_callback()[0](0)
        run_workers()
        pop_callback()[0](0)
        check("cpu: frames advance on the CPU backend",
              state is not None and state["landed_frames"] == 1
              and len(drains) == 2, state)

        # ---- 5b. CPU fallback still animates (real drainer) ----
        # The real drainer in-harness falls back to CPU (the worker
        # runs inline on Kivy's GL thread); assert the frames still
        # advance and record which backend actually drained.
        reset()
        app = make_app()
        seen = []

        def real_drain_recorder(queue):
            res = kc.sHadesClientApp._drain_page_gpu(app, queue)
            seen.append(res.get("backend"))
            return res

        def cpu_run(source, bindings):
            frame = 0.0
            for name, value in bindings:
                if name == "frame":
                    frame = value
            return {"ok": 1.0, "result": 1.0, "out": [],
                    "gpu": [["gpu_open", 1.0],
                            ["gpu_upload_f32", 1.0,
                             [-0.75, 0.0, 3.0, 48.0, 0.0, 1.0]],
                            ["gpu_dispatch", 1.0, frame],
                            ["gpu_close", 1.0]]}

        app._run_page_script = cpu_run
        app._drain_page_gpu = real_drain_recorder
        page = render(app)
        run_workers()
        pop_callback()[0](0)
        state = anim_state(page)
        pop_callback()[0](0)
        run_workers()
        pop_callback()[0](0)
        emb = find_embed(page)
        check("cpu-real: a real-drained frame lands and swaps",
              state is not None and state["landed_frames"] == 1
              and emb is not None and emb.texture is not None
              and seen == ["cpu", "cpu"], seen)

        # ---- 6. a failing frame stops the loop with a note ----
        reset()
        app = make_app()
        calls = []

        def fail_on_frame_1(source, bindings):
            calls.append(bindings)
            frame = 0.0
            for name, value in bindings:
                if name == "frame":
                    frame = value
            if frame >= 1.0:
                return {"ok": 0.0, "result": "type error: boom",
                        "out": [], "gpu": []}
            return {"ok": 1.0, "result": 1.0, "out": [],
                    "gpu": [["gpu_open", 1.0]]}

        app._run_page_script = fail_on_frame_1
        app._drain_page_gpu = anim_drain
        page = render(app)
        run_workers()
        pop_callback()[0](0)  # frame-0 landing, loop started
        state = anim_state(page)
        pop_callback()[0](0)  # tick: frame 1 worker starts
        run_workers()  # frame 1 fails
        pop_callback()[0](0)  # frame-1 landing
        check("error: failing frame stops the loop",
              state["stopped"] is True and len(calls) == 2, calls)
        check("error: a visible note names the failure",
              any("animation stopped:" in t and "type error" in t
                  for t in visual_texts(page)), visual_texts(page))
        check("error: no tick scheduled after the stop",
              _CaptureClock.callbacks == [], _CaptureClock.callbacks)

        # ---- 7. anim block with no GPU image: no loop ----
        reset()
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 1.0, "out": [], "gpu": []}
        app._drain_page_gpu = lambda queue: (_ for _ in ()).throw(
            AssertionError("drainer must not run with an empty queue"))
        page = render(app)
        run_workers()
        pop_callback()[0](0)
        check("no-image: anim block without a GPU image stays one-shot",
              anim_state(page) is None
              and _CaptureClock.callbacks == [], visual_texts(page))

        # ---- 8. adaptive delay arithmetic ----
        check("delay: a quick frame holds the target cadence",
              abs(app_site._anim_next_delay(0.1, 0.05) - 0.05) < 1e-9,
              app_site._anim_next_delay(0.1, 0.05))
        check("delay: an overrunning frame floors at 20 ms",
              app_site._anim_next_delay(0.1, 0.3) == 0.02,
              app_site._anim_next_delay(0.1, 0.3))
        check("delay: a frame exactly on target still floors",
              app_site._anim_next_delay(0.1, 0.1) == 0.02,
              app_site._anim_next_delay(0.1, 0.1))

        # ---- 9. preview never animates ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = lambda s, b: calls.append((s, b)) or {
            "ok": 1.0, "result": 1.0, "out": [], "gpu": []}
        page = render(app, media_ok=False)
        check("preview: anim source renders as code, never runs",
              any("gpu_dispatch" in t for t in visual_texts(page))
              and calls == []
              and _CaptureThread.targets == [], visual_texts(page))
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread
        sanguis_script.scripting_available = real_avail

    print("PASS anim frames: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
