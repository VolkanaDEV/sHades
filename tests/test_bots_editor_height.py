#!/usr/bin/env python3
"""Bots perms editor height (closed-editor blank space regression).

Each bot row builds a perms editor (9 checkbox rows + a Save row) that
starts closed (height 0, opacity 0). A minimum_height -> height binding
on the editor re-asserted its full height (~418px) on every layout pass,
so closed editors reserved blank vertical space in the list.

Probe: fresh HOME boots to onboarding; a fake GuildAPI (no live server)
backs the screen with 2 canned bots. Asserts:
  (a) after the list renders and a layout pass runs, every CLOSED
      editor has height == 0 and opacity == 0
  (b) opening one editor via toggle_edit gives height > 0,
      height == minimum_height, opacity == 1
  (c) closing it again gives height == 0 and opacity == 0 after
      another layout pass
  (d) the other (never-opened) editor stays at height 0 throughout

Run: xvfb-run -a python tests/test_bots_editor_height.py
"""
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-bots-editor-height-")
os.environ["HOME"] = TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402
import app_settings  # noqa: E402

kc._app_data_dir = lambda: TMP
app_settings._app_data_dir = lambda: TMP

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


def _walk(widget):
    yield widget
    for child in list(getattr(widget, "children", [])):
        yield from _walk(child)


def _button(app, screen_name, text):
    from client_widgets import FancyMDButton
    scr = app.root.get_screen(screen_name)
    for widget in _walk(scr):
        if not isinstance(widget, FancyMDButton):
            continue
        for child in widget.children:
            if getattr(child, "text", None) == text:
                return widget
    return None


class FakeBotsAPI:
    """Canned bots_list; records calls. No live server."""

    def __init__(self):
        self.calls = []
        self.bots = [
            {"bot_id": "b1", "name": "echo-bot", "owner_hash": "a" * 64,
             "perms": 1 | 64, "created_at": 1793000000,
             "revoked_at": None, "last_seen": 1793003600},
            {"bot_id": "b2", "name": "old-bot", "owner_hash": "b" * 64,
             "perms": 1, "created_at": 1792000000,
             "revoked_at": 1792500000, "last_seen": None},
        ]

    def bots_list(self):
        self.calls.append(("bots_list",))
        return {"ok": 1, "bots": [dict(b) for b in self.bots]}


class BotsEditorProbe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        Clock.schedule_once(self.step_boot, 1.5)

    def step_boot(self, dt):
        check("fresh install starts on onboarding",
              self.root.current == "onboarding", self.root.current)
        self.fake = FakeBotsAPI()
        self.guild_api = self.fake
        self.hub_address = "127.0.0.1:29221"
        self.guild_node_hash = "f" * 64
        self.delivery_hash = "g" * 64
        self.root.current = "profile"
        Clock.schedule_once(self.step_profile, 0.6)

    def step_profile(self, dt):
        btn = _button(self, "profile", "Bots")
        check("(a0) Bots entry point found on the profile screen",
              btn is not None)
        if btn is None:
            self._finish()
            return
        btn.dispatch("on_press")
        Clock.schedule_once(self.step_list, 0.6)

    def step_list(self, dt):
        self.scr = self.root.get_screen("bots")
        check("(a1) Bots button opens the Bots screen",
              self.root.current == "bots", self.root.current)
        check("(a2) both editors exist", set(self.scr._edit_boxes) == {
            "b1", "b2"}, list(self.scr._edit_boxes))
        # (a) closed editors must take no vertical space after layout.
        for bot_id in ("b1", "b2"):
            ed = self.scr._edit_boxes[bot_id]
            check("(a3) closed editor %s height == 0" % bot_id,
                  ed.height == 0, ed.height)
            check("(a4) closed editor %s opacity == 0" % bot_id,
                  ed.opacity == 0, ed.opacity)
            check("(a5) closed editor %s has a real minimum_height"
                  % bot_id, ed.minimum_height > 0, ed.minimum_height)
        # (b) open b1's editor.
        self.scr.toggle_edit("b1")
        Clock.schedule_once(self.step_open, 0.5)

    def step_open(self, dt):
        ed = self.scr._edit_boxes["b1"]
        check("(b1) opened editor height > 0", ed.height > 0, ed.height)
        check("(b2) opened editor height == minimum_height",
              ed.height == ed.minimum_height,
              (ed.height, ed.minimum_height))
        check("(b3) opened editor opacity == 1", ed.opacity == 1,
              ed.opacity)
        other = self.scr._edit_boxes["b2"]
        check("(d1) never-opened editor stays height 0 while b1 is open",
              other.height == 0, other.height)
        check("(d2) never-opened editor stays opacity 0 while b1 is open",
              other.opacity == 0, other.opacity)
        # (c) close b1 again; a layout pass must not grow it back.
        self.scr.toggle_edit("b1")
        Clock.schedule_once(self.step_closed, 0.5)

    def step_closed(self, dt):
        ed = self.scr._edit_boxes["b1"]
        check("(c1) re-closed editor height == 0 after a layout pass",
              ed.height == 0, ed.height)
        check("(c2) re-closed editor opacity == 0", ed.opacity == 0,
              ed.opacity)
        other = self.scr._edit_boxes["b2"]
        check("(d3) never-opened editor still height 0 at the end",
              other.height == 0, other.height)
        check("(d4) never-opened editor still opacity 0 at the end",
              other.opacity == 0, other.opacity)
        self._finish()

    def _finish(self):
        print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)), flush=True)
        self.stop()


def _watchdog():
    time.sleep(45)
    print("WATCHDOG: app did not exit; forcing exit", flush=True)
    os._exit(2)


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()
    Window.size = (1280, 720)
    probe = BotsEditorProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
