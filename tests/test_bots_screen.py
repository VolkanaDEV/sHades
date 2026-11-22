#!/usr/bin/env python3
"""Bots setup/config screen (Group 3).

The Bots screen is a submenu of Profile: a "Bots" button on the profile
screen opens it; it lists the user's bots (name, truncated owner hash
with copy, plain-language perms, created/last seen, active/revoked),
lets the user create a bot (name + permission checkboxes), shows the
one-time API key + gateway info on create, revokes with a two-tap
confirm on the button itself, and edits perms inline.

Probe: fresh HOME boots to onboarding; a fake GuildAPI (no live server)
backs the screen. Asserts:
  (a) the Bots entry point on the profile screen opens the Bots screen
  (b) the list renders name/owner/perms/state for an active and a
      revoked bot (revoked shown dimmed, actions disabled)
  (c) the create flow calls bots_create with the checked perms and then
      displays the returned one-time key + gateway info + ONCE warning
  (d) dismissing the key refreshes the list (new bot appears)
  (e) the revoke flow (two taps) calls bots_revoke and refreshes
  (f) perms editing calls bots_update with the new bits
  (g) with no guild connection (and with a failing list call) the
      screen shows a status error, keeps the last list, and does not crash

Run: xvfb-run -a python tests/test_bots_screen.py
"""
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-bots-screen-")
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


def _label_texts(widget):
    from kivy.uix.label import Label
    return [w.text for w in _walk(widget)
            if isinstance(w, Label) and w.text]


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


def _row_for(screen, name):
    """The bot row BoxLayout whose subtree holds a Label == name."""
    from kivy.uix.label import Label
    for row in list(screen._list_box.children):
        for w in _walk(row):
            if isinstance(w, Label) and w.text == name:
                return row
    return None


def _row_button(row, text):
    from client_widgets import FancyMDButton
    for widget in _walk(row):
        if not isinstance(widget, FancyMDButton):
            continue
        for child in widget.children:
            if getattr(child, "text", None) == text:
                return widget
    return None


class FakeBotsAPI:
    """Canned bots_* responses; records every call. No live server."""

    def __init__(self):
        self.calls = []
        self.fail_list = False
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
        if self.fail_list:
            return {"ok": 0, "error": "link dead"}
        return {"ok": 1, "bots": [dict(b) for b in self.bots]}

    def bots_create(self, name, perms):
        self.calls.append(("bots_create", name, perms))
        bot = {"bot_id": "b3", "name": name, "owner_hash": "c" * 64,
               "perms": perms, "created_at": 1793100000,
               "revoked_at": None, "last_seen": None}
        self.bots.append(bot)
        return {"ok": 1, "bot": dict(bot), "api_key": "sk-test-KEY-12345",
                "gateway": {
                    "node_hash": "d" * 64, "delivery_hash": "e" * 64,
                    "endpoints": {
                        "subscribe": "/api/v1/stream/subscribe",
                        "poll": "/api/v1/stream/poll",
                        "post": "/api/v1/bots/post",
                        "react": "/api/v1/bots/react"}}}

    def bots_update(self, bot_id, name=None, perms=None):
        self.calls.append(("bots_update", bot_id, name, perms))
        for bot in self.bots:
            if bot["bot_id"] == bot_id:
                if name is not None:
                    bot["name"] = name
                if perms is not None:
                    bot["perms"] = perms
                return {"ok": 1, "bot": dict(bot)}
        return {"ok": 0, "error": "no such bot"}

    def bots_revoke(self, bot_id):
        self.calls.append(("bots_revoke", bot_id))
        for bot in self.bots:
            if bot["bot_id"] == bot_id:
                bot["revoked_at"] = 1793200000
                return {"ok": 1, "bot": dict(bot)}
        return {"ok": 0, "error": "no such bot"}


class BotsProbe(kc.sHadesClientApp):
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
        Clock.schedule_once(self.step_bots, 0.6)

    def step_bots(self, dt):
        self.scr = self.root.get_screen("bots")
        check("(a) Bots button opens the Bots screen",
              self.root.current == "bots", self.root.current)
        check("(b0) nine create-form permission checkboxes",
              len(self.scr._create_checks) == 10,
              len(self.scr._create_checks))
        texts = _label_texts(self.scr)
        check("(b1) list shows the active bot name",
              "echo-bot" in texts, texts)
        check("(b2) list shows the revoked bot name",
              "old-bot" in texts, texts)
        check("(b3) revoked bot is marked Revoked",
              "Revoked" in texts, texts)
        check("(b4) active bot is marked Active",
              "Active" in texts, texts)
        check("(b5) owner hash shown truncated",
              ("a" * 12 + "…") in " ".join(texts), texts)
        check("(b6) perms shown as plain-language labels",
              "Read messages" in texts and "Post messages" in texts, texts)
        check("(b7) created / last-seen meta shown",
              any("Created" in t and "Last seen" in t for t in texts), texts)
        row = _row_for(self.scr, "old-bot")
        check("(b8) revoked row is dimmed", row is not None
              and row.opacity < 1, getattr(row, "opacity", None))
        if row is not None:
            check("(b9) revoked row actions are disabled",
                  _row_button(row, "Revoke") is not None
                  and _row_button(row, "Revoke").disabled
                  and _row_button(row, "Edit permissions").disabled)
        # (c) create flow: name + Read messages (1) + Post messages (64)
        self.scr._name_field.text = "new-bot"
        self.scr._create_checks[1].active = True
        self.scr._create_checks[64].active = True
        create = _button(self, "bots", "Create bot")
        check("(c0) Create button found", create is not None)
        if create is None:
            self._finish()
            return
        create.dispatch("on_press")
        Clock.schedule_once(self.step_created, 0.5)

    def step_created(self, dt):
        check("(c1) create calls bots_create with the checked perms",
              ("bots_create", "new-bot", 65) in self.fake.calls,
              self.fake.calls)
        check("(c2) one-time key is displayed",
              "sk-test-KEY-12345" in self.scr._key_label.text,
              self.scr._key_label.text)
        warn = [t for t in _label_texts(self.scr._key_box)
                if "ONCE" in t]
        check("(c3) key display warns it is shown once", bool(warn), warn)
        gw = self.scr._gateway_label.text
        check("(c4) gateway shows hub address",
              "127.0.0.1:29221" in gw, gw)
        check("(c5) gateway shows guild node + delivery hashes",
              ("f" * 64) in gw and ("g" * 64) in gw, gw)
        check("(c6) gateway shows endpoint paths",
              "/api/v1/bots/post" in gw and "/api/v1/stream/poll" in gw, gw)
        check("(c7) key box is visible after create",
              self.scr._key_box.opacity == 1, self.scr._key_box.opacity)
        dismiss = _button(self, "bots", "Dismiss")
        check("(d0) Dismiss button found", dismiss is not None)
        if dismiss is None:
            self._finish()
            return
        dismiss.dispatch("on_press")
        Clock.schedule_once(self.step_dismissed, 0.5)

    def step_dismissed(self, dt):
        check("(d1) key box hidden after dismiss",
              self.scr._key_box.opacity == 0, self.scr._key_box.opacity)
        check("(d2) list refreshes after dismiss (new bot appears)",
              _row_for(self.scr, "new-bot") is not None)
        # (e) revoke flow: two taps on echo-bot's Revoke button
        row = _row_for(self.scr, "echo-bot")
        check("(e0) echo-bot row found", row is not None)
        if row is None:
            self._finish()
            return
        self.revoke_btn = _row_button(row, "Revoke")
        check("(e1) Revoke button found", self.revoke_btn is not None)
        if self.revoke_btn is None:
            self._finish()
            return
        self.revoke_btn.dispatch("on_press")
        Clock.schedule_once(self.step_revoke_armed, 0.4)

    def step_revoke_armed(self, dt):
        check("(e2) first tap arms the confirm (re-labels the button)",
              self.revoke_btn._bots_label.text == "Confirm revoke",
              self.revoke_btn._bots_label.text)
        check("(e3) first tap does NOT call bots_revoke yet",
              ("bots_revoke", "b1") not in self.fake.calls, self.fake.calls)
        self.revoke_btn.dispatch("on_press")
        Clock.schedule_once(self.step_revoked, 0.5)

    def step_revoked(self, dt):
        check("(e4) second tap calls bots_revoke",
              ("bots_revoke", "b1") in self.fake.calls, self.fake.calls)
        check("(e5) revoke status confirms",
              "revoked" in self.scr.ids["bots_status"].text.lower(),
              self.scr.ids["bots_status"].text)
        row = _row_for(self.scr, "echo-bot")
        check("(e6) list refreshes: revoked bot now marked Revoked",
              row is not None and "Revoked" in _label_texts(row),
              _label_texts(row) if row is not None else None)
        # (f) perms editing on new-bot: drop Read messages (1), add
        # Read reactions (8) -> 64 | 8 == 72
        row = _row_for(self.scr, "new-bot")
        check("(f0) new-bot row found", row is not None)
        if row is None:
            self._finish()
            return
        edit = _row_button(row, "Edit permissions")
        check("(f1) Edit permissions button found", edit is not None)
        if edit is None:
            self._finish()
            return
        edit.dispatch("on_press")
        Clock.schedule_once(self.step_edit_open, 0.4)

    def step_edit_open(self, dt):
        check("(f2) editor opens for the bot",
              self.scr._edit_boxes["b3"].opacity == 1,
              self.scr._edit_boxes["b3"].opacity)
        checks = self.scr._edit_checks["b3"]
        checks[1].active = False
        checks[8].active = True
        save = _row_button(self.scr._edit_boxes["b3"], "Save")
        check("(f3) Save button found in editor", save is not None)
        if save is None:
            self._finish()
            return
        save.dispatch("on_press")
        Clock.schedule_once(self.step_saved, 0.5)

    def step_saved(self, dt):
        check("(f4) save calls bots_update with the new bits",
              ("bots_update", "b3", None, 72) in self.fake.calls,
              self.fake.calls)
        check("(f5) save status confirms",
              "Permissions saved" in self.scr.ids["bots_status"].text,
              self.scr.ids["bots_status"].text)
        # (g) offline: no guild connection, then a failing list call
        self.guild_api = None
        self.scr.refresh()
        Clock.schedule_once(self.step_offline_none, 0.4)

    def step_offline_none(self, dt):
        check("(g1) no guild connection shows a status error",
              "unavailable" in self.scr.ids["bots_status"].text.lower(),
              self.scr.ids["bots_status"].text)
        check("(g2) last list is kept with no connection",
              _row_for(self.scr, "new-bot") is not None)
        self.guild_api = self.fake
        self.fake.fail_list = True
        self.scr.refresh()
        Clock.schedule_once(self.step_offline_fail, 0.4)

    def step_offline_fail(self, dt):
        check("(g3) failing list call shows a status error",
              "unavailable" in self.scr.ids["bots_status"].text.lower(),
              self.scr.ids["bots_status"].text)
        check("(g4) last list is kept after a failed fetch",
              _row_for(self.scr, "new-bot") is not None)
        self.fake.fail_list = False
        self.scr.refresh()
        Clock.schedule_once(self.step_recovered, 0.4)

    def step_recovered(self, dt):
        check("(g5) list recovers once the guild answers",
              "Loaded 3 bot(s)" in self.scr.ids["bots_status"].text,
              self.scr.ids["bots_status"].text)
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
    probe = BotsProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
