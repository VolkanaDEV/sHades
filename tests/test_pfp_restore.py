#!/usr/bin/env python3
"""Profile pfp persistence across restarts (tay device note, v0.32).

Root cause: `_save_pfp_crop*` wrote the resized crops to pfps/ inside the
app data directory, but nothing ever RE-READ them at startup, so every
restart came up without the avatar. `_restore_avatar_from_disk()` now
binds the newest small_*.png when the main screen opens.
"""

import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = []
FAIL = []


def check(ok, message, detail=""):
    print(("PASS " if ok else "FAIL ") + message, flush=True)
    (PASS if ok else FAIL).append(message if ok else message + str(detail))


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0

    if not hasattr(kc.sHadesClientApp, "_restore_avatar_from_disk"):
        check(False, "restore hook exists on the app class")
        return 1
    check(True, "restore hook exists on the app class")

    tmp = tempfile.mkdtemp(prefix="pfp_restore_")
    pfp_dir = os.path.join(tmp, "pfps")
    os.mkdir(pfp_dir)

    old_pfp = os.path.join(pfp_dir, "small_aaaa.png")
    new_pfp = os.path.join(pfp_dir, "small_bbbb.png")
    junk = [
        os.path.join(pfp_dir, "big_aaaa.png"),
        os.path.join(pfp_dir, "notes.txt"),
        os.path.join(pfp_dir, "small_cccc.jpg"),
    ]
    for path in (old_pfp, new_pfp, junk[0], junk[2]):
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG")
    for path in (junk[1],):
        with open(path, "wb") as fh:
            fh.write(b"junk")
    os.utime(old_pfp, (time.time() - 100, time.time() - 100))
    os.utime(new_pfp, (time.time(), time.time()))

    real_app_data = kc._app_data_dir
    kc._app_data_dir = lambda: tmp
    try:
        # 1. fresh start (no avatar bound) picks the newest small png and
        #    binds it to both the small avatar and the composer pfp
        stub = types.SimpleNamespace()
        stub.my_avatar_small = None
        stub.composer_pfp_path = None
        kc.sHadesClientApp._restore_avatar_from_disk(stub)
        check(stub.my_avatar_small == new_pfp,
              "fresh restore binds the newest small png",
              stub.my_avatar_small)
        check(stub.composer_pfp_path == new_pfp,
              "composer pfp path follows the restore",
              stub.composer_pfp_path)

        # 2. an already-bound session is left untouched
        stub2 = types.SimpleNamespace()
        stub2.my_avatar_small = "/somewhere/on/device/small.png"
        stub2.composer_pfp_path = "/somewhere/on/device/small.png"
        kc.sHadesClientApp._restore_avatar_from_disk(stub2)
        check(stub2.my_avatar_small == "/somewhere/on/device/small.png",
              "existing session avatar is not overwritten")

        # 3. an empty pfps dir leaves the session untouched
        empty = tempfile.mkdtemp(prefix="pfp_restore_empty_")
        os.mkdir(os.path.join(empty, "pfps"))
        kc._app_data_dir = lambda: empty
        stub3 = types.SimpleNamespace()
        stub3.my_avatar_small = None
        stub3.composer_pfp_path = None
        kc.sHadesClientApp._restore_avatar_from_disk(stub3)
        check(stub3.my_avatar_small is None,
              "no saved crops -> nothing bound")
    finally:
        kc._app_data_dir = real_app_data

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
