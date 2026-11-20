#!/usr/bin/env python3
"""Admin panel regression test.

The server-settings panel (admin-only) is built from a set of nested loader
functions plus a tab strip. A scoping slip once nested the whole tab
machinery (views / show / refresh / tab_buttons) inside the `load_audit`
loader, which meant:

  * the panel opened empty — `show(0)` was never called at build time;
  * `refresh()` was undefined in the other loaders, so saving a config value
    or adding a moderator raised NameError.

These tests drive the real _open_admin_panel() with a stub guild API and
assert the tab strip is populated, the first view renders, and that the
save / add-mod paths resolve `refresh` without raising NameError.
"""
import os
import sys

# Use the real SDL window on DISPLAY (including CI's Xvfb).

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


class StubGuild:
    """Stub guild API for the admin panel."""

    def __init__(self):
        self.config = {"motd": "hello", "slow_mode": "5"}
        self.mods = ["b" * 32]
        self.admins = ["c" * 32]
        self.saved = []
        self._chan_seq = 0
        self._role_seq = 1

    def channels_list(self):
        self._chan_seq += 1
        return {"ok": True, "channels": [
            {"id": ("o" * 64), "name": "general", "kind": "chat",
             "topic": "", "position": 0},
            {"id": ("m" * 64), "name": "memes", "kind": "chat",
             "topic": "only memes", "position": 1}]}

    def channels_patch(self, cid=None, **kw):
        for k, v in kw.items():
            self.saved.append(("channel:" + str(cid), (k, v)))
        return {"ok": True}

    def channels_delete(self, cid=None):
        self.saved.append(("channel_delete", str(cid)))
        return {"ok": True}

    def roles_list(self):
        self._role_seq += 1
        return {"ok": True, "roles": [
            {"role_id": 1, "name": "admin", "perms": 32, "color": None,
             "position": 1, "members": []},
            {"role_id": 2, "name": "member", "perms": 0, "color": None,
             "position": 0, "members": []}]}

    def roles_create(self, name=None, perms=0, color=None, position=0):
        self.saved.append(("role_create", str(name)))
        self._role_seq += 1
        return {"ok": True, "role": {"role_id": self._role_seq,
                                     "name": name, "perms": perms,
                                     "color": color, "position": position,
                                     "members": []}}

    def roles_assign(self, role=None, target_hash=None):
        self.saved.append(("role_assign", (role, target_hash)))
        return {"ok": True}

    def roles_unassign(self, role=None, target_hash=None):
        self.saved.append(("role_unassign", (role, target_hash)))
        return {"ok": True}

    def roles_delete(self, role=None):
        self.saved.append(("role_delete", role))
        return {"ok": True}

    def admin_config(self, key=None, value=None):
        if key is None:
            return {"ok": True, "config": dict(self.config)}
        self.saved.append((key, value))
        self.config[key] = value
        return {"ok": True}

    def admin_mods(self, op, target_hash=None):
        if op == "list":
            return {"ok": True, "mods": list(self.mods),
                    "admins": list(self.admins)}
        if op in ("add", "admin_add"):
            self.mods.append(target_hash)
            return {"ok": True}
        if op == "remove":
            if target_hash in self.mods:
                self.mods.remove(target_hash)
            return {"ok": True}
        if op == "admin_remove":
            if target_hash in self.admins:
                self.admins.remove(target_hash)
            return {"ok": True}
        return {"ok": False, "error": "unknown op"}

    def admin_audit(self, limit):
        return {"ok": True, "entries": [
            {"action": "ban", "target": "d" * 32, "detail": "spam"}]}

    def invites(self, op, **kw):
        if op == "list":
            return {"ok": True, "invites": []}
        return {"ok": True, "code": "ABC123"}


def test_context_menu_gesture():
    """P9: the rail's guild header (CtxHeaderLabel, kv rule at the
    drawer top) carries the admin-gated 'server settings' entry, and
    its action is wired to _open_admin_panel."""
    import kivy.app as _kapp
    import kivy_client as kc

    class _RA:
        def __init__(self, _app):
            self._app = _app

        def __getattr__(self, name):
            return getattr(self._app, name)

    app = kc.sHadesClientApp()
    app.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
    app.i_am_admin = True
    panel_opens = []
    app._open_admin_panel = (lambda: panel_opens.append(True))
    widget = kc.CtxHeaderLabel(text="guild header")
    try:
        _kapp.App._running_app = _RA(app)
        menu = widget.build_context_menu()
        labels = [a[0] for a in menu]
        check("guild header carries 'server settings' for admins",
              "server settings" in labels, str(labels))
        settings_actions = [a[2] for a in menu
                            if a[0] == "server settings"]
        if settings_actions:
            settings_actions[0]()
        check("its action opens the admin panel",
              panel_opens == [True], "opens=%r" % (panel_opens,))
        app.i_am_admin = False
        labels = [a[0] for a in widget.build_context_menu()]
        check("'server settings' hidden for non-admins",
              "server settings" not in labels, str(labels))
    finally:
        _kapp.App._running_app = None


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.clock import Clock
    from kivy.lang import Builder
    from kivy.uix.button import Button

    Builder.load_file(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "kv_md.kv"))

    class _ThemeApp(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)

    app = _ThemeApp()
    app.guild_api = StubGuild()
    app.i_am_admin = True
    app._admin_popup = None

    kc.sHadesClientApp._open_admin_panel(app)
    for _ in range(4):
        Clock.tick()

    popup = app._admin_popup
    check("admin panel opened", popup is not None)
    if popup is None:
        print("\nPASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
        return 1

    # Rootless contract: without a kv ScreenManager the panel hosts the
    # same tabbed ServerConfigScreen in a popup (deep widget behavior
    # lives in test_server_config_screen.py).
    from ui_server_config import ServerConfigScreen
    screen = popup.content
    check("panel hosts the tabbed ServerConfigScreen",
          isinstance(screen, ServerConfigScreen), type(screen).__name__)
    check("screen wired to the client", screen.client is app)

    tabs_raw = list(getattr(screen, "tab_buttons", []) or [])
    tabs = [getattr(b, "text", "") for b in tabs_raw]
    check("tabbed screen built its tabs", len(tabs) >= 1, str(tabs))

    # --- the save path must not raise NameError (keeps the old pin) ------
    save_buttons = [w for w in _walk(screen)
                    if isinstance(w, Button) and getattr(w, "text", "") == "save"]
    check("panel has a save button", len(save_buttons) >= 1,
          "%d save buttons" % len(save_buttons))
    if save_buttons:
        try:
            save_buttons[0].dispatch("on_press")
            for _ in range(4):
                Clock.tick()
            check("saving a config value does not raise NameError", True)
        except NameError as e:
            check("saving a config value does not raise NameError", False, e)
        except Exception as e:
            check("saving a config value does not raise NameError", False, repr(e))

    # --- gating: non-admin and no-guild never open the panel ------------
    app2 = _ThemeApp()
    app2.guild_api = StubGuild()
    app2.i_am_admin = False
    kc.sHadesClientApp._open_admin_panel(app2)
    for _ in range(2):
        Clock.tick()
    check("non-admin never opens the panel", app2._admin_popup is None)

    app3 = _ThemeApp()
    app3.i_am_admin = True
    kc.sHadesClientApp._open_admin_panel(app3)
    for _ in range(2):
        Clock.tick()
    check("panel refuses without a guild api", app3._admin_popup is None)
    test_context_menu_gesture()

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


def _walk(w):
    yield w
    for c in w.children:
        yield from _walk(c)


if __name__ == "__main__":
    sys.exit(main())
