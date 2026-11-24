#!/usr/bin/env python3
"""Interactive micron script blocks — tap to re-run tests.

Covers the `=sanguis interact opener: the parser flags the block,
the first run carries event="load" with event_x/event_y/state 0.0,
and tapping the result label re-runs the script with event="tap",
the tap position in the block's local coordinates, and the stored
numeric state (the previous run's numeric result; a non-numeric
result leaves the stored state unchanged -- sticky). The landed
widgets live in a dedicated box that each event landing rebuilds
in place; the block stays tappable after a failing run; a busy tap
is dropped; preview never executes; any two of anim/markup/interact
renders as code; a script fence printed by an event run stays code
and the runner is called exactly once per tap; GPU re-renders
texture-only on tap and audio drains + auto-plays on every tap;
media bindings fetched at first render are carried into event runs.

Threading/Clock are captured, not run: the test drives work() and
the scheduled landings by hand, and dispatches fake touches on the
result label. Run under xvfb-run (real Kivy widgets).
"""
import base64
import hashlib
import io
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
SRC = ("cell main() {\n  let n = state + 1.0;\n"
       "  print(\"count\");\n  output n;\n}")
PAGE = "> Page\n\n`=sanguis interact\n" + SRC + "\n`=\n\ntrailing text"
FAKE_WAV = b"RIFFxxxxWAVEfake"


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


class FakeAPI(object):
    """att_get only: probe (length=1) returns total + 1 byte, a
    full read returns all bytes. No fetch_file: _add_site_media's
    worker catches that and the page embed stays a placeholder."""

    def __init__(self, blobs):
        self.blobs = blobs
        self.calls = []

    def att_get(self, path, offset=0, length=None):
        self.calls.append((path, offset, length))
        data, mode = self.blobs.get(path, (b"", "missing"))
        if mode == "missing":
            return {"ok": 0, "error": "attachment unavailable"}
        total = len(data)
        if length is not None:
            chunk = data[offset:offset + length]
        else:
            chunk = data[offset:]
        return {"ok": 1, "path": path, "total": total, "offset": offset,
                "eof": offset + len(chunk) >= total,
                "data_b64": base64.b64encode(chunk).decode("ascii")}


def visual_texts(container):
    """Widget texts in visual (top-to-bottom) order."""
    out = []
    for w in reversed(container.children):
        t = getattr(w, "text", None)
        if isinstance(t, str):
            out.append(t)
    return out


def make_app(api=None):
    main = types.SimpleNamespace(
        _current_channel="wiki",
        ids=types.SimpleNamespace(message_grid=None, scroll=None))

    def get_screen(name):
        if name == "main":
            return main
        raise KeyError(name)

    root = types.SimpleNamespace(get_screen=get_screen)
    app = types.SimpleNamespace(
        root=root, guild_api=api,
        _site_current=(CID, "/index.mu"), _site_stack=[],
        _site_forward=[], _site_theme={}, _site_pending_scroll=1.0,
        _msg_widgets=[], _skeleton_cid=None, _site_src_text=None,
        status=[], saved_blobs=[], _audio_media=None, play_calls=[])
    for name in ("_micron_blocks_into", "_micron_label",
                 "_add_site_script", "_land_site_script",
                 "_script_result_widgets", "_markup_result_widgets",
                 "_script_audio_row", "_add_site_media",
                 "_script_bindings",
                 "_script_page_path", "_save_gpu_image",
                 "_save_audio_wav", "_run_page_script",
                 "_drain_page_gpu", "_drain_page_audio"):
        setattr(app, name, types.MethodType(
            getattr(kc.sHadesClientApp, name), app))
    app._hex6_to_rgba = kc.sHadesClientApp._hex6_to_rgba
    app._set_status = lambda text: app.status.append(text)
    app._channel_cid = lambda name: CID

    def _save(blob, name):
        app.saved_blobs.append((blob, name))
        if name == "script-audio.wav":
            return "/tmp/fake-audio.wav"
        return "/tmp/fake-gpu.png"

    app._save_attachment_blob = _save

    def fake_play(path):
        app.play_calls.append(path)
        media = types.SimpleNamespace(
            stop=lambda: None, release=lambda: None)
        app._audio_media = media
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


def drive_callbacks():
    """Run every scheduled callback, then clear the list."""
    for fn, _dt in list(_CaptureClock.callbacks):
        fn(0)
    _CaptureClock.callbacks = []


def drive_all():
    """Run every captured worker, then every scheduled landing."""
    run_workers()
    drive_callbacks()


def reset():
    _CaptureThread.targets = []
    _CaptureClock.callbacks = []


def find_interact_box(page):
    """The dedicated interact box (the child carrying the state)."""
    for w in page.children:
        if getattr(w, "_interact_state", None) is not None:
            return w
    return None


def interact_state(page):
    box = find_interact_box(page)
    return getattr(box, "_interact_state", None) if box else None


def box_texts(box):
    return visual_texts(box) if box is not None else []


def result_label(box):
    """The first visual widget in the box: the result/error label."""
    if box is None:
        return None
    widgets = list(reversed(box.children))
    return widgets[0] if widgets else None


def find_embed(box):
    if box is None:
        return None
    for w in box.children:
        if isinstance(w, app_site.MessageEmbed):
            return w
    return None


def find_audio_row(box):
    if box is None:
        return None
    for w in box.children:
        if getattr(w, "_is_script_audio_row", False):
            return w
    return None


def place_for_tap(page, box, label):
    """Give the box/label real geometry, then return the touch
    point for local (10, 10): the box sits at (5, 50) in the
    page, the label at (10, 100) in the box, so the label's
    offset chain up to the page is (15, 150) and the touch
    goes at (25, 160). This pins the anchor math: a naive
    touch.x - label.x would report (15, 60), not (10, 10)."""
    page.pos = (0, 0)
    box.pos = (5, 50)
    label.pos = (10, 100)
    label.size = (200, 20)
    return (25, 160)


def tap(label, x, y):
    """Dispatch a fake touch on the label; returns whether the
    dispatch reports the touch consumed."""
    touch = types.SimpleNamespace(x=x, y=y, pos=(x, y))
    return label.dispatch("on_touch_down", touch)


def tap_box(page, box):
    """Tap the box's current result label at local (10, 10);
    returns (dispatched, state)."""
    label = result_label(box)
    x, y = place_for_tap(page, box, label)
    return tap(label, x, y), interact_state(page)


def counter_run(calls, mode):
    """Fake runner: a counter script. The result is the state's
    binding + 1.0, so each run's result becomes the next run's
    stored state. mode keys: fail_tap (count of tap runs that
    fail), sticky (one tap at state 1.0 returns a string),
    fence (a tap's out lines carry a script fence), gpu/audio
    (every run returns that queue), media_digest (the result
    depends on the media binding being present)."""

    def fake_run(source, bindings):
        calls.append((source, list(bindings)))
        bd = {}
        for name, value in bindings:
            bd[name] = value
        event = bd.get("event")
        state = bd.get("state", 0.0)
        if mode.get("fail_tap", 0) > 0 and event == "tap":
            mode["fail_tap"] -= 1
            return {"ok": 0.0, "result": "type error: boom",
                    "out": ["before the boom"], "gpu": []}
        if mode.get("sticky") and event == "tap" and state == 1.0:
            mode["sticky"] = False
            return {"ok": 1.0, "result": "hello",
                    "out": ["not a number"], "gpu": []}
        if mode.get("media_digest"):
            digest = mode["media_digest"]
            if "media_" + digest in bd:
                result = state + 1.0
                out = ["has media"]
            else:
                return {"ok": 1.0, "result": 0.0,
                        "out": ["no media"], "gpu": []}
        else:
            result = state + 1.0
            out = ["count %d" % int(result)]
        if mode.get("fence") and event == "tap":
            out = ["`=sanguis", "cell main() { output 9.0; }", "`="]
        gpu = [["gpu_open", 1.0]] if mode.get("gpu") else []
        res = {"ok": 1.0, "result": result, "out": out, "gpu": gpu}
        if mode.get("audio"):
            res["audio"] = [["audio_note", 440.0, 0.5, "square", 0.5]]
        return res

    return fake_run


def main():
    real_clock, real_thread = app_site.Clock, app_site.threading.Thread
    real_avail = sanguis_script.scripting_available
    app_site.Clock = _CaptureClock
    app_site.threading.Thread = _CaptureThread
    sanguis_script.scripting_available = lambda: True
    try:
        # ---- 1. parser: interact opener / plain / anim / markup ----
        blocks = micron.micron_parse(PAGE)
        script = [b for b in blocks if b.get("type") == "script"]
        check("interact opener parses as a script block with interact",
              len(script) == 1 and script[0].get("interact") is True
              and script[0].get("text") == SRC
              and "anim" not in script[0]
              and "markup" not in script[0], script)
        plain = micron.micron_parse("`=sanguis\nprint(1)\n`=\n")
        check("plain opener carries no interact key",
              plain[0].get("type") == "script"
              and "interact" not in plain[0], plain)
        anim = micron.micron_parse("`=sanguis anim\nprint(1)\n`=\n")
        check("anim opener carries no interact key",
              anim[0].get("type") == "script"
              and anim[0].get("anim") is True
              and "interact" not in anim[0], anim)
        markup = micron.micron_parse("`=sanguis markup\nprint(1)\n`=\n")
        check("markup opener carries no interact key",
              markup[0].get("type") == "script"
              and markup[0].get("markup") is True
              and "interact" not in markup[0], markup)
        combos = ["`=sanguis anim interact",
                  "`=sanguis interact anim",
                  "`=sanguis markup interact",
                  "`=sanguis interact markup",
                  "`=sanguis anim markup",
                  "`=sanguis markup anim"]
        all_plain = True
        for opener in combos:
            got = micron.micron_parse(opener + "\nx\n`=\n")
            if any(b.get("type") == "script" for b in got):
                all_plain = False
        check("every two-suffix opener (all pairs, both orders) "
              "is not a script block",
              all_plain, combos)
        unter = micron.micron_parse("`=sanguis interact\nprint(1)\n")
        check("unterminated interact block is a code block, never run",
              len(unter) == 1 and unter[0].get("type") == "code", unter)
        unknown = micron.micron_parse("`=sanguis foo\nprint(1)\n`=\n")
        check("unknown suffix stays not-a-script-block",
              not any(b.get("type") == "script" for b in unknown),
              unknown)

        # ---- 1b. micron_parse_emitted neutralization ----
        emit = micron.micron_parse_emitted(
            "`=sanguis interact\nprint(1)\n`=")
        check("emitted nested interact block neutralized with its "
              "suffix verbatim",
              len(emit) == 1
              and emit[0].get("type") == "code"
              and emit[0].get("text", "").startswith(
                  "`=sanguis interact\n")
              and "print(1)" in emit[0].get("text", ""), emit)
        emit_anim = micron.micron_parse_emitted(
            "`=sanguis anim\nprint(1)\n`=")
        check("emitted nested anim block still neutralized",
              len(emit_anim) == 1
              and emit_anim[0].get("type") == "code"
              and emit_anim[0].get("text", "").startswith(
                  "`=sanguis anim\n"), emit_anim)
        emit_markup = micron.micron_parse_emitted(
            "`=sanguis markup\nprint(1)\n`=")
        check("emitted nested markup block still neutralized",
              len(emit_markup) == 1
              and emit_markup[0].get("type") == "code"
              and emit_markup[0].get("text", "").startswith(
                  "`=sanguis markup\n"), emit_markup)
        emit_plain = micron.micron_parse_emitted(
            "`=sanguis\nprint(1)\n`=")
        check("emitted nested plain block still neutralized",
              len(emit_plain) == 1
              and emit_plain[0].get("type") == "code"
              and emit_plain[0].get("text", "").startswith(
                  "`=sanguis\n"), emit_plain)

        # ---- 2. first landing: result + out + hint + tap target ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = counter_run(calls, {})
        page = render(app)
        check("interact placeholder inserted while the run is pending",
              any("running script" in t for t in visual_texts(page)),
              visual_texts(page))
        drive_all()
        box = find_interact_box(page)
        state = interact_state(page)
        check("first landing wraps the widgets in a dedicated box",
              box is not None and box.parent is page, box)
        check("first landing keeps the result widget + out lines",
              box is not None
              and "[b]1[/b]" in box_texts(box)
              and "count 1" in box_texts(box), box_texts(box))
        check("first landing appends the tap hint",
              box is not None
              and any("tap the result to interact" in t
                      for t in box_texts(box)), box_texts(box))
        check("first landing starts the interact state at the "
              "run's result (0.0 -> 1.0)",
              state is not None and state["state"] == 1.0
              and state["taps"] == 0 and state["busy"] is False
              and state["stopped"] is False, state)
        check("first landing binds the tap target to the result "
              "label",
              state is not None
              and state["result_widget"] is result_label(box)
              and result_label(box) is not None, state)
        check("first run carries event=load, event_x/y 0.0, "
              "state 0.0 (and no frame binding)",
              len(calls) == 1
              and calls[0][1][:3] == [["page", "/index.mu"],
                                      ["gpu_present", 1.0],
                                      ["channel", "wiki"]]
              and ["event", "load"] in calls[0][1]
              and ["event_x", 0.0] in calls[0][1]
              and ["event_y", 0.0] in calls[0][1]
              and ["state", 0.0] in calls[0][1]
              and not any(b[0] == "frame" for b in calls[0][1]),
              calls[0][1] if calls else calls)
        check("page survives (heading + trailing text)",
              any("Page" in t for t in visual_texts(page))
              and any("trailing text" in t
                      for t in visual_texts(page)), visual_texts(page))

        # ---- 3. one tap = one run; counter advances; coords ----
        dispatched, state = tap_box(page, box)
        check("tap on the result label reports the touch consumed",
              dispatched is True, dispatched)
        check("one tap spawns exactly one worker thread",
              len(_CaptureThread.targets) == 1,
              len(_CaptureThread.targets))
        run_workers()
        check("tap re-runs with event=tap, the anchor coords "
              "(10, 10), and the stored state 1.0",
              len(calls) == 2
              and ["event", "tap"] in calls[1][1]
              and ["event_x", 10.0] in calls[1][1]
              and ["event_y", 10.0] in calls[1][1]
              and ["state", 1.0] in calls[1][1], calls[1][1])
        drive_callbacks()
        box = find_interact_box(page)
        state = interact_state(page)
        check("tap landing rebuilds the box: result 2, hint kept",
              box is not None
              and "[b]2[/b]" in box_texts(box)
              and "count 2" in box_texts(box)
              and any("tap the result to interact" in t
                      for t in box_texts(box)), box_texts(box))
        check("state persists across the tap (now 2.0, taps 1)",
              state is not None and state["state"] == 2.0
              and state["taps"] == 1 and state["busy"] is False,
              state)
        dispatched, state = tap_box(page, box)
        run_workers()
        drive_callbacks()
        box = find_interact_box(page)
        state = interact_state(page)
        check("second tap advances the counter to 3",
              len(calls) == 3
              and ["state", 2.0] in calls[2][1]
              and "[b]3[/b]" in box_texts(box)
              and state is not None and state["state"] == 3.0
              and state["taps"] == 2, (calls, box_texts(box)))
        check("the re-bound tap target is the NEW result label",
              state is not None
              and state["result_widget"] is result_label(box),
              state)
        check("a tap outside the result label dispatches nothing",
              tap(result_label(box), 500, 500) is False
              and len(calls) == 3
              and len(_CaptureThread.targets) == 0,
              (len(calls), len(_CaptureThread.targets)))

        # ---- 4. state is sticky on a non-numeric result ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = counter_run(calls, {"sticky": True})
        page = render(app)
        drive_all()
        box = find_interact_box(page)
        tap_box(page, box)  # tap at state 1.0 -> "hello"
        run_workers()
        drive_callbacks()
        box = find_interact_box(page)
        state = interact_state(page)
        check("non-numeric tap result lands as the result text",
              "[b]hello[/b]" in box_texts(box), box_texts(box))
        check("non-numeric tap result leaves the stored state "
              "unchanged (still 1.0)",
              state is not None and state["state"] == 1.0, state)
        tap_box(page, box)  # next tap still sends state 1.0
        run_workers()
        check("the next tap still sends the last numeric state",
              len(calls) == 3
              and ["state", 1.0] in calls[2][1], calls[2][1])
        drive_callbacks()
        state = interact_state(page)
        check("counter resumes from the sticky state (result 2.0)",
              "[b]2[/b]" in box_texts(find_interact_box(page))
              and state is not None and state["state"] == 2.0, state)

        # ---- 5. state resets on re-render ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = counter_run(calls, {})
        page = render(app)
        drive_all()
        box = find_interact_box(page)
        tap_box(page, box)
        run_workers()
        drive_callbacks()
        check("pre-rerender state advanced to 2.0",
              interact_state(page)["state"] == 2.0,
              interact_state(page))
        page2 = render(app)  # same app, fresh render
        drive_all()
        check("re-render starts a fresh block at state 0.0",
              len(calls) == 3
              and ["event", "load"] in calls[2][1]
              and ["state", 0.0] in calls[2][1]
              and "[b]1[/b]" in box_texts(find_interact_box(page2))
              and interact_state(page2)["state"] == 1.0,
              (calls[2][1] if len(calls) > 2 else calls,
               box_texts(find_interact_box(page2))))

        # ---- 6. failing event run: error lands, block stays tappable ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = counter_run(calls, {"fail_tap": 1})
        page = render(app)
        drive_all()
        box = find_interact_box(page)
        tap_box(page, box)  # this tap fails
        run_workers()
        drive_callbacks()
        box = find_interact_box(page)
        state = interact_state(page)
        check("failing tap lands the error block + prior out",
              any("script error:" in t and "type error: boom" in t
                  for t in box_texts(box))
              and "before the boom" in box_texts(box),
              box_texts(box))
        check("failing tap leaves the stored state unchanged",
              state is not None and state["state"] == 1.0
              and state["busy"] is False, state)
        dispatched, state = tap_box(page, box)  # tap the ERROR label
        check("the error label is the new tap target",
              dispatched is True, dispatched)
        run_workers()
        drive_callbacks()
        check("the next tap after a failure still runs and lands",
              len(calls) == 3
              and "[b]2[/b]" in box_texts(find_interact_box(page)),
              (len(calls), box_texts(find_interact_box(page))))

        # ---- 7. busy tap is dropped ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = counter_run(calls, {})
        page = render(app)
        drive_all()
        box = find_interact_box(page)
        tap_box(page, box)  # tap 1: worker captured, not yet run
        check("busy: first tap spawns one worker, busy set",
              len(_CaptureThread.targets) == 1
              and interact_state(page)["busy"] is True,
              interact_state(page))
        dispatched, _st = tap_box(page, box)  # tap 2 while busy
        check("busy: second tap is dropped (not consumed, no run)",
              dispatched is False
              and len(_CaptureThread.targets) == 1
              and len(calls) == 1,
              (dispatched, len(_CaptureThread.targets), len(calls)))
        run_workers()
        drive_callbacks()
        check("busy: the first tap still lands once it runs",
              len(calls) == 2
              and interact_state(page)["busy"] is False
              and "[b]2[/b]" in box_texts(find_interact_box(page)),
              (len(calls), box_texts(find_interact_box(page))))

        # ---- 8. editor preview never executes ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = counter_run(calls, {})
        page = render(app, media_ok=False)
        texts = visual_texts(page)
        check("preview shows the interact source as code",
              any("cell main()" in t for t in texts), texts)
        check("preview starts no worker thread",
              _CaptureThread.targets == [], _CaptureThread.targets)
        check("preview never calls the runner", calls == [], calls)
        check("preview creates no interact box",
              find_interact_box(page) is None, texts)

        # ---- 9. anim+interact / markup+interact render as code ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = counter_run(calls, {})
        from app_site import SitePage
        page = SitePage()
        app._add_site_script(page, CID, SRC, anim=True, interact=True)
        check("anim+interact renders the source as code, never runs",
              any("cell main()" in t for t in visual_texts(page))
              and calls == []
              and _CaptureThread.targets == [], visual_texts(page))
        page = SitePage()
        app._add_site_script(page, CID, SRC, markup=True,
                             interact=True)
        check("markup+interact renders the source as code, never runs",
              any("cell main()" in t for t in visual_texts(page))
              and calls == []
              and _CaptureThread.targets == [], visual_texts(page))

        # ---- 10. a script fence printed by an event run stays code ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = counter_run(calls, {"fence": True})
        page = render(app)
        drive_all()
        box = find_interact_box(page)
        tap_box(page, box)
        run_workers()
        drive_callbacks()
        texts = box_texts(find_interact_box(page))
        check("fence printed by an event run lands as literal code",
              any("`=sanguis" in t for t in texts)
              and any("cell main() { output 9.0; }" in t
                      for t in texts), texts)
        check("the emitted fence never re-executes the runner",
              len(calls) == 2, calls)
        check("the emitted fence spawns no extra worker thread",
              len(_CaptureThread.targets) == 0,
              len(_CaptureThread.targets))

        # ---- 11. stale event landing is a no-op ----
        reset()
        app = make_app()
        calls = []
        app._run_page_script = counter_run(calls, {})
        page = render(app)
        drive_all()
        box = find_interact_box(page)
        tap_box(page, box)
        run_workers()
        app._site_current = (CID, "/other.mu")  # navigated away
        drive_callbacks()
        check("navigated-away event landing leaves the box alone",
              "[b]1[/b]" in box_texts(find_interact_box(page))
              and "[b]2[/b]" not in box_texts(find_interact_box(page)),
              box_texts(find_interact_box(page)))
        check("stale landing still clears busy",
              interact_state(page)["busy"] is False,
              interact_state(page))

        # ---- 12. GPU re-renders on tap, texture-only ----
        reset()
        app = make_app()
        calls = []
        drains = []
        from PIL import Image
        app._run_page_script = counter_run(calls, {"gpu": True})

        def gpu_drain(queue):
            drains.append(list(queue))
            i = len(drains) - 1
            colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
            return {"image": Image.new(
                        "RGB", (4, 3), color=colors[i % len(colors)]),
                    "notes": ["GPU backend: CPU"],
                    "handled": ["gpu_open"], "unhandled": [],
                    "backend": "cpu"}

        app._drain_page_gpu = gpu_drain
        page = render(app)
        drive_all()
        box = find_interact_box(page)
        emb0 = find_embed(box)
        tex0 = emb0.texture.pixels if emb0 is not None \
            and emb0.texture is not None else None
        check("first-run embed lands texture-only (no source)",
              emb0 is not None and not emb0.source
              and emb0.texture is not None
              and getattr(emb0, "_anim_source", None)
              == "/tmp/fake-gpu.png",
              (emb0.source if emb0 is not None else None,
               getattr(emb0, "_anim_source", None)
               if emb0 is not None else None))
        tap_box(page, box)
        run_workers()
        drive_callbacks()
        box = find_interact_box(page)
        emb1 = find_embed(box)
        tex1 = emb1.texture.pixels if emb1 is not None \
            and emb1.texture is not None else None
        check("tap drains the GPU queue again (one drain per run)",
              len(drains) == 2, len(drains))
        check("tap swaps in the new run's texture",
              tex1 is not None and tex0 is not None
              and tex1 != tex0, (tex0 is not None, tex1 is not None))
        check("tap embed stays texture-only with the file behind it",
              emb1 is not None and not emb1.source
              and getattr(emb1, "_anim_source", None)
              == "/tmp/fake-gpu.png",
              getattr(emb1, "_anim_source", None)
              if emb1 is not None else None)

        # ---- 13. audio drains on an event run, auto-plays again ----
        reset()
        app = make_app()
        calls = []
        audio_drains = []
        app._run_page_script = counter_run(calls, {"audio": True})
        app._drain_page_audio = lambda queue: (
            audio_drains.append(list(queue)) or {
                "wav": FAKE_WAV, "duration": 0.5, "count": 1,
                "handled": ["audio_note"], "unhandled": [],
                "notes": []})
        page = render(app)
        drive_all()
        box = find_interact_box(page)
        check("first landing drains audio and auto-plays once",
              len(audio_drains) == 1
              and app.play_calls == ["/tmp/fake-audio.wav"]
              and find_audio_row(box) is not None,
              (audio_drains, app.play_calls))
        tap_box(page, box)
        run_workers()
        drive_callbacks()
        box = find_interact_box(page)
        check("event run drains the audio queue again",
              len(audio_drains) == 2, len(audio_drains))
        check("event landing auto-plays the new audio",
              app.play_calls == ["/tmp/fake-audio.wav",
                                 "/tmp/fake-audio.wav"],
              app.play_calls)
        check("event landing rebuilds the stop row",
              find_audio_row(box) is not None
              and find_audio_row(box)._audio_started is True,
              find_audio_row(box))

        # ---- 14. media bindings are carried into event runs ----
        reset()
        from PIL import Image as _PILImage
        buf = io.BytesIO()
        _PILImage.new("RGB", (4, 3), color=(9, 8, 7)).save(
            buf, format="PNG")
        png = buf.getvalue()
        digest = hashlib.sha256(png).hexdigest()[:32]
        target = "/file/attachments/%s.png" % digest
        api = FakeAPI({target: (png, "ok")})
        app = make_app(api)
        calls = []
        app._run_page_script = counter_run(
            calls, {"media_digest": digest})
        media_page = ("`[demo`%s]\n\n`=sanguis interact\n" % target
                      + SRC + "\n`=\n")
        page = render(app, page_text=media_page)
        drive_all()
        box = find_interact_box(page)
        check("first run fetches the page blob once (probe + read)",
              api.calls == [(target, 0, 1), (target, 0, None)],
              api.calls)
        check("first run passes the media binding to the runner",
              len(calls) == 1
              and any(b[0] == "media_" + digest
                      for b in calls[0][1]), calls[0][1] if calls else [])
        check("script reads the page media on the first run",
              "has media" in box_texts(box), box_texts(box))
        tap_box(page, box)
        run_workers()
        check("event run never re-fetches (att_get count unchanged)",
              api.calls == [(target, 0, 1), (target, 0, None)],
              api.calls)
        check("event run carries the fetched media binding",
              len(calls) == 2
              and any(b[0] == "media_" + digest
                      for b in calls[1][1]), calls[1][1] if len(calls) > 1 else [])
        drive_callbacks()
        check("script still reads the page media on the tap",
              "has media" in box_texts(find_interact_box(page)),
              box_texts(find_interact_box(page)))

        # ---- 15. real runner: event/state bindings end to end ----
        reset()
        app = make_app()
        app._run_page_script = types.MethodType(
            kc.sHadesClientApp._run_page_script, app)
        real_src = ("cell main() {\n"
                    "  gate event == \"tap\" then {\n"
                    "    output state + 10.0;\n  }\n"
                    "  output state + 1.0;\n}")
        real_page = ("> Real\n\n`=sanguis interact\n" + real_src
                     + "\n`=\n")
        page = render(app, page_text=real_page)
        drive_all()
        box = find_interact_box(page)
        check("real runner: first run (event=load) lands 1.0",
              box is not None and "[b]1[/b]" in box_texts(box),
              box_texts(box))
        check("real runner: stored state after the first run is 1.0",
              interact_state(page) is not None
              and interact_state(page)["state"] == 1.0,
              interact_state(page))
        dispatched, _st = tap_box(page, box)
        check("real runner: tap dispatches", dispatched is True,
              dispatched)
        run_workers()
        drive_callbacks()
        check("real runner: tap branch (event=tap) lands 11.0",
              "[b]11[/b]" in box_texts(find_interact_box(page)),
              box_texts(find_interact_box(page)))
        check("real runner: stored state after the tap is 11.0",
              interact_state(page) is not None
              and interact_state(page)["state"] == 11.0,
              interact_state(page))
    finally:
        app_site.Clock = real_clock
        app_site.threading.Thread = real_thread
        sanguis_script.scripting_available = real_avail

    print("PASS script interact: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
