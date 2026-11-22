#!/usr/bin/env python3
"""Polish2 item 4: server-config tab-content buttons are 40px.

The tab bar was already 40px (G6), but every button inside the tabs
was 30px (control rows) or 26px (scroll-grid rows) -- below the 40px
touch target. The rows have been reworked to 40px (scaffold spacing
8px -> 4px so the General tab still fits at 640x480).

Pins down, at 1280x720 and at 640x480, with a fake client (no network,
no run()):
  - every tab-content button is exactly 40px tall and >= 48px wide;
  - no button extends past its parent row's bounds;
  - no two siblings overlap inside a tab's content;
  - the General tab's control rows + audit scroll still fit the
    content slot (audit ScrollView keeps a positive height);
  - the tab-flow popups (icon/banner chooser, channel perm editor)
    also carry 40px buttons.

State assertions only: layout is settled with a fixed number of
Clock ticks, never by waiting on wall-clock time.
"""
import os
import sys

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy.uix.button import Button  # noqa: E402
from kivy.uix.popup import Popup  # noqa: E402

import ui_server_config as usc  # noqa: E402

TOL = 1.0
EXPECT = {"General": 10, "Membership": 4, "Channels": 3, "Roles": 4,
          "Invites": 3}


class FakeApi:
    """Canned guild calls; no network."""
    def admin_config(self, key=None, value=None):
        cfg = {"guild_name": "TestHub", "registration": "open",
               "slowmode": "0", "motd": "hi", "guild_icon": "",
               "attachments": "saved", "banner": ""}
        return {"ok": 1, "config": {key: value} if key else cfg}

    def admin_mods(self, op="list", user=None):
        return {"ok": 1, "mods": ["a" * 32], "admins": ["b" * 32]}

    def admin_audit(self, limit=100):
        return {"ok": 1, "entries": []}

    def channels_list(self):
        return {"ok": 1, "channels": [
            {"id": "c" * 31 + "1", "name": "general", "kind": "chat",
             "topic": "", "position": 1, "overwrites": []}]}

    def roles_list(self):
        return {"ok": 1, "roles": [
            {"role_id": 1, "name": "Founder", "perms": 32, "color": None,
             "position": 100, "members": 1}]}

    def invites(self, op="list", **kw):
        return {"ok": 1, "invites": [
            {"code": "ABC123", "label": "w", "uses": 0, "max_uses": 5,
             "expires_at": 0, "channel_id": None}]}

    def blobs_get(self, blob_id):
        return {"ok": 0, "error": "no blob in the fake"}


class FakeClient:
    def __init__(self):
        self.guild_api = FakeApi()
        self.i_am_admin = True
        self.my_roles = [{"role_id": 1, "name": "Founder", "perms": 32}]
        self.status = []

    def _set_status(self, txt):
        self.status.append(txt)

    def _can_manage_roles(self):
        return True

    def _can_create_channels(self):
        return True


def settle(n=5):
    """Fixed number of Clock ticks to land scheduled layout; no timing."""
    for _ in range(n):
        Clock._process_events()
        Clock.tick()


def walk(root, cls):
    out = []
    stack = [root]
    while stack:
        w = stack.pop()
        if isinstance(w, cls):
            out.append(w)
        stack.extend(w.children)
    return out


def inside_parent(w):
    """Child rect within its parent's rect (parent-relative coords)."""
    p = w.parent
    if p is None:
        return True
    return (w.x >= p.x - TOL and w.y >= p.y - TOL
            and w.right <= p.right + TOL and w.top <= p.top + TOL)


def overlaps(root):
    """Sibling pairs whose rects intersect, at every level."""
    bad = []

    def scan(w):
        kids = [c for c in w.children if c.width > 0 and c.height > 0]
        for i in range(len(kids)):
            for j in range(i + 1, len(kids)):
                a, b = kids[i], kids[j]
                iw = min(a.right, b.right) - max(a.x, b.x)
                ih = min(a.top, b.top) - max(a.y, b.y)
                if iw > TOL and ih > TOL:
                    bad.append("%s %r overlaps %s %r"
                               % (type(a).__name__, getattr(a, "text", "?"),
                                  type(b).__name__, getattr(b, "text", "?")))
        for c in w.children:
            scan(c)

    scan(root)
    return bad


def render_all(scr):
    """Synchronous canned renders: rows exist without worker threads."""
    scr.tab_content["General"]._render(
        {"ok": 1, "config": {"guild_name": "T", "motd": "hi", "slowmode": "0",
                             "registration": "open", "guild_icon": "",
                             "attachments": "saved", "banner": ""}})
    scr.tab_content["Membership"]._render(
        {"ok": 1, "mods": ["a" * 32], "admins": ["b" * 32]})
    scr.tab_content["Channels"]._render(
        {"ok": 1, "channels": [{"id": "c" * 31 + "1", "name": "general",
                                "kind": "chat", "topic": "", "position": 1,
                                "overwrites": []}]})
    scr.tab_content["Roles"]._render(
        {"ok": 1, "roles": [{"role_id": 1, "name": "Founder", "perms": 32,
                             "color": None, "position": 100,
                             "members": 1}]})
    scr.tab_content["Invites"]._render(
        {"ok": 1, "invites": [{"code": "ABC123", "label": "w", "uses": 0,
                               "max_uses": 5, "expires_at": 0,
                               "channel_id": None}]})


def check_size(scr, tag):
    """Button heights/widths, parent bounds and sibling overlap per tab."""
    for title, want_n in EXPECT.items():
        scr._show_tab(title)
        settle()
        content = scr._tab_contents[title]
        btns = walk(content, Button)
        check("%s %s: %d tab-content buttons built" % (tag, title, want_n),
              len(btns) == want_n,
              [(b.text, b.height) for b in btns])
        under = [(b.text, round(b.height, 1), round(b.width, 1))
                 for b in btns if abs(b.height - dp(40)) > TOL]
        check("%s %s: every button is 40px tall" % (tag, title),
              not under, under)
        narrow = [(b.text, round(b.width, 1)) for b in btns
                  if b.width < 48 - TOL]
        check("%s %s: every button is >= 48px wide" % (tag, title),
              not narrow, narrow)
        outside = [(b.text, type(b.parent).__name__) for b in btns
                   if not inside_parent(b)]
        check("%s %s: no button extends past its parent row"
              % (tag, title), not outside, outside)
        short_rows = [(b.text, round(b.parent.height, 1)) for b in btns
                      if b.parent.height < dp(40) - TOL]
        check("%s %s: every button row is >= 40px tall" % (tag, title),
              not short_rows, short_rows)
        lap = overlaps(content)
        check("%s %s: no two siblings overlap" % (tag, title),
              not lap, lap[:3])
    # the tab bar itself was already 40px; it must stay that way
    bar_btns = [b for b in walk(scr, Button)
                if b.text in ("< back",) + tuple(EXPECT)]
    check("%s tab bar: back + 5 tab buttons are 40px tall" % tag,
          len(bar_btns) == 6
          and all(abs(b.height - dp(40)) <= TOL for b in bar_btns),
          [(b.text, round(b.height, 1)) for b in bar_btns])


def check_general_fit(scr, tag):
    """General tab: all control rows + the audit scroll fit the slot."""
    scr._show_tab("General")
    settle()
    gen = scr.tab_content["General"]
    slot = scr._content_slot
    check("%s General: control box fits the content slot" % tag,
          gen.box.height <= slot.height + TOL,
          (round(gen.box.height, 1), round(slot.height, 1)))
    scroll = gen.grid.parent
    check("%s General: audit scroll keeps a positive height" % tag,
          scroll.height > 0, round(scroll.height, 1))
    rows = [c for c in gen.box.children
            if c is not scroll and c.height > 0]
    low = [(type(c).__name__, round(c.y, 1)) for c in rows if c.y < -TOL]
    check("%s General: no control row hangs below the window" % tag,
          not low, low)


def check_popups(scr):
    """Tab-flow popups: chooser buttons and the perm-editor apply."""
    gen = scr.tab_content["General"]
    gen._open_icon_chooser()
    settle()
    pop = gen._icon_chooser_popup
    btns = walk(pop.content, Button)
    check("icon chooser: cancel + use-this-file are 40px tall",
          len(btns) == 2
          and all(abs(b.height - dp(40)) <= TOL for b in btns),
          [(b.text, round(b.height, 1)) for b in btns])
    check("icon chooser: buttons stay inside their row",
          all(inside_parent(b) for b in btns),
          [(b.text, round(b.parent.height, 1)) for b in btns])
    # dismiss() fades out before removing the popup; animation=False
    # removes it at once, so the next popup count is deterministic.
    pop.dismiss(animation=False)
    gen._open_banner_chooser()
    settle()
    pop = gen._banner_chooser_popup
    btns = walk(pop.content, Button)
    check("banner chooser: cancel + use-this-file are 40px tall",
          len(btns) == 2
          and all(abs(b.height - dp(40)) <= TOL for b in btns),
          [(b.text, round(b.height, 1)) for b in btns])
    check("banner chooser: buttons stay inside their row",
          all(inside_parent(b) for b in btns),
          [(b.text, round(b.parent.height, 1)) for b in btns])
    pop.dismiss(animation=False)
    ch = scr.tab_content["Channels"]
    ch._open_perm_editor({"id": "c" * 31 + "1", "name": "general",
                          "kind": "chat", "overwrites": []})
    settle()
    pops = [w for w in Window.children if isinstance(w, Popup)]
    check("perm editor popup opened", len(pops) == 1,
          [type(w).__name__ for w in Window.children])
    if pops:
        btns = walk(pops[0].content, Button)
        check("perm editor: apply is 40px tall",
              len(btns) == 1
              and abs(btns[0].height - dp(40)) <= TOL,
              [(b.text, round(b.height, 1)) for b in btns])
        pops[0].dismiss(animation=False)


def main():
    from kivy.app import App
    from kivy.uix.screenmanager import ScreenManager

    class ProbeApp(App):
        def build(self):
            return ScreenManager()
        theme_colors = {"primary": (0.2, 0.3, 0.5, 1),
                        "sidebar_ctrl": (0.2, 0.2, 0.2, 1)}

    probe = ProbeApp()
    probe.theme_colors = ProbeApp.theme_colors

    check("_btn default height is 40px",
          abs(usc._btn("x").height - dp(40)) <= TOL,
          usc._btn("x").height)

    client = FakeClient()
    scr = usc.ServerConfigScreen(client=client, name="server_config")
    sm = ScreenManager()
    sm.add_widget(scr)
    Window.size = (1280, 720)
    sm.size = Window.size
    scr.build_tabs()
    settle()
    render_all(scr)
    check_size(scr, "1280x720")
    check_general_fit(scr, "1280x720")
    check_popups(scr)

    Window.size = (640, 480)
    sm.size = Window.size
    settle()
    render_all(scr)
    check_size(scr, "640x480")
    check_general_fit(scr, "640x480")

    print("RESULT: %d PASS, %d FAIL" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
