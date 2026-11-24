#!/usr/bin/env python3
"""Polish (b): live message rows fade in; bulk history does not.

Asserts STATE, never wall-clock timing or frame counts. The 0.42s
reveal animation is driven deterministically: the running Kivy
Animation is advanced by explicit _update(dt) calls, so no real time
has to elapse.

- _render_message(animate=True): row starts at opacity 0, passes
  through a strictly partial opacity mid-fade, and lands at opacity 1
- _render_message() / animate=False: row is opacity 1 at once, and no
  opacity animation is running for it
- REDUCED_MOTION on + animate=True: row is opacity 1 at once, no
  animation started
- re-rendering the same msg_id dedupes: no second row, no second
  animation
- _fetch_messages: delta (since_id / since_ts) renders animate=True,
  initial bulk and prepend render animate=False
- _show_cached_channel (bulk offline render) renders animate=False
- _show_notice (console notice) renders animate=True
- send_message and _send_with_attachment (optimistic own-send) render
  animate=True

Fake-app harness in the sitenav tests' style: the real
_render_message / _fetch_messages / send_message methods are bound
onto a SimpleNamespace, Clock and Thread are stubbed so scheduled
work runs inline, and the reveal Animation is pumped by hand.
"""
import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_menus  # noqa: E402
import kivy_client as kc  # noqa: E402
import ui_fx  # noqa: E402
from kivy.animation import Animation  # noqa: E402

PASS, FAIL = [], []
CID = "c" * 64
MY_HASH = "b" * 64


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


class _NowClock(object):
    @staticmethod
    def schedule_once(fn, dt=0):
        fn(0)


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


class FakeApi(object):
    def __init__(self):
        self.calls = []
        self.n = 0

    def messages(self, cid, since_id=None, since_ts=None, before=None,
                 limit=50):
        self.calls.append({"since_id": since_id, "since_ts": since_ts,
                           "before": before, "prepend": before is not None})
        self.n += 1
        return {"ok": True,
                "messages": [msg("m-fetch-%d" % self.n)],
                "updates": []}

    def receipts(self, cid, newest):
        return {"ok": True}

    def avatar_get(self, author):
        return {"ok": False}

    def post(self, cid, text, attachments=None):
        self.n += 1
        return {"ok": True, "msg_id": "m-post-%d" % self.n}

    def post_v1(self, cid, text, attachments=None):
        self.n += 1
        return {"ok": True, "msg_id": "m-postv1-%d" % self.n}

    def blobs_put(self, name, mime, blob, on_progress=None):
        return {"ok": True, "blob_id": "blob-1"}


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
        _channel_kind=lambda cid: "chat",
        _cache_guild=lambda: None,
        _cache_write_lock=threading.Lock(),
        _pending_attachment=None, _up_active=False,
        _pending_reply=None,
        _stream_cid="general", status=[],
        _remove_skeleton=lambda cid: None,
        _scroll_to_bottom=lambda: None,
        _set_status=lambda text: None,
        _attach_composer_stage=lambda text: None,
        _add_to_history=lambda m: None,
        _queue_cache=lambda m: None,
        _save_history=lambda: None,
        _save_attachment_blob=lambda blob, name: "/tmp/nope-polish-test",
        _offline_path=lambda: "/tmp/nope-polish-offline",
        _do_update=lambda row, content, flag: None,
        _apply_update=lambda u: None)
    app._record_member = types.MethodType(
        kc.sHadesClientApp._record_member, app)
    app._set_row_reactions = types.MethodType(
        kc.sHadesClientApp._set_row_reactions, app)
    app._regroup_messages = kc.sHadesClientApp._regroup_messages
    app._fetch_messages = types.MethodType(
        kc.sHadesClientApp._fetch_messages, app)
    app._show_cached_channel = types.MethodType(
        kc.sHadesClientApp._show_cached_channel, app)
    app.send_message = types.MethodType(
        kc.sHadesClientApp.send_message, app)
    app._send_with_attachment = types.MethodType(
        kc.sHadesClientApp._send_with_attachment, app)
    app._show_notice = types.MethodType(
        app_menus.MenusMixin._show_notice, app)
    # Recording wrapper around the real renderer: every caller is
    # observed, the real row is still built.
    app.rendered = []
    real_render = types.MethodType(kc.sHadesClientApp._render_message, app)

    def recording_render(grid, m, animate=False):
        app.rendered.append((m.get("msg_id"), animate))
        return real_render(grid, m, animate=animate)

    app._render_message = recording_render
    app.grid = grid
    app.main = main
    return app


def _anim_for(row):
    for anim in list(Animation._instances):
        if row.uid in getattr(anim, "_widgets", {}):
            return anim
    return None


def _step(row, dt):
    """Advance the row's running opacity animation by dt seconds."""
    anim = _anim_for(row)
    if anim is None:
        return False
    anim._update(dt)
    return True


def _pump_to_end(row):
    for _ in range(10):
        if _anim_for(row) is None:
            break
        _step(row, 0.15)
    return row.opacity


def _row_for(app, msg_id):
    return app._msg_widgets.get(msg_id)


def main():
    real_clock, real_thread = kc.Clock, kc.threading.Thread
    real_menu_clock = app_menus.Clock
    real_recent = kc.offline_store.recent
    kc.Clock = _NowClock
    kc.threading.Thread = _InlineThread
    app_menus.Clock = _NowClock
    try:
        app = make_app()
        grid = app.grid

        # --- core: live render fades, bulk render does not ---------------
        app._render_message(grid, msg("m-live"), animate=True)
        row = _row_for(app, "m-live")
        check("live render returns a row", row is not None)
        check("live row starts at opacity 0", row.opacity == 0,
              repr(row.opacity))
        check("live row has a running opacity animation",
              _anim_for(row) is not None)
        _step(row, 0.21)  # first update arms the clock; opacity still 0
        check("live row still 0 after the arming update", row.opacity == 0,
              repr(row.opacity))
        _step(row, 0.21)  # halfway through the 0.42s fade
        check("live row is mid-fade (0 < opacity < 1)",
              0 < row.opacity < 1, repr(row.opacity))
        check("live row animation still running at mid-fade",
              _anim_for(row) is not None)
        check("live row lands at opacity 1", _pump_to_end(row) == 1,
              repr(row.opacity))
        check("live row animation finished after landing",
              _anim_for(row) is None)

        app._render_message(grid, msg("m-bulk"))
        brow = _row_for(app, "m-bulk")
        check("bulk render (default) returns a row", brow is not None)
        check("bulk row is opacity 1 at once", brow.opacity == 1,
              repr(brow.opacity))
        check("bulk row starts no animation", _anim_for(brow) is None)

        app._render_message(grid, msg("m-bulk2"), animate=False)
        brow2 = _row_for(app, "m-bulk2")
        check("explicit animate=False row is opacity 1 at once",
              brow2.opacity == 1, repr(brow2.opacity))
        check("explicit animate=False starts no animation",
              _anim_for(brow2) is None)

        # --- dedupe: a second render of the same id is a no-op ----------
        before = len(grid.children)
        app._render_message(grid, msg("m-live"), animate=True)
        check("re-render of the same msg_id adds no row",
              len(grid.children) == before, "%d vs %d"
              % (len(grid.children), before))
        check("re-render returns the same row object",
              _row_for(app, "m-live") is row)

        # --- reduced motion: live render lands at once -------------------
        ui_fx.set_reduced_motion(True)
        try:
            app._render_message(grid, msg("m-reduced"), animate=True)
        finally:
            ui_fx.set_reduced_motion(False)
        rrow = _row_for(app, "m-reduced")
        check("reduced-motion live row is opacity 1 at once",
              rrow.opacity == 1, repr(rrow.opacity))
        check("reduced-motion live row starts no animation",
              _anim_for(rrow) is None)

        # --- fetch paths: delta animates, bulk and prepend do not -------
        app.rendered = []
        app._fetch_messages("general", since_id="m-fetch-0")
        check("delta fetch (since_id) renders animate=True",
              app.rendered == [("m-fetch-1", True)], str(app.rendered))
        drow = _row_for(app, "m-fetch-1")
        check("delta row starts at opacity 0", drow.opacity == 0,
              repr(drow.opacity))
        check("delta row lands at opacity 1", _pump_to_end(drow) == 1,
              repr(drow.opacity))

        app.rendered = []
        app._fetch_messages("general", since_ts=1700000000)
        check("delta fetch (since_ts) renders animate=True",
              app.rendered == [("m-fetch-2", True)], str(app.rendered))

        app.rendered = []
        app._fetch_messages("general")
        check("initial bulk fetch renders animate=False",
              app.rendered == [("m-fetch-3", False)], str(app.rendered))
        check("bulk fetch row is opacity 1 at once",
              _row_for(app, "m-fetch-3").opacity == 1)

        app.rendered = []
        app._fetch_messages("general", before="m-fetch-3", prepend=True)
        check("prepend fetch renders animate=False",
              app.rendered == [("m-fetch-4", False)], str(app.rendered))
        check("prepend row is opacity 1 at once",
              _row_for(app, "m-fetch-4").opacity == 1)

        # --- cached-channel bulk render does not animate -----------------
        kc.offline_store.recent = lambda *a: [msg("m-cached-1"),
                                              msg("m-cached-2")]
        app.rendered = []
        app._show_cached_channel(grid, CID)
        check("cached-channel render is animate=False",
              app.rendered == [("m-cached-1", False),
                               ("m-cached-2", False)], str(app.rendered))
        kc.offline_store.recent = real_recent

        # --- console notice animates -------------------------------------
        app.rendered = []
        app._show_notice("console hello")
        check("console notice renders animate=True",
              len(app.rendered) == 1 and app.rendered[0][1] is True,
              str(app.rendered))
        nrow = _row_for(app, app.rendered[0][0])
        check("console notice row starts at opacity 0", nrow.opacity == 0,
              repr(nrow.opacity))
        check("console notice row lands at opacity 1",
              _pump_to_end(nrow) == 1, repr(nrow.opacity))

        # --- optimistic own-send (plain) animates ------------------------
        app.rendered = []
        app.main.ids.msg_input.text = "hello live"
        app.send_message()
        check("optimistic plain send renders animate=True",
              len(app.rendered) == 1 and app.rendered[0][1] is True,
              str(app.rendered))
        srow = _row_for(app, app.rendered[0][0])
        check("optimistic row starts at opacity 0", srow.opacity == 0,
              repr(srow.opacity))
        check("optimistic row lands at opacity 1",
              _pump_to_end(srow) == 1, repr(srow.opacity))

        # --- optimistic own-send (with attachment) animates -------------
        app.rendered = []
        app._send_with_attachment("hi attach", "f.txt", "text/plain",
                                  b"data", kc.ATT_EPHEMERAL)
        check("optimistic attachment send renders animate=True",
              len(app.rendered) == 1 and app.rendered[0][1] is True,
              str(app.rendered))
        arow = _row_for(app, app.rendered[0][0])
        check("attachment row starts at opacity 0", arow.opacity == 0,
              repr(arow.opacity))
        check("attachment row lands at opacity 1",
              _pump_to_end(arow) == 1, repr(arow.opacity))

        # --- rendering a plain message never raises ----------------------
        try:
            app._render_message(grid, msg("m-noraise"), animate=True)
            noraise = _row_for(app, "m-noraise")
            check("rendering a live message does not raise", True)
            check("no-raise row lands at opacity 1",
                  _pump_to_end(noraise) == 1, repr(noraise.opacity))
        except Exception as e:
            check("rendering a live message does not raise", False, repr(e))
    finally:
        kc.Clock = real_clock
        kc.threading.Thread = real_thread
        app_menus.Clock = real_menu_clock
        kc.offline_store.recent = real_recent
        ui_fx.set_reduced_motion(False)
        # No cleanup of Animation._instances needed: every animation
        # started in this file was pumped to completion above, and the
        # file runs as its own process.

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
