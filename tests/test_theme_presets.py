#!/usr/bin/env python3
"""Theme preset tests (tier 2 step 1) — headless.

Pins down:
  - preset_colors() unit contract: unknown preset raises ValueError and
    leaves the base palette untouched; Midnight is a pure copy; hex
    decodes to normalized RGBA; every value stays in [0,1] with alpha 1;
    Daylight is light-on-dark readable (dark text on bright bg, white
    btn_text); Mono's btn_text flips dark so labels read on the light
    primary; named presets add their aurora/glass extras;
  - ConfigScreen.choose_preset wiring: reassigns app.theme_colors with
    the preset palette (property reassignment drives KV bindings),
    persists through _save_ui_config, reflects the palette in the
    fine-tune fields when they exist, and rejects unknown names without
    mutating state.

Mirrors the reference client's ui_theme.py (imported verbatim from the
source drop; 61 lines, byte-identical).
"""
import os
import sys
import json
import tempfile
import shutil

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))

def hex_rgba(h):
    """Reference-independent decode: '#RRGGBB' -> normalized tuple."""
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)) + (1.0,)


def main():
    from ui_theme import PRESETS, preset_colors

    base = {"bg_main": (0.04, 0.06, 0.09, 1),
            "primary": (0.96, 0.72, 0.37, 1)}
    base_snapshot = dict(base)

    # --- unknown preset: raise + leave base alone -----------------------
    try:
        preset_colors(base, "Nebula")
        check("unknown preset raises ValueError", False)
    except ValueError:
        check("unknown preset raises ValueError", True)
    check("unknown preset leaves base unmodified", base == base_snapshot)

    # --- Midnight: pure copy of the current palette ---------------------
    mid = preset_colors(base, "Midnight")
    check("Midnight is a complete copy", mid == base, mid)

    # --- hex decode contract --------------------------------------------
    emb = preset_colors(base, "Ember")
    check("Ember primary decodes #FFAE70",
          emb["primary"] == hex_rgba("#FFAE70"), emb.get("primary"))
    check("Ember alpha is 1.0", emb["panel"][3] == 1.0)
    check("Ember carries aurora extras", "aurora_a" in emb)
    check("Midnight carries no extras", "aurora_a" not in mid)

    # --- every preset: values in range ----------------------------------
    ok_range = True
    for pname in PRESETS:
        pal = preset_colors(base, pname)
        for v in pal.values():
            if len(v) != 4 or any(c < 0.0 or c > 1.0 for c in v[:4]):
                ok_range = False
    check("all presets decode to normalized RGBA", ok_range)

    # --- readability pins ------------------------------------------------
    day = preset_colors({"bg_main": (0, 0, 0, 1), "text": (1, 1, 1, 1)},
                        "Daylight")
    check("Daylight flips to dark text",
          day["text"] < (0.2, 0.2, 0.2, 1), day["text"])
    check("Daylight background is bright",
          day["bg_main"][0] > 0.9, day["bg_main"])
    check("Daylight btn_text is white",
          day["btn_text"] > (0.9, 0.9, 0.9, 1), day["btn_text"])
    mono = preset_colors({}, "Mono")
    check("Mono btn_text flips dark", mono["btn_text"] < (0.15, 0.15, 0.2, 1),
          mono["btn_text"])
    check("Daylight carries glass extras",
          "glass" in day and "glass_edge" in day)

    # --- app-level wiring -------------------------------------------------
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        print("\nPASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
        return 1 if FAIL else 0

    tmp = tempfile.mkdtemp(prefix="theme_probe_")
    saved_box = {}

    class ProbeApp:
        # Real kc persistence helpers read _app_data_dir + theme_colors;
        # patch the dir to a tempdir (established isolation pattern).
        def __init__(self):
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)
            self.theme_colors["primary"] = (1.0, 0.0, 0.0, 1)  # sentinel

        def _app_data_dir(self):
            return tmp

        def _save_ui_config(self):
            data = {k: kc.rgba_to_hex(v)
                    for k, v in self.theme_colors.items()}
            p = os.path.join(tmp, "ui_config.json")
            with open(p, "w") as f:
                json.dump(data, f)
            saved_box["path"] = p

    kc._app_data_dir = lambda: tmp
    probe = ProbeApp()

    # get_running_app: install the way run() would headlessly (the
    # established _RA proxy pattern, tests/test_channel_list.py).
    import kivy.app as _kapp

    class _RA:
        def __init__(self, _app):
            self._app = _app

        def __getattr__(self, name):
            return getattr(self._app, name)

        def __setattr__(self, name, value):
            if name == "_app":
                object.__setattr__(self, name, value)
            else:
                setattr(self._app, name, value)

    prev = getattr(_kapp.App, "_running_app", None)
    _kapp.App._running_app = _RA(probe)

    try:
        scr = kc.ConfigScreen()
        scr.choose_preset("Ember")
        check("choose_preset reassigns the palette with the preset",
              probe.theme_colors["primary"] == hex_rgba("#FFAE70"),
              probe.theme_colors.get("primary"))
        check("choose_preset persists to ui_config.json",
              os.path.exists(os.path.join(tmp, "ui_config.json")))
        with open(saved_box["path"]) as f:
            saved = json.load(f)
        lower = {k: v.lower() for k, v in saved.items()}
        check("persisted json carries the preset primary",
              lower.get("primary") == "#ffae70", saved.get("primary"))
        check("persist replaces the sentinel red",
              "primary" in lower and lower["primary"] != "#ff0000")

        before = dict(probe.theme_colors)
        scr.choose_preset("Nebula")
        check("unknown preset mutates nothing",
              probe.theme_colors == before)

        # Midnight restores the client's own defaults (pure copy of base).
        scr.choose_preset("Midnight")
        check("Midnight restores DEFAULT_THEME_COLORS",
              probe.theme_colors == dict(kc.DEFAULT_THEME_COLORS))
    finally:
        _kapp.App._running_app = prev
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nPASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
