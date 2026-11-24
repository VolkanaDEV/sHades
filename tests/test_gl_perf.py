#!/usr/bin/env python3
"""Maximally-performant GPU drainer — cached backend + program cache.

Covers the GL-perf job (see GL-PERF-REPORT): a thread-local cached
GLESBackend reused across drains (same EGL context, ~5 ms a drain
instead of ~50 ms fresh), a refcounted program cache (same source
never recompiles), no cross-thread backend reuse (each thread gets
its own context), CPU fallback when acquire fails (nothing cached),
pixel-identical output after caching (frame-9 md5s, GL-vs-CPU
agreement), release semantics, the raw-bytes texture path (no PNG
round-trip, pixels identical to the CoreImage PNG path, right way
up), and the animation worker thread serving several queued frames
from one thread and exiting on its stop sentinel.

Run under xvfb-run (the texture section needs a Kivy window).
"""
import gc
import hashlib
import os
import queue
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpu_drain as gd  # noqa: E402
from PIL import Image  # noqa: E402

PASS, FAIL = [], []

RINGS_SRC = ("precision mediump float;\nuniform vec2 u_size;\n"
             "uniform float u_frame;\nvoid main() {\n"
             "    vec2 uv = gl_FragCoord.xy / u_size;\n"
             "    vec2 c = uv - 0.5;\n"
             "    float r = length(c) * 2.0;\n"
             "    float rings = 0.5 + 0.5 * sin(r * 20.0 - u_frame * 0.5);\n"
             "    vec3 col = mix(vec3(0.1, 0.2, 0.5), "
             "vec3(1.0, 0.8, 0.3), rings);\n"
             "    gl_FragColor = vec4(col, 1.0);\n}\n")
MANDEL_Q = [["gpu_open", 1.0],
            ["gpu_upload_f32", 1.0, [-0.75, 0.0, 3.0, 48.0, 0.0, 1.0]],
            ["gpu_dispatch", 1.0, 9.0],
            ["gpu_close", 1.0]]
RINGS_Q = [["gpu_open", 1.0],
           ["gpu_program", 1.0, RINGS_SRC],
           ["gpu_upload_f32", 1.0, [-0.5, 0.0, 2.0, 48.0, 0.0, 1.0]],
           ["gpu_dispatch", 1.0, 9.0],
           ["gpu_close", 1.0]]
# Frame-9 image md5s, reproduced headlessly before this job (see the
# anim-frames report) and pinned here: caching must not change a pixel.
MANDEL_FRAME9_MD5 = "8604a6cec5acbbe032848db46827037a"
RINGS_FRAME9_MD5 = "6b7766ee054464eaafaec616b417e45a"


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def image_md5(image):
    return hashlib.md5(image.tobytes()).hexdigest()


def agreement(img_a, img_b):
    """(exact %, within-4 %) of pixels between two RGB images."""
    a = img_a.tobytes()
    b = img_b.tobytes()
    total = len(a) // 3
    exact = 0
    within = 0
    for i in range(0, len(a), 3):
        if a[i:i + 3] == b[i:i + 3]:
            exact += 1
            within += 1
        elif (abs(a[i] - b[i]) <= 4 and abs(a[i + 1] - b[i + 1]) <= 4
                and abs(a[i + 2] - b[i + 2]) <= 4):
            within += 1
    return 100.0 * exact / total, 100.0 * within / total


def main():
    # ---- 1. context reuse across drains on one thread ----
    gd.release_thread_backend()
    r1 = gd.drain_gpu_commands(MANDEL_Q)
    b1 = gd._thread_backends.backend
    ctx1 = b1._context if b1 is not None else None
    t0 = time.perf_counter()
    r2 = gd.drain_gpu_commands(MANDEL_Q)
    cached_ms = (time.perf_counter() - t0) * 1000.0
    b2 = gd._thread_backends.backend
    check("cache: two implicit drains use the OpenGL backend",
          r1.get("backend") == "opengl" and r2.get("backend") == "opengl",
          (r1.get("backend"), r2.get("backend")))
    check("cache: same backend object and EGL context across drains",
          b1 is not None and b1 is b2 and ctx1 is not None
          and b1._context == ctx1, (ctx1, b2._context if b2 else None))
    check("cache: cached backend is NOT released after a drain",
          b2 is not None and b2._released is False, b2._released
          if b2 is not None else None)
    check("cache: second drain is fast (under 30 ms on this box)",
          cached_ms < 30.0, round(cached_ms, 2))
    check("cache: repeated drains give identical images",
          r1.get("image") is not None and r2.get("image") is not None
          and image_md5(r1["image"]) == image_md5(r2["image"]),
          (image_md5(r1["image"]) if r1.get("image") else None,
           image_md5(r2["image"]) if r2.get("image") else None))

    # ---- 2. program cache: hit reuses, miss compiles ----
    be = gd.acquire_backend()
    be.create_surface(1.0, 160, 120)
    be.program(1.0, RINGS_SRC)
    prog1 = be.surfaces[1]["program"]
    check("progcache: first program() compiles and caches at refcount 1",
          prog1 != 0 and be._programs.get(RINGS_SRC, [0, 0, 0, 0])[3] == 1,
          be._programs.get(RINGS_SRC))
    be.create_surface(2.0, 160, 120)
    be.program(2.0, RINGS_SRC)
    check("progcache: same source on a second surface reuses the program",
          be.surfaces[2]["program"] == prog1
          and be._programs[RINGS_SRC][3] == 2,
          be._programs.get(RINGS_SRC))
    be.program(1.0, RINGS_SRC)
    check("progcache: same source on the same surface does not recompile",
          be.surfaces[1]["program"] == prog1
          and be._programs[RINGS_SRC][3] == 2,
          be._programs.get(RINGS_SRC))
    other = ("precision mediump float;\nuniform vec2 u_size;\n"
             "void main() { gl_FragColor = vec4(1.0, 0.0, 0.0, 1.0); }\n")
    be.program(1.0, other)
    prog_other = be.surfaces[1]["program"]
    check("progcache: a different source compiles a new program",
          prog_other != 0 and prog_other != prog1
          and other in be._programs
          and be._programs[other][3] == 1
          and be._programs[RINGS_SRC][3] == 1,
          be._programs)
    be.close(2.0)
    check("progcache: closing the last user drops the entry",
          RINGS_SRC not in be._programs
          and other in be._programs, list(be._programs))
    be.close(1.0)
    check("progcache: closing every surface empties the cache",
          be._programs == {}, be._programs)
    gd.release_thread_backend()

    # ---- 3. no cross-thread use ----
    results = {}

    def drain_on_thread(tag):
        res = gd.drain_gpu_commands(MANDEL_Q)
        backend = gd._thread_backends.backend
        results[tag] = {
            "ctx": backend._context if backend is not None else None,
            "backend": backend,
            "md5": image_md5(res["image"]) if res.get("image") else None,
            "name": res.get("backend"),
        }

    ta = threading.Thread(target=lambda: drain_on_thread("a"))
    ta.start()
    ta.join()
    tb = threading.Thread(target=lambda: drain_on_thread("b"))
    tb.start()
    tb.join()
    check("xthread: each thread gets its own EGL context",
          results["a"]["ctx"] is not None
          and results["b"]["ctx"] is not None
          and results["a"]["ctx"] != results["b"]["ctx"]
          and results["a"]["backend"] is not results["b"]["backend"],
          (results["a"]["ctx"], results["b"]["ctx"]))
    check("xthread: both threads shade the correct image",
          results["a"]["md5"] == MANDEL_FRAME9_MD5
          and results["b"]["md5"] == MANDEL_FRAME9_MD5
          and results["a"]["name"] == "opengl"
          and results["b"]["name"] == "opengl",
          (results["a"]["md5"], results["b"]["md5"]))
    # Both backends are registered: release_all tears them down.
    gd.release_all_cached_backends()
    check("xthread: release_all releases both thread backends",
          results["a"]["backend"]._released
          and results["b"]["backend"]._released,
          (results["a"]["backend"]._released,
           results["b"]["backend"]._released))
    del results["a"]["backend"], results["b"]["backend"]
    results.clear()
    gc.collect()

    # ---- 4. fallback still works when EGL is gone ----
    gd.release_all_cached_backends()
    gd.release_thread_backend()
    real_egl = gd.EGL_LIB
    gd.EGL_LIB = "libEGL-no-such-library.so.1"
    try:
        raised = None
        try:
            gd.acquire_backend()
        except Exception as exc:  # noqa: BLE001 - the raise is the point
            raised = exc
        check("fallback: acquire_backend raises with no EGL",
              raised is not None, raised)
        check("fallback: a failed acquire leaves nothing cached",
              getattr(gd._thread_backends, "backend", None) is None,
              getattr(gd._thread_backends, "backend", None))
        r_cpu = gd.drain_gpu_commands(MANDEL_Q)
        check("fallback: drain falls back to the CPU backend + note",
              r_cpu.get("backend") == "cpu"
              and r_cpu.get("notes")
              and r_cpu["notes"][0].startswith(
                  "GPU backend: CPU (OpenGL ES unavailable:"),
              (r_cpu.get("backend"), r_cpu.get("notes", [])[:1]))
        check("fallback: CPU fallback image is still frame 9",
              r_cpu.get("image") is not None
              and image_md5(r_cpu["image"]) == MANDEL_FRAME9_MD5,
              image_md5(r_cpu["image"]) if r_cpu.get("image") else None)
        check("fallback: the fallback drain caches nothing either",
              getattr(gd._thread_backends, "backend", None) is None,
              getattr(gd._thread_backends, "backend", None))
    finally:
        gd.EGL_LIB = real_egl

    # ---- 5. pixel equivalence after caching ----
    gd.release_thread_backend()
    r_gl = gd.drain_gpu_commands(MANDEL_Q)
    r_rings = gd.drain_gpu_commands(RINGS_Q)
    cpu = gd.CpuPixelBackend()
    r_cpu2 = gd.drain_gpu_commands(MANDEL_Q, backend=cpu)
    check("pixels: cached GL frame-9 Mandelbrot md5 reproduces",
          r_gl.get("image") is not None
          and image_md5(r_gl["image"]) == MANDEL_FRAME9_MD5,
          image_md5(r_gl["image"]) if r_gl.get("image") else None)
    check("pixels: cached GL frame-9 rings md5 reproduces",
          r_rings.get("image") is not None
          and image_md5(r_rings["image"]) == RINGS_FRAME9_MD5,
          image_md5(r_rings["image"]) if r_rings.get("image") else None)
    exact, within = agreement(r_gl["image"], r_cpu2["image"])
    check("pixels: GL-vs-CPU agreement stays 99.99% (exact/within-4)",
          exact >= 99.99 and within >= 99.99, (exact, within))
    check("pixels: the CPU backend re-runs the program cache path too",
          r_cpu2.get("backend") == "cpu"
          and r_cpu2.get("image") is not None, r_cpu2.get("backend"))
    gd.release_thread_backend()

    # ---- 5b. fatal program failure drops the cached backend ----
    bad_q = [["gpu_open", 1.0],
             ["gpu_program", 1.0, "this is not a shader;"],
             ["gpu_upload_f32", 1.0, [-0.75, 0.0, 3.0, 48.0, 0.0, 1.0]],
             ["gpu_dispatch", 1.0, 9.0],
             ["gpu_close", 1.0]]
    gd.release_thread_backend()
    r_bad = gd.drain_gpu_commands(bad_q)
    check("fatal: a bad custom program re-runs the queue on CPU",
          r_bad.get("backend") == "cpu"
          and any("GL backend failed" in n for n in r_bad.get("notes", []))
          and r_bad.get("image") is not None
          and image_md5(r_bad["image"]) == MANDEL_FRAME9_MD5,
          (r_bad.get("backend"), r_bad.get("notes", [])[:1]))
    check("fatal: the failed backend is dropped, not kept cached",
          getattr(gd._thread_backends, "backend", None) is None,
          getattr(gd._thread_backends, "backend", None))
    r_after = gd.drain_gpu_commands(MANDEL_Q)
    check("fatal: the next drain rebuilds a healthy cached backend",
          r_after.get("backend") == "opengl"
          and gd._thread_backends.backend is not None
          and gd._thread_backends.backend.fatal_error is None,
          r_after.get("backend"))
    gd.release_thread_backend()

    # ---- 6. release semantics ----
    fresh, reason = gd.select_backend()
    check("release: select_backend still returns a fresh backend",
          isinstance(fresh, gd.GLESBackend) and reason is None, reason)
    r_exp = gd.drain_gpu_commands(MANDEL_Q, backend=fresh)
    check("release: an explicit backend is released after its drain",
          fresh._released is True and r_exp.get("backend") == "opengl",
          (fresh._released, r_exp.get("backend")))
    rel = gd.GLESBackend()
    rel.release()
    rel.release()
    check("release: release() is idempotent and safe to call twice",
          rel._released is True, rel._released)
    gd.acquire_backend()
    gd.release_all_cached_backends()
    check("release: release_all clears this thread's slot",
          getattr(gd._thread_backends, "backend", None) is None,
          getattr(gd._thread_backends, "backend", None))
    gd.release_all_cached_backends()
    check("release: release_all with nothing cached does not raise",
          True)

    # ---- 7. raw-bytes texture path (Kivy, under xvfb) ----
    import app_site  # noqa: E402
    from kivy.core.image import Image as CoreImage  # noqa: E402
    import io  # noqa: E402
    width, height = 8, 6
    px = bytearray(width * height * 3)
    for py in range(height):
        for px_i in range(width):
            pos = (py * width + px_i) * 3
            px[pos] = (px_i * 36) % 256
            px[pos + 1] = (py * 51) % 256
            px[pos + 2] = ((px_i + py) * 17) % 256
    image = Image.frombytes("RGB", (width, height), bytes(px))
    raw = image.tobytes()
    tex_raw = app_site._texture_from_raw(raw, (width, height))
    png_bytes = app_site._pil_to_png_bytes(image)
    tex_png = CoreImage(io.BytesIO(png_bytes), ext="png").texture
    check("texture: raw blit has the right size",
          tuple(tex_raw.size) == (width, height), tex_raw.size)
    check("texture: raw-blit pixels are identical to the PNG path",
          tex_raw.pixels == tex_png.pixels,
          (len(tex_raw.pixels or b""), len(tex_png.pixels or b"")))
    # Texture.pixels comes back RGBA (alpha 255) on this provider,
    # on both paths alike; strip the alpha and the top row must be
    # the image's top row -- no vertical flip anywhere.
    rgba = tex_raw.pixels or b""
    rgb = b"".join(rgba[i:i + 3] for i in range(0, len(rgba), 4))
    check("texture: raw-blit pixels are right way up (top row first)",
          rgb[:width * 3] == raw[:width * 3],
          (rgb[:6], raw[:6]))
    tex_wrap = app_site._rgb_bytes_to_texture(raw, (width, height))
    check("texture: the raw/PNG wrapper lands the same pixels",
          tex_wrap.pixels == tex_png.pixels, None)

    # ---- 8. one worker thread serves several queued frames ----
    import kivy_client as kc  # noqa: E402
    real_clock = app_site.Clock

    class _CaptureClock(object):
        callbacks = []

        @staticmethod
        def schedule_once(fn, dt=0):
            _CaptureClock.callbacks.append((fn, dt))

    app_site.Clock = _CaptureClock
    try:
        app = types.SimpleNamespace()
        app._script_bindings = types.MethodType(
            kc.sHadesClientApp._script_bindings, app)
        app._site_current = ("c" * 64, "/index.mu")
        app.root = types.SimpleNamespace(
            get_screen=lambda name: types.SimpleNamespace(
                _current_channel="wiki"))
        calls = []
        drains = []

        def fake_run(source, bindings):
            calls.append(bindings)
            return {"ok": 1.0, "result": 1.0, "out": [],
                    "gpu": [["gpu_open", 1.0]]}

        def fake_drain(queue):
            drains.append(list(queue))
            return {"image": Image.new("RGB", (4, 3), color=(0, 255, 0)),
                    "notes": [], "handled": ["gpu_open"],
                    "unhandled": [], "backend": "cpu"}

        app._run_page_script = fake_run
        app._drain_page_gpu = fake_drain
        state = {"queue": queue.Queue(), "worker": object(),
                 "source": "cell main() {}", "cid": "c" * 64}
        state["queue"].put(1)
        state["queue"].put(2)
        state["queue"].put(None)
        app_site._anim_worker(app, state)
        frames = [b[-1][1] for b in calls
                  if b and b[-1][0] == "frame"]
        check("worker: one worker run serves two queued frames",
              frames == [1.0, 2.0] and len(drains) == 2,
              (frames, len(drains)))
        check("worker: both frames schedule a main-thread landing",
              len(_CaptureClock.callbacks) == 2, _CaptureClock.callbacks)
        check("worker: the sentinel clears the worker slot",
              state.get("worker") is None, state.get("worker"))
        # The stop sentinel alone: no run, prompt exit.
        _CaptureClock.callbacks = []
        calls[:] = []
        drains[:] = []
        state2 = {"queue": queue.Queue(), "worker": object(),
                  "source": "cell main() {}", "cid": "c" * 64}
        state2["queue"].put(None)
        app_site._anim_worker(app, state2)
        check("worker: a lone sentinel runs nothing and exits",
              calls == [] and drains == []
              and state2.get("worker") is None, (calls, drains))
        app_site._anim_stop(state)
        check("worker: _anim_stop marks stopped and enqueues a sentinel",
              state.get("stopped") is True
              and state["queue"].get_nowait() is None, state.get("stopped"))
    finally:
        app_site.Clock = real_clock

    print("PASS GL perf: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
