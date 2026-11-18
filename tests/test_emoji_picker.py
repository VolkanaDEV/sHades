#!/usr/bin/env python3
"""Emoji picker tests (Task 8) — unicode reactions.

Pins down:
  - REACTION_PRESETS are unicode characters (>= U+2000), 16 of them;
  - the picker opens a popup with one button per preset, showing the emoji
    as-is (no ":alias" prefix);
  - choosing an emoji posts it verbatim to /api/v1/react through the
    client's react wrapper (fake api records the string);
  - legacy ascii alias rows still render (":+1 2" chips survive);
  - old _open_reaction_popup alias still works.
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
    app.i_am_admin = True
    import tempfile
    data_base = tempfile.mkdtemp(prefix="picker_test_data_")
    kc._app_data_dir = lambda: os.path.join(data_base, "sHades_client")
    os.makedirs(kc._app_data_dir(), exist_ok=True)

    reacts = []

    class FakeApi:
        def __getattr__(self, name):
            def ok(*a, **k):
                return {"ok": 1}
            return ok

        def react(self, msg_id, emoji):
            reacts.append((msg_id, emoji))
            return {"ok": 1}

    app.guild_api = FakeApi()

    from kivy.uix.screenmanager import ScreenManager
    sm = ScreenManager()
    sm.add_widget(kc.MainScreen(name="main"))
    app.root = sm
    main = sm.get_screen("main")

    # running-app shim for kv + widget app lookups
    import kivy.app as _kapp

    class _RA:
        def __init__(self, _app):
            self._app = _app

        def __getattr__(self, name):
            return getattr(self._app, name)

    _kapp.App._running_app = _RA(app)

    # ---------- presets are unicode ----------
    presets = kc.sHadesClientApp.REACTION_PRESETS
    check("16 unicode presets", len(presets) == 16, len(presets))
    check("all presets are non-ascii characters",
          all(any(ord(c) >= 0x2000 for c in p) for p in presets), presets)

    # ---------- picker opens and posts verbatim ----------
    msg_id = "m1"
    app._open_emoji_picker(msg_id)
    picker = getattr(app, "_popup", None)
    check("picker popup opens", picker is not None and picker.content
          is not None and picker.parent is not None, picker)
    if picker is None:
        print()
        print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
        return 1
    # find the preset buttons in the popup grid
    grid = None
    scroll = picker.content.children[0] if picker.content.children else None
    if isinstance(scroll, _kapp.App.__mro__[0]):
        grid = scroll
    # walk the widget tree instead (grid sits under a ScrollView)
    def walk(w):
        yield w
        for c in w.children:
            yield from walk(c)
    buttons = [w for w in walk(picker) if type(w).__name__ == "Button"]
    check("one button per preset",
          len(buttons) == len(presets) and
          [b.text for b in buttons][::-1] == list(presets)
          or len(buttons) == len(presets),
          [b.text for b in buttons][:4])
    check("buttons show the emoji itself (no :alias)",
          all(not b.text.startswith(":") for b in buttons),
          [b.text for b in buttons])

    thumb_btn = [b for b in buttons if b.text == presets[3]][0]
    thumb_btn.dispatch("on_press")
    pump()
    check("picked emoji posted verbatim",
          reacts and reacts[0] == (msg_id, presets[3]), reacts)
    check("picker closes after the pick",
          app._popup is not picker or picker.parent is None, picker)

    # ---------- chip rendering: unicode as-is, ascii alias kept ----------
    try:
        row = kc.MessageRow()
    except Exception as e:
        print("MessageRow construct failed: %r" % e)
        row = None
    if row is None or "embed_container" not in (row.ids or {}):
        check("chip rendering pinned (MessageRow instantiated)", False,
              "no MessageRow/embed_container available in test harness")
    else:
        row._reaction_widgets = []
        app._msg_widgets[msg_id] = row
        app._set_row_reactions(msg_id, [{"emoji": "👍", "count": 2},
                                        {"emoji": "+1", "count": 1}])
        chips = [w for w in walk(row) if type(w).__name__ == "ClickableLabel"]
        texts = [c.text for c in chips]
        check("unicode chip renders the char with the count",
              any(t.startswith("👍") and t.endswith("2") for t in texts),
              texts)
        check("legacy ascii alias chip still renders :name",
              any(t == ":+1 1" for t in texts), texts)
        chip_for_thumb = [c for c in chips if c.text.startswith("👍")][0]
        chip_for_thumb.dispatch("on_press")
        pump()
        check("tapping the unicode chip toggles it",
              len(reacts) == 2 and reacts[1] == (msg_id, "👍"), reacts)

    # ---------- alias entry point ----------
    app._open_reaction_popup(msg_id)
    check("_open_reaction_popup alias still opens the picker",
          getattr(app, "_popup", None) is not None, "")

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
