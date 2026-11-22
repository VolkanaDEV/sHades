#!/usr/bin/env python3
"""Task 2 group D: popup buttons meet the 40px minimum (DESIGN.md) and
popup TextInputs are themed like the add-server popup (input_bg field,
text-coloured text and cursor) instead of the white Kivy default.

Popups covered: create-channel, roles, edit, search, MOTD, join, DM.
The roles list-toggle rows stay 24px by design: they are scrollable
list rows, not action buttons.

Run: xvfb-run -a python tests/test_task2_popup_polish.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402


def walk(widget):
    """Yield a widget and all of its descendants."""
    yield widget
    for child in getattr(widget, "children", []):
        yield from walk(child)


class Probe(kc.sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.passes = 0
        self.my_hash = "a" * 32
        self.username = "probeuser"
        # permission gates for the create-channel / roles popups
        self.guild_api = object()
        self.i_am_admin = True
        self.my_roles = [{"perms": self.PERM_MANAGE_ROLES
                          | self.PERM_MANAGE_CHANNEL | self.PERM_ADMIN}]
        Clock.schedule_once(self.check_create_channel, 1.8)

    def check(self, condition, message, extra=""):
        print(("PASS " if condition else "FAIL ") + message
              + ((" -- " + str(extra)) if extra and not condition else ""),
              flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def themed(self, ti, name):
        bg = list(self.theme_colors["input_bg"])
        self.check(list(ti.background_color) == bg,
                   "%s TextInput uses input_bg" % name, ti.background_color)
        self.check(list(ti.foreground_color)
                   == list(self.theme_colors["text"]),
                   "%s TextInput uses theme text colour" % name)
        self.check(list(ti.cursor_color) == list(self.theme_colors["text"]),
                   "%s TextInput uses theme cursor colour" % name)

    def tall(self, widget, name):
        self.check(widget.height >= 39.9,
                   "%s is at least 40px tall" % name, widget.height)

    def fits(self, popup, name):
        """Every direct child of the popup content must sit inside the
        content box -- a fixed-height stack that is taller than the box
        pokes its top child into the popup title bar (seen at 640x480)."""
        box = popup.content
        top = max(w.top for w in box.children)
        bottom = min(w.y for w in box.children)
        self.check(top <= box.top + 0.5,
                   "%s fits inside its content box (top)" % name,
                   top - box.top)
        self.check(bottom >= box.y - 0.5,
                   "%s fits inside its content box (bottom)" % name,
                   box.y - bottom)

    def by_text(self, root, text):
        for w in walk(root):
            if getattr(w, "text", None) == text:
                return w
        return None

    def check_create_channel(self, dt):
        self.root.current = "main"
        self._open_create_channel_popup()
        popup = self._popup
        self.check(popup is not None, "create-channel popup opens")
        if popup is None:
            return self.finish()
        box = popup.content
        name_in = box.children[3]  # vertical box: close, create, kind, name, title
        self.themed(name_in, "create-channel name")
        self.check(name_in.height >= 39.9,
                   "create-channel name field is 40px tall", name_in.height)
        kind_box = box.children[2]
        self.tall(kind_box, "create-channel kind row")
        for w in walk(kind_box):
            if type(w).__name__ == "Button":
                self.tall(w, "create-channel kind button %r" % w.text)
        self.tall(self.by_text(box, "create"), "create-channel create button")
        self.tall(self.by_text(box, "close"), "create-channel close button")
        self.fits(popup, "create-channel popup")
        popup.dismiss()
        Clock.schedule_once(self.check_roles, 0.3)

    def check_roles(self, dt):
        self._open_roles_popup("b" * 32, "alice")
        popup = self._popup
        self.check(popup is not None, "roles popup opens")
        if popup is None:
            return self.finish()
        box = popup.content
        row = box.children[1]  # close, row, scroll, title
        self.tall(row, "roles create row")
        name_in = row.children[1]
        self.themed(name_in, "roles name")
        self.tall(self.by_text(box, "create"), "roles create button")
        self.tall(self.by_text(box, "close"), "roles close button")
        self.fits(popup, "roles popup")
        popup.dismiss()
        Clock.schedule_once(self.check_edit_search, 0.3)

    def check_edit_search(self, dt):
        self._open_edit_popup("m1", "hello")
        popup = self._popup
        self.check(popup is not None, "edit popup opens")
        if popup is not None:
            box = popup.content
            ti = box.children[1]
            self.themed(ti, "edit message")
            self.tall(self.by_text(box, "save"), "edit save button")
            self.tall(self.by_text(box, "cancel"), "edit cancel button")
            self.fits(popup, "edit popup")
            popup.dismiss()
        self.open_search()
        popup = self._popup
        self.check(popup is not None, "search popup opens")
        if popup is not None:
            box = popup.content
            query = box.children[2]
            self.themed(query, "search query")
            self.tall(self.by_text(box, "search"), "search button")
            self.fits(popup, "search popup")
            popup.dismiss()
        Clock.schedule_once(self.check_motd_join, 0.3)

    def check_motd_join(self, dt):
        self.server_motd = "hello world"
        popup = self._build_motd_popup()
        self.check(popup is not None, "MOTD popup builds")
        if popup is not None:
            close = self.by_text(popup.content, "Close")
            self.tall(close, "MOTD close button")
        self._open_join_prompt()
        popup = self._join_popup
        self.check(popup is not None, "join popup opens")
        if popup is not None:
            box = popup.content
            entry = box.children[2]
            self.themed(entry, "join code")
            self.check(entry.height >= 39.9,
                       "join code field is 40px tall", entry.height)
            row = box.children[1]
            self.tall(row, "join button row")
            self.tall(self.by_text(box, "join"), "join submit button")
            self.tall(self.by_text(box, "cancel"), "join cancel button")
            self.fits(popup, "join popup")
            popup.dismiss()
        Clock.schedule_once(self.check_dm, 0.3)

    def check_dm(self, dt):
        self._open_dm_popup("b" * 32, "alice")
        popup = self._dm_popup
        self.check(popup is not None, "DM popup opens")
        if popup is not None:
            box = popup.content
            row = box.children[1]
            self.tall(row, "DM input row")
            text_in = row.children[1]
            self.themed(text_in, "DM message")
            self.tall(self.by_text(box, "send"), "DM send button")
            self.tall(self.by_text(box, "close"), "DM close button")
            self.fits(popup, "DM popup")
            popup.dismiss()
        self.finish()

    def finish(self):
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    size = os.environ.get("T2D_SIZE", "1280x720")
    Window.size = tuple(int(v) for v in size.split("x"))
    probe = Probe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
