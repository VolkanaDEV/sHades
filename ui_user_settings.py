#!/usr/bin/env python3
"""ui_user_settings.py -- tabbed user settings screen (tay 2026-11-25).

Follows the ui_server_config idiom: own module (kivy_client would be an
import cycle), client object passed in, button-row tabs (MDTabs stays
out; the Android-fallback tab row from the server-config plan), content
rebuilt per tab on the main thread.

Tabs:
  Voice    transmit mode (always / ptt / voc), voc RMS threshold,
           and PTT key setup: a capture row binding the next Window
           key down (esc cancels), persistence via client_voice_prefs
           into voice.json, applied to the live engine if one exists.
  Account  viewer identity as the client knows it, plus where the
           voice prefs file lives.

Android note: only the key events the platform actually delivers reach
Window.on_key_down; desktop keys and media/hardware buttons that arrive
work out of the box, the rest keeps the on-screen hold button.
"""
import client_voice_prefs as prefs_mod
from kivy.app import App
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.screenmanager import Screen
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider

ACTIVE_BG = (0.25, 0.55, 0.90, 1)
IDLE_BG = (0.13, 0.15, 0.18, 1)

TABS = (("voice", "voice"), ("account", "account"))


def _row_label(text, w=0.42, h=26):
    lbl = Label(text=text, size_hint_x=w, halign="left", valign="middle",
                size_hint_y=None, height=dp(h), font_size=sp(12),
                color=(0.85, 0.87, 0.9, 1))
    lbl.bind(size=lambda inst, s: setattr(inst, "text_size", s))
    return lbl


def _btn(text, width=90, h=34):
    return Button(text=text, size_hint_x=None, width=dp(width),
                  size_hint_y=None, height=dp(h),
                  background_normal="", background_color=IDLE_BG)


def _value_label(text, w=0.2):
    lbl = Label(text=text, size_hint_x=w, halign="left", valign="middle",
                size_hint_y=None, height=dp(26), font_size=sp(12),
                color=(0.65, 0.68, 0.72, 1))
    lbl.bind(size=lambda inst, s: setattr(inst, "text_size", s))
    return lbl


class UserSettingsScreen(Screen):

    def __init__(self, client=None, name=None, **kw):
        super().__init__(name=name or "user_settings", **kw)
        self.client = client
        self._capturing = False
        self._tab_buttons = {}
        self._content = None
        self._status = None
        self._mode_row = None
        self._vad_val = None
        self._ptt_cur = None
        self._cap_btn = None

    # ---- tab skeleton ---------------------------------------------------

    def build_tabs(self):
        root = self.ids["settings_root"] if "settings_root" in getattr(
            self, "ids", {}) else self
        root.clear_widgets()
        col = BoxLayout(orientation="vertical", spacing=dp(6),
                        padding=[dp(10), dp(8)])
        head = BoxLayout(size_hint_y=None, height=dp(44))
        title = _row_label("user settings", w=1, h=44)
        title.size_hint_x = 1
        head.add_widget(title)
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        for key, label in TABS:
            b = _btn(label, width=110, h=36)
            b.bind(on_press=lambda inst, k=key: self._switch(k))
            self._tab_buttons[key] = b
            row.add_widget(b)
        col.add_widget(row)
        self._status = _value_label("", w=1)
        self._status.size_hint_x = 1
        col.add_widget(self._status)
        self._content = BoxLayout(orientation="vertical", spacing=dp(6))
        sv = ScrollView(do_scroll_x=False)
        inner = BoxLayout(orientation="vertical",
                          size_hint_y=None, spacing=dp(6),
                          padding=[0, dp(4)])
        inner.bind(minimum_height=inner.setter("height"))
        self._inner = inner
        sv.add_widget(inner)
        self._content.add_widget(sv)
        col.add_widget(self._content)
        root.add_widget(col)
        self._switch(TABS[0][0])
        return self

    def _switch(self, key):
        for k, b in self._tab_buttons.items():
            b.background_color = ACTIVE_BG if k == key else IDLE_BG
        self._inner.clear_widgets()
        builder = {"voice": self._build_voice,
                   "account": self._build_account}.get(key)
        if builder is not None:
            builder()

    def _say(self, text):
        if self._status is not None:
            self._status.text = text

    # ---- voice tab ------------------------------------------------------

    def _live_engine(self):
        engine = getattr(self.client, "_voice_engine", None)
        return engine

    def _row_mode(self):
        prefs = self._prefs()
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        row.add_widget(_row_label("transmit mode"))
        active = prefs.get("mode", "always")
        for mode, label in (("always", "always"), ("ptt", "ptt"),
                            ("vad", "voc")):
            b = _btn(label, width=84)
            b.background_color = ACTIVE_BG if mode == active else IDLE_BG
            b.bind(on_press=lambda inst, m=mode: self._pick_mode(m))
            row.add_widget(b)
        self._mode_row = row
        self._inner.add_widget(row)

    def _row_vad(self):
        prefs = self._prefs()
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        row.add_widget(_row_label("voc threshold (rms)"))
        val = _value_label(str(prefs.get("vad_rms", 800)), w=0.14)
        sl = Slider(min=prefs_mod.VAD_MIN, max=prefs_mod.VAD_MAX,
                    value=prefs.get("vad_rms", 800))
        sl.bind(value=lambda inst, v: setattr(val, "text",
                                              str(int(v))))
        sl.bind(on_release=lambda inst: self._pick_vad(
            int(inst.value)))
        self._vad_val = val
        row.add_widget(sl)
        row.add_widget(val)
        self._inner.add_widget(row)

    def _row_ptt_key(self):
        prefs = self._prefs()
        pk = prefs.get("ptt_key")
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        row.add_widget(_row_label("ptt key"))
        cur = _value_label(pk["name"] if pk else "none", w=0.2)
        cap = _btn("capture", width=84)
        cap.bind(on_press=lambda inst: self._start_capture(
            cap, cur))
        clear = _btn("clear", width=70)
        clear.bind(on_press=lambda inst: self._clear_ptt_key(cur))
        self._ptt_cur = cur
        self._cap_btn = cap
        row.add_widget(cur)
        row.add_widget(cap)
        row.add_widget(clear)
        self._inner.add_widget(row)

    def _build_voice(self):
        note = Label(
            text=("mode gates tx in the pacer: always = open mic, "
                  "ptt = the bound key, voc = voice-activated (rms "
                  "gate). esc cancels a capture."),
            size_hint_y=None, height=dp(40), font_size=sp(10),
            color=(0.55, 0.58, 0.62, 1), halign="left",
            valign="middle")
        note.bind(size=lambda inst, s: setattr(inst, "text_size", s))
        self._inner.add_widget(note)
        self._row_mode()
        self._row_vad()
        self._row_ptt_key()

    # ---- account tab ----------------------------------------------------

    def _build_account(self):
        app = App.get_running_app() or self.client
        my_hash = getattr(app, "my_user_hash", "") or "unknown"
        rows = (
            ("user", my_hash[:10] + ("..." if len(my_hash) > 10 else "")),
            ("voice prefs", prefs_mod.prefs_path(app)),
            ("tx mode", self._prefs().get("mode", "always")),
        )
        for label, value in rows:
            row = BoxLayout(size_hint_y=None, height=dp(30),
                            spacing=dp(6))
            row.add_widget(_row_label(label))
            val = _value_label(str(value), w=0.55)
            val.size_hint_x = 0.55
            row.add_widget(val)
            self._inner.add_widget(row)

    # ---- actions ---------------------------------------------------------

    def _prefs(self):
        app = App.get_running_app() or self.client
        stored = getattr(app, "_voice_prefs", None)
        if not isinstance(stored, dict):
            app._voice_prefs = prefs_mod.load_voice_prefs(app=app)
            stored = app._voice_prefs
        return stored

    def _pick_mode(self, mode):
        prefs = self._prefs()
        prefs["mode"] = mode
        ok = prefs_mod.save_voice_prefs(prefs, app=App.get_running_app())
        engine = self._live_engine()
        fn = getattr(engine, "set_voice_mode", None) if engine else None
        if fn is not None:
            fn(mode)
        self._row_mode()
        self._say("tx mode: %s%s" % (mode, "" if ok else " (unsaved)"))

    def _pick_vad(self, rms):
        prefs = self._prefs()
        prefs["vad_rms"] = int(rms)
        ok = prefs_mod.save_voice_prefs(prefs, app=App.get_running_app())
        engine = self._live_engine()
        if engine is not None:
            try:
                engine.vad_rms = int(rms)
            except Exception:
                pass
        self._say("voc threshold: %d%s" % (int(rms),
                                           "" if ok else " (unsaved)"))

    def _start_capture(self, btn, cur):
        if self._capturing:
            return
        self._capturing = True
        btn.text = "press any key"
        self._say("press the ptt key (esc cancels)")
        try:
            from kivy.core.window import Window
            Window.bind(on_key_down=self._capture_key_down)
        except Exception:
            self._capturing = False
            self._say("no key surface on this platform")

    def _capture_key_down(self, window, key, scancode, codepoint, modifier):
        if not self._capturing:
            return
        try:
            from kivy.core.window import Window
            Window.unbind(on_key_down=self._capture_key_down)
        except Exception:
            pass
        self._capturing = False
        if int(key) == 27:  # esc: cancel, binding untouched
            self._say("ptt key capture cancelled")
            return
        prefs = self._prefs()
        prefs["ptt_key"] = {
            "key": int(key),
            "scancode": int(scancode or 0),
            "name": prefs_mod.key_name(key, scancode, codepoint),
        }
        ok = prefs_mod.save_voice_prefs(prefs, app=App.get_running_app())
        self._row_ptt_key()
        self._say("ptt key bound: %s%s"
                  % (prefs["ptt_key"]["name"], "" if ok else " (unsaved)"))

    def _clear_ptt_key(self, cur):
        prefs = self._prefs()
        prefs["ptt_key"] = None
        ok = prefs_mod.save_voice_prefs(prefs, app=App.get_running_app())
        self._row_ptt_key()
        self._say("ptt key cleared%s" % ("" if ok else " (unsaved)"))
