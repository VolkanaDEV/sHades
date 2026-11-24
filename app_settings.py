"""UI settings and config-file helpers.

Moved verbatim out of kivy_client.py's sHadesClientApp during
the phase-2 client split (methods keep their bodies, decorators and
call order; they reach the rest of the app through ``self`` exactly
as before). The app class inherits this mixin, so every method stays
reachable as ``app.<name>`` and ``kc.sHadesClientApp.<name>``.
"""

from client_widgets import _app_data_dir
import json
import os
from client_util import (hex_to_rgba, rgba_to_hex)


class SettingsMixin:
    """UI settings and config-file helpers."""

    def _ui_config_path(self):
        return os.path.join(_app_data_dir(), "ui_config.json")

    def _load_ui_config(self):
        """Load persisted user theme colors from ui_config.json, if any."""
        try:
            with open(self._ui_config_path()) as f:
                saved = json.load(f)
        except Exception:
            return
        if not isinstance(saved, dict):
            return
        for key, value in list(self.theme_colors.items()):
            if key in saved and isinstance(saved[key], str):
                try:
                    self.theme_colors[key] = hex_to_rgba(saved[key])
                except Exception:
                    pass

    def _save_ui_config(self):
        """Persist the current theme colors to ui_config.json."""
        try:
            os.makedirs(_app_data_dir(), exist_ok=True)
            data = {key: rgba_to_hex(value)
                    for key, value in self.theme_colors.items()}
            with open(self._ui_config_path(), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print("save ui config failed:", repr(e))

    def open_config(self):
        """Open the config screen (theme colors)."""
        self.root.current = "config"

    def _select_random_banners(self):
        """No-op kept for the banner kv bindings (app.banner_*_path).

        tay (receipt 2026-11-19): "cull the media images, it's bloating
        the repo and they are random placeholders" - the fork's 18 MB
        z-image-turbo set is gone from the tree and the APK. The kv
        widgets tolerate the empty source (source: app.banner_... or '').
        If real banners ever return, land them as vetted assets, not
        an 18 MB placeholder folder that ships in every binary.
        """
        self.banner_config_path = ""
        self.banner_channel_path = ""
