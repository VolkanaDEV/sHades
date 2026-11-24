#!/usr/bin/env python3
"""Sanguis scripting packaging contract (static checks only).

Verifies the data files and the packaging wiring for micron script
blocks, without importing any scripting module (sanguis_script.py /
gpu_drain.py are owned by other workers and may not exist yet when
this file is first added):

  1. sanguis/interp.bc, sanguis/bch, sanguis/bch.exe exist with the
     expected hashes and sizes; bch carries an executable bit.
  2. .github/workflows/build.yml parses as YAML (when PyYAML is
     available; skipped, not failed, otherwise -- PyYAML is not in
     requirements.txt, so the CI lint env has no yaml module) and
     contains the --add-data lines for both PyInstaller jobs, plus
     sanguis_script.py and gpu_drain.py in BOTH lint lists
     (py_compile and flake8).
  3. buildozer.spec lists `bc` in source.include_exts (so
     sanguis/interp.bc ships in the APK) and lists the extensionless
     sanguis/bch in source.include_patterns.

Run: ~/workspace/venvs/shades/bin/python tests/test_script_packaging.py
"""
import hashlib
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP = [], [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


def skip(name, reason):
    SKIP.append(name)
    print("SKIP  " + name + "  | " + reason)


def _hash(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _job_section(text, job):
    """Return the build.yml text from `job:` up to the next top-level job."""
    start = text.index(job + ":")
    rest = text[start + len(job) + 1:]
    for marker in ("\n  build-", "\n  server-bundle:", "\n  lint-and-test:"):
        idx = rest.find(marker)
        if idx != -1:
            rest = rest[:idx]
    return rest


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 1. Data files: existence, hash, size, exec bit.
    expected = {
        "sanguis/interp.bc": ("md5",
                              # interact bindings (interp 1,768 lines);
                              # rebuilt after the media pin.
                              "043100a328f16009906547abfeb0582a", 132726),
        "sanguis/bch": ("sha256",
                        "8b8f476d9ea12845a41ca146688088c7586d961e4fc70b4295d"
                        "5b46766ff947b", 459864),
        "sanguis/bch.exe": ("sha256",
                            "8c20f676e09ba80851d06e9b3953e973616f250468eec768"
                            "81050294d6db3261", 633344),
    }
    for rel, (algo, digest, size) in sorted(expected.items()):
        path = os.path.join(root, rel)
        check(rel + " exists", os.path.isfile(path), path)
        if not os.path.isfile(path):
            continue
        check(rel + " " + algo + " matches",
              _hash(path, algo) == digest,
              _hash(path, algo))
        check(rel + " size is %d bytes" % size,
              os.path.getsize(path) == size,
              os.path.getsize(path))
    bch = os.path.join(root, "sanguis", "bch")
    if os.path.isfile(bch):
        mode = os.stat(bch).st_mode
        check("sanguis/bch has an executable bit",
              bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)),
              oct(mode))

    # 2. build.yml: YAML parse + PyInstaller --add-data + lint lists.
    yml_path = os.path.join(root, ".github", "workflows", "build.yml")
    yml = open(yml_path, "r", encoding="utf-8").read()
    try:
        import yaml
    except ImportError:
        skip("build.yml parses as YAML",
             "PyYAML not installed in this env (not in requirements.txt); "
             "verified separately with system python3")
    else:
        try:
            parsed = yaml.safe_load(yml)
            jobs = sorted(parsed.get("jobs", {}))
        except Exception as exc:  # noqa: BLE001 - report any parse failure
            check("build.yml parses as YAML", False, exc)
        else:
            check("build.yml parses as YAML", True)
            check("build.yml has lint/build-linux/build-windows jobs",
                  {"lint-and-test", "build-linux", "build-windows"}.issubset(
                      set(jobs)), jobs)
    linux = _job_section(yml, "build-linux")
    windows = _job_section(yml, "build-windows")
    check("Linux job adds sanguis/interp.bc",
          '--add-data "sanguis/interp.bc:sanguis"' in linux)
    check("Linux job adds sanguis/bch",
          '--add-data "sanguis/bch:sanguis"' in linux)
    check("Windows job adds sanguis/interp.bc",
          '--add-data "sanguis/interp.bc;sanguis"' in windows)
    check("Windows job adds sanguis/bch.exe",
          '--add-data "sanguis/bch.exe;sanguis"' in windows)
    lint = _job_section(yml, "lint-and-test")
    py_compile = [ln for ln in lint.splitlines()
                  if "py_compile" in ln]
    flake8 = [ln for ln in lint.splitlines() if "flake8" in ln]
    check("lint py_compile line exists", len(py_compile) == 1,
          len(py_compile))
    check("lint flake8 line exists", len(flake8) == 1, len(flake8))
    for module in ("sanguis_script.py", "gpu_drain.py"):
        check("py_compile lists " + module,
              bool(py_compile) and module in py_compile[0])
        check("flake8 lists " + module,
              bool(flake8) and module in flake8[0])

    # 3. buildozer.spec: bc ext + extensionless bch pattern.
    spec = open(os.path.join(root, "buildozer.spec"),
                "r", encoding="utf-8").read()
    exts = [ln for ln in spec.splitlines()
            if ln.startswith("source.include_exts")]
    check("buildozer.spec has source.include_exts", len(exts) == 1,
          len(exts))
    if exts:
        listed = [e.strip() for e in
                  exts[0].split("=", 1)[1].split(",")]
        check("source.include_exts contains bc", "bc" in listed, listed)
    pats = [ln for ln in spec.splitlines()
            if ln.startswith("source.include_patterns")]
    check("buildozer.spec has source.include_patterns",
          len(pats) == 1, len(pats))
    if pats:
        check("source.include_patterns lists sanguis/bch",
              "sanguis/bch" in pats[0], pats[0])
    live = [ln for ln in spec.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    check("buildozer.spec does not bundle Windows bch.exe in the APK",
          not any("bch.exe" in ln for ln in live),
          [ln for ln in live if "bch.exe" in ln])

    print("RESULT: %d PASS, %d FAIL, %d SKIP" %
          (len(PASS), len(FAIL), len(SKIP)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
