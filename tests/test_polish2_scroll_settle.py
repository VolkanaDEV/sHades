#!/usr/bin/env python3
"""Polish 2 (scroll settle): the message list must land at the bottom
in the same tick as the fetch render, not a Clock hop later.

Measured live under Xvfb (logs/scroll/before): a 40-message fetch
rendered in one ~0.8s main-thread tick (apply 0.38s + label textures
+ first draw), and the scroll -- a separate Clock.schedule_once(0.1)
-- could only fire on the tick AFTER that frame, so the rows appeared
at the top (scroll_y=1) for a beat and then jumped to the bottom:
fetch -> bottom took 0.98s, rows visible at 0.83s, scroll at 0.91s.
The fix scrolls synchronously at the end of the render, guarded by
a follow-bottom snapshot taken before the render, so the first frame
that shows the rows already shows them at the bottom -- and a reader
scrolled up to older messages is never yanked.

Asserts STATE, never wall-clock timing. The Clock is a recording
stub: scheduled callbacks are recorded and never run unless pumped,
so a scroll that only happens via a later schedule is distinguishable
from a synchronous scroll (scroll_y is already 0 with the clock
unpumped, and nothing new was scheduled).

- fresh bulk fetch: scroll_y is 0 with the clock unpumped, rows
  rendered, no scroll scheduled afterwards
- following fetch (already at bottom): stays at 0
- reading fetch (scrolled up, rows present): scroll_y untouched,
  _scroll_to_bottom never called, rows still rendered
- prepend fetch: position restored, never yanked to the bottom
- _load_history_ui: same synchronous scroll when fresh, same
  no-yank guard when reading
- real ScrollView: once scroll_y is 0, growing the grid keeps it
  pinned at 0 (so no delayed re-assert schedule is needed)
- static: the old 0.1s scroll schedules are gone from kivy_client.py

Fake-app harness in the sitenav / polish-b style: the real
_fetch_messages / _load_history_ui / _follow_snapshot /
_scroll_to_bottom_if_following methods are bound onto a
SimpleNamespace, the worker thread runs inline, and the Clock only
records.
"""
import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []
CID = "c" * 64
MY_HASH = "b" * 64


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


class _RecordingClock(object):
    """schedule_once records (fn, dt) and never runs anything."""

    def __init__(self):
        self.recorded = []

    def schedule_once(self, fn, dt=0):
        self.recorded.append((fn, dt))

    def schedule_interval(self, fn, dt=0):
        self.recorded.append((fn, dt))
        return None

    def pump_one(self):
        fn, _dt = self.recorded.pop(0)
        fn(0)

    def pump_all(self):
        while self.recorded:
            self.pump_one()


class _InlineThread(object):
    def __init__(self, target=None, daemon=False, **k):
        self._target = target

    def start(self):
        self._target()


class FakeGrid(object):
    def __init__(self):
        self.children = []
        self.height = 1000

    def clear_widgets(self):
        self.children = []

    def add_widget(self, w):
        self.children.append(w)


class FakeScroll(object):
    def __init__(self):
        self.scroll_y = 1.0
        self.height = 500
        self.bound = {}

    def bind(self, **kw):
        self.bound.update(kw)


class FakeApi(object):
    def __init__(self):
        self.n = 0

    def messages(self, cid, since_id=None, since_ts=None, before=None,
                 limit=50):
        self.n += 1
        tag = "old" if before else "fetch"
        return {"ok": True,
                "messages": [msg("%s-%d-a" % (tag, self.n)),
                             msg("%s-%d-b" % (tag, self.n))],
                "updates": []}

    def receipts(self, cid, newest):
        return {"ok": True}

    def avatar_get(self, author):
        return {"ok": False}


def msg(msg_id, author="", username="alice"):
    return {"msg_id": msg_id, "author_hash": author, "username": username,
            "content": "hello " + msg_id, "ts": 1700000000,
            "reactions": [], "attachments": []}


def make_app():
    grid = FakeGrid()
    scroll = FakeScroll()
    msg_input = types.SimpleNamespace(text="")
    main = types.SimpleNamespace(
        _current_channel="general",
        ids=types.SimpleNamespace(message_grid=grid, scroll=scroll,
                                  msg_input=msg_input))

    def get_screen(name):
        if name == "main":
            return main
        raise KeyError(name)

    root = types.SimpleNamespace(get_screen=get_screen)
    app = types.SimpleNamespace(
        root=root, guild_api=FakeApi(), my_hash=MY_HASH,
        username="tester", my_avatar_small="dummy.png",
        _msg_widgets={}, _members={}, _older={},
        _channel_kinds={"general": "chat"},
        _channel_cid=lambda ch: ch,
        _cache_guild=lambda: None,
        _cache_write_lock=threading.Lock(),
        _scroll_bound=False,
        _history_msgs=[],
        _remove_skeleton=lambda cid: None,
        _set_status=lambda text: None,
        _do_update=lambda row, content, flag: None,
        _apply_update=lambda u: None,
        _offline_path=lambda: "/tmp/nope-polish2-offline",
        scroll_calls=[])
    app._load_history = lambda: list(app._history_msgs)

    def scroll_to_bottom():
        app.scroll_calls.append("bottom")
        scroll.scroll_y = 0

    app._scroll_to_bottom = scroll_to_bottom
    app._record_member = types.MethodType(
        kc.sHadesClientApp._record_member, app)
    app._set_row_reactions = types.MethodType(
        kc.sHadesClientApp._set_row_reactions, app)
    app._regroup_messages = kc.sHadesClientApp._regroup_messages
    app._follow_snapshot = types.MethodType(
        kc.sHadesClientApp._follow_snapshot, app)
    app._scroll_to_bottom_if_following = types.MethodType(
        kc.sHadesClientApp._scroll_to_bottom_if_following, app)
    app._fetch_messages = types.MethodType(
        kc.sHadesClientApp._fetch_messages, app)
    app._load_history_ui = types.MethodType(
        kc.sHadesClientApp._load_history_ui, app)
    app.rendered = []
    real_render = types.MethodType(kc.sHadesClientApp._render_message, app)

    def recording_render(grid, m, animate=False):
        app.rendered.append((m.get("msg_id"), animate))
        return real_render(grid, m, animate=animate)

    app._render_message = recording_render
    app.grid = grid
    app.scroll = scroll
    app.main = main
    return app


def _row_count(app):
    return len(app.grid.children)


def main():
    real_clock, real_thread = kc.Clock, kc.threading.Thread
    clock = _RecordingClock()
    kc.Clock = clock
    kc.threading.Thread = _InlineThread
    try:
        # --- static: the old delayed scroll hop is gone --------------
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "kivy_client.py"), encoding="utf-8").read()
        check("no 0.1s _scroll_to_bottom schedule remains",
              "lambda dt: self._scroll_to_bottom(), 0.1" not in src)
        check("follow-guarded synchronous scroll exists",
              "_scroll_to_bottom_if_following" in src)

        # --- fresh bulk fetch: scroll lands synchronously ------------
        app = make_app()
        app._fetch_messages("general")
        check("bulk fetch schedules only the apply callback",
              len(clock.recorded) == 1, clock.recorded)
        clock.pump_one()  # apply() runs: renders + scrolls, same tick
        check("fresh bulk fetch lands at the bottom with the clock "
              "unpumped", app.scroll.scroll_y == 0,
              repr(app.scroll.scroll_y))
        check("fresh bulk fetch rendered both rows",
              _row_count(app) == 2, _row_count(app))
        check("fresh bulk fetch scheduled no delayed scroll",
              len(clock.recorded) == 0, clock.recorded)
        check("fresh bulk fetch scrolled exactly once",
              app.scroll_calls == ["bottom"], app.scroll_calls)

        # --- following fetch: already at the bottom, stays there -----
        app = make_app()
        app._render_message(app.grid, msg("m-pre-0"))
        app._render_message(app.grid, msg("m-pre-1"))
        app.scroll.scroll_y = 0.0
        app.scroll_calls = []
        app._fetch_messages("general")
        clock.pump_one()
        check("following fetch stays at the bottom",
              app.scroll.scroll_y == 0, repr(app.scroll.scroll_y))
        check("following fetch rendered the new rows",
              _row_count(app) == 4, _row_count(app))
        check("following fetch scrolled exactly once",
              app.scroll_calls == ["bottom"], app.scroll_calls)

        # --- reading fetch: scrolled up, never yanked ----------------
        app = make_app()
        app._render_message(app.grid, msg("m-pre-0"))
        app._render_message(app.grid, msg("m-pre-1"))
        app.scroll.scroll_y = 0.6
        app.scroll_calls = []
        app._fetch_messages("general")
        clock.pump_one()
        check("reading fetch leaves scroll_y untouched",
              app.scroll.scroll_y == 0.6, repr(app.scroll.scroll_y))
        check("reading fetch rendered the new rows anyway",
              _row_count(app) == 4, _row_count(app))
        check("reading fetch never called _scroll_to_bottom",
              app.scroll_calls == [], app.scroll_calls)

        # --- prepend fetch: position restored, not bottom ------------
        app = make_app()
        app._render_message(app.grid, msg("m-pre-0"))
        app._render_message(app.grid, msg("m-pre-1"))
        app.scroll.scroll_y = 0.6
        app.scroll_calls = []
        app._fetch_messages("general", before="m-pre-0", prepend=True)
        clock.pump_one()  # apply(): prepends + schedules restore
        check("prepend fetch schedules only the restore callback",
              len(clock.recorded) == 1, clock.recorded)
        clock.pump_one()  # restore() runs
        check("prepend fetch restores the reading position",
              app.scroll.scroll_y == 0.6, repr(app.scroll.scroll_y))
        check("prepend fetch added the older rows",
              _row_count(app) == 4, _row_count(app))
        check("prepend fetch never called _scroll_to_bottom",
              app.scroll_calls == [], app.scroll_calls)

        # --- _load_history_ui: fresh load scrolls synchronously ------
        app = make_app()
        app._history_msgs = [msg("m-hist-0"), msg("m-hist-1"),
                             msg("m-hist-2")]
        app._load_history_ui()
        check("history load lands at the bottom with the clock "
              "unpumped", app.scroll.scroll_y == 0,
              repr(app.scroll.scroll_y))
        check("history load rendered the history rows",
              _row_count(app) == 3, _row_count(app))
        check("history load scheduled no delayed scroll",
              len(clock.recorded) == 0, clock.recorded)

        # --- _load_history_ui: reader is not yanked ------------------
        app = make_app()
        app._render_message(app.grid, msg("m-pre-0"))
        app._render_message(app.grid, msg("m-pre-1"))
        app.scroll.scroll_y = 0.6
        app.scroll_calls = []
        app._history_msgs = [msg("m-hist-0")]
        app._load_history_ui()
        check("history load leaves a reader's scroll_y untouched",
              app.scroll.scroll_y == 0.6, repr(app.scroll.scroll_y))
        check("history load still rendered the history row",
              _row_count(app) == 3, _row_count(app))

        # --- real ScrollView: bottom pins as the grid grows ----------
        from kivy.clock import Clock as RealClock  # noqa: E402
        from kivy.core.window import Window  # noqa: E402
        from kivy.uix.gridlayout import GridLayout  # noqa: E402
        from kivy.uix.label import Label  # noqa: E402
        from kivy.uix.scrollview import ScrollView  # noqa: E402
        Window.size = (400, 300)
        sv = ScrollView(size=(400, 300), size_hint=(None, None))
        rgrid = GridLayout(cols=1, size_hint_y=None, spacing=2)
        rgrid.bind(minimum_height=rgrid.setter("height"))
        sv.add_widget(rgrid)

        def _add_rows(n, tag):
            for i in range(n):
                rgrid.add_widget(Label(text="%s-%d" % (tag, i),
                                       size_hint_y=None, height=50))

        _add_rows(10, "a")
        for _ in range(5):
            RealClock.tick()
        sv.scroll_y = 0
        for _ in range(5):
            RealClock.tick()
        check("real ScrollView reaches scroll_y 0",
              sv.scroll_y == 0, repr(sv.scroll_y))
        _add_rows(10, "b")
        for _ in range(5):
            RealClock.tick()
        check("real ScrollView stays pinned at 0 as the grid grows",
              sv.scroll_y == 0, repr(sv.scroll_y))
    finally:
        kc.Clock = real_clock
        kc.threading.Thread = real_thread

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
