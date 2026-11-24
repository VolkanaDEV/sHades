#!/usr/bin/env python3
"""Android bch exec-bit shim tests — stdlib only, no Kivy, no xvfb.

Files extracted from an APK lose the executable bit, so on Android
sanguis_script must stage a copy of bch into the app's private dir
with mode 0o755 before running it (executable_bch_path). Android
is simulated here by setting ANDROID_ROOT and stubbing _private_dir
to a temp dir — no device, no NDK binary needed: the "bundled bch"
is a small Python script standing in for the floor binary.

Run: ~/workspace/venvs/shades/bin/python tests/test_android_bch_exec.py
"""
import json
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sanguis_script  # noqa: E402

PASS, FAIL = [], []

FAKE_BCH = '''#!%s
import json, os, sys
record = os.environ.get("FAKE_BCH_RECORD", "")
if record:
    with open(record, "w", encoding="utf-8") as f:
        json.dump({"argv": sys.argv}, f)
print(json.dumps({"ok": 1, "result": 42.0, "out": [], "gpu": []}))
''' % sys.executable


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


class _Android:
    """Simulate Android: ANDROID_ROOT set, _private_dir stubbed.

    Restores everything on exit. bch_path/interp_path are stubbed
    to the given temp files (or left alone when None)."""

    def __init__(self, tmpdir, bch_src=None, interp_src=None):
        self.tmpdir = tmpdir
        self.private = os.path.join(tmpdir, "private")
        self.bch_src = bch_src
        self.interp_src = interp_src
        self.old_env = dict(os.environ)
        self.old_private = sanguis_script._private_dir
        self.old_bch = sanguis_script.bch_path
        self.old_interp = sanguis_script.interp_path

    def __enter__(self):
        os.environ["ANDROID_ROOT"] = "/system"
        sanguis_script._private_dir = lambda: self.private
        if self.bch_src is not None:
            sanguis_script.bch_path = lambda: self.bch_src
        if self.interp_src is not None:
            sanguis_script.interp_path = lambda: self.interp_src
        return self

    def __exit__(self, *exc):
        sanguis_script._private_dir = self.old_private
        sanguis_script.bch_path = self.old_bch
        sanguis_script.interp_path = self.old_interp
        os.environ.clear()
        os.environ.update(self.old_env)
        return False


def _write(path, content, mode=None):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)
    return path


def main():
    tmpdir = tempfile.mkdtemp(prefix="sanguis-android-bch-")
    try:
        # A bundled bch as it arrives from an APK: readable, not
        # executable (0o644), plus a dummy interp.bc.
        bundled = _write(os.path.join(tmpdir, "bch"), FAKE_BCH, 0o644)
        interp = _write(os.path.join(tmpdir, "interp.bc"), "BC", 0o644)
        check("bundled fake bch is not executable, as from an APK",
              stat.S_IMODE(os.stat(bundled).st_mode) == 0o644)

        # ---- desktop: shim is a no-op ----
        check("executable_bch_path returns bundled path off Android",
              sanguis_script.executable_bch_path()
              == sanguis_script.bch_path())

        # ---- Android: stage, chmod, reuse, re-copy ----
        with _Android(tmpdir, bundled, interp):
            dest = sanguis_script.executable_bch_path()
            check("staged copy lands under private/sanguis",
                  dest == os.path.join(tmpdir, "private", "sanguis",
                                       "bch"), dest)
            check("staged copy is byte-identical to bundled",
                  open(dest, "rb").read() == open(bundled, "rb").read())
            check("staged copy is mode 0o755",
                  stat.S_IMODE(os.stat(dest).st_mode) == 0o755)
            check("bundled copy untouched (still 0o644)",
                  stat.S_IMODE(os.stat(bundled).st_mode) == 0o644)

            # Same size + executable -> reused, not re-copied.
            sentinel = b"X" * os.path.getsize(bundled)
            with open(dest, "wb") as f:
                f.write(sentinel)
            os.chmod(dest, 0o755)
            check("matching staged copy is reused, not re-copied",
                  sanguis_script.executable_bch_path() == dest
                  and open(dest, "rb").read() == sentinel)

            # Size differs -> re-copied from bundled.
            with open(dest, "wb") as f:
                f.write(b"short")
            os.chmod(dest, 0o755)
            sanguis_script.executable_bch_path()
            check("changed bundled size triggers a re-copy",
                  open(dest, "rb").read() == open(bundled, "rb").read())

            # Executable bit lost on the staged copy -> re-copied.
            os.chmod(dest, 0o644)
            sanguis_script.executable_bch_path()
            check("staged copy without exec bit is restored to 0o755",
                  stat.S_IMODE(os.stat(dest).st_mode) == 0o755)

            check("scripting_available true on Android once bch ships",
                  sanguis_script.scripting_available() is True)
            check("gpu_present 1.0 on Android once bch ships",
                  sanguis_script.gpu_present_value() == 1.0)

            # run_script actually executes through the staged copy.
            record = os.path.join(tmpdir, "record.json")
            old_env = dict(os.environ)
            os.environ["FAKE_BCH_RECORD"] = record
            try:
                res = sanguis_script.run_script(
                    "cell main() {\n  output 1.0;\n}\n", timeout=30)
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            rec = json.load(open(record))
            check("run_script on Android executes the staged copy",
                  rec["argv"][0] == dest, rec["argv"][0])
            check("run_script on Android returns the fake result",
                  res == {"ok": 1.0, "result": 42.0, "out": [],
                          "gpu": [], "audio": []}, res)

        # ---- Android without a shipped bch: still unavailable ----
        with _Android(tmpdir, os.path.join(tmpdir, "no-such-bch"),
                      interp):
            check("scripting_available false on Android, bch missing",
                  sanguis_script.scripting_available() is False)
            check("gpu_present 0.0 on Android, bch missing",
                  sanguis_script.gpu_present_value() == 0.0)
            res = sanguis_script.run_script("cell main() {}\n")
            check("run_script without bch fails cleanly, no crash",
                  res["ok"] == 0.0
                  and res["result"].startswith("runner failed: bch "
                                               "not found"), res)

        # ---- Android where the private dir cannot be created ----
        blocker = os.path.join(tmpdir, "blocker")
        _write(blocker, "not a dir")
        with _Android(tmpdir, bundled, interp):
            sanguis_script._private_dir = lambda: blocker
            res = sanguis_script.run_script("cell main() {}\n")
            check("unwritable private dir -> runner failed, no crash",
                  res["ok"] == 0.0
                  and res["result"].startswith(
                      "runner failed: could not prepare bch"), res)

        # ---- private-dir fallback on desktop ----
        check("private dir falls back under home without p4a",
              sanguis_script._private_dir() == os.path.join(
                  os.path.expanduser("~"), ".sHades_client"),
              sanguis_script._private_dir())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
