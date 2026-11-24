#!/usr/bin/env python3
"""Site script rendering tests — fake runner + fake drainer.

Covers _micron_blocks_into's "script" branch: the placeholder is
inserted, the run happens off the main thread, and the landing
replaces the placeholder with the result + printed lines. Error
blocks show the error string AND lines printed before the error. A
stale landing (navigated away, re-rendered, token claimed) is a
no-op. Editor preview (media_ok=False) never executes. An
unavailable platform renders a notice, never a crash.

Threading/Clock are captured, not run: the test drives work() and
the scheduled landing by hand, so the stale-page interleavings can
be staged exactly. Run under xvfb-run (real Kivy widgets).
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
# Parsed script text is the literal lines joined with "\n": the line
# before the closer carries no trailing newline (same as code blocks).
EXPECTED_SRC = ("cell main() {\n  let n = 6.0;\n"
                "  print(\"hello from a micron page\");\n"
                "  output n * 7.0;\n}")
SCRIPT_SRC = EXPECTED_SRC + "\n"
PAGE = "> Demo\n\n`=sanguis\n" + SCRIPT_SRC + "`=\n\ntrailing text"


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
    """Clock stand-in: records scheduled callbacks, runs none."""
    callbacks = []

    @staticmethod
    def schedule_once(fn, dt=0):
        _CaptureClock.callbacks.append(fn)


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
                 "_run_page_script", "_drain_page_gpu"):
        setattr(app, name, types.MethodType(
            getattr(kc.sHadesClientApp, name), app))
    app._hex6_to_rgba = kc.sHadesClientApp._hex6_to_rgba
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID
    app._save_attachment_blob = lambda blob, name: (
        app.saved_blobs.append((blob, name)) or "/tmp/fake-gpu.png")
    return app


def render(app, page_text=PAGE, media_ok=True):
    from app_site import SitePage
    page = SitePage()
    app._micron_blocks_into(page, micron.micron_parse(page_text),
                            CID, media_ok=media_ok)
    return page


def drive_all():
    """Run every captured worker, then every scheduled landing."""
    for target in list(_CaptureThread.targets):
        if target is not None:
            target()
    for cb in list(_CaptureClock.callbacks):
        cb(0)


def main():
    real_clock, real_thread = app_site.Clock, app_site.threading.Thread
    real_avail = sanguis_script.scripting_available
    app_site.Clock = _CaptureClock
    app_site.threading.Thread = _CaptureThread
    sanguis_script.scripting_available = lambda: True
    try:
        # ---- 1. placeholder -> landed result + out lines ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        calls = []

        def fake_run(source, bindings):
            calls.append((source, bindings))
            return {"ok": 1.0, "result": 42.0,
                    "out": ["one", "two"], "gpu": []}

        app = make_app()
        app._run_page_script = fake_run
        app._drain_page_gpu = lambda queue: (_ for _ in ()).throw(
            AssertionError("drainer must not run with an empty queue"))
        page = render(app)
        texts = visual_texts(page)
        check("placeholder inserted while the run is pending",
              any("running script" in t for t in texts), texts)
        check("runner not called before the worker runs", calls == [], calls)
        check("heading + trailing text render immediately",
              any("Demo" in t for t in texts)
              and any("trailing text" in t for t in texts), texts)
        _CaptureThread.targets[0]()
        check("worker calls the runner with the script source",
              len(calls) == 1 and calls[0][0] == EXPECTED_SRC, calls)
        check("worker injects page + gpu_present + channel bindings",
              calls[0][1] == [["page", "/index.mu"],
                              ["gpu_present", 1.0],
                              ["channel", "wiki"]], calls[0][1])
        check("landing is scheduled, not run inline",
              len(_CaptureClock.callbacks) == 1,
              len(_CaptureClock.callbacks))
        _CaptureClock.callbacks[0](0)
        texts = visual_texts(page)
        check("placeholder replaced by the landed result",
              not any("running script" in t for t in texts)
              and any(t == "[b]42[/b]" for t in texts), texts)
        check("printed lines land after the result",
              texts.index("[b]42[/b]") < texts.index("one")
              < texts.index("two"), texts)

        # ---- 2. error block: error string + prior out lines ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 0.0, "result": "type error: + needs numbers",
            "out": ["before the boom"], "gpu": []}
        app._drain_page_gpu = lambda queue: (_ for _ in ()).throw(
            AssertionError("drainer must not run on a failed script"))
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("error block shows the error string",
              any("script error:" in t and "type error" in t
                  for t in texts), texts)
        check("error block still shows lines printed before the error",
              "before the boom" in texts, texts)
        check("page survives a bad script (heading + trailing text)",
              any("Demo" in t for t in texts)
              and any("trailing text" in t for t in texts), texts)

        # ---- 3a. stale landing: navigated away ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 42.0, "out": [], "gpu": []}
        page = render(app)
        app._site_current = (CID, "/other.mu")
        drive_all()
        texts = visual_texts(page)
        check("navigated-away landing is a no-op",
              any("running script" in t for t in texts)
              and not any(t == "[b]42[/b]" for t in texts), texts)

        # ---- 3b. stale landing: placeholder re-rendered away ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 42.0, "out": [], "gpu": []}
        page = render(app)
        page.clear_widgets()
        drive_all()
        check("re-rendered landing writes nothing into the new page",
              visual_texts(page) == [], visual_texts(page))

        # ---- 3c. stale landing: per-block token claimed ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 42.0, "out": [], "gpu": []}
        page = render(app)
        placeholder = [w for w in page.children
                       if "running script" in (w.text or "")][0]
        placeholder._script_token = object()
        drive_all()
        texts = visual_texts(page)
        check("token-claimed landing is a no-op",
              any("running script" in t for t in texts)
              and not any(t == "[b]42[/b]" for t in texts), texts)

        # ---- 4. editor preview never executes ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        calls = []
        app._run_page_script = lambda s, b: calls.append((s, b)) or {
            "ok": 1.0, "result": 1.0, "out": [], "gpu": []}
        page = render(app, media_ok=False)
        texts = visual_texts(page)
        check("preview shows the script source as code",
              any("cell main()" in t for t in texts), texts)
        check("preview starts no worker thread",
              _CaptureThread.targets == [], _CaptureThread.targets)
        check("preview never calls the runner", calls == [], calls)
        check("preview schedules no landing",
              _CaptureClock.callbacks == [], _CaptureClock.callbacks)

        # ---- 5. unavailable platform renders the notice ----
        sanguis_script.scripting_available = lambda: False
        try:
            _CaptureThread.targets = []
            _CaptureClock.callbacks = []
            app = make_app()
            calls = []
            app._run_page_script = lambda s, b: calls.append((s, b)) or {
                "ok": 1.0, "result": 1.0, "out": [], "gpu": []}
            page = render(app)
            texts = visual_texts(page)
            check("unavailable platform renders the notice",
                  any("scripting unavailable on this platform" in t
                      for t in texts), texts)
            check("unavailable platform starts no worker",
                  _CaptureThread.targets == [], _CaptureThread.targets)
            check("unavailable platform never calls the runner",
                  calls == [], calls)
        finally:
            sanguis_script.scripting_available = lambda: True

        # ---- 6. GPU drain: notes land with the result ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        drains = []

        def fake_drain(queue):
            drains.append(list(queue))
            return {"image": None, "notes": ["drew 3 commands"],
                    "handled": ["gpu_open"], "unhandled": []}

        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 1.0, "out": [],
            "gpu": [["gpu_open", 1.0], ["gpu_dispatch", 1.0, 2.0]]}
        app._drain_page_gpu = fake_drain
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("drainer receives the script's gpu queue",
              drains == [[["gpu_open", 1.0],
                          ["gpu_dispatch", 1.0, 2.0]]], drains)
        check("drain notes land with the result",
              any(t == "[b]1[/b]" for t in texts)
              and any("drew 3 commands" in t for t in texts), texts)

        # ---- 6b. drainer raising keeps the script result ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()

        def boom(queue):
            raise RuntimeError("no GPU here")

        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 7.0, "out": [], "gpu": [["gpu_open"]]}
        app._drain_page_gpu = boom
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("drainer failure lands a visible note, result kept",
              any(t == "[b]7[/b]" for t in texts)
              and any("GPU output unavailable: no GPU here" in t
                      for t in texts), texts)

        # ---- 6c. missing gpu_drain module -> same visible note ----
        # Simulate the module being absent by parking None in
        # sys.modules (a from-import then raises ImportError).
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 7.0, "out": [], "gpu": [["gpu_open"]]}
        # Bind the REAL _drain_page_gpu (lazy import of gpu_drain)
        # instead of the fake.
        app._drain_page_gpu = types.MethodType(
            kc.sHadesClientApp._drain_page_gpu, app)
        page = render(app)
        real_mod = sys.modules.get("gpu_drain")
        sys.modules["gpu_drain"] = None
        try:
            drive_all()
        finally:
            if real_mod is None:
                sys.modules.pop("gpu_drain", None)
            else:
                sys.modules["gpu_drain"] = real_mod
        texts = visual_texts(page)
        check("missing gpu_drain module lands a visible note",
              any(t == "[b]7[/b]" for t in texts)
              and any("GPU output unavailable:" in t for t in texts),
              texts)

        # ---- 6e. real drainer integration (skips if not present) ----
        # A well-formed queue through the REAL _drain_page_gpu: the
        # Mandelbrot frame lands as an embed with the result.
        try:
            import gpu_drain  # noqa: F401
            have_drainer = True
        except ImportError:
            have_drainer = False
        if not have_drainer:
            print("SKIP  real drainer integration  | "
                  "gpu_drain module not present")
        else:
            _CaptureThread.targets = []
            _CaptureClock.callbacks = []
            app = make_app()
            app._run_page_script = lambda s, b: {
                "ok": 1.0, "result": 1.0, "out": [],
                "gpu": [["gpu_open", 1.0], ["gpu_dispatch", 1.0, 0.0]]}
            app._drain_page_gpu = types.MethodType(
                kc.sHadesClientApp._drain_page_gpu, app)
            page = render(app)
            drive_all()
            texts = visual_texts(page)
            embeds = [w for w in page.children
                      if isinstance(w, app_site.MessageEmbed)]
            check("real drainer: frame lands as an embed with result",
                  len(embeds) == 1
                  and embeds[0].source == "/tmp/fake-gpu.png"
                  and any(t == "[b]1[/b]" for t in texts), texts)

        # ---- 6d. drained image lands as an embed ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        from PIL import Image
        img = Image.new("RGB", (4, 3), color=(255, 0, 0))
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 3.0, "out": [],
            "gpu": [["gpu_open", 1.0]]}
        app._drain_page_gpu = lambda queue: {
            "image": img, "notes": [], "handled": ["gpu_open"],
            "unhandled": []}
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        embeds = [w for w in page.children
                  if isinstance(w, app_site.MessageEmbed)]
        check("drained image lands as an embed",
              len(embeds) == 1 and embeds[0].source == "/tmp/fake-gpu.png"
              and any(t == "[b]3[/b]" for t in texts), texts)
        check("image saved as PNG bytes via _save_attachment_blob",
              len(app.saved_blobs) == 1
              and app.saved_blobs[0][0][:8] == b"\x89PNG\r\n\x1a\n"
              and app.saved_blobs[0][1] == "script-gpu.png",
              [len(b) for b, _n in app.saved_blobs])

        # ---- 7. runner raising still lands an error block ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()

        def raise_run(source, bindings):
            raise OSError("bch vanished")

        app._run_page_script = raise_run
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("runner raising lands an error block, page intact",
              any("runner failed: bch vanished" in t for t in texts)
              and any("trailing text" in t for t in texts), texts)

        # ---- 8. bindings without a channel name omit it ----
        app = make_app()
        app.root = types.SimpleNamespace(
            get_screen=lambda name: (_ for _ in ()).throw(
                KeyError(name)))
        check("no channel name available -> no channel binding",
              app._script_bindings(CID) == [
                  ["page", "/index.mu"], ["gpu_present", 1.0]],
              app._script_bindings(CID))
        check("page path comes from _site_current",
              app._script_page_path(CID) == "/index.mu")
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread
        sanguis_script.scripting_available = real_avail

    print("PASS site script render: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
