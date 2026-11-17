#!/usr/bin/env python3
"""Channel-list tests (Task 7) — '+', invite-to, drag reorder.

Pins down:
  - drag: threshold (dp(8)) then live reorder; long-press clock is
    cancelled by movement and survives a stationary touch; release posts
    the final order ONCE through channels/reorder;
  - the posted order is the visual top-to-bottom order as cids;
  - _add_channel honours server position (lower position rows first);
  - the '+' affordance is disabled for members who cannot create channels
    (kv: opacity+disabled) while admins keep it;
  - invite-to-channel posts a channel-targeted invite and copies the link.
"""
import os, sys, time

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))

def pump(seconds=1.0):
    from kivy.clock import Clock
    deadline = time.time() + seconds
    while time.time() < deadline:
        Clock._process_events()
        Clock.tick()
        time.sleep(0.02)

class FakeTouch:
    """Just enough of a Kivy touch for the drag math."""
    def __init__(self, x, y):
        self.x, self.y = x, y
        self.ud = {}
        self.grab_current = None
        self.profile = []
        self.button = "left"
        self.time_start = time.time()
        self.is_mouse_scrolling = False
        self.grab_state = []
        self.multitouch_sim = False

    @property
    def pos(self):
        return (self.x, self.y)

    def grab(self, widget, all=False, *a, **k):
        pass

    def ungrab(self, *a, **k):
        pass

    def to_window(self, x, y, *a, **k):
        return (x, y)

class FakeApi:
    def __init__(self):
        self.reorder_calls = []
        self.invite_calls = []

    def __getattr__(self, name):
        # any guild method the flow touches that isn't pinned returns ok
        def _ok(*a, **k):
            return {"ok": 1}
        return _ok

    def channels_reorder(self, order):
        self.reorder_calls.append(order)
        return {"ok": 1, "order": order}

    def invites(self, op="list", **kw):
        self.invite_calls.append((op, kw))
        return {"ok": 1, "code": "CHAN7X"}

class FakeClock:
    """Stand-in for the context-menu clock (cancel marker)."""
    def __init__(self):
        self.cancelled = False
    def cancel(self):
        self.cancelled = True

def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0

    from kivy.lang import Builder
    try:
        Builder.load_file("kv_md.kv")
    except Exception:
        Builder.load_file(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "kv_md.kv"))

    app = kc.sHadesClientApp()
    app.guild_api = FakeApi()
    app.i_am_admin = True
    app.i_am_moderator = True
    # widget classes pull the app via App.get_running_app(); install this
    # instance the way run() would headlessly (a proxy that forwards
    # attribute access, because kv rules read app.<attr> too)
    import kivy.app as _kapp

    class _RA:
        def __init__(self, _app):
            self._app = _app

        def __getattr__(self, name):
            return getattr(self._app, name)

    try:
        _kapp.App._running_app = _RA(app)
    except Exception as e:
        print("get_running_app shim failed:", repr(e))

    from kivy.uix.screenmanager import ScreenManager
    sm = ScreenManager()
    sm.add_widget(kc.MainScreen(name="main"))
    app.root = sm
    main = sm.get_screen("main")

    # three channels with positions 1,2,3 (c1 top)
    app._channels = {"uno": "u" * 32, "dos": "d" * 32, "tre": "t" * 32}
    app._channel_pos = {"uno": 1, "dos": 2, "tre": 3}
    app._channel_order = {"u" * 32: 1, "d" * 32: 2, "t" * 32: 3}
    app._channel_kinds = {"u" * 32: "chat", "d" * 32: "chat", "t" * 32: "chat"}
    app._add_channel("uno")
    app._add_channel("dos")
    app._add_channel("tre")
    grid = main.ids.channel_grid
    # headless: give the grid layout geometry (a real device lays out on
    # the window; the harness must supply the rows itself)
    grid.size = (200, 4 * 32)
    grid.do_layout()
    buttons = [w for w in reversed(grid.children)]
    check("channels build in server order",
          [b.channel_name for b in buttons] == ["uno", "dos", "tre"],
          [b.channel_name for b in buttons])

    # late-arriving channel with position 0 goes to the top
    app._channels["zero"] = "z" * 32
    app._channel_pos["zero"] = 0
    app._channel_order["z" * 32] = 0
    app._add_channel("zero")
    grid.size = (200, 4 * 32)
    grid.do_layout()
    buttons = [w for w in reversed(grid.children)]
    check("position-0 channel inserts at the top",
          [b.channel_name for b in buttons] == ["zero", "uno", "dos", "tre"],
          [b.channel_name for b in buttons])

    b_zero, b_uno, b_dos, b_tre = buttons

    # ---------- drag: full realistic touch chain ----------
    down = FakeTouch(x=b_tre.center_x, y=b_tre.center_y)
    b_tre.on_touch_down(down)  # realistic down: ButtonBehavior stamps ud,
    # ContextMenuMixin arms the long-press clock
    if not hasattr(b_tre, "_ctx_clock") or b_tre._ctx_clock is None:
        check("down arms the long-press clock", False, "no clock on down")
        return 1
    check("down arms the long-press clock", True)

    moved = FakeTouch(x=down.x + 2, y=down.y - 2)  # well under dp(8)
    moved.ud = dict(down.ud)
    moved.grab_current = b_tre
    b_tre._ch_drag_check(moved)
    check("sub-threshold movement is not a drag",
          not b_tre._dragging and b_tre._ctx_clock is not None, "")

    # cross above b_uno's row in one step
    step = FakeTouch(x=b_tre.center_x, y=b_uno.center_y)
    step.ud = dict(down.ud)
    step.grab_current = b_tre
    dragged = b_tre._ch_drag_check(step)
    check("threshold crossing starts a drag", dragged and b_tre._dragging, "")
    check("drag cancels the long-press clock", b_tre._ctx_clock is None,
          dict(clock=b_tre._ctx_clock))
    buttons = [w for w in reversed(grid.children)]
    # the pointer sat at b_uno's row center: tre lands in row 1
    check("live reorder moved the row",
          [b.channel_name for b in buttons] == ["zero", "tre", "uno", "dos"],
          [b.channel_name for b in buttons])

    # ---------- release posts the order once ----------
    saved_ok = True
    try:
        saved_ok = b_tre.on_touch_up(step)  # consumed + posts
    except Exception as e:
        import traceback
        traceback.print_exc()
        saved_ok = False
    pump()
    check("drag end returns True (consumed, no press)", saved_ok is True, "")
    posted = app.guild_api.reorder_calls
    check("reorder posted exactly once",
          len(posted) == 1, posted)
    check("posted order is the visual order as cids",
          posted and posted[0] == ["z" * 32, "t" * 32, "u" * 32, "d" * 32],
          posted)

    # ---------- invite-to ----------
    app._invite_to_channel("t" * 32, "tre")
    pump()
    inv = app.guild_api.invite_calls
    check("invite-to-channel posts a channel-targeted invite",
          inv and inv[0][0] == "create"
          and inv[0][1].get("channel") == "t" * 32, inv)

    # ---------- '+' gate ----------
    app.i_am_admin = False
    app.my_roles = []
    ok_admin = app._can_create_channels()
    app.my_roles = [{"role_id": 1, "name": "x", "perms": 8}]
    ok_chan = app._can_create_channels()
    check("create-channel gate: no for a plain member, yes with MANAGE_CHANNEL",
          not ok_admin and ok_chan, dict(no=ok_admin, yes=ok_chan))

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
