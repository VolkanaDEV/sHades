#!/usr/bin/env python3
"""Script-emitted micron markup tests — fake runner + real runner.

Covers the `=sanguis markup block: the parser marks it, the
renderer re-parses the run's printed lines as micron source on a
successful landing (headings, runs, links, media), the result
widget stays, and any nested script block the output tries to
emit is neutralized to literal code and never executes (the
runner is called exactly once). A failing script keeps the error
block + prior out lines as code; preview never executes; an
anim+markup combination renders as code, never runs; an emitted
#!bg= directive cannot restyle the page.

Threading/Clock are captured, not run: the test drives work() and
the scheduled landing by hand. Run under xvfb-run (real Kivy
widgets).
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
MARKUP_SRC = ("cell main() {\n  print(\"> Hello\");\n"
              "  output 1.0;\n}")
MARKUP_PAGE = ("> Page\n\n`=sanguis markup\n" + MARKUP_SRC + "\n`=\n\n"
               "trailing text")


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
                 "_script_result_widgets", "_markup_result_widgets",
                 "_add_site_media", "_script_bindings",
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


def render(app, page_text=MARKUP_PAGE, media_ok=True):
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
        # ---- 1. parser: markup opener / plain / anim / combos ----
        blocks = micron.micron_parse(MARKUP_PAGE)
        script = [b for b in blocks if b.get("type") == "script"]
        check("markup opener parses as a script block with markup",
              len(script) == 1 and script[0].get("markup") is True
              and script[0].get("text") == MARKUP_SRC
              and "anim" not in script[0], script)
        plain = micron.micron_parse("`=sanguis\nprint(1)\n`=\n")
        check("plain opener carries no markup key",
              plain[0].get("type") == "script"
              and "markup" not in plain[0], plain)
        anim = micron.micron_parse("`=sanguis anim\nprint(1)\n`=\n")
        check("anim opener carries no markup key",
              anim[0].get("type") == "script"
              and anim[0].get("anim") is True
              and "markup" not in anim[0], anim)
        both = micron.micron_parse("`=sanguis anim markup\nx\n`=\n")
        check("anim+markup opener is not a script block",
              not any(b.get("type") == "script" for b in both)
              and any("`=sanguis anim markup" in
                      (r.get("text") or "")
                      for b in both for r in b.get("runs", [])), both)
        both2 = micron.micron_parse("`=sanguis markup anim\nx\n`=\n")
        check("markup+anim opener is not a script block either",
              not any(b.get("type") == "script" for b in both2), both2)
        unter = micron.micron_parse("`=sanguis markup\nprint(1)\n")
        check("unterminated markup block is a code block, never run",
              len(unter) == 1 and unter[0].get("type") == "code", unter)
        unknown = micron.micron_parse("`=sanguis foo\nprint(1)\n`=\n")
        check("unknown suffix stays not-a-script-block",
              not any(b.get("type") == "script" for b in unknown),
              unknown)

        # ---- 1b. micron_parse_emitted neutralization ----
        emit = micron.micron_parse_emitted(
            "> Hi\n\n`=sanguis\nprint(1)\n`=\n\nbye")
        check("emitted nested script becomes a code block",
              len([b for b in emit if b.get("type") == "script"]) == 0
              and any(b.get("type") == "code"
                      and b.get("text", "").startswith("`=sanguis\n")
                      and "print(1)" in b.get("text", "")
                      and b.get("text", "").endswith("\n`=")
                      for b in emit)
              and any(b.get("type") == "heading" for b in emit)
              and any(b.get("type") == "para" for b in emit), emit)
        emit_anim = micron.micron_parse_emitted(
            "`=sanguis anim\nprint(1)\n`=")
        check("emitted nested anim block neutralized with its suffix",
              len(emit_anim) == 1
              and emit_anim[0].get("type") == "code"
              and emit_anim[0].get("text", "").startswith(
                  "`=sanguis anim\n"), emit_anim)
        emit_markup = micron.micron_parse_emitted(
            "`=sanguis markup\nprint(1)\n`=")
        check("emitted nested markup block neutralized with its suffix",
              len(emit_markup) == 1
              and emit_markup[0].get("type") == "code"
              and emit_markup[0].get("text", "").startswith(
                  "`=sanguis markup\n"), emit_markup)
        emit_plain = micron.micron_parse_emitted(
            "> H\npara `[l`/other.mu] end")
        check("emitted headings/links pass through untouched",
              any(b.get("type") == "heading" for b in emit_plain)
              and any(b.get("type") == "para"
                      and any(r.get("link") == "/other.mu"
                              for r in b.get("runs", []))
                      for b in emit_plain), emit_plain)

        # ---- 2. markup landing: out lines become markup ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        calls = []

        def fake_run(source, bindings):
            calls.append((source, bindings))
            return {"ok": 1.0, "result": 1.0,
                    "out": ["> Hello from a script",
                            "a `!bold!` line and `*italic*` too",
                            "`[go home`/index.mu]"],
                    "gpu": []}

        app = make_app()
        app._run_page_script = fake_run
        page = render(app)
        check("markup placeholder inserted while the run is pending",
              any("running script" in t for t in visual_texts(page)),
              visual_texts(page))
        drive_all()
        texts = visual_texts(page)
        check("markup landing keeps the result widget",
              any(t == "[b]1[/b]" for t in texts), texts)
        check("emitted heading renders as a heading, not code",
              "Hello from a script" in texts, texts)
        check("emitted bold/italic runs render as markup",
              any(t == "a [b]bold[/b] line and [i]italic[/i] too"
                  for t in texts), texts)
        check("emitted link renders as a ref",
              any(t == "[ref=/index.mu]go home[/ref]" for t in texts),
              texts)
        check("no out line survives as literal code text",
              not any("`!bold!`" in t or "`*italic*`" in t
                      for t in texts), texts)
        check("markup run calls the runner exactly once",
              len(calls) == 1 and calls[0][0] == MARKUP_SRC, calls)
        check("page survives (heading + trailing text)",
              any("Page" in t for t in texts)
              and any("trailing text" in t for t in texts), texts)

        # ---- 3. nested script in the output is neutralized ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        calls = []
        app = make_app()

        def nested_run(source, bindings):
            calls.append(source)
            return {"ok": 1.0, "result": 9.0,
                    "out": ["before",
                            "`=sanguis",
                            "cell main() { output 9.0; }",
                            "`=",
                            "after"],
                    "gpu": []}

        app._run_page_script = nested_run
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("nested script fence renders as literal code",
              any("`=sanguis" in t for t in texts)
              and any("cell main() { output 9.0; }" in t
                      for t in texts), texts)
        check("surrounding emitted lines still render as paras",
              "before" in texts and "after" in texts, texts)
        check("nested script never re-executes the runner",
              len(calls) == 1, calls)
        check("nested script spawns no second worker thread",
              len(_CaptureThread.targets) == 1,
              len(_CaptureThread.targets))
        check("no second placeholder appears for the nested block",
              not any("running script" in t for t in texts), texts)

        # ---- 3b. nested markup block in the output ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        calls = []
        app = make_app()
        app._run_page_script = lambda s, b: calls.append(s) or {
            "ok": 1.0, "result": 1.0,
            "out": ["`=sanguis markup", "print(1)", "`="], "gpu": []}
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("nested markup fence renders as literal code",
              any("`=sanguis markup" in t for t in texts), texts)
        check("nested markup block never runs either",
              len(calls) == 1, calls)

        # ---- 4. failing script: error + prior out as code ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 0.0, "result": "type error: + needs numbers",
            "out": ["> Would-be heading", "before the boom"], "gpu": []}
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("failing markup script lands an error block",
              any("script error:" in t and "type error" in t
                  for t in texts), texts)
        check("failing script's out stays code, never markup",
              "> Would-be heading" in texts
              and not any(t == "Would-be heading" for t in texts),
              texts)

        # ---- 5. empty output renders nothing extra ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 5.0, "out": [], "gpu": []}
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("empty markup output lands just the result",
              any(t == "[b]5[/b]" for t in texts)
              and len(texts) == 3, texts)

        # ---- 6. editor preview never executes ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        calls = []
        app._run_page_script = lambda s, b: calls.append((s, b)) or {
            "ok": 1.0, "result": 1.0, "out": [], "gpu": []}
        page = render(app, media_ok=False)
        texts = visual_texts(page)
        check("preview shows the markup source as code",
              any("cell main()" in t for t in texts), texts)
        check("preview starts no worker thread",
              _CaptureThread.targets == [], _CaptureThread.targets)
        check("preview never calls the runner", calls == [], calls)

        # ---- 7. anim+markup direct call renders as code ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        calls = []
        app._run_page_script = lambda s, b: calls.append((s, b)) or {
            "ok": 1.0, "result": 1.0, "out": [], "gpu": []}
        from app_site import SitePage
        page = SitePage()
        app._add_site_script(page, CID, MARKUP_SRC, anim=True,
                             markup=True)
        texts = visual_texts(page)
        check("anim+markup renders the source as code, never runs",
              any("cell main()" in t for t in texts)
              and calls == []
              and _CaptureThread.targets == [], texts)

        # ---- 8. emitted media line renders an embed ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 1.0,
            "out": ["`[pic`/file/x.png]"], "gpu": []}
        page = render(app)
        drive_all()
        embeds = [w for w in page.children
                  if isinstance(w, app_site.MessageEmbed)]
        check("emitted media line renders an inline embed",
              len(embeds) == 1, len(embeds))

        # ---- 9. emitted #!bg= directive cannot restyle the page ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 1.0,
            "out": ["#!bg=223", "plain line"], "gpu": []}
        page = render(app)
        drive_all()
        texts = visual_texts(page)
        check("emitted directive leaves the page background alone",
              list(page.bg_color) == [0, 0, 0, 0]
              and "plain line" in texts,
              (list(page.bg_color), texts))

        # ---- 10. stale landing is a no-op (markup path) ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = lambda s, b: {
            "ok": 1.0, "result": 1.0,
            "out": ["> Late heading"], "gpu": []}
        page = render(app)
        app._site_current = (CID, "/other.mu")
        drive_all()
        texts = visual_texts(page)
        check("navigated-away markup landing is a no-op",
              any("running script" in t for t in texts)
              and "Late heading" not in texts, texts)

        # ---- 11. real runner: a script that prints micron ----
        _CaptureThread.targets = []
        _CaptureClock.callbacks = []
        app = make_app()
        app._run_page_script = types.MethodType(
            kc.sHadesClientApp._run_page_script, app)
        real_page = ("> Real\n\n`=sanguis markup\ncell main() {\n"
                     "  print(\"> Hello from a script\");\n"
                     "  print(\"a `!bold!` line\");\n"
                     "  output 1.0;\n}\n`=\n")
        page = render(app, page_text=real_page)
        drive_all()
        texts = visual_texts(page)
        check("real runner: emitted heading lands",
              "Hello from a script" in texts, texts)
        check("real runner: emitted bold run lands as markup",
              any(t == "a [b]bold[/b] line" for t in texts), texts)
        check("real runner: result widget lands",
              any(t == "[b]1[/b]" for t in texts), texts)
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread
        sanguis_script.scripting_available = real_avail

    print("PASS script markup: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
