#!/usr/bin/env python3
"""Polish (g): composer upload-progress bar -- a thin visual strip
above the composer while an attachment uploads.

Runs the real app under Xvfb and asserts STATE, never wall-clock
timing or frame counts. The send flow runs on its worker thread
against stub guild APIs; the test polls Clock-driven state (a bounded
number of poll ticks, never a sleep-then-hope) and moves on only when
the observed state says so:

- bar hidden initially (height 0 / opacity 0, pct 0)
- a progress report sets pct and shows the bar, fill fraction == pct/100
- completion clears it (inactive, pct 0, hidden, helper text cleared)
- the legacy inline fallback clears it too
- a post_v1 raise after a good blob clears it too
- a full failure (blob + legacy post both raise) clears it too
- a second upload re-shows it from 0
- a theme change re-tints the fill / track live
- the fill is never driven by an animation (progress is state)

Deterministic under xvfb-run. Harness modelled on
tests/test_polish_e_fancy_buttons.py (real app run) and
~/workspace/sHades-fix/polish/probe_group2.py (live probe driver).
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.animation import Animation  # noqa: E402
from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402

from kivymd.uix.progressindicator import MDLinearProgressIndicator  # noqa: E402

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def _close(a, b, tol=0.002):
    try:
        return (len(a) == len(b)
                and all(abs(float(x) - float(y)) <= tol
                        for x, y in zip(a, b)))
    except Exception:
        return False


def _value_anims(widget):
    """Plain Animations currently driving `widget`'s value.

    The indicator's own determinate animation would show up here;
    progress must be state, so this must stay empty. Compound
    animations carry bare True flags, not property dicts -- skip them.
    """
    out = []
    for anim in list(Animation._instances):
        entry = getattr(anim, "_widgets", {}).get(widget.uid)
        if (isinstance(entry, dict)
                and "value" in entry.get("properties", {})):
            out.append(anim)
    return out


class StagedApi(object):
    """blobs_put reports 0% then stop_at%, then blocks until the test
    releases it -- the bar's mid-flight state is observed, not timed."""

    def __init__(self, stop_at, msg_id):
        self.stop_at = stop_at
        self.msg_id = msg_id
        self.proceed = threading.Event()
        self.entered = threading.Event()
        self.post_called = False

    def avatar_get(self, h):
        return {"ok": False}

    def blobs_put(self, name, mime, blob, on_progress=None):
        self.entered.set()
        if on_progress is not None:
            on_progress(0, 100)
            if self.stop_at:
                on_progress(self.stop_at, 100)
        self.proceed.wait(30)
        return {"ok": True, "blob_id": "b-staged"}

    def post_v1(self, cid, text, attachments=None):
        self.post_called = True
        return {"ok": True, "msg_id": self.msg_id,
                "attachment": {"path": "/srv/f.bin"}}


class LegacyApi(object):
    """Pre-/api/v1 guild: blobs_put raises, the legacy inline post
    carries the attach."""

    def __init__(self):
        self.post_called = False

    def avatar_get(self, h):
        return {"ok": False}

    def blobs_put(self, name, mime, blob, on_progress=None):
        raise RuntimeError("no v1 blobs here")

    def post(self, cid, text, attachments=None):
        self.post_called = True
        return {"ok": True, "msg_id": "m-legacy",
                "attachment": {"path": "/srv/f.bin"}}


class PostV1FailApi(object):
    """Blob uploads fine, then post_v1 raises -- the blob-ok early
    return inside _send_with_attachment."""

    def __init__(self):
        self.post_v1_called = False

    def avatar_get(self, h):
        return {"ok": False}

    def blobs_put(self, name, mime, blob, on_progress=None):
        if on_progress is not None:
            on_progress(100, 100)
        return {"ok": True, "blob_id": "b-ok"}

    def post_v1(self, cid, text, attachments=None):
        self.post_v1_called = True
        raise RuntimeError("link dropped after blob ok")


class FailingApi(object):
    """Both paths fail: blobs_put raises and the legacy post raises."""

    def __init__(self):
        self.post_called = False

    def avatar_get(self, h):
        return {"ok": False}

    def blobs_put(self, name, mime, blob, on_progress=None):
        raise RuntimeError("upload boom")

    def post(self, cid, text, attachments=None):
        self.post_called = True
        raise RuntimeError("post boom")


class _TestApp(kc.sHadesClientApp):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
        self.my_hash = "a" * 32
        # History/cache writes stay out of the test: the send tail
        # calls these, and the real ones touch disk.
        self._add_to_history = lambda m: None
        self._queue_cache = lambda m: None
        self._save_history = lambda: None

    def on_start(self):
        self.phase = "boot"
        self.polls = 0
        self.settle = 0
        self.seen_mid = False
        self.rows0 = 0
        Clock.schedule_once(self.step_boot, 0.5)
        Clock.schedule_once(self.step_timeout, 60)
        Clock.schedule_interval(self.poll, 0.02)

    def step_boot(self, dt):
        self.root.current = "main"
        Clock.schedule_once(self.step_ids, 0.5)

    def step_ids(self, dt):
        main = self.root.get_screen("main")
        self.main = main
        self.bar = main.ids.upload_progress_bar
        self.fill = main.ids.upload_progress_fill
        self.input = main.ids.msg_input
        self.grid = main.ids.message_grid
        self.rows0 = len(self.grid.children)
        self.phase = "initial"

    def step_timeout(self, dt):
        check("all phases finished inside the poll bound", False,
              "stuck in phase %r" % (self.phase,))
        self.finish()

    def finish(self):
        if getattr(self, "_finished", False):
            return
        self._finished = True
        print()
        print("%d passed, %d failed" % (len(PASS), len(FAIL)))
        self.stop()

    # -- poll: one phase at a time, each gated on observed state -----
    def poll(self, dt):
        self.polls += 1
        if self.polls > 2500:
            check("all phases finished inside the poll bound", False,
                  "stuck in phase %r after 2500 polls" % (self.phase,))
            self.finish()
            return
        fn = getattr(self, "phase_" + self.phase, None)
        if fn is not None:
            fn()

    def _settled(self, cond, need=3):
        """True once `cond` has held for `need` consecutive polls --
        the Clock callbacks the worker scheduled have all flushed."""
        if cond():
            self.settle += 1
        else:
            self.settle = 0
        return self.settle >= need

    def _cleared(self):
        return (not self.upload_active and self.upload_pct == 0
                and self.bar.height == 0 and self.bar.opacity == 0
                and self.input.helper_text == "")

    def phase_initial(self):
        check("bar hidden initially (inactive, pct 0)",
              not self.upload_active and self.upload_pct == 0,
              "active=%r pct=%r" % (self.upload_active, self.upload_pct))
        check("bar height is 0 initially", self.bar.height == 0,
              repr(self.bar.height))
        check("bar opacity is 0 initially", self.bar.opacity == 0,
              repr(self.bar.opacity))
        check("fill value is ~0 initially", abs(self.fill.value) < 0.05,
              repr(self.fill.value))
        check("fill is a determinate MDLinearProgressIndicator",
              self.fill.type == "determinate"
              and isinstance(self.fill, MDLinearProgressIndicator),
              "%r %r" % (self.fill.type, type(self.fill).__name__))
        self.phase = "direct"

    def phase_direct(self):
        self._set_upload_progress(50)
        check("progress report shows the bar",
              self.upload_active and self.bar.opacity == 1,
              "active=%r opacity=%r"
              % (self.upload_active, self.bar.opacity))
        check("bar height is 4dp while active",
              abs(self.bar.height - 4) < 0.5, repr(self.bar.height))
        check("fill value jumps to the reported pct (50)",
              self.fill.value == 50, repr(self.fill.value))
        check("fill fraction == pct/100 (0.5)",
              abs(self.fill.value_normalized - 0.5) < 0.002,
              repr(self.fill.value_normalized))
        check("no animation drives the fill value",
              _value_anims(self.fill) == [],
              repr(_value_anims(self.fill)))
        check("progress report leaves the helper text alone",
              self.input.helper_text == "", repr(self.input.helper_text))
        self._set_upload_progress(150)
        check("pct above 100 clamps to 100", self.upload_pct == 100,
              repr(self.upload_pct))
        self._set_upload_progress(-5)
        check("pct below 0 clamps to 0", self.upload_pct == 0,
              repr(self.upload_pct))
        check("bar stays shown at clamped 0",
              self.upload_active and self.bar.opacity == 1,
              "active=%r" % (self.upload_active,))
        self._attach_composer_stage("")
        check("clearing the stage hides the bar",
              self._cleared(),
              "active=%r pct=%r h=%r"
              % (self.upload_active, self.upload_pct, self.bar.height))
        self.phase = "tint"

    def phase_tint(self):
        self.theme_colors["primary"] = (1, 0, 0, 1)
        self.theme_colors["panel_darker"] = (0, 1, 0, 1)
        self.phase = "tint_check"

    def phase_tint_check(self):
        check("fill re-tints live with theme primary",
              _close(self.fill.indicator_color, (1, 0, 0, 1)),
              repr(self.fill.indicator_color))
        check("track re-tints live with theme panel_darker",
              _close(self.fill.track_color, (0, 1, 0, 1)),
              repr(self.fill.track_color))
        self.theme_colors["primary"] = kc.DEFAULT_THEME_COLORS["primary"]
        self.theme_colors["panel_darker"] = (
            kc.DEFAULT_THEME_COLORS["panel_darker"])
        self.phase = "flow1_start"

    def phase_flow1_start(self):
        self.api1 = StagedApi(stop_at=50, msg_id="m-staged-1")
        self.guild_api = self.api1
        self._send_with_attachment("hello flow", "f.bin",
                                   "application/octet-stream", b"x" * 1000,
                                   kc.ATT_SERVER)
        self.phase = "flow1_mid"

    def phase_flow1_mid(self):
        if self.upload_pct == 50 and self.upload_active:
            check("staged upload shows the bar at 50%",
                  self.bar.opacity == 1
                  and abs(self.bar.height - 4) < 0.5,
                  "opacity=%r h=%r"
                  % (self.bar.opacity, self.bar.height))
            check("staged upload fill fraction is 0.5",
                  abs(self.fill.value_normalized - 0.5) < 0.002,
                  repr(self.fill.value_normalized))
            check("helper text still reports the pct",
                  "(50%)" in self.input.helper_text,
                  repr(self.input.helper_text))
            check("no animation drives the fill mid-flight",
                  _value_anims(self.fill) == [],
                  repr(_value_anims(self.fill)))
            self.api1.proceed.set()
            self.phase = "flow1_done"
            self.settle = 0

    def phase_flow1_done(self):
        if self._settled(lambda: (self._cleared()
                                  and len(self.grid.children)
                                  == self.rows0 + 1)):
            check("completion clears the bar (inactive, pct 0, hidden)",
                  True)
            check("completion clears the helper text",
                  self.input.helper_text == "",
                  repr(self.input.helper_text))
            check("completion clears the input", self.input.text == "",
                  repr(self.input.text))
            check("completion renders the message",
                  len(self.grid.children) == self.rows0 + 1,
                  "%d rows, was %d"
                  % (len(self.grid.children), self.rows0))
            self.rows0 = len(self.grid.children)
            self.phase = "flow2_start"

    def phase_flow2_start(self):
        self.api2 = StagedApi(stop_at=0, msg_id="m-staged-2")
        self.guild_api = self.api2
        self._send_with_attachment("second upload", "g.bin",
                                   "application/octet-stream", b"y" * 500,
                                   kc.ATT_SERVER)
        self.phase = "flow2_zero"

    def phase_flow2_zero(self):
        if self.upload_active and self.upload_pct == 0:
            check("second upload re-shows the bar from 0",
                  self.bar.opacity == 1
                  and abs(self.bar.height - 4) < 0.5,
                  "opacity=%r h=%r pct=%r"
                  % (self.bar.opacity, self.bar.height, self.upload_pct))
            check("second upload fill is ~0 at 0%",
                  abs(self.fill.value_normalized) < 0.01,
                  repr(self.fill.value_normalized))
            self.api2.proceed.set()
            self.phase = "flow2_done"
            self.settle = 0

    def phase_flow2_done(self):
        if self._settled(lambda: (self._cleared()
                                  and len(self.grid.children)
                                  == self.rows0 + 1)):
            check("second upload clears on completion", True)
            check("second upload renders its message",
                  len(self.grid.children) == self.rows0 + 1,
                  "%d rows, was %d"
                  % (len(self.grid.children), self.rows0))
            self.rows0 = len(self.grid.children)
            self.phase = "legacy_start"

    def phase_legacy_start(self):
        self.api3 = LegacyApi()
        self.guild_api = self.api3
        self._send_with_attachment("legacy attach", "h.bin",
                                   "application/octet-stream", b"z" * 500,
                                   kc.ATT_SERVER)
        self.phase = "legacy_done"

    def phase_legacy_done(self):
        if self.api3.post_called and self._settled(
                lambda: (self._cleared()
                         and len(self.grid.children) == self.rows0 + 1)):
            check("legacy inline fallback clears the bar", True)
            check("legacy fallback renders its message",
                  len(self.grid.children) == self.rows0 + 1,
                  "%d rows, was %d"
                  % (len(self.grid.children), self.rows0))
            self.rows0 = len(self.grid.children)
            self.phase = "postv1_start"

    def phase_postv1_start(self):
        self.api4 = PostV1FailApi()
        self.guild_api = self.api4
        self._send_with_attachment("blob ok post fails", "i.bin",
                                   "application/octet-stream", b"w" * 500,
                                   kc.ATT_SERVER)
        self.phase = "postv1_done"

    def phase_postv1_done(self):
        if self.api4.post_v1_called and self._settled(
                lambda: (self._cleared()
                         and len(self.grid.children) == self.rows0)):
            check("post_v1 raise after blob ok clears the bar", True)
            check("post_v1 raise renders no message",
                  len(self.grid.children) == self.rows0,
                  "%d rows, was %d"
                  % (len(self.grid.children), self.rows0))
            self.phase = "fail_start"

    def phase_fail_start(self):
        self.api5 = FailingApi()
        self.guild_api = self.api5
        self._send_with_attachment("both fail", "j.bin",
                                   "application/octet-stream", b"v" * 500,
                                   kc.ATT_SERVER)
        self.phase = "fail_done"

    def phase_fail_done(self):
        if self.api5.post_called and self._settled(
                lambda: (self._cleared()
                         and len(self.grid.children) == self.rows0)):
            check("full failure clears the bar", True)
            check("full failure renders no message",
                  len(self.grid.children) == self.rows0,
                  "%d rows, was %d"
                  % (len(self.grid.children), self.rows0))
            check("upload flag is down after failure",
                  self._up_active is False, repr(self._up_active))
            self.finish()


def main():
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0
    Window.size = (1280, 720)
    _TestApp().run()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
