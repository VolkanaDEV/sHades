#!/usr/bin/env python3
"""Voice preferences + user-settings page lanes (tay 2026-11-25).

Pins down the client-side store and the tabbed settings screen:
  - client_voice_prefs: defaults on missing file, roundtrip, mode
    validation, vad clamping, ptt-key shape validation;
  - ptt key dispatch: match on key code, scancode tolerance, clear;
  - the settings screen: tabs switch, a mode tap reaches a real engine
    and persists, the vad slider persists + applies, capture binds the
    next key (esc cancels), clear removes the binding.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""), flush=True)


def ui_lane_enabled():
    try:
        import kivy.utils  # noqa: F401
        return True
    except Exception:
        return False


def main():
    import client_voice_prefs as vp
    import tempfile

    tmp = os.path.join(tempfile.mkdtemp(), "voice.json")

    # defaults on missing file
    p = vp.load_voice_prefs(path=tmp + "-missing")
    check("missing file yields defaults",
          p["mode"] == "always" and p["vad_rms"] == 800
          and p["ptt_key"] is None)

    # mode validation + vad clamp + ptt-key shape
    vp.save_voice_prefs({"mode": "ptt", "vad_rms": 99999,
                         "ptt_key": {"key": 275, "scancode": 139,
                                     "name": "key right"}},
                        path=tmp)
    p = vp.load_voice_prefs(path=tmp)
    check("save/load roundtrip with clamps",
          p["mode"] == "ptt" and p["vad_rms"] == vp.VAD_MAX
          and p["ptt_key"]["key"] == 275,
          p)

    p = vp.load_voice_prefs(path=tmp)
    check("bogus mode survives load as default",
          vp.save_voice_prefs({"mode": "bogus"}, path=tmp)
          and vp.load_voice_prefs(path=tmp)["mode"] == "always")

    p = vp.load_voice_prefs(path=tmp)
    check("non-dict ptt_key rejected", bool(
        vp.save_voice_prefs({"ptt_key": "space"}, path=tmp)
        and vp.load_voice_prefs(path=tmp)["ptt_key"] is None))

    # dispatch: match, scancode tolerance, mismatch, clear
    prefs = {"ptt_key": {"key": 275, "scancode": 139, "name": "right"}}
    check("ptt match on code+scancode",
          vp.ptt_key_matches(prefs, 275, 139))
    check("ptt match tolerates scancode 0 event",
          vp.ptt_key_matches(prefs, 275, 0))
    check("ptt mismatch scancode rejected",
          not vp.ptt_key_matches(prefs, 275, 140))
    check("ptt mismatch code rejected",
          not vp.ptt_key_matches(prefs, 276, 139))
    check("no binding never matches",
          not vp.ptt_key_matches({"ptt_key": None}, 275, 139))

    # names
    check("letter names itself", vp.key_name(97, 38, "a") == "key 'a'")
    check("arrows map to names", vp.key_name(276) == "left")

    # ---- settings screen (kivy) ----------------------------------------
    if ui_lane_enabled():
        from kivy.clock import Clock
        from kivy.uix.screenmanager import ScreenManager
        import kivy.app as _kapp
        import app_voice as av
        import kivy_client as kc
        from ui_user_settings import UserSettingsScreen

        def pump():
            Clock.tick()

        app = kc.sHadesClientApp()

        class _RA:
            def __init__(self, a):
                self._a = a

            def __getattr__(self, name):
                return getattr(self._a, name)

        _kapp.App._running_app = _RA(app)
        sm = ScreenManager()
        sm.add_widget(kc.MainScreen(name="main"))
        app.root = sm

        engine = av.VoiceEngine()
        app.my_user_hash = "cef43e" + "0" * 58
        app._voice_prefs = dict(vp.DEFAULTS)
        app._voice_engine = engine
        screen = UserSettingsScreen(client=app, name="user_settings")
        screen.build_tabs()
        pump()
        check("screen builds with voice tab default",
              screen._tab_buttons and screen._status is not None
              and screen._mode_row is not None)

        # mode tap reaches the engine + persists
        screen._pick_mode("vad")
        p = vp.load_voice_prefs(path=vp.prefs_path(app))
        check("mode tap applies + persists",
              engine.voice_mode == "vad" and p["mode"] == "vad")
        check("mode row repaints", screen._mode_row is not None)

        # vad slider persists + applies
        screen._pick_vad(1234)
        check("vad applies + persists",
              engine.vad_rms == 1234
              and vp.load_voice_prefs(
                  path=vp.prefs_path(app))["vad_rms"] == 1234)

        # capture binds the next key; esc cancels
        screen._start_capture(screen._cap_btn, screen._ptt_cur)
        screen._capture_key_down(None, 27, 0, None, [])
        check("esc cancels capture",
              not screen._capturing
              and vp.load_voice_prefs(
                  path=vp.prefs_path(app))["ptt_key"] is None)
        screen._start_capture(screen._cap_btn, screen._ptt_cur)
        # a space presses with codepoint " ": it strips empty, so the
        # name comes from the key-code map ("space"), not the quote form
        screen._capture_key_down(None, 32, 44, " ", [])
        p = vp.load_voice_prefs(path=vp.prefs_path(app))
        check("key capture binds + persists",
              p["ptt_key"] and p["ptt_key"]["key"] == 32
              and p["ptt_key"]["name"] == "space", p["ptt_key"])
        screen._clear_ptt_key(screen._ptt_cur)
        p = vp.load_voice_prefs(path=vp.prefs_path(app))
        check("clear removes the binding", p["ptt_key"] is None)

        # account tab rebuilds without error
        screen._switch("account")
        check("account tab renders", len(screen._inner.children) >= 3)
    else:
        print(" (kivy unavailable: ui lanes skipped)")

    print("RESULT: %d pass, %d fail" % (len(PASS), len(FAIL)))
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
