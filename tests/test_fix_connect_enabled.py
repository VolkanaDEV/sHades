#!/usr/bin/env python3
"""Connect button must enable once a username is typed (fresh boot).

Root cause (kv_md.kv:1332, pre-fix): the binding was
    disabled: app.connect_username_required and not username.text.strip()
Kivy's dependency extractor reads that as the watched key path
``username.text.strip`` and binds only its LAST attribute -- ``strip``,
a plain string method, not a Kivy property -- so no binding is ever
registered on ``username.text`` and typing never re-evaluates the
button. The ``app.connect_username_required`` half worked (it is a
real property), which is why toggling it did re-enable the button
while typing did nothing.

Fix: drop the ``.strip()`` call so the watched key is exactly
``username.text``. A whitespace-only username now enables the button,
but ``connect_to_guild`` strips the name in Python and rejects it
with the visible "A username is required" validation error, so the
safety net is unchanged.

Probe: fresh HOME boots to onboarding; switch to the connect screen.
Asserts:
  (a) with an empty username field the button is disabled
  (b) typing a username enables it (direct .text assignment)
  (c) clearing the field disables it again
  (d) typing via insert_text (the real typing path) enables it
  (e) a whitespace-only username enables the button but pressing
      Connect shows the username validation error, not a connect
      attempt

Run: xvfb-run -a python tests/test_fix_connect_enabled.py
"""
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades-fix-connect-enabled-")
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


def _connect_button(app):
    from client_widgets import FancyMDButton
    for widget in _walk(app.root):
        if not isinstance(widget, FancyMDButton):
            continue
        for child in widget.children:
            if getattr(child, "text", None) == "Connect":
                return widget
    return None


def _field(app):
    return app.root.get_screen("connect").ids.username


def _status(app):
    try:
        return app.root.get_screen("connect").ids.status.text
    except Exception as e:
        return "<unreadable: %r>" % (e,)


class ConnectEnabledProbe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        Clock.schedule_once(self.step_boot, 1.5)

    def step_boot(self, dt):
        check("fresh install starts on onboarding",
              self.root.current == "onboarding", self.root.current)
        self.root.current = "connect"
        Clock.schedule_once(self.step_initial, 0.6)

    def step_initial(self, dt):
        btn = _connect_button(self)
        check("Connect button found", btn is not None)
        if btn is None:
            self._finish()
            return
        self.btn = btn
        # Establish the empty baseline explicitly: on a fresh boot the
        # field shows the app's default username yet the (pre-fix)
        # button is disabled anyway, so clear it first.
        _field(self).text = ""
        Clock.schedule_once(self.step_empty, 0.4)

    def step_empty(self, dt):
        check("(a) button disabled with an empty username field",
              self.btn.disabled is True, self.btn.disabled)
        _field(self).text = "alice"
        Clock.schedule_once(self.step_typed, 0.4)

    def step_typed(self, dt):
        check("(b) button enables after typing a username",
              self.btn.disabled is False, self.btn.disabled)
        _field(self).text = ""
        Clock.schedule_once(self.step_cleared, 0.4)

    def step_cleared(self, dt):
        check("(c) button disables again after clearing the field",
              self.btn.disabled is True, self.btn.disabled)
        _field(self).insert_text("bob")
        Clock.schedule_once(self.step_inserted, 0.4)

    def step_inserted(self, dt):
        check("(d) button enables after insert_text typing",
              self.btn.disabled is False, self.btn.disabled)
        # Whitespace-only: the button enables (the .strip() is gone
        # from the binding) but Connect must still refuse it.
        scr = self.root.get_screen("connect")
        scr.ids.hub_address.text = "127.0.0.1:29221"
        _field(self).text = "   "
        Clock.schedule_once(self.step_spaces, 0.4)

    def step_spaces(self, dt):
        check("(e1) whitespace-only username enables the button",
              self.btn.disabled is False, self.btn.disabled)
        self.btn.dispatch("on_press")
        Clock.schedule_once(self.step_spaces_pressed, 0.5)

    def step_spaces_pressed(self, dt):
        check("(e2) whitespace-only username is rejected by validation",
              "A username is required" in _status(self), _status(self))
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
    probe = ConnectEnabledProbe()
    probe.run()
    sys.exit(1 if FAIL else 0)
