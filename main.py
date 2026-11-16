#!/usr/bin/env python3
"""Android entry point for sHades client.

p4a executes this module via PyRun_SimpleFile with __name__ == "__main__".
stdout/stderr are redirected to Android logcat (tags: python / stderr).

v0.4 diagnostics: everything the process ever prints (including Kivy's
Logger, GL/window startup chatter, and any traceback) is TEE'd from
process start into sHades_boot.log -- external files dir preferred
(browsable by on-phone file managers), app-storage fallback, home
fallback. The used path is recorded INSIDE the log. So the launch story
survives even when rendering is a black screen and no adb exists. On any
failure the traceback is additionally:
  1. printed to logcat, and
  2. appended to the launch log, and
  3. rendered in a BARE Kivy window (amber text on dark) so the
     phone itself shows the autopsy -- no adb required.

The console self-closes after 5 minutes.
"""

import os
import sys
import time
import traceback

_START = time.time()


def _log_path():
    """Browsable launch-log path: external files dir if reachable."""
    try:
        from jnius import autoclass  # p4a only
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        d = act.getExternalFilesDir(None)
        if d is not None:
            return os.path.join(str(d.getAbsolutePath()), "sHades_boot.log")
    except Exception:
        pass
    try:
        from android.storage import app_storage_path  # p4a only
        return os.path.join(app_storage_path(), "sHades_boot.log")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "sHades_boot.log")


class _Tee:
    """Write to the original stream AND the launch log; never raise."""

    def __init__(self, original, logf):
        self._original = original
        self._logf = logf

    def write(self, data):
        try:
            self._logf.write(data)
            self._logf.flush()
        except Exception:
            pass
        try:
            self._original.write(data)
            self._original.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self._logf.flush()
        except Exception:
            pass
        try:
            self._original.flush()
        except Exception:
            pass


def _install_tee():
    """Tee stdout/stderr into the launch log from the very start."""
    try:
        logf = open(_log_path(), "a", encoding="utf-8", buffering=1)
    except Exception:
        return
    logf.write("\n===== sHades launch %.0fs after epoch =====\n" % _START)
    logf.write("launch log path: %s\n" % _log_path())
    sys.stdout = _Tee(getattr(sys, "__stdout__", sys.stdout), logf)
    sys.stderr = _Tee(getattr(sys, "__stderr__", sys.stderr), logf)
    print("TEE installed; stdout/stderr also go to the launch log",
          flush=True)


def _show_on_screen(text):
    """Render a failure traceback in a minimal, dependency-free Kivy window."""
    try:
        import kivy
        kivy.require("2.3.0")
        from kivy.app import App
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.label import Label
        from kivy.core.window import Window
        from kivy.clock import Clock

        Window.clearcolor = (0.02, 0.02, 0.02, 1)

        root = BoxLayout(orientation="vertical", padding=8, spacing=4)
        sv = ScrollView(do_scroll_x=False)
        lbl = Label(
            text=text, color=(1, 0.85, 0.4, 1), font_size="11sp",
            halign="left", valign="top", markup=False,
            size_hint=(1, None),
        )
        lbl.bind(
            width=lambda *a: setattr(lbl, "text_size", (lbl.width, None)),
            texture_size=lambda *a: setattr(
                lbl, "height", max(lbl.texture_size[1], 1)),
        )
        sv.add_widget(lbl)
        root.add_widget(sv)
        root.add_widget(Label(
            text="sHades failed to start -- traceback above -- closes in 5 min",
            size_hint=(1, None), height=24, color=(1, 1, 1, 0.7),
            font_size="10sp",
        ))

        class CrashConsole(App):
            def build(self):
                return root

        app = CrashConsole()
        Clock.schedule_once(lambda dt: app.stop(), 300)
        app.run()
    except Exception:
        # Kivy itself is unusable: logcat is the only channel left.
        traceback.print_exc()


def main():
    """Entry point called from Android activity. All Python execution
    is wrapped so any import/runtime crash is visible on-device."""
    _install_tee()
    print("PYTHON: main() starting", flush=True)
    try:
        print("PYTHON: importing kivy_client", flush=True)
        from kivy_client import sHadesClientApp
        print("PYTHON: imported kivy_client", flush=True)
        app = sHadesClientApp()
        print("PYTHON: created app", flush=True)
        app.run()
        print("PYTHON: app.run() returned", flush=True)
    except Exception:
        tb = traceback.format_exc()
        print(tb, flush=True)
        try:
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write("\n--- TRACEBACK ---\n" + tb)
        except Exception:
            pass
        # Keep process alive so the on-screen console can be read.
        time.sleep(30)
        _show_on_screen(tb)
        sys.exit(1)


if __name__ == "__main__":
    main()
