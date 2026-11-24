#!/usr/bin/env python3
"""UIX P3-2 (aurora alpha): saving the theme in the config screen must
not destroy the aurora tokens' translucency -- the hex fields carry no
alpha, so save_config preserves each key's live alpha.

  - custom theme with aurora alphas 0.22 / 0.18 / 0.20 survives a Save
    with its RGB changed and its alpha untouched
  - opaque keys still save with alpha 1.0
  - all 20 config rows have human-readable labels (no raw key names)

Run: xvfb-run -a python tests/test_uix_aurora_alpha.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402

TMP = tempfile.mkdtemp(prefix="shades-uix-aurora-")
# _save/_load_ui_config live in app_settings.py (SettingsMixin) and use
# THAT module's _app_data_dir import -- patching kc._app_data_dir alone
# still writes the real ~/.sHades_client/ui_config.json (found 2026-11-21:
# a test run had persisted its red/green/blue aurora theme there).
import app_settings as _app_settings  # noqa: E402
kc._app_data_dir = lambda: TMP
_app_settings._app_data_dir = lambda: TMP

AURORA = {"aurora_a": (1.0, 0.0, 0.0, 0.22),
          "aurora_b": (0.0, 1.0, 0.0, 0.18),
          "aurora_c": (0.0, 0.0, 1.0, 0.20)}
AURORA_LABELS = {"aurora_a": "Aurora amber",
                 "aurora_b": "Aurora teal",
                 "aurora_c": "Aurora violet"}


class AuroraProbe(kc.sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.passes = 0
        theme = dict(kc.DEFAULT_THEME_COLORS)
        theme.update(AURORA)
        self.theme_colors = theme
        self.root.current = "config"
        Clock.schedule_once(self.check_labels, 1.5)

    def check(self, condition, message):
        print(("PASS " if condition else "FAIL ") + message, flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def check_labels(self, dt):
        cfg = self.root.get_screen("config")
        self.check(len(cfg._color_fields) == 20,
                   "config screen renders 20 colour rows (got %d)"
                   % len(cfg._color_fields))
        labels = {key: lbl.text for key, lbl in
                  zip(kc.THEME_KEYS, cfg._color_labels)}
        raw = [k for k, text in labels.items() if text == k]
        self.check(not raw, "no row shows a raw key name as its label "
                   "(raw: %r)" % (raw,))
        for key, want in AURORA_LABELS.items():
            self.check(labels.get(key) == want,
                       "row %s is labelled %r (got %r)"
                       % (key, want, labels.get(key)))
        # Change the aurora RGB (as a user editing the hex would) and save.
        cfg._color_fields["aurora_a"].text = "#00ff00"
        cfg._color_fields["aurora_b"].text = "#0000ff"
        cfg._color_fields["aurora_c"].text = "#ff0000"
        cfg._color_fields["panel"].text = "#000000"
        cfg.save_config()
        for key, rgb in (("aurora_a", (0.0, 1.0, 0.0)),
                         ("aurora_b", (0.0, 0.0, 1.0)),
                         ("aurora_c", (1.0, 0.0, 0.0))):
            got = self.theme_colors[key]
            want = rgb + (AURORA[key][3],)
            self.check(tuple(got) == want,
                       "save keeps %s alpha %.2f with new RGB (got %r)"
                       % (key, want[3], got))
        panel = self.theme_colors["panel"]
        self.check(tuple(panel) == (0.0, 0.0, 0.0, 1.0),
                   "opaque keys still save with alpha 1.0 (got %r)"
                   % (panel,))
        try:
            status = cfg.ids["config_status"].text
        except Exception:
            status = None
        self.check(status == "Theme saved.",
                   "config screen reports the save (got %r)" % (status,))
        import json as _json
        persisted = os.path.join(TMP, "ui_config.json")
        self.check(os.path.isfile(persisted),
                   "save persists ui_config.json to the app data dir")
        if os.path.isfile(persisted):
            with open(persisted) as f:
                saved = _json.load(f)
            self.check(saved.get("aurora_a") == "#00ff00",
                       "persisted aurora_a hex matches the edit (got %r)"
                       % (saved.get("aurora_a"),))
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (1280, 720)
    try:
        probe = AuroraProbe()
        probe.run()
        sys.exit(1 if probe.failures else 0)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
