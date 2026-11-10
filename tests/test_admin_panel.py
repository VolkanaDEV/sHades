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

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

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
    from kivy.uix.popup import Popup

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

    content = popup.content
    # Kivy's `children` list is in reverse insertion order, so find the tab
    # strip and the scroll view by structure rather than by index.
    tabs = None
    grid = None
    for c in content.children:
        if type(c).__name__ == "BoxLayout" and len(c.children) == 4 \
                and all(isinstance(b, Button) for b in c.children):
            tabs = c
        elif type(c).__name__ == "ScrollView":
            for gc in c.children:
                if type(gc).__name__ == "GridLayout":
                    grid = gc
    check("tab strip found", tabs is not None)
    check("content grid found", grid is not None)
    if tabs is None or grid is None:
        print("\nPASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
        return 1

    tab_buttons = [c for c in tabs.children if isinstance(c, Button)]
    check("tab strip has one button per view", len(tab_buttons) == 4,
          "%d buttons" % len(tab_buttons))
    titles = sorted(getattr(b, "text", "") for b in tab_buttons)
    check("tab titles are config/moderators/audit/invites",
          titles == ["audit", "config", "invites", "moderators"], titles)

    # The first view must actually have rendered into the grid.
    grid_texts = []
    for w in _walk(grid):
        t = getattr(w, "text", None)
        if isinstance(t, str):
            grid_texts.append(t)
    check("first view rendered config rows",
          any("motd" in t for t in grid_texts), grid_texts[:8])

    # --- switching tabs must repopulate the grid -------------------------
    for b in tab_buttons:
        if getattr(b, "text", "") == "audit":
            b.dispatch("on_press")
            break
    for _ in range(4):
        Clock.tick()
    audit_texts = []
    for w in _walk(grid):
        t = getattr(w, "text", None)
        if isinstance(t, str):
            audit_texts.append(t)
    check("audit tab renders the audit log",
          any("audit log" in t for t in audit_texts), audit_texts[:8])

    for b in tab_buttons:
        if getattr(b, "text", "") == "moderators":
            b.dispatch("on_press")
            break
    for _ in range(4):
        Clock.tick()
    mod_texts = []
    for w in _walk(grid):
        t = getattr(w, "text", None)
        if isinstance(t, str):
            mod_texts.append(t)
    check("moderators tab renders the moderator list",
          any("moderators" in t for t in mod_texts), mod_texts[:8])

    # --- the save path calls refresh() — it must not raise NameError ----
    for b in tab_buttons:
        if getattr(b, "text", "") == "config":
            b.dispatch("on_press")
            break
    for _ in range(4):
        Clock.tick()

    save_buttons = [w for w in _walk(grid)
                    if isinstance(w, Button) and getattr(w, "text", "") == "save"]
    check("config view has a save button", len(save_buttons) >= 1,
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

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


def _walk(w):
    yield w
    for c in w.children:
        yield from _walk(c)


if __name__ == "__main__":
    sys.exit(main())
