#!/usr/bin/env python3
"""Sanguis script runner for micron pages — stdlib only, NO Kivy.

Spawns the Sanguis floor (``bch``) with the compiled interpreter
(``sanguis/interp.bc``) to run one page script, and parses the single
JSON line the runner prints back. The interpreter itself is total:
a bad script returns ok 0.0 with an error string, never a host fault;
this module only adds the process boundary around it (timeouts,
missing files, unparseable output).

Wire format (runner cell ``run`` in interp.bc): the host invokes
``[bch, interp.bc, run, scriptfile]`` and the runner prints exactly
one stdout line::

    {"ok": 1, "result": <value>, "out": [...], "gpu": [[...], ...],
     "audio": [[...], ...]}

The ``audio`` field is the queued audio commands (``audio_note`` /
``audio_noise`` records, ``[name, args...]``), mirroring ``gpu``:
the interpreter records them, never executes them; the host drains
them into WAV bytes (see audio_drain). A runner that predates the
audio field omits it, and run_script defaults it to [].

Exit 0 on script success AND script error; nonzero only when the
runner itself fails (it still prints a JSON error line first).

Bindings travel as an optional second argument: a temp file with one
``name=value`` line per binding (string values verbatim, numbers as
repr, so 1.0 stays ``1.0``), so the argv is ``[bch, interp.bc, run,
scriptfile, bindingsfile]`` when bindings are non-empty and exactly
the four-element form when they are empty. The runner reads args()[0]
as the script path and args()[1] as the optional bindings file (the
transport the WIRING-DESIGN.md §3 sketch proposes: Sanguis has no env
or stdin builtin, so argv is the only channel a binding can cross).
Both temp files are deleted after the run.

NOTE (2026-11-23): the runner as shipped in interp.bc at the time
this module was written (md5 13cb99beeae551e8790e2158e6cc81c8) reads
only args()[0] and calls script_run(src, []) — the bindings file is
passed but ignored, so `page` / `gpu_present` are unbound until the
runner is extended to read args()[1]. Passing the extra arg is
harmless (the runner ignores it, exit stays 0), so no client change
is needed when the extension lands.

Android note (2026-11-23): files extracted from an APK lose the
executable bit, so a bundled bch can never run in place on Android.
run_script therefore executes a copy staged into the app's private
dir with mode 0o755 (executable_bch_path); scripting_available is
True on Android once the NDK-built bch actually ships in the APK,
and False until then.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

BCH_NAME = "bch.exe" if sys.platform == "win32" else "bch"
INTERP_NAME = "interp.bc"


def _is_android():
    """Android has no bch build yet (NDK build is a separate job), so
    scripting is unavailable there. python-for-android reports
    sys.platform == "linux", so the ANDROID_* env vars are the tell
    (sys.platform == "android" is checked too, for completeness)."""
    return (sys.platform == "android"
            or "ANDROID_ROOT" in os.environ
            or "ANDROID_DATA" in os.environ)


def _private_dir():
    """Writable private directory on this device.

    On Android (p4a) the app's internal storage, so scoped storage
    never blocks the write; on desktop ~/.sHades_client — the same
    pattern client_widgets._app_data_dir uses. Only used to stage
    an executable copy of bch (see executable_bch_path)."""
    try:
        from android.storage import app_storage_path  # p4a only
        return app_storage_path()
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".sHades_client")


def _app_dir():
    """Directory holding the app's bundled resources (kv_md.kv, media/).

    The 6-line sys._MEIPASS pattern from kivy_client._app_dir,
    duplicated here: importing kivy_client would be a cycle, and this
    module must stay importable without Kivy so tests run headless.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.abspath(__file__))


def sanguis_data_path(name):
    """<repo>/sanguis/<name> (or sys._MEIPASS/sanguis/<name> frozen)."""
    return os.path.join(_app_dir(), "sanguis", name)


def bch_path():
    """Path of the floor binary for this platform."""
    return sanguis_data_path(BCH_NAME)


def interp_path():
    """Path of the compiled interpreter."""
    return sanguis_data_path(INTERP_NAME)


def executable_bch_path():
    """Path to pass to subprocess for bch.

    On Linux/Windows the bundled binary as-is. On Android the APK
    extraction strips the executable bit, so the bundled copy can
    never run in place: copy it once into the private dir and chmod
    0o755 there, re-copying only when the bundled size changes (a
    new binary shipped) or the staged copy lost its exec bit.
    Raises OSError when the copy/chmod fails; run_script turns
    that into a runner failure."""
    bundled = bch_path()
    if not _is_android():
        return bundled
    dest_dir = os.path.join(_private_dir(), "sanguis")
    dest = os.path.join(dest_dir, BCH_NAME)
    os.makedirs(dest_dir, exist_ok=True)
    bundled_size = os.path.getsize(bundled)
    if (os.path.isfile(dest)
            and os.path.getsize(dest) == bundled_size
            and os.access(dest, os.X_OK)):
        return dest
    shutil.copyfile(bundled, dest)
    os.chmod(dest, 0o755)
    return dest


def scripting_available():
    """True when bch + interp.bc both exist, on any platform.

    On Android that also means the NDK-built bch has landed in the
    APK (buildozer include_patterns); until then it is False and
    script blocks render the fallback notice. The executable bit
    the APK strips is restored at run time by executable_bch_path,
    so availability here is purely about the files existing."""
    return os.path.isfile(bch_path()) and os.path.isfile(interp_path())


def gpu_present_value():
    """1.0 where the host can drain the GPU queue,
    0.0 when scripting is unavailable."""
    return 1.0 if scripting_available() else 0.0


def _format_bindings(bindings):
    """Bindings ([[name, value], ...]) -> ``name=value`` lines."""
    lines = []
    for pair in bindings or []:
        name, value = pair[0], pair[1]
        if isinstance(value, str):
            text = value
        elif isinstance(value, bool):
            text = "1.0" if value else "0.0"
        elif isinstance(value, (int, float)):
            text = repr(float(value))
        else:
            text = str(value)
        lines.append("%s=%s" % (name, text))
    return "".join(line + "\n" for line in lines)


def _failure(message):
    """A runner failure (not a script error): ok 0.0, out/gpu/audio
    empty, the cause in the result string."""
    return {"ok": 0.0, "result": "runner failed: " + message,
            "out": [], "gpu": [], "audio": []}


def _normalize(parsed):
    """A parsed runner JSON object -> the run_script result shape,
    with ok coerced to 1.0/0.0 and out/gpu/audio defaulted to lists.
    A runner that predates the audio field omits it; a non-list
    audio value degrades to [] rather than breaking the caller."""
    try:
        ok = float(parsed.get("ok", 0))
    except (TypeError, ValueError):
        ok = 0.0
    out = parsed.get("out") or []
    gpu = parsed.get("gpu") or []
    audio = parsed.get("audio") or []
    if not isinstance(out, list):
        out = [out]
    if not isinstance(gpu, list):
        gpu = []
    if not isinstance(audio, list):
        audio = []
    return {"ok": 1.0 if ok else 0.0, "result": parsed.get("result"),
            "out": [str(line) for line in out], "gpu": gpu,
            "audio": audio}


def run_script(source, bindings=None, timeout=10.0):
    """Run one Sanguis page script through bch + interp.bc.

    Returns {"ok": 1.0/0.0, "result": <value or error string>,
    "out": [printed lines], "gpu": [queued GPU commands],
    "audio": [queued audio commands]}. "audio" is always present
    (a runner that predates the field yields []); each record is
    [name, args...] for audio_note / audio_noise. On script
    success AND script error the parsed runner object is returned
    as-is (ok 0.0 + the error string is a script outcome, not a
    failure of this function). Timeout, nonzero runner exit,
    unparseable stdout, and missing files all return ok 0.0 with a
    "runner failed: ..." / "script timed out" result string.
    """
    bch = bch_path()
    interp = interp_path()
    if not os.path.isfile(bch):
        return _failure("bch not found: %s" % bch)
    if not os.path.isfile(interp):
        return _failure("interp.bc not found: %s" % interp)
    try:
        bch = executable_bch_path()
    except OSError as e:
        return _failure("could not prepare bch: %s" % e)
    scriptfile = None
    bindingsfile = None
    try:
        fd, scriptfile = tempfile.mkstemp(suffix=".sanguis",
                                          prefix="sanguis-script-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(source or "")
        argv = [bch, interp, "run", scriptfile]
        if bindings:
            fd, bindingsfile = tempfile.mkstemp(
                suffix=".bindings", prefix="sanguis-script-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_format_bindings(bindings))
            argv.append(bindingsfile)
        # The timeout rides a watchdog thread instead of
        # subprocess.run(timeout=...): communicate(timeout) on the
        # uv cpython 3.12 Windows build raises AttributeError
        # ("_CaptureThread object has no attribute join") in the
        # stdlib's own _communicate, which killed every real-runner
        # script run on this host. Same contract: a run that exceeds
        # `timeout` is killed and returns the "script timed out"
        # shape; stderr detail is only read on a clean exit.
        proc = None
        watch_done = threading.Event()
        timed_out = {"v": False}

        def _watchdog():
            watch_done.wait(timeout)
            if not watch_done.is_set():
                timed_out["v"] = True
                try:
                    proc.kill()
                except Exception:
                    pass

        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True)
        except OSError as e:
            return _failure("could not start bch: %r" % e)
        watcher = threading.Thread(target=_watchdog, daemon=True)
        watcher.start()
        try:
            out, err = proc.communicate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            proc.communicate()
            watch_done.set()
            watcher.join(timeout=1)
            raise
        rc = proc.returncode
        watch_done.set()
        watcher.join(timeout=2)
        if timed_out["v"]:
            return {"ok": 0.0, "result": "script timed out",
                    "out": [], "gpu": [], "audio": []}
        if rc != 0:
            detail = (err or "").strip().splitlines()
            msg = "bch exited %d" % rc
            if detail:
                msg += ": " + detail[0]
            return _failure(msg)
        text = (out or "").strip()
        parsed = None
        try:
            parsed = json.loads(text)
        except ValueError:
            # The runner prints exactly one line; tolerate a stray
            # extra line by taking the last parseable one.
            for line in reversed(text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                break
        if not isinstance(parsed, dict):
            return _failure("unparseable runner output: %r" % text[:200])
        return _normalize(parsed)
    finally:
        for path in (scriptfile, bindingsfile):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
