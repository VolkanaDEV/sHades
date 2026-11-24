#!/usr/bin/env python3
"""Android bch binary contract — static checks only, no Kivy, no xvfb.

Verifies the NDK-built Android floor binary (sanguis/bch-android)
without executing it (it is aarch64; the live probe runs it under
qemu-aarch64-static — see ANDROID-BCH-REPORT.md):

  1. sanguis/bch-android is a valid 64-bit little-endian AArch64 ELF,
     statically linked (no PT_INTERP / PT_DYNAMIC), with the Android
     ident note carrying min SDK 26 and the r27c NDK tag, and a
     PT_TLS alignment of at least 64 (the ARM64 Bionic requirement —
     a p_align 8 static binary aborts before main).
  2. Its sha256/size pin matches the reproducible build
     (android-ndk/build-bch-android.sh — NDK r27c, API 26).
  3. The Linux sanguis/bch is untouched (still x86-64, same sha256
     as the packaging pin) — the Android binary has a distinct home
     and must never replace it.
  4. buildozer.spec still ships `sanguis/bch` via include_patterns
     and does NOT list bch-android or bch.exe: CI copies bch-android
     onto sanguis/bch in the ephemeral checkout (see build.yml).
  5. build.yml has that staging step before `buildozer android debug`
     and the APK listing check afterwards.
  6. The exec-bit shim resolves the staged Android binary correctly
     on simulated Android (as tests/test_android_bch_exec.py does):
     executable_bch_path copies the real bch-android bytes into
     private/sanguis/bch with mode 0o755. The copy is NOT executed
     here (aarch64 on an x86 host).

Run: ~/workspace/venvs/shades/bin/python tests/test_android_bch_binary.py
"""
import hashlib
import os
import stat
import struct
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sanguis_script  # noqa: E402

PASS, FAIL = [], []

ANDROID_SHA256 = ("3d624bb9e62fcf59a42bd30537ff457f8387227f8c01b60eb7e19b73b"
                  "3a1bdfb")
ANDROID_SIZE = 2628256
LINUX_SHA256 = ("8b8f476d9ea12845a41ca146688088c7586d961e4fc70b4295d5b46766"
                "ff947b")


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class _Elf(object):
    """Minimal ELF64 parser: header, program headers, section headers,
    and the .note.android.ident note. Raises ValueError on bad input."""

    PT_DYNAMIC = 2
    PT_INTERP = 3
    PT_TLS = 7

    def __init__(self, data):
        if len(data) < 64 or data[:4] != b"\x7fELF":
            raise ValueError("not an ELF file")
        if data[4] != 2:
            raise ValueError("not ELF64")
        if data[5] != 1:
            raise ValueError("not little-endian")
        (self.e_type, self.e_machine) = struct.unpack_from("<HH", data, 16)
        (self.e_phoff,) = struct.unpack_from("<Q", data, 0x20)
        (self.e_shoff,) = struct.unpack_from("<Q", data, 0x28)
        (self.e_phentsize, self.e_phnum) = struct.unpack_from(
            "<HH", data, 0x36)
        (self.e_shentsize, self.e_shnum,
         self.e_shstrndx) = struct.unpack_from("<HHH", data, 0x3a)
        self.data = data
        self.phdrs = []
        for i in range(self.e_phnum):
            off = self.e_phoff + i * self.e_phentsize
            p_type, p_flags = struct.unpack_from("<II", data, off)
            vals = struct.unpack_from("<QQQQQQ", data, off + 8)
            self.phdrs.append({"type": p_type, "flags": p_flags,
                               "offset": vals[0], "filesz": vals[3],
                               "memsz": vals[4], "align": vals[5]})
        self.sections = []
        for i in range(self.e_shnum):
            off = self.e_shoff + i * self.e_shentsize
            vals = struct.unpack_from("<IIQQQQIIQQ", data, off)
            self.sections.append({"name_off": vals[0], "type": vals[1],
                                  "offset": vals[4], "size": vals[5],
                                  "align": vals[8]})
        strsh = self.sections[self.e_shstrndx]
        base = strsh["offset"]
        for sec in self.sections:
            end = data.index(b"\0", base + sec["name_off"])
            sec["name"] = data[base + sec["name_off"]:end].decode(
                "ascii", "replace")

    def android_note(self):
        """(min_sdk, ndk_tag) from .note.android.ident, or None."""
        for sec in self.sections:
            if sec["name"] != ".note.android.ident":
                continue
            raw = self.data[sec["offset"]:sec["offset"] + sec["size"]]
            if len(raw) < 12:
                return None
            namesz, descsz, ntype = struct.unpack_from("<III", raw, 0)
            if ntype != 1:
                return None
            desc_off = 12 + ((namesz + 3) & ~3)
            desc = raw[desc_off:desc_off + descsz]
            if len(desc) < 4:
                return None
            (sdk,) = struct.unpack_from("<I", desc, 0)
            tag = desc[4:].split(b"\0", 1)[0].decode("ascii", "replace")
            return (sdk, tag)
        return None


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    android = os.path.join(root, "sanguis", "bch-android")
    linux = os.path.join(root, "sanguis", "bch")

    # 1. Android binary: valid AArch64 ELF, static, API 26, TLS >= 64.
    check("sanguis/bch-android exists", os.path.isfile(android), android)
    elf = None
    if os.path.isfile(android):
        try:
            elf = _Elf(open(android, "rb").read())
        except ValueError as exc:
            check("sanguis/bch-android parses as ELF64", False, exc)
    if elf is not None:
        check("bch-android is ELF64 little-endian", True)
        check("bch-android machine is AArch64 (183)",
              elf.e_machine == 183, elf.e_machine)
        check("bch-android is an executable (ET_EXEC)",
              elf.e_type == 2, elf.e_type)
        types = [p["type"] for p in elf.phdrs]
        check("bch-android has no PT_INTERP (static)",
              _Elf.PT_INTERP not in types, types)
        check("bch-android has no PT_DYNAMIC (static)",
              _Elf.PT_DYNAMIC not in types, types)
        tls = [p for p in elf.phdrs if p["type"] == _Elf.PT_TLS]
        check("bch-android has a PT_TLS segment", len(tls) == 1,
              len(tls))
        if tls:
            check("bch-android PT_TLS alignment is >= 64",
                  tls[0]["align"] >= 64, tls[0]["align"])
        note = elf.android_note()
        check("bch-android carries .note.android.ident",
              note is not None, note)
        if note is not None:
            check("bch-android min SDK is 26", note[0] == 26, note)
            check("bch-android NDK tag is r27c", note[1] == "r27c",
                  note)

    # 2. Reproducible-build pin.
    if os.path.isfile(android):
        check("bch-android sha256 matches the build script output",
              _sha256(android) == ANDROID_SHA256, _sha256(android))
        check("bch-android size is %d bytes" % ANDROID_SIZE,
              os.path.getsize(android) == ANDROID_SIZE,
              os.path.getsize(android))
        mode = os.stat(android).st_mode
        check("bch-android has an executable bit",
              bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)),
              oct(mode))

    # 3. Linux bch untouched.
    check("sanguis/bch still exists", os.path.isfile(linux), linux)
    if os.path.isfile(linux):
        try:
            lin = _Elf(open(linux, "rb").read())
            check("sanguis/bch is still x86-64 (62)",
                  lin.e_machine == 62, lin.e_machine)
        except ValueError as exc:
            check("sanguis/bch still parses as ELF64", False, exc)
        check("sanguis/bch sha256 unchanged (Linux build)",
              _sha256(linux) == LINUX_SHA256, _sha256(linux))

    # 4. buildozer.spec: ships sanguis/bch, never bch-android/bch.exe.
    spec = open(os.path.join(root, "buildozer.spec"),
                "r", encoding="utf-8").read()
    pats = [ln for ln in spec.splitlines()
            if ln.startswith("source.include_patterns")]
    check("buildozer.spec has source.include_patterns",
          len(pats) == 1, len(pats))
    if pats:
        check("source.include_patterns lists sanguis/bch",
              "sanguis/bch" in pats[0], pats[0])
    live = [ln for ln in spec.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    check("buildozer.spec does not ship bch-android as-is",
          not any("bch-android" in ln for ln in live),
          [ln for ln in live if "bch-android" in ln])
    check("buildozer.spec does not bundle Windows bch.exe",
          not any("bch.exe" in ln for ln in live),
          [ln for ln in live if "bch.exe" in ln])

    # 5. build.yml: CI stages bch-android onto sanguis/bch, then
    # verifies the APK actually carries it.
    yml = open(os.path.join(root, ".github", "workflows", "build.yml"),
               "r", encoding="utf-8").read()
    check("build.yml stages bch-android onto sanguis/bch",
          "cp sanguis/bch-android sanguis/bch" in yml)
    check("build.yml stages before buildozer android debug",
          yml.index("cp sanguis/bch-android sanguis/bch")
          < yml.index("buildozer android debug"))
    check("build.yml verifies the staged binary is AArch64",
          "AArch64" in yml)
    check("build.yml checks the APK carries sanguis/bch",
          'grep -q "sanguis/bch"' in yml
          or "grep sanguis" in yml)
    check("build.yml checks the APK carries sanguis/interp.bc",
          'grep -q "sanguis/interp.bc"' in yml)

    # 6. Exec-bit shim stages the real Android binary (simulated
    # Android; the staged copy is NOT executed — aarch64 on x86).
    tmpdir = tempfile.mkdtemp(prefix="sanguis-android-bch-binary-")
    try:
        old_env = dict(os.environ)
        old_private = sanguis_script._private_dir
        old_bch = sanguis_script.bch_path
        old_interp = sanguis_script.interp_path
        private = os.path.join(tmpdir, "private")
        interp = os.path.join(root, "sanguis", "interp.bc")
        os.environ["ANDROID_ROOT"] = "/system"
        sanguis_script._private_dir = lambda: private
        sanguis_script.bch_path = lambda: android
        sanguis_script.interp_path = lambda: interp
        try:
            dest = sanguis_script.executable_bch_path()
            check("shim stages bch-android under private/sanguis/bch",
                  dest == os.path.join(private, "sanguis", "bch"),
                  dest)
            check("staged copy is byte-identical to bch-android",
                  open(dest, "rb").read()
                  == open(android, "rb").read())
            check("staged copy is mode 0o755",
                  stat.S_IMODE(os.stat(dest).st_mode) == 0o755)
            check("scripting_available true with bch-android staged",
                  sanguis_script.scripting_available() is True)
        finally:
            sanguis_script._private_dir = old_private
            sanguis_script.bch_path = old_bch
            sanguis_script.interp_path = old_interp
            os.environ.clear()
            os.environ.update(old_env)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
