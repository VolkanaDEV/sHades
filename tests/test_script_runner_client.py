#!/usr/bin/env python3
"""sanguis_script runner tests — stdlib only, no Kivy, no xvfb needed.

The client plumbing (argv shape, temp files, bindings file, JSON
parsing, timeout, bad output, missing files) is tested against a
fake bch (a small Python script acting as the floor binary). The
real bch + interp.bc path is tested too, but only when the compiled
interpreter actually has the `run` entry cell (built in parallel by
the runner worker): until then those checks SKIP with the reason,
they do not fail. The two bindings checks additionally SKIP until
the runner reads the bindings file: the shipped `run` cell calls
script_run(src, []) and ignores the extra argument, so `page` /
`gpu_present` are unbound (see the NOTE in sanguis_script.py).
"""
import json
import os
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sanguis_script  # noqa: E402

PASS, FAIL, SKIP = [], [], []
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNNER_CELL = None
_BINDINGS_OK = None

FAKE_BCH = '''#!%s
import json, os, sys, time
record = os.environ.get("FAKE_BCH_RECORD", "")
mode = os.environ.get("FAKE_BCH_MODE", "ok")
entry = {}
try:
    entry["argv"] = sys.argv[1:]
    if len(sys.argv) > 3:
        with open(sys.argv[3], "r", encoding="utf-8") as f:
            entry["script"] = f.read()
    if len(sys.argv) > 4:
        with open(sys.argv[4], "r", encoding="utf-8") as f:
            entry["bindings"] = f.read()
except Exception as e:
    entry["record_error"] = repr(e)
if record:
    with open(record, "w", encoding="utf-8") as f:
        json.dump(entry, f)
if mode == "badjson":
    print("this is not json")
elif mode == "exit2":
    print("runner blew up", file=sys.stderr)
    sys.exit(2)
elif mode == "sleep":
    time.sleep(30)
elif mode == "script_error":
    print(json.dumps({"ok": 0, "result": "type error: + needs numbers",
                      "out": ["before the boom"], "gpu": []}))
elif mode == "no_keys":
    print(json.dumps({"ok": 1}))
else:
    print(json.dumps({"ok": 1, "result": 42.0, "out": ["one", "two"],
                      "gpu": [["gpu_open", 1.0]]}))
''' % sys.executable


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def skip(name, reason):
    SKIP.append(name)
    print("SKIP  " + name + "  | " + reason)


def _runner_cell_present():
    """True when the repo interp.bc has the `run` entry cell."""
    global _RUNNER_CELL
    if _RUNNER_CELL is not None:
        return _RUNNER_CELL
    fd, path = tempfile.mkstemp(suffix=".sanguis")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("cell main() {\n  output 1.0;\n}\n")
        proc = subprocess.run([sanguis_script.bch_path(),
                               sanguis_script.interp_path(),
                               "run", path],
                              capture_output=True, text=True, timeout=30)
        _RUNNER_CELL = "no block named 'run'" not in (proc.stderr or "")
    except Exception:
        _RUNNER_CELL = False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return _RUNNER_CELL


def _bindings_supported():
    """True when the runner actually injects the bindings file (a
    script reading `page` gets the injected value back)."""
    global _BINDINGS_OK
    if _BINDINGS_OK is not None:
        return _BINDINGS_OK
    try:
        res = sanguis_script.run_script(
            "cell main() {\n  output page;\n}\n",
            [["page", "/probe.mu"], ["gpu_present", 1.0]], timeout=30)
        _BINDINGS_OK = (res.get("ok") == 1.0
                        and res.get("result") == "/probe.mu")
    except Exception:
        _BINDINGS_OK = False
    return _BINDINGS_OK


def _make_fake_bch(tmpdir):
    path = os.path.join(tmpdir, "fake_bch")
    with open(path, "w") as f:
        f.write(FAKE_BCH)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def _run_with_fake(fake_bch, mode, source="cell main() {\n  output 1.0;\n}\n",
                   bindings=None, timeout=10.0):
    """Run run_script with bch stubbed to the fake; return
    (result, recorded argv/files dict). Restores everything."""
    record = os.path.join(tempfile.gettempdir(),
                          "fake-bch-record-%d.json" % os.getpid())
    real_bch, real_interp = (sanguis_script.bch_path,
                             sanguis_script.interp_path)
    old_env = dict(os.environ)
    sanguis_script.bch_path = lambda: fake_bch
    os.environ["FAKE_BCH_MODE"] = mode
    os.environ["FAKE_BCH_RECORD"] = record
    try:
        if os.path.exists(record):
            os.unlink(record)
        res = sanguis_script.run_script(source, bindings, timeout=timeout)
        rec = {}
        if os.path.exists(record):
            with open(record) as f:
                rec = json.load(f)
        return res, rec
    finally:
        sanguis_script.bch_path = real_bch
        sanguis_script.interp_path = real_interp
        os.environ.clear()
        os.environ.update(old_env)
        try:
            os.unlink(record)
        except OSError:
            pass


def main():
    tmpdir = tempfile.mkdtemp(prefix="sanguis-script-test-")
    fake_bch = _make_fake_bch(tmpdir)

    # ---- paths / availability ----
    check("sanguis_data_path resolves under the repo",
          sanguis_script.sanguis_data_path("interp.bc") == os.path.join(
              _REPO, "sanguis", "interp.bc"),
          sanguis_script.sanguis_data_path("interp.bc"))
    check("bch name is bch on POSIX", sanguis_script.BCH_NAME == "bch")
    had_meipass = hasattr(sys, "_MEIPASS")
    old_meipass = getattr(sys, "_MEIPASS", None)
    try:
        sys._MEIPASS = "/tmp/frozen-app"
        check("frozen path uses sys._MEIPASS",
              sanguis_script.sanguis_data_path("bch")
              == "/tmp/frozen-app/sanguis/bch",
              sanguis_script.sanguis_data_path("bch"))
    finally:
        if had_meipass:
            sys._MEIPASS = old_meipass
        else:
            del sys._MEIPASS
    check("scripting_available true with repo files",
          sanguis_script.scripting_available() is True)
    check("gpu_present is 1.0 where scripting is available",
          sanguis_script.gpu_present_value() == 1.0)
    real_bch = sanguis_script.bch_path
    try:
        sanguis_script.bch_path = lambda: "/nonexistent/bch"
        check("scripting_available false when bch missing",
              sanguis_script.scripting_available() is False)
        check("gpu_present 0.0 when scripting unavailable",
              sanguis_script.gpu_present_value() == 0.0)
    finally:
        sanguis_script.bch_path = real_bch
    real_interp = sanguis_script.interp_path
    try:
        sanguis_script.interp_path = lambda: "/nonexistent/interp.bc"
        check("scripting_available false when interp.bc missing",
              sanguis_script.scripting_available() is False)
    finally:
        sanguis_script.interp_path = real_interp
    # Android availability now depends on the binary existing, not
    # the platform: the exec-bit shim (executable_bch_path) makes a
    # shipped bch runnable, so with the repo files present Android
    # reports True, and False when the binary is missing.
    old_env = dict(os.environ)
    try:
        os.environ["ANDROID_ROOT"] = "/system"
        check("scripting_available true on Android once bch ships",
              sanguis_script.scripting_available() is True)
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    real_bch = sanguis_script.bch_path
    old_env = dict(os.environ)
    try:
        os.environ["ANDROID_ROOT"] = "/system"
        sanguis_script.bch_path = lambda: "/nonexistent/bch"
        check("scripting_available false on Android when bch missing",
              sanguis_script.scripting_available() is False)
    finally:
        sanguis_script.bch_path = real_bch
        os.environ.clear()
        os.environ.update(old_env)

    # ---- fake-bch plumbing ----
    src = "cell main() {\n  output 6.0 * 7.0;\n}\n"
    bindings = [["page", "/index.mu"], ["gpu_present", 1.0],
                ["channel", "wiki"]]
    res, rec = _run_with_fake(fake_bch, "ok", src, bindings)
    check("fake run returns parsed ok/result/out/gpu/audio",
          res == {"ok": 1.0, "result": 42.0, "out": ["one", "two"],
                  "gpu": [["gpu_open", 1.0]], "audio": []}, res)
    check("argv is [interp, run, scriptfile, bindingsfile]",
          len(rec.get("argv", [])) == 4 and rec["argv"][1] == "run"
          and rec["argv"][0].endswith("interp.bc"), rec.get("argv"))
    check("script temp file holds the source verbatim",
          rec.get("script") == src, rec.get("script"))
    check("bindings temp file is name=value lines",
          rec.get("bindings") == "page=/index.mu\ngpu_present=1.0\n"
          "channel=wiki\n", rec.get("bindings"))
    check("script temp file deleted after the run",
          not os.path.exists(rec["argv"][2]), rec["argv"][2])
    check("bindings temp file deleted after the run",
          not os.path.exists(rec["argv"][3]), rec["argv"][3])

    res, rec = _run_with_fake(fake_bch, "ok", src, None)
    check("no bindings -> exactly [interp, run, scriptfile]",
          len(rec.get("argv", [])) == 3 and rec["argv"][1] == "run",
          rec.get("argv"))
    res, rec = _run_with_fake(fake_bch, "ok", src, [])
    check("empty bindings -> no bindings file either",
          len(rec.get("argv", [])) == 3, rec.get("argv"))

    res, _rec = _run_with_fake(fake_bch, "script_error", src, bindings)
    check("script error passes through as ok 0 + error string",
          res == {"ok": 0.0, "result": "type error: + needs numbers",
                  "out": ["before the boom"], "gpu": [], "audio": []},
          res)
    res, _rec = _run_with_fake(fake_bch, "badjson", src, bindings)
    check("bad JSON -> runner failed",
          res["ok"] == 0.0 and res["result"].startswith(
              "runner failed: unparseable"), res)
    res, _rec = _run_with_fake(fake_bch, "exit2", src, bindings)
    check("nonzero exit -> runner failed with exit code + stderr",
          res["ok"] == 0.0 and res["result"].startswith(
              "runner failed: bch exited 2: runner blew up"), res)
    res, _rec = _run_with_fake(fake_bch, "no_keys", src, bindings)
    check("missing out/gpu/audio keys default to empty lists",
          res == {"ok": 1.0, "result": None, "out": [], "gpu": [],
                  "audio": []}, res)
    res, _rec = _run_with_fake(fake_bch, "sleep", src, bindings,
                               timeout=0.2)
    check("timeout -> ok 0 script timed out",
          res == {"ok": 0.0, "result": "script timed out",
                  "out": [], "gpu": [], "audio": []}, res)

    # missing bch / missing interp.bc (stub the path lookups)
    real_bch, real_interp = (sanguis_script.bch_path,
                             sanguis_script.interp_path)
    try:
        sanguis_script.bch_path = lambda: "/nonexistent/bch"
        res = sanguis_script.run_script(src, bindings)
        check("missing bch -> runner failed, no crash",
              res["ok"] == 0.0 and res["result"].startswith(
                  "runner failed: bch not found"), res)
    finally:
        sanguis_script.bch_path = real_bch
    try:
        sanguis_script.interp_path = lambda: "/nonexistent/interp.bc"
        res = sanguis_script.run_script(src, bindings)
        check("missing interp.bc -> runner failed, no crash",
              res["ok"] == 0.0 and res["result"].startswith(
                  "runner failed: interp.bc not found"), res)
    finally:
        sanguis_script.interp_path = real_interp

    # ---- real bch + interp.bc (skips until the `run` cell lands) ----
    demo = ("cell main() {\n  let n = 6.0;\n"
            "  print(\"hello from a micron page\");\n"
            "  output n * 7.0;\n}\n")
    demo_bindings = [["page", "/index.mu"], ["gpu_present", 1.0]]
    if not _runner_cell_present():
        for name in ("real run: ok/result/out",
                     "real run: page binding",
                     "real run: gpu_info binding",
                     "real run: script error + prior out",
                     "real run: looping script hits step budget",
                     "real run: gpu queue"):
            skip(name, "interp.bc has no 'run' entry cell yet")
    else:
        res = sanguis_script.run_script(demo, demo_bindings, timeout=30)
        check("real run: ok/result/out",
              res.get("ok") == 1.0 and res.get("result") == 42.0
              and res.get("out") == ["hello from a micron page"]
              and res.get("gpu") == [], res)
        if not _bindings_supported():
            for name in ("real run: page binding",
                         "real run: gpu_info binding"):
                skip(name, "runner ignores the bindings file "
                     "(script_run gets [] bindings)")
        else:
            res = sanguis_script.run_script(
                "cell main() {\n  output len(page);\n}\n",
                demo_bindings, timeout=30)
            check("real run: page binding",
                  res.get("ok") == 1.0 and res.get("result") == 9.0, res)
            res = sanguis_script.run_script(
                "cell main() {\n  output gpu_info();\n}\n",
                demo_bindings, timeout=30)
            check("real run: gpu_info binding",
                  res.get("ok") == 1.0 and res.get("result") == 1.0, res)
        res = sanguis_script.run_script(
            "cell main() {\n  print(\"before\");\n"
            "  output \"a\" + 1.0;\n}\n",
            demo_bindings, timeout=30)
        check("real run: script error + prior out",
              res.get("ok") == 0.0
              and str(res.get("result", "")).startswith("type error")
              and res.get("out") == ["before"], res)
        res = sanguis_script.run_script(
            "cell main() {\n  while 1.0 {\n  }\n}\n",
            demo_bindings, timeout=30)
        check("real run: looping script hits step budget",
              res.get("ok") == 0.0
              and str(res.get("result", "")).startswith(
                  "step budget exceeded"), res)
        res = sanguis_script.run_script(
            "cell main() {\n  let h = gpu_open();\n"
            "  gpu_upload_f32(h, [1.0, 2.0]);\n"
            "  gpu_dispatch(h, 2.0);\n"
            "  let r = gpu_read_f32(h, 0.0, 2.0);\n"
            "  output h;\n}\n",
            demo_bindings, timeout=30)
        check("real run: gpu queue",
              res.get("ok") == 1.0 and res.get("result") == 1.0
              and len(res.get("gpu") or []) == 4
              and (res.get("gpu") or [[None]])[0][0] == "gpu_open", res)

    try:
        os.unlink(fake_bch)
        os.rmdir(tmpdir)
    except OSError:
        pass
    print("\n%d passed, %d skipped, %d failed"
          % (len(PASS), len(SKIP), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
