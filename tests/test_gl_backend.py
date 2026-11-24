#!/usr/bin/env python3
"""OpenGL ES backend tests for gpu_drain.py -- imports gpu_drain only.

Real-GL tests run when EGL is available here and skip cleanly (named
skips, same pattern as test_script_packaging.py) when it is not.
Fake-EGL tests monkeypatch gpu_drain.EGL_LIB / GLES_LIB to missing
names, so they pass on a machine without GL too. Garbage-input and
custom-backend checks need no GL at all.

Covers: context init + default Mandelbrot compile, demo queue drain,
a valid custom program (the rings shader from demo-gpu-page.mu block
1), a bad custom shader -> fatal_error -> whole-drain CPU fallback,
gpu_read_planes (3 planes, stats equal to gpu_read_plane),
gpu_open_cap (stored; dispatch refused when bit 0 is set and float
textures are simulated absent), gpu_matvec [[1,2],[3,4]]x[5,6]=[17,39],
release() idempotent, result dict "backend" key, GL-vs-CPU image
equivalence (demo + off-centre params), garbage never raising, and a
generous speed check (GL demo drain under 1 s at 160x120).

Run: ~/workspace/venvs/shades/bin/python tests/test_gl_backend.py
"""
import hashlib
import io
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpu_drain  # noqa: E402
from gpu_drain import (CpuPixelBackend, GLESBackend, drain_gpu_commands,  # noqa: E402
                       select_backend)

PASS, FAIL, SKIP = [], [], []

DEMO_PAGE = os.path.expanduser(
    "~/workspace/your_files/sHades-refactor/demo-gpu-page.mu")
CPU_DEMO_MD5 = "f6805c0f29d4ccf2ce68eb21f8d49c5b"

DEMO = [["gpu_open", 1.0],
        ["gpu_upload_f32", 1.0, [-0.75, 0.0, 3.0, 48.0, 0.0, 1.0]],
        ["gpu_dispatch", 1.0, 2.0],
        ["gpu_read_f32", 1.0, 0.0, 4.0],
        ["gpu_close", 1.0]]

OFF_CENTRE = [["gpu_open", 1.0],
              ["gpu_upload_f32", 1.0, [-0.75, 0.3, 2.0, 64.0, 10.0, 1.2]],
              ["gpu_dispatch", 1.0, 0.0],
              ["gpu_close", 1.0]]

GRADIENT = """
precision highp float;
uniform vec2 u_size;
void main() {
  float fx = gl_FragCoord.x / u_size.x;
  float fy = 1.0 - gl_FragCoord.y / u_size.y;
  gl_FragColor = vec4(fx, fy, 0.5, 1.0);
}
"""

BAD_SHADER = "precision highp float; void main() { this is not glsl; }"

#: Names of the real-GL checks, in order; skipped as a group when
#: EGL is unavailable on this machine.
REAL_GL_CHECKS = [
    "select_backend picks OpenGL ES here",
    "GL version string is non-empty",
    "GL demo queue drains to an image",
    "GL run reports backend opengl",
    "GL run first note names the backend",
    "GL demo image size, mode and variety",
    "gradient custom program renders on GL",
    "gradient top-left is dark red/green, blue ~128",
    "gradient bottom-right is bright red/green",
    "rings shader from demo page block 1 shades on GL",
    "rings centre pixel is the mid-mix colour",
    "rings image differs from CPU Mandelbrot",
    "bad shader falls back to CPU backend",
    "bad shader fallback note is first",
    "bad shader fallback image is the CPU Mandelbrot",
    "bad shader: gpu_program still listed handled",
    "GL read_planes notes one block per plane",
    "GL read_planes stats equal read_plane stats",
    "GL open_cap note recorded and cap stored",
    "GL cap 1 dispatch succeeds with float textures",
    "GL matvec -> [17, 39]",
    "GL matvec note shows shape/result",
    "cap-bit-0 refusal: no image",
    "cap-bit-0 refusal: noted, no fatal re-run",
    "release() is idempotent and safe to call twice",
    "GL demo vs CPU >=99% within tol 4",
    "off-centre GL vs CPU >=99% within tol 4",
    "GL demo drain completes in under 1 s at 160x120",
]


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def skip(name, reason):
    SKIP.append(name)
    print("SKIP  " + name + "  | " + reason)


def compare(img_a, img_b, tol=4):
    """Return (pct within tol, pct exact) over all pixels."""
    a = list(img_a.getdata())
    b = list(img_b.getdata())
    assert len(a) == len(b)
    within = exact = 0
    for pa, pb in zip(a, b):
        if all(abs(x - y) <= tol for x, y in zip(pa, pb)):
            within += 1
        if pa == pb:
            exact += 1
    return 100.0 * within / len(a), 100.0 * exact / len(a)


def _mean_brightness(img):
    pixels = list(img.getdata())
    total = sum(r + g + b for r, g, b in pixels)
    return total / (3.0 * len(pixels)) if pixels else 0.0


def _script_blocks(page_text):
    """Extract the `=sanguis blocks from a micron page, in order."""
    lines = page_text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if lines[i] == "`=sanguis":
            start = i + 1
            for j in range(start, len(lines)):
                if lines[j] == "`=":
                    blocks.append("\n".join(lines[start:j]) + "\n")
                    i = j
                    break
            else:
                break
        i += 1
    return blocks


def rings_shader():
    """The rings fragment shader from demo-gpu-page.mu block 1.

    The Sanguis source carries it as one escaped string literal
    (``let src = "...\\n..."``); decode the escapes to GLSL.
    """
    with open(DEMO_PAGE, "r", encoding="utf-8") as handle:
        blocks = _script_blocks(handle.read())
    match = re.search(r'let src = "(.*)";', blocks[1])
    return match.group(1).encode("utf-8").decode("unicode_escape")


class MinimalFakeBackend:
    """Smallest backend the drainer accepts: every call is a no-op."""

    def create_surface(self, handle, width, height):
        pass

    def upload(self, handle, values):
        pass

    def dispatch(self, handle, frame):
        pass

    def read_floats(self, handle, offset, count):
        return []

    def read_plane(self, handle, plane):
        return None

    def read_planes(self, handle, planes):
        return [None for _ in planes]

    def read_window(self, handle, x, y, w, h):
        return None

    def program(self, handle, source):
        pass

    def set_cap(self, handle, cap):
        pass

    def matvec(self, handle, other):
        return None

    def zero(self, handle):
        pass

    def close(self, handle):
        pass

    def image(self, handle):
        return None


def main():
    cpu_demo = drain_gpu_commands(DEMO, backend=CpuPixelBackend())
    buf = io.BytesIO()
    cpu_demo["image"].save(buf, format="PNG")
    check("CPU demo PNG md5 unchanged",
          hashlib.md5(buf.getvalue()).hexdigest() == CPU_DEMO_MD5,
          hashlib.md5(buf.getvalue()).hexdigest())

    # ---- matvec on the CPU backend (no GL needed) ----
    cpu = CpuPixelBackend()
    res_cpu = drain_gpu_commands(
        [["gpu_open", 1.0], ["gpu_open", 2.0],
         ["gpu_upload_f32", 1.0, [1.0, 2.0, 3.0, 4.0]],
         ["gpu_upload_f32", 2.0, [5.0, 6.0]],
         ["gpu_matvec", 1.0, 2.0],
         ["gpu_read_f32", 1.0, 0.0, 2.0]],
        width=40, height=30, backend=cpu)
    check("CPU matvec -> [17, 39]",
          cpu.surfaces[1]["data"] == [17.0, 39.0],
          cpu.surfaces[1]["data"])
    check("CPU matvec note shows shape/result",
          any("gpu_matvec" in n and "2x2" in n and "17.0" in n
              for n in res_cpu["notes"]), res_cpu["notes"])
    check("explicit CPU backend reports backend cpu",
          res_cpu["backend"] == "cpu", res_cpu["backend"])

    # ---- result dict carries a "backend" key for a custom backend ----
    fake_res = drain_gpu_commands(DEMO, backend=MinimalFakeBackend())
    check("custom backend reports backend custom",
          fake_res["backend"] == "custom", fake_res["backend"])
    check("custom backend still lists handled commands",
          fake_res["handled"] == ["gpu_open", "gpu_upload_f32",
                                  "gpu_dispatch", "gpu_read_f32",
                                  "gpu_close"], fake_res["handled"])

    # ---- fake-EGL: no EGL / no GLES lib -> CPU + a reason note ----
    saved_egl = gpu_drain.EGL_LIB
    gpu_drain.EGL_LIB = "libNoSuchEGL.so.1"
    try:
        backend, reason = select_backend()
        check("no-EGL select_backend falls back to CPU",
              isinstance(backend, CpuPixelBackend) and reason is not None,
              (type(backend).__name__, reason))
        try:
            GLESBackend()
            raised = False
        except RuntimeError:
            raised = True
        check("no-EGL GLESBackend() raises RuntimeError", raised)
        res_noegl = drain_gpu_commands(DEMO)
    finally:
        gpu_drain.EGL_LIB = saved_egl
    check("no-EGL drain reports backend cpu",
          res_noegl["backend"] == "cpu", res_noegl["backend"])
    check("no-EGL note names the unavailability",
          res_noegl["notes"] and res_noegl["notes"][0].startswith(
              "GPU backend: CPU (OpenGL ES unavailable:"),
          res_noegl["notes"][:1])
    check("no-EGL image still renders (CPU)",
          res_noegl["image"] is not None
          and compare(res_noegl["image"],
                      cpu_demo["image"])[0] == 100.0)
    saved_gles = gpu_drain.GLES_LIB
    gpu_drain.GLES_LIB = "libNoSuchGLES.so.2"
    try:
        backend, reason = select_backend()
        check("no-GLES select_backend falls back to CPU",
              isinstance(backend, CpuPixelBackend) and reason is not None,
              (type(backend).__name__, reason))
        try:
            GLESBackend()
            raised = False
        except RuntimeError:
            raised = True
        check("no-GLES GLESBackend() raises RuntimeError", raised)
        res_nogles = drain_gpu_commands(DEMO)
    finally:
        gpu_drain.GLES_LIB = saved_gles
    check("no-GLES drain reports backend cpu",
          res_nogles["backend"] == "cpu", res_nogles["backend"])
    check("no-GLES note names the unavailability",
          res_nogles["notes"] and res_nogles["notes"][0].startswith(
              "GPU backend: CPU (OpenGL ES unavailable:"),
          res_nogles["notes"][:1])

    # ---- garbage inputs for the new commands never raise ----
    try:
        res_garbage = drain_gpu_commands(
            [["gpu_program", 1.0, "x"], ["gpu_program"],
             ["gpu_program", 1.0, 123], ["gpu_program", 1.0, None],
             ["gpu_program", 1.0, "x" * 70000],
             ["gpu_program", 99.0, "precision highp float;"],
             ["gpu_read_planes", 1.0, "nope"],
             ["gpu_read_planes", 1.0, [7.0]],
             ["gpu_read_planes", 1.0, []],
             ["gpu_read_planes", 1.0, [0.0, "x"]],
             ["gpu_read_planes", 99.0, [0.0]],
             ["gpu_open_cap", 1.0, "bad"], ["gpu_open_cap", 1.0],
             ["gpu_open_cap", 99.0, 1.0],
             ["gpu_matvec", 1.0], ["gpu_matvec", 1.0, 99.0],
             ["gpu_matvec", 1.0, "x"], ["gpu_matvec", 99.0, 1.0],
             ["gpu_open", 1.0], ["gpu_matvec", 1.0, 1.0],
             ["gpu_read_planes", 1.0, [0.0]]],
            width=40, height=30)
        raised = None
    except Exception as exc:  # noqa: BLE001 - the test must see any raise
        res_garbage, raised = None, exc
    check("garbage new commands never raise", raised is None, raised)
    check("garbage new commands still drain",
          res_garbage is not None and "gpu_open" in res_garbage["handled"]
          and res_garbage["backend"] in ("opengl", "cpu", "custom"),
          res_garbage)

    # ---- rings shader source (no GL needed to extract it) ----
    rings = rings_shader()
    check("rings shader extracted from demo page block 1",
          "sin(r * 20.0" in rings and "u_frame" in rings, rings[:60])

    # ---- real GL: skip the whole section cleanly without EGL ----
    backend, reason = select_backend()
    if not isinstance(backend, GLESBackend):
        for name in REAL_GL_CHECKS:
            skip(name, "no EGL here (%s)" % (reason,))
        print("\n%d passed, %d skipped, %d failed"
              % (len(PASS), len(SKIP), len(FAIL)))
        return 1 if FAIL else 0
    print("  gl_version: %s | OES_texture_float: %s"
          % (backend.gl_version, backend.has_float_texture))
    backend.release()
    check("select_backend picks OpenGL ES here",
          isinstance(backend, GLESBackend) and reason is None,
          (type(backend).__name__, reason))
    check("GL version string is non-empty",
          bool(backend.gl_version), backend.gl_version)

    # ---- demo queue on GL ----
    gl_demo = drain_gpu_commands(DEMO)
    check("GL demo queue drains to an image",
          gl_demo["image"] is not None, gl_demo["notes"][:2])
    check("GL run reports backend opengl",
          gl_demo["backend"] == "opengl", gl_demo["backend"])
    check("GL run first note names the backend",
          gl_demo["notes"] and gl_demo["notes"][0]
          == "GPU backend: OpenGL ES", gl_demo["notes"][:2])
    check("GL demo image size, mode and variety",
          gl_demo["image"] is not None
          and gl_demo["image"].size == (160, 120)
          and gl_demo["image"].mode == "RGB"
          and len(set(gl_demo["image"].getdata())) > 20
          and _mean_brightness(gl_demo["image"]) > 5.0,
          None if gl_demo["image"] is None
          else (gl_demo["image"].size, _mean_brightness(gl_demo["image"])))

    # ---- valid custom programs: gradient + rings from the demo page ----
    gl_grad = GLESBackend()
    try:
        res_grad = drain_gpu_commands(
            [["gpu_open", 1.0], ["gpu_program", 1.0, GRADIENT],
             ["gpu_dispatch", 1.0, 0.0]],
            width=40, height=30, backend=gl_grad)
    finally:
        gl_grad.release()
    img_grad = res_grad["image"]
    check("gradient custom program renders on GL",
          res_grad["backend"] == "opengl" and img_grad is not None,
          (res_grad["backend"], res_grad["notes"][:2]))
    if img_grad is not None:
        top_left = img_grad.getpixel((0, 0))
        bottom_right = img_grad.getpixel((39, 29))
        print("  gradient TL: %s BR: %s" % (top_left, bottom_right))
        check("gradient top-left is dark red/green, blue ~128",
              top_left[0] < 10 and top_left[1] < 10
              and abs(top_left[2] - 128) <= 2, top_left)
        check("gradient bottom-right is bright red/green",
              bottom_right[0] > 240 and bottom_right[1] > 240,
              bottom_right)
    else:
        check("gradient top-left is dark red/green, blue ~128",
              False, "no image")
        check("gradient bottom-right is bright red/green",
              False, "no image")

    gl_rings = GLESBackend()
    try:
        res_rings = drain_gpu_commands(
            [["gpu_open", 1.0], ["gpu_program", 1.0, rings],
             ["gpu_upload_f32", 1.0, [-0.5, 0.0, 2.0, 48.0, 0.0, 1.0]],
             ["gpu_dispatch", 1.0, 0.0], ["gpu_close", 1.0]],
            width=160, height=120, backend=gl_rings)
    finally:
        gl_rings.release()
    img_rings = res_rings["image"]
    check("rings shader from demo page block 1 shades on GL",
          res_rings["backend"] == "opengl" and img_rings is not None,
          (res_rings["backend"], res_rings["notes"][:2]))
    if img_rings is not None:
        centre = img_rings.getpixel((80, 60))
        print("  rings centre: %s" % (centre,))
        # Centre pixel FragCoord is (80.5, 60.5): r ~ 0.01, so
        # rings ~ 0.5 + 0.5*sin(0.2) ~ 0.60 and the mix lands at
        # (0.639, 0.559, 0.380) * 255 = (163, 143, 97).
        check("rings centre pixel is the mid-mix colour",
              all(abs(c - e) <= 6 for c, e in zip(
                  centre, (163, 143, 97))), centre)
        cpu_rings = drain_gpu_commands(
            [["gpu_open", 1.0],
             ["gpu_upload_f32", 1.0, [-0.5, 0.0, 2.0, 48.0, 0.0, 1.0]],
             ["gpu_dispatch", 1.0, 0.0], ["gpu_close", 1.0]],
            width=160, height=120, backend=CpuPixelBackend())
        within, _exact = compare(img_rings, cpu_rings["image"])
        check("rings image differs from CPU Mandelbrot",
              within < 50.0, within)
    else:
        check("rings centre pixel is the mid-mix colour", False, "no image")
        check("rings image differs from CPU Mandelbrot", False, "no image")

    # ---- bad custom shader -> fatal_error -> whole drain on CPU ----
    res_bad = drain_gpu_commands(
        [["gpu_open", 1.0], ["gpu_program", 1.0, BAD_SHADER],
         ["gpu_dispatch", 1.0, 2.0], ["gpu_close", 1.0]])
    check("bad shader falls back to CPU backend",
          res_bad["backend"] == "cpu", res_bad["backend"])
    check("bad shader fallback note is first",
          res_bad["notes"] and res_bad["notes"][0].startswith(
              "GL backend failed (custom program failed:"),
          res_bad["notes"][:2])
    check("bad shader fallback image is the CPU Mandelbrot",
          res_bad["image"] is not None
          and compare(res_bad["image"], cpu_demo["image"])[0] == 100.0)
    check("bad shader: gpu_program still listed handled",
          "gpu_program" in res_bad["handled"], res_bad["handled"])

    # ---- read_planes / open_cap on GL ----
    gl2 = GLESBackend()
    try:
        res_planes = drain_gpu_commands(
            [["gpu_open", 1.0],
             ["gpu_upload_f32", 1.0, [-0.75, 0.0, 3.0, 48.0, 0.0, 1.0]],
             ["gpu_open_cap", 1.0, 1.0],
             ["gpu_dispatch", 1.0, 0.0],
             ["gpu_read_planes", 1.0, [0.0, 1.0, 2.0]],
             ["gpu_read_plane", 1.0, 0.0],
             ["gpu_read_plane", 1.0, 1.0],
             ["gpu_read_plane", 1.0, 2.0]],
            width=80, height=60, backend=gl2)
    finally:
        gl2.release()
    check("GL read_planes notes one block per plane",
          len([n for n in res_planes["notes"]
               if n.startswith("gpu_read_planes")]) == 3,
          res_planes["notes"])
    multi = [n for n in res_planes["notes"]
             if n.startswith("gpu_read_planes")]
    single = [n for n in res_planes["notes"]
              if n.startswith("gpu_read_plane:")]
    check("GL read_planes stats equal read_plane stats",
          len(multi) == 3 and len(single) == 3 and all(
              a.split("mean")[1] == b.split("mean")[1]
              for a, b in zip(multi, single)), (multi, single))
    check("GL open_cap note recorded and cap stored",
          any("gpu_open_cap" in n and "cap 1 stored" in n
              for n in res_planes["notes"])
          and gl2.surfaces.get(1, {}).get("cap") == 1,
          res_planes["notes"])
    if gl2.has_float_texture:
        check("GL cap 1 dispatch succeeds with float textures",
              res_planes["backend"] == "opengl"
              and res_planes["image"] is not None,
              (res_planes["backend"], res_planes["notes"][:2]))
    else:
        skip("GL cap 1 dispatch succeeds with float textures",
             "driver lacks OES_texture_float")

    # ---- matvec on GL ----
    gl_mv = GLESBackend()
    try:
        res_mv = drain_gpu_commands(
            [["gpu_open", 1.0], ["gpu_open", 2.0],
             ["gpu_upload_f32", 1.0, [1.0, 2.0, 3.0, 4.0]],
             ["gpu_upload_f32", 2.0, [5.0, 6.0]],
             ["gpu_matvec", 1.0, 2.0],
             ["gpu_read_f32", 1.0, 0.0, 2.0]],
            width=40, height=30, backend=gl_mv)
    finally:
        gl_mv.release()
    check("GL matvec -> [17, 39]",
          gl_mv.surfaces[1]["data"] == [17.0, 39.0],
          gl_mv.surfaces[1]["data"])
    check("GL matvec note shows shape/result",
          any("gpu_matvec" in n and "2x2" in n and "17.0" in n
              for n in res_mv["notes"]), res_mv["notes"])

    # ---- cap bit 0 with float textures simulated absent -> refused ----
    gl3 = GLESBackend()
    gl3.has_float_texture = False  # simulate a driver without it
    try:
        res_cap = drain_gpu_commands(
            [["gpu_open", 1.0], ["gpu_open_cap", 1.0, 1.0],
             ["gpu_dispatch", 1.0, 0.0], ["gpu_close", 1.0]],
            width=40, height=30, backend=gl3)
    finally:
        gl3.release()
    check("cap-bit-0 refusal: no image",
          res_cap["image"] is None, res_cap["image"])
    check("cap-bit-0 refusal: noted, no fatal re-run",
          res_cap["backend"] == "opengl"
          and any("dispatch refused" in n for n in res_cap["notes"])
          and not any(n.startswith("GL backend failed")
                      for n in res_cap["notes"]),
          res_cap["notes"])

    # ---- release() idempotent ----
    gl_rel = GLESBackend()
    try:
        gl_rel.release()
        gl_rel.release()
        released_ok = True
    except Exception as exc:  # noqa: BLE001 - release must never raise
        released_ok = exc
    check("release() is idempotent and safe to call twice",
          released_ok is True, released_ok)

    # ---- GL-vs-CPU image equivalence ----
    within, exact = compare(gl_demo["image"], cpu_demo["image"])
    print("  demo GL vs CPU: %.2f%% within tol 4, %.2f%% exact"
          % (within, exact))
    check("GL demo vs CPU >=99% within tol 4", within >= 99.0, within)
    cpu_off = drain_gpu_commands(OFF_CENTRE, width=80, height=60,
                                 backend=CpuPixelBackend())
    gl_off = drain_gpu_commands(OFF_CENTRE, width=80, height=60)
    within_off, exact_off = compare(gl_off["image"], cpu_off["image"])
    print("  off-centre GL vs CPU: %.2f%% within tol 4, %.2f%% exact"
          % (within_off, exact_off))
    check("off-centre GL vs CPU >=99% within tol 4",
          within_off >= 99.0, within_off)

    # ---- speed (informational, asserted generously) ----
    start = time.perf_counter()
    drain_gpu_commands(DEMO)
    elapsed = time.perf_counter() - start
    print("  GL demo drain took %.3f s" % elapsed)
    check("GL demo drain completes in under 1 s at 160x120",
          elapsed < 1.0, elapsed)

    print("\n%d passed, %d skipped, %d failed"
          % (len(PASS), len(SKIP), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
