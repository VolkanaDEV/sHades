#!/usr/bin/env python3
"""fixes.md #17 (plaintext half): _save_session writes atomically and
the session file is owner-only (mode 0o600).

  (a) a save with dummy hub values parses as JSON with the expected keys
  (b) the saved file's mode is 0o600
  (c) crash-safety: a failed write (json.dump patched to raise) leaves
      the previous session intact and _load_session() still returns it
  (d) no .tmp file is left behind after a normal save or a failed one

Full session encryption is deliberately NOT covered here: it needs a
device-key decision from Jeffery.

Run: xvfb-run -a python tests/test_fix17_session.py
"""
import json
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []
TMP = tempfile.mkdtemp(prefix="shades-fix17-")

EXPECTED_KEYS = {"node_hash", "delivery_hash", "username",
                 "hub_address", "hub_host", "hub_port"}


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


def make_app():
    """A bare app object: _save_session only needs the six saved attrs."""
    app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    app.guild_node_hash = "aa" * 16
    app.delivery_hash = "bb" * 16
    app.username = "tester"
    app.hub_address = "10.0.0.1:29221"
    app.hub_host = "10.0.0.1"
    app.hub_port = 29221
    return app


def test_save_parses_and_keys():
    app = make_app()
    app._save_session()
    path = app._session_path()
    check("session file exists after save", os.path.isfile(path))
    with open(path) as f:
        data = json.load(f)
    check("saved session parses as JSON with the expected keys",
          set(data) == EXPECTED_KEYS, sorted(data))
    check("saved values round-trip",
          data["username"] == "tester"
          and data["hub_address"] == "10.0.0.1:29221"
          and data["hub_port"] == 29221, data)


def test_mode_0600():
    app = make_app()
    path = app._session_path()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    check("session file mode is 0o600", mode == 0o600, oct(mode))


def test_crash_safety():
    app = make_app()
    app._save_session()
    path = app._session_path()
    with open(path, "rb") as f:
        before = f.read()
    orig_dump = json.dump

    def _boom(*_a, **_k):
        raise RuntimeError("simulated crash mid-write")

    json.dump = _boom
    try:
        app._save_session()
    finally:
        json.dump = orig_dump
    with open(path, "rb") as f:
        after = f.read()
    check("failed write leaves the old session file intact",
          before == after)
    loaded = app._load_session()
    check("failed write: _load_session still returns the old session",
          loaded is not None and loaded.get("username") == "tester", loaded)
    check("failed write leaves no .tmp behind",
          not os.path.exists(path + ".tmp"))


def test_no_tmp_after_save():
    app = make_app()
    app._save_session()
    check("normal save leaves no .tmp behind",
          not os.path.exists(app._session_path() + ".tmp"))


def main():
    saved_appdir = kc._app_data_dir
    kc._app_data_dir = lambda: TMP
    try:
        for fn in (test_save_parses_and_keys, test_mode_0600,
                   test_crash_safety, test_no_tmp_after_save):
            try:
                fn()
            except Exception as e:
                check(fn.__name__ + " raised", False, repr(e))
    finally:
        kc._app_data_dir = saved_appdir
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
