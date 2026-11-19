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


def _export_bootlog_for_user():
    """Mirror sHades_boot.log into the user-visible Downloads collection
    ( tay 2026-11-19: "crashes are difficult to debug when adb is
    unavailable" ). MediaStore.Downloads on API 29+ -- own rows need no
    runtime permission and every file manager lists Downloads; on
    API 26-28 the direct public Downloads dir rides the declared
    WRITE_EXTERNAL_STORAGE. Java-side copy only (the v0.15 lesson:
    jbyte/jarray imports fail on-device; autoclass + Files.copy is the
    proven bridge). Never raises; off-device or degraded bridges are a
    quiet no-op. Returns True when a copy landed in Downloads."""
    try:
        src_path = _log_path()
        with open(src_path, "rb") as f:
            if not f.read(1) and f.seek(0, 2) == 0:
                return False  # nothing to export yet
    except OSError:
        return False
    try:
        from jnius import autoclass  # p4a only
    except Exception:
        return False
    try:
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        SDK_INT = int(autoclass("android.os.Build$VERSION").SDK_INT)
        if SDK_INT >= 29:
            resolver = act.getContentResolver()
            DL = autoclass("android.provider.MediaStore$Downloads")
            coll = DL.getContentUri(DL.VOLUME_EXTERNAL_PRIMARY)
            uri = None
            # find an existing row first so re-boots overwrite instead
            # of stacking sHades_boot (1).log, (2).log, ...
            try:
                cur = resolver.query(coll, ["_id"],
                                     "_display_name = "
                                     "'sHades_boot.log'",
                                     None, None)
                try:
                    if cur is not None and cur.moveToFirst():
                        uri = autoclass(
                            "android.content.ContentUris"
                        ).withAppendedId(coll, cur.getLong(0))
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
            except Exception:
                uri = None
            if uri is None:
                cv = autoclass("android.content.ContentValues")()
                cv.put("_display_name", "sHades_boot.log")
                cv.put("mime_type", "text/plain")
                uri = resolver.insert(coll, cv)
                if uri is None:
                    return False
            try:
                out = resolver.openOutputStream(uri)
                fin = autoclass("java.io.FileInputStream")(src_path)
                copied = autoclass("java.nio.file.Files").copy(fin, out)
                try:
                    fin.close()
                    out.close()
                except Exception:
                    pass
                return copied > 0
            except Exception:
                return False
        # API 26-28: legacy public Downloads dir
        env = autoclass("android.os.Environment")
        dest = env.getExternalStoragePublicDirectory(
            env.DIRECTORY_DOWNLOADS)
        dest_path = os.path.join(str(dest.getAbsolutePath()),
                                 "sHades_boot.log")
        with open(src_path, "rb") as fin, open(dest_path, "wb") as fout:
            fout.write(fin.read())
        return True
    except Exception as e:
        print("bootlog export failed:", e, flush=True)
        return False


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
    # first mirror: the log exists from the tee header onward, so even
    # a pre-app crash leaves a readable copy in Downloads
    try:
        _export_bootlog_for_user()
    except Exception:
        pass
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
        # mirror the (now fatal) log into Downloads: no adb needed
        try:
            _export_bootlog_for_user()
        except Exception:
            pass
        # Show the traceback immediately; the console itself remains open
        # for five minutes. A delay here left a blank screen after a crash.
        _show_on_screen(tb)
        sys.exit(1)


if __name__ == "__main__":
    main()
