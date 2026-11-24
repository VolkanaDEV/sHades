#!/usr/bin/env python3
"""Runner-mode tests for the Sanguis interpreter.

Invokes the repo data files sanguis/bch + sanguis/interp.bc via subprocess
with the fixed command line [bch, interp.bc, run, scriptfile] and parses
stdout as JSON. Subprocess + stdlib only: no Kivy import, no xvfb needed.

Run: ~/workspace/venvs/shades/bin/python tests/test_script_runner.py
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Windows cannot exec the ELF `bch`: pick the platform binary the
# packaging ships (bch.exe on win32), same names sanguis_script uses.
if sys.platform == "win32" and os.path.isfile(
        os.path.join(ROOT, "sanguis", "bch.exe")):
    BCH = os.path.join(ROOT, "sanguis", "bch.exe")
else:
    BCH = os.path.join(ROOT, "sanguis", "bch")
BC = os.path.join(ROOT, "sanguis", "interp.bc")

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


def run_argv(argv):
    """Run bch with the given argv; return (returncode, stdout, stderr)."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def run_script(path):
    return run_argv([BCH, BC, "run", path])


def parse_stdout(out):
    """Parse stdout as JSON; return (data, error-string-or-"")."""
    try:
        return json.loads(out), ""
    except ValueError as exc:
        return None, "unparsable stdout %r (%s)" % (out, exc)


def run_source(source):
    """Write source to a temp file and run it; return (rc, data, out, err)."""
    fd, path = tempfile.mkstemp(suffix=".sanguis")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(source)
        rc, out, err = run_script(path)
        data, perr = parse_stdout(out)
        return rc, data, out, err, perr
    finally:
        os.unlink(path)


def one_line(out):
    return len(out.splitlines()) == 1


def main():
    # ---- working script: ok 1, correct result/out/gpu, one stdout line ----
    rc, data, out, err, perr = run_source(
        'cell main() {\n'
        '    print("hello");\n'
        '    let h = gpu_open();\n'
        '    gpu_upload_f32(h, [1.0, 2.5]);\n'
        '    gpu_dispatch(h, 2.0);\n'
        '    output [42.0, "done"];\n'
        '}\n')
    check("working: exit 0", rc == 0, rc)
    check("working: stdout parses as JSON", data is not None, perr)
    check("working: stdout is exactly one line", one_line(out), repr(out))
    check("working: stderr empty", err == "", repr(err))
    if data is not None:
        check("working: ok is 1", data.get("ok") == 1, data)
        check("working: result", data.get("result") == [42, "done"], data)
        check("working: out", data.get("out") == ["hello"], data)
        check("working: gpu queue", data.get("gpu") == [
            ["gpu_open", 1], ["gpu_upload_f32", 1, [1, 2.5]],
            ["gpu_dispatch", 1, 2]], data)

    # ---- script error: ok 0, exit 0, error string in result, out kept ----
    rc, data, out, err, perr = run_source(
        'cell main() {\n'
        '    print("before error");\n'
        '    output "a" + 1.0;\n'
        '}\n')
    check("error: exit 0 on script error", rc == 0, rc)
    check("error: stdout parses as JSON", data is not None, perr)
    check("error: stdout is exactly one line", one_line(out), repr(out))
    check("error: stderr empty", err == "", repr(err))
    if data is not None:
        check("error: ok is 0", data.get("ok") == 0, data)
        check("error: result is the error string",
              isinstance(data.get("result"), str)
              and data["result"].startswith("type error"), data)
        check("error: out preserved", data.get("out") == ["before error"],
              data)
        check("error: gpu empty", data.get("gpu") == [], data)

    # ---- unreadable script file: nonzero exit + JSON error line ----
    fd, missing = tempfile.mkstemp(suffix=".sanguis")
    os.close(fd)
    os.unlink(missing)
    rc, out, err = run_script(missing)
    data, perr = parse_stdout(out)
    check("missing: nonzero exit", rc != 0, rc)
    check("missing: stdout parses as JSON", data is not None, perr)
    check("missing: stdout is exactly one line", one_line(out), repr(out))
    check("missing: stderr empty", err == "", repr(err))
    if data is not None:
        check("missing: ok is 0", data.get("ok") == 0, data)
        check("missing: result names the file",
              isinstance(data.get("result"), str)
              and missing in data["result"], data)
        check("missing: out/gpu empty",
              data.get("out") == [] and data.get("gpu") == [], data)

    # ---- quotes/backslashes in printed lines: JSON must still parse ----
    rc, data, out, err, perr = run_source(
        'cell main() {\n'
        '    print("say \\"hi\\" \\\\ back");\n'
        '    print("tab\\there");\n'
        '    output "q\\"uote\\\\";\n'
        '}\n')
    check("quotes: exit 0", rc == 0, rc)
    check("quotes: stdout parses as JSON", data is not None, perr)
    check("quotes: stdout is exactly one line", one_line(out), repr(out))
    if data is not None:
        check("quotes: out round-trips",
              data.get("out") == ['say "hi" \\ back', "tab\there"], data)
        check("quotes: result round-trips",
              data.get("result") == 'q"uote\\', data)

    # ---- GPU script: queue present in the JSON ----
    rc, data, out, err, perr = run_source(
        'cell main() {\n'
        '    let h = gpu_open();\n'
        '    gpu_upload_f32(h, [1.0, 2.0]);\n'
        '    gpu_dispatch(h, 2.0);\n'
        '    output h;\n'
        '}\n')
    check("gpu: exit 0", rc == 0, rc)
    check("gpu: stdout parses as JSON", data is not None, perr)
    if data is not None:
        check("gpu: ok is 1", data.get("ok") == 1, data)
        check("gpu: result is the handle", data.get("result") == 1, data)
        check("gpu: queue in order", data.get("gpu") == [
            ["gpu_open", 1], ["gpu_upload_f32", 1, [1, 2]],
            ["gpu_dispatch", 1, 2]], data)

    # ---- the `main run <script>` form dispatches to the same runner ----
    fd, path = tempfile.mkstemp(suffix=".sanguis")
    try:
        with os.fdopen(fd, "w") as f:
            f.write('cell main() {\n    output 7.0;\n}\n')
        rc, out, err = run_argv([BCH, BC, "main", "run", path])
        data, perr = parse_stdout(out)
        check("main-run: exit 0", rc == 0, rc)
        check("main-run: stdout parses as JSON", data is not None, perr)
        check("main-run: stdout is exactly one line", one_line(out),
              repr(out))
        if data is not None:
            check("main-run: result",
                  data.get("ok") == 1 and data.get("result") == 7, data)
    finally:
        os.unlink(path)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
