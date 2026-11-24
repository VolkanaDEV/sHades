"""GPU queue drainer for sHades micron pages.

The interpreter (``interp.sanguis``, ``script_run``) never executes a
``gpu_*`` call: it records it. Every call except ``gpu_info`` appends a
command record ``[name, args...]`` (raw values, unwrapped) to the GPU
queue, and ``gpu_open`` returns a synthetic incrementing handle id
starting at 1.0 whose queue record carries that handle as its first
argument. This module is the host side of that contract: it drains the
queue after ``script_run`` returns, on the sHades host (Linux / Windows
here; the renderer calls it on a worker thread).

There is no Sanguis OS in this deployment and the kit documents NO
``gpu_*`` calling conventions (they were Sanguis OS builtins), so this
drainer DEFINES what each queued command means on the host. The table
below is the contract; the same table is in the delivery report.

Host-side contract -- one row per queued command name
-----------------------------------------------------
``gpu_info()``
    Never queued: the interpreter answers it inline from the host's
    ``gpu_present`` binding (1.0/0.0). If it appears in a hand-built
    queue the drainer only notes it; there is nothing to drain.
``gpu_open(handle)``
    Create a surface for the interpreter-assigned ``handle`` at the
    drain size (``width`` x ``height``). The surface holds a float
    data buffer (initially empty), a 6-slot parameter vector (see
    ``gpu_upload_f32``), and a pixel buffer (initially none).
``gpu_open_cap(handle, cap)``
    Sets a capability/flags word on the surface; ``cap`` is a float
    whose bits select features (bit 0: float32 textures). The GL
    backend refuses a dispatch on a surface that asks for float32
    textures when the driver lacks ``OES_texture_float``; the CPU
    backend stores the word only.
``gpu_dispatch(handle, frame)``
    Shade one frame into the surface's pixel buffer with the
    Mandelbrot pixel function (see ``mandel_pixel``) using the
    surface's parameter vector and ``frame`` (int, >= 0; the frame
    zooms the view, exactly like the kit's ``shade_frame`` zoom).
    The result image is the most recently dispatched frame,
    snapshotted at dispatch time, so a later ``gpu_close`` does not
    discard it.
``gpu_upload_f32(handle, values)``
    Store ``values`` (a list of numbers) as the surface's float data
    buffer, and take its first six entries as the shader parameter
    vector ``P`` (missing entries keep the defaults, non-finite
    entries fall back to the default for that slot)::

        P[0] centre of the view, real axis      (default -0.75)
        P[1] centre of the view, imag axis      (default  0.0)
        P[2] view span, complex-plane width     (default  3.0)
        P[3] max Mandelbrot iterations 1..512   (default 48.0)
        P[4] palette shift, added to the escape count (default 0.0)
        P[5] brightness scale 0..2              (default  1.0)

``gpu_read_f32(handle, offset, count)``
    Read ``count`` floats from the surface's data buffer starting at
    ``offset`` (clamped to the buffer). The values are reported in
    ``notes``; this command produces no image by itself.
``gpu_read_plane(handle, plane)``
    Read one colour plane of the most recently shaded frame:
    plane 0 = R, 1 = G, 2 = B. Reports min / max / mean of that
    plane in ``notes``. Needs a dispatched frame first.
``gpu_read_planes(handle, planes)``
    Read several colour planes of the most recently shaded frame
    at once; ``planes`` is a list of plane indices 0..2. Reports
    one min / max / mean stat block per plane in ``notes``. Needs
    a dispatched frame first.
``gpu_zero(handle)``
    Zero the surface's data buffer and discard its pixel buffer
    (a later readback without a new dispatch yields no image).
``gpu_read_window(handle, x, y, w, h)``
    Read the rectangle ``(x, y, w, h)`` of the most recently shaded
    frame (clamped to the surface). Reports per-channel mean in
    ``notes``. Needs a dispatched frame first.
``gpu_save_buffer(handle, name)``
    DEFINED, NOT EXECUTED. Persist the surface's data buffer under
    ``name`` in host-side storage. Deliberately not implemented:
    a script must never choose host filesystem paths. A future
    backend would map ``name`` into a page-scoped store. Reported
    unhandled.
``gpu_load_buffer(handle, name)``
    DEFINED, NOT EXECUTED. Restore a buffer previously saved with
    ``gpu_save_buffer`` under ``name``. Reported unhandled.
``gpu_load_file(handle, path)``
    DEFINED, NOT EXECUTED. Load float data from a host file. A
    future backend would restrict ``path`` to the page directory
    and refuse anything else; the drainer never opens a
    script-supplied path. Reported unhandled.
``gpu_edge_beat(handle)``
    DEFINED, NOT EXECUTED. Advance the surface's edge/oscillator
    clock by one beat (the edge family is a beat-synchronised
    effect state: beat / mark / decay / walk). Reported unhandled.
``gpu_edge_mark(handle, t)``
    DEFINED, NOT EXECUTED. Mark an edge event at time ``t`` on the
    surface. Reported unhandled.
``gpu_edge_decay(handle, rate)``
    DEFINED, NOT EXECUTED. Set the edge decay rate on the surface.
    Reported unhandled.
``gpu_edge_walk(handle, steps)``
    DEFINED, NOT EXECUTED. Advance the edge state ``steps`` steps.
    Reported unhandled.
``gpu_matvec(handle, other)``
    Matrix-vector product, computed on the CPU in both backends
    (GLES2 has no guaranteed float render target, and the op is
    exact linear algebra): treat ``handle``'s data buffer as a
    row-major matrix and ``other``'s as the vector; write the
    result into ``handle``'s buffer. The row count is
    ``len(matrix) // len(vector)`` (leftover entries ignored);
    an empty buffer or a zero-length vector is a no-op.
``gpu_close(handle)``
    Release the surface. Later commands naming the handle are
    reported as unknown-handle notes, never a crash.
``gpu_hybrid_stats()``
    DEFINED, NOT EXECUTED. Report hybrid CPU/GPU backend
    statistics (frames shaded, time spent, backend name). Takes no
    handle. A future backend would return the stat block; this
    drainer reports the call as unhandled. Reported unhandled.
``gpu_program(handle, source)``
    Compile ``source`` as the surface's fragment shader (GLSL ES
    1.00, fixed vertex shader, uniforms ``u_params`` / ``u_params2``
    / ``u_frame`` / ``u_size`` as in ``mandel_pixel``) and shade
    later dispatches on that surface with it. The CPU backend
    stores the source and keeps shading Mandelbrot. A GL
    compile/link failure is fatal for the whole drain: the queue
    is re-run on the CPU backend (see ``drain_gpu_commands``).

Pixel function (``mandel_pixel``)
----------------------------------
The CPU backend shades the kit's ``shade_frame`` shape -- a
``width`` x ``height`` top-down RGB buffer -- with this exact
function (floats, not the kit's Q16 integers, so the values below
are the definition, not a bit-exact copy of the kit shader)::

    span_eff = P[2] * 4096 / (4096 + frame * 2048)
    cr = P[0] + (px / width  - 0.5) * span_eff
    ci = P[1] + (py / height - 0.5) * span_eff * (height / width)
    z = 0; k = 0
    while k < P[3] and |z|^2 <= 4: z = z*z + (cr + i*ci); k += 1
    inside (never escaped)            -> (0, 0, 4)   (kit: c = 4)
    escaped at k                      -> r = (k+P[4])*5 mod 256
                                         g = (k+P[4])*6 mod 256
                                         b = (k+P[4])*3 mod 256
                                         each scaled by P[5], clamped
                                         to 0..255 (kit palette)

``frame`` zooms exactly like the kit: at frame 0 the span is P[2];
each frame narrows it by ``4096 / (4096 + frame * 2048)``. The
aspect term ``height / width`` keeps pixels square; the kit maps
both axes with the raw span.

Backend protocol
----------------
``drain_gpu_commands`` takes an optional ``backend``. With none
given it uses the calling thread's cached :class:`GLESBackend`
(:func:`acquire_backend` -- one backend per thread, its context
re-made current on every use, because a context is bound to the
thread that made it current) and falls back to
:class:`CpuPixelBackend` on any failure; the returned dict's
``"backend"`` key says which one shaded (``"opengl"`` / ``"cpu"``)
and the first note names it. A caller-supplied backend is still
released after the drain; the cached backend is kept for the
next drain (and dropped when it records a fatal error).
:func:`select_backend` stays as the fresh-backend primitive
(one new :class:`GLESBackend` per call, never cached). A backend implements::

    create_surface(handle, width, height)
    upload(handle, values)               values: list[float]
    dispatch(handle, frame)              frame: int
    read_floats(handle, offset, count)   -> list[float]
    read_plane(handle, plane)            -> dict | None
    read_planes(handle, planes)          -> list[dict | None]
    read_window(handle, x, y, w, h)      -> dict | None
    program(handle, source)              source: GLSL ES 1.00 string
    set_cap(handle, cap)                 cap: int flags word
    matvec(handle, other)                -> dict | None
    zero(handle)
    close(handle)
    image(handle)                        -> PIL Image | None
    release()                            optional; destroy GL state

``CpuPixelBackend`` is pure Python + Pillow, no GL, no window,
no display. ``GLESBackend`` renders into a per-surface FBO with
an RGBA8 texture and reads the frame back with ``glReadPixels``;
its fragment shader flips Y in-shader (``fy`` is the top-down
row coordinate) and the readback rows are reversed, so the
stored image lands top-down like the CPU backend (see
``DEFAULT_FRAGMENT_SHADER``).
A GL backend that hits a fatal error (bad custom program,
context loss) sets ``fatal_error``; the drainer then re-runs the
whole queue on a fresh CPU backend and says so in the notes.

The drainer validates queue entries and handle liveness before
calling the backend, and never lets a backend exception escape:
it is caught, noted, and draining continues. Tests inject a
recording fake implementing the same methods.

Safety: the drainer never raises on garbage input -- wrong arg
counts, wrong types, unknown command names, an empty queue
(``image`` None, ``notes`` empty), ``None`` entries, a ``None``
queue -- all are handled, reported in ``notes`` / ``unhandled``,
and no exception escapes ``drain_gpu_commands``.
"""

import ctypes
import math
import threading
import weakref

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow is present in the venv
    Image = None

__all__ = [
    "drain_gpu_commands",
    "CpuPixelBackend",
    "GLESBackend",
    "select_backend",
    "acquire_backend",
    "release_thread_backend",
    "release_all_cached_backends",
    "DEFAULT_VERTEX_SHADER",
    "DEFAULT_FRAGMENT_SHADER",
    "mandel_pixel",
    "DEFAULT_PARAMS",
    "IMPLEMENTED_COMMANDS",
    "ALL_GPU_COMMANDS",
    "DEFAULT_WIDTH",
    "DEFAULT_HEIGHT",
]

#: Library names for the GL backend (module globals so a probe can
#: point the loader at a missing name to simulate "no EGL").
EGL_LIB = "libEGL.so.1"
GLES_LIB = "libGLESv2.so.2"

#: Max custom-program source length (chars); longer is noted + skipped.
MAX_PROGRAM_SOURCE = 65536

DEFAULT_WIDTH = 160
DEFAULT_HEIGHT = 120
MAX_DIMENSION = 1024
MAX_ITERATIONS = 512
MAX_UPLOAD = 65536

#: Default shader parameter vector (see ``gpu_upload_f32``).
DEFAULT_PARAMS = [-0.75, 0.0, 3.0, 48.0, 0.0, 1.0]

#: Commands this drainer executes (the live subset).
IMPLEMENTED_COMMANDS = frozenset([
    "gpu_open",
    "gpu_open_cap",
    "gpu_upload_f32",
    "gpu_dispatch",
    "gpu_read_f32",
    "gpu_read_plane",
    "gpu_read_planes",
    "gpu_read_window",
    "gpu_zero",
    "gpu_matvec",
    "gpu_program",
    "gpu_close",
])

#: All 21 gpu_* names the interpreter can queue, in its allowlist order.
ALL_GPU_COMMANDS = (
    "gpu_info", "gpu_open_cap", "gpu_dispatch", "gpu_open",
    "gpu_upload_f32", "gpu_read_f32", "gpu_read_plane",
    "gpu_read_planes", "gpu_zero", "gpu_read_window",
    "gpu_save_buffer", "gpu_load_buffer", "gpu_load_file",
    "gpu_edge_beat", "gpu_edge_mark", "gpu_edge_decay",
    "gpu_edge_walk", "gpu_matvec", "gpu_close", "gpu_hybrid_stats",
    "gpu_program",
)

#: One-line defined meaning for every unhandled command (used in notes).
CONTRACT = {
    "gpu_info": "answered inside the interpreter from the gpu_present "
                "binding and never queued; nothing to drain",
    "gpu_save_buffer": "persists the surface data buffer under a "
                       "host-side name (page-scoped store)",
    "gpu_load_buffer": "restores a buffer saved with gpu_save_buffer",
    "gpu_load_file": "loads float data from a host file (page "
                     "directory only)",
    "gpu_edge_beat": "advances the surface edge/oscillator clock one beat",
    "gpu_edge_mark": "marks an edge event at time t on the surface",
    "gpu_edge_decay": "sets the edge decay rate on the surface",
    "gpu_edge_walk": "advances the edge state a number of steps",
    "gpu_hybrid_stats": "reports hybrid CPU/GPU backend statistics",
}


def _is_number(value):
    """True for a real int/float (bools and NaN are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value  # NaN != NaN


def _handle_key(value):
    """Normalise a queued handle to a dict key, or None if not a handle."""
    if not _is_number(value):
        return None
    if isinstance(value, float):
        if value in (math.inf, -math.inf):
            return None
        if value.is_integer():
            return int(value)
        return value
    return value


def _sanitize_params(values):
    """Build a 6-slot shader parameter vector from uploaded floats."""
    params = list(DEFAULT_PARAMS)
    for i, default in enumerate(DEFAULT_PARAMS):
        if i >= len(values):
            break
        value = values[i]
        if not _is_number(value) or value in (math.inf, -math.inf):
            continue
        if i == 2:  # span: must be positive
            params[i] = min(max(float(value), 1e-6), 1e6)
        elif i == 3:  # max iterations: int, 1..512
            params[i] = float(min(max(int(value), 1), MAX_ITERATIONS))
        elif i == 5:  # brightness: 0..2
            params[i] = min(max(float(value), 0.0), 2.0)
        else:
            params[i] = float(value)
    return params


def mandel_pixel(px, py, width, height, params, frame):
    """One shaded pixel (r, g, b), per the contract in the docstring.

    ``params`` is a 6-slot vector as built by ``_sanitize_params``;
    ``frame`` is the dispatch frame number (zooms the view).
    """
    cx, cy, span, max_iter, shift, bright = params
    span_eff = span * 4096.0 / (4096.0 + frame * 2048.0)
    cr = cx + (px / width - 0.5) * span_eff
    ci = cy + (py / height - 0.5) * span_eff * (height / width)
    zr = 0.0
    zi = 0.0
    k = 0
    limit = int(max_iter)
    while k < limit:
        if zr * zr + zi * zi > 4.0:
            break
        zr, zi = zr * zr - zi * zi + cr, 2.0 * zr * zi + ci
        k += 1
    if k >= limit:
        return (0, 0, 4)
    kk = k + shift
    r = min(255, int(((kk * 5) % 256) * bright))
    g = min(255, int(((kk * 6) % 256) * bright))
    b = min(255, int(((kk * 3) % 256) * bright))
    return (r, g, b)


def _plane_stats(pixels, plane):
    """Min / max / mean of one colour plane of an RGB bytearray."""
    channel = pixels[plane::3]
    total = sum(channel)
    count = len(channel)
    return {
        "plane": plane,
        "count": count,
        "min": min(channel),
        "max": max(channel),
        "mean": total / count if count else 0.0,
    }


def _window_stats(pixels, width, height, x, y, w, h):
    """Per-channel mean over a clamped rectangle, or None if empty."""
    x = min(max(x, 0), width - 1)
    y = min(max(y, 0), height - 1)
    w = min(max(w, 0), width - x)
    h = min(max(h, 0), height - y)
    if w <= 0 or h <= 0:
        return None
    sums = [0, 0, 0]
    count = 0
    for py in range(y, y + h):
        base = (py * width + x) * 3
        for px in range(w):
            pos = base + px * 3
            sums[0] += pixels[pos]
            sums[1] += pixels[pos + 1]
            sums[2] += pixels[pos + 2]
            count += 1
    return {
        "x": x, "y": y, "w": w, "h": h, "count": count,
        "mean_r": sums[0] / count if count else 0.0,
        "mean_g": sums[1] / count if count else 0.0,
        "mean_b": sums[2] / count if count else 0.0,
    }


def _matvec_compute(matrix, vector):
    """Row-major matrix x vector -> {"rows", "cols", "result"} or None.

    ``rows`` is ``len(matrix) // len(vector)``; leftover matrix
    entries are ignored. An empty buffer or vector is a no-op
    (None). The caller owns writing the result back.
    """
    if not matrix or not vector:
        return None
    cols = len(vector)
    rows = len(matrix) // cols
    if rows == 0:
        return None
    result = []
    for row in range(rows):
        base = row * cols
        total = 0.0
        for col in range(cols):
            total += matrix[base + col] * vector[col]
        result.append(total)
    return {"rows": rows, "cols": cols, "result": result}


class CpuPixelBackend:
    """CPU pixel-buffer backend: shades with ``mandel_pixel``.

    Pure Python + Pillow. No GL, no window, no display. Surfaces are
    kept per handle: ``data`` is the uploaded float buffer, ``params``
    the 6-slot shader vector, ``pixels`` the last shaded RGB buffer
    (``bytearray`` of ``width * height * 3``) or None, ``cap`` the
    capability word, ``program_source`` the last ``gpu_program``
    source (stored only; shading stays Mandelbrot).
    """

    def __init__(self):
        self.surfaces = {}

    def create_surface(self, handle, width, height):
        key = _handle_key(handle)
        if key is None:
            return
        self.surfaces[key] = {
            "width": int(width),
            "height": int(height),
            "data": [],
            "params": list(DEFAULT_PARAMS),
            "pixels": None,
            "cap": 0,
            "program_source": None,
        }

    def upload(self, handle, values):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        surface["data"] = [float(v) for v in values]
        surface["params"] = _sanitize_params(values)

    def dispatch(self, handle, frame):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        width = surface["width"]
        height = surface["height"]
        params = surface["params"]
        pixels = bytearray(width * height * 3)
        pos = 0
        for py in range(height):
            for px in range(width):
                r, g, b = mandel_pixel(px, py, width, height, params, frame)
                pixels[pos] = r
                pixels[pos + 1] = g
                pixels[pos + 2] = b
                pos += 3
        surface["pixels"] = pixels

    def read_floats(self, handle, offset, count):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return []
        return surface["data"][offset:offset + count]

    def read_plane(self, handle, plane):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None or surface["pixels"] is None:
            return None
        return _plane_stats(surface["pixels"], plane)

    def read_planes(self, handle, planes):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None or surface["pixels"] is None:
            return [None for _ in planes]
        return [_plane_stats(surface["pixels"], plane)
                for plane in planes]

    def program(self, handle, source):
        """Store the source; the CPU backend always shades Mandelbrot."""
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        surface["program_source"] = source

    def set_cap(self, handle, cap):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        surface["cap"] = int(cap)

    def matvec(self, handle, other):
        surface = self.surfaces.get(_handle_key(handle))
        other_surface = self.surfaces.get(_handle_key(other))
        if surface is None or other_surface is None:
            return None
        result = _matvec_compute(list(surface["data"]),
                                 list(other_surface["data"]))
        if result is None:
            return None
        surface["data"] = list(result["result"])
        return result

    def read_window(self, handle, x, y, w, h):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None or surface["pixels"] is None:
            return None
        return _window_stats(surface["pixels"], surface["width"],
                             surface["height"], x, y, w, h)

    def zero(self, handle):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        surface["data"] = [0.0] * len(surface["data"])
        surface["pixels"] = None

    def close(self, handle):
        self.surfaces.pop(_handle_key(handle), None)

    def image(self, handle):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None or surface["pixels"] is None or Image is None:
            return None
        return Image.frombytes("RGB", (surface["width"],
                                       surface["height"]),
                               bytes(surface["pixels"]))


#: Fixed vertex shader shared by every GL program (fullscreen triangle).
DEFAULT_VERTEX_SHADER = """
attribute vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
"""

#: Default fragment shader: a GLSL ES 1.00 port of ``mandel_pixel``.
#: Uniforms: ``u_params`` (vec4: centre real, centre imag, span,
#: max iterations), ``u_params2`` (vec2: palette shift, brightness),
#: ``u_frame`` (float: zoom frame), ``u_size`` (vec2: width, height).
#: Y handling: ``glReadPixels`` returns rows bottom-first, so the
#: framebuffer is painted the right way up -- ``fy`` is the
#: top-down row coordinate (0 at the top row, 1 at the bottom) --
#: and the readback rows are reversed when the RGB buffer is
#: built, which lands the stored image top-down, exactly like
#: the CPU backend. Using ``fy`` directly as the row coordinate
#: would paint the framebuffer the right way up and hand it back
#: upside down; the ``- 0.5 / u_size.y`` term only removes the
#: half-pixel centre offset so ``py_norm`` is exactly
#: ``py / height`` for the fragment's top-down row.
DEFAULT_FRAGMENT_SHADER = """
precision highp float;
uniform vec4 u_params;
uniform vec2 u_params2;
uniform float u_frame;
uniform vec2 u_size;
void main() {
  float span_eff = u_params.z * 4096.0 / (4096.0 + u_frame * 2048.0);
  float px_norm = (gl_FragCoord.x - 0.5) / u_size.x;
  float fy = 1.0 - gl_FragCoord.y / u_size.y;
  float py_norm = fy - 0.5 / u_size.y;
  float cr = u_params.x + (px_norm - 0.5) * span_eff;
  float ci = u_params.y + (py_norm - 0.5) * span_eff
             * (u_size.y / u_size.x);
  float zr = 0.0;
  float zi = 0.0;
  int limit = int(u_params.w);
  int k = 0;
  for (int i = 0; i < 512; i++) {
    if (k >= limit) { break; }
    if (zr * zr + zi * zi > 4.0) { break; }
    float nzr = zr * zr - zi * zi + cr;
    zi = 2.0 * zr * zi + ci;
    zr = nzr;
    k++;
  }
  vec3 col;
  if (k >= limit) {
    col = vec3(0.0, 0.0, 4.0);
  } else {
    float kk = float(k) + u_params2.x;
    col = vec3(mod(kk * 5.0, 256.0),
               mod(kk * 6.0, 256.0),
               mod(kk * 3.0, 256.0)) * u_params2.y;
    col = clamp(floor(col), 0.0, 255.0);
  }
  gl_FragColor = vec4(col / 255.0, 1.0);
}
"""

#: Uniform names every fragment shader may declare (looked up per
#: program; a missing one is skipped silently).
_PROGRAM_UNIFORMS = ("u_params", "u_params2", "u_frame", "u_size")

# EGL / GLES2 constants used below (standard Khronos values).
_EGL_NONE = 0x3038
_EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
_EGL_RENDERABLE_TYPE = 0x3040
_EGL_OPENGL_ES2_BIT = 0x0004
_EGL_SURFACE_TYPE = 0x3033
_EGL_PBUFFER_BIT = 0x0001
_EGL_RED_SIZE = 0x3024
_EGL_GREEN_SIZE = 0x3023
_EGL_BLUE_SIZE = 0x3022
_EGL_ALPHA_SIZE = 0x3021
_EGL_CONTEXT_CLIENT_VERSION = 0x3098
_EGL_WIDTH = 0x3057
_EGL_HEIGHT = 0x3056
_EGL_SUCCESS = 0x3000
_EGL_VENDOR = 0x3053
_EGL_VERSION = 0x3054
_GL_VERTEX_SHADER = 0x8B31
_GL_FRAGMENT_SHADER = 0x8B30
_GL_COMPILE_STATUS = 0x8B81
_GL_LINK_STATUS = 0x8B82
_GL_ARRAY_BUFFER = 0x8892
_GL_STATIC_DRAW = 0x88E4
_GL_FLOAT = 0x1406
_GL_TRIANGLES = 0x0004
_GL_COLOR_BUFFER_BIT = 0x4000
_GL_RGBA = 0x1908
_GL_UNSIGNED_BYTE = 0x1401
_GL_VERSION = 0x1F02
_GL_EXTENSIONS = 0x1F03
_GL_TEXTURE_2D = 0x0DE1
_GL_TEXTURE_MIN_FILTER = 0x2801
_GL_TEXTURE_MAG_FILTER = 0x2800
_GL_NEAREST = 0x2600
_GL_CLAMP_TO_EDGE = 0x812F
_GL_TEXTURE_WRAP_S = 0x2802
_GL_TEXTURE_WRAP_T = 0x2803
_GL_FRAMEBUFFER = 0x8D40
_GL_COLOR_ATTACHMENT0 = 0x8CE0
_GL_FRAMEBUFFER_COMPLETE = 0x8CD5

_EGL_FUNCS = [
    ("eglGetError", ctypes.c_int, []),
    ("eglGetPlatformDisplay", ctypes.c_void_p,
     [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]),
    ("eglGetDisplay", ctypes.c_void_p, [ctypes.c_void_p]),
    ("eglInitialize", ctypes.c_int,
     [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
      ctypes.POINTER(ctypes.c_int)]),
    ("eglChooseConfig", ctypes.c_int,
     [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
      ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
      ctypes.POINTER(ctypes.c_int)]),
    ("eglCreateContext", ctypes.c_void_p,
     [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
      ctypes.POINTER(ctypes.c_int)]),
    ("eglCreatePbufferSurface", ctypes.c_void_p,
     [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]),
    ("eglDestroySurface", ctypes.c_int,
     [ctypes.c_void_p, ctypes.c_void_p]),
    ("eglDestroyContext", ctypes.c_int,
     [ctypes.c_void_p, ctypes.c_void_p]),
    ("eglMakeCurrent", ctypes.c_int,
     [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
      ctypes.c_void_p]),
    ("eglQueryString", ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_int]),
    ("eglGetCurrentContext", ctypes.c_void_p, []),
    ("eglTerminate", ctypes.c_int, [ctypes.c_void_p]),
]

_GL_FUNCS = [
    ("glGetError", ctypes.c_uint, []),
    ("glGetString", ctypes.c_char_p, [ctypes.c_uint]),
    ("glCreateShader", ctypes.c_uint, [ctypes.c_uint]),
    ("glShaderSource", None,
     [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p),
      ctypes.POINTER(ctypes.c_int)]),
    ("glCompileShader", None, [ctypes.c_uint]),
    ("glGetShaderiv", None,
     [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]),
    ("glGetShaderInfoLog", None,
     [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
      ctypes.c_char_p]),
    ("glDeleteShader", None, [ctypes.c_uint]),
    ("glCreateProgram", ctypes.c_uint, []),
    ("glAttachShader", None, [ctypes.c_uint, ctypes.c_uint]),
    ("glLinkProgram", None, [ctypes.c_uint]),
    ("glGetProgramiv", None,
     [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]),
    ("glGetProgramInfoLog", None,
     [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
      ctypes.c_char_p]),
    ("glDeleteProgram", None, [ctypes.c_uint]),
    ("glUseProgram", None, [ctypes.c_uint]),
    ("glGetAttribLocation", ctypes.c_int, [ctypes.c_uint, ctypes.c_char_p]),
    ("glGetUniformLocation", ctypes.c_int,
     [ctypes.c_uint, ctypes.c_char_p]),
    ("glUniform4fv", None,
     [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_float)]),
    ("glUniform2fv", None,
     [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_float)]),
    ("glUniform1f", None, [ctypes.c_int, ctypes.c_float]),
    ("glUniform2f", None,
     [ctypes.c_int, ctypes.c_float, ctypes.c_float]),
    ("glGenBuffers", None,
     [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
    ("glBindBuffer", None, [ctypes.c_uint, ctypes.c_uint]),
    ("glBufferData", None,
     [ctypes.c_uint, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint]),
    ("glDeleteBuffers", None,
     [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
    ("glEnableVertexAttribArray", None, [ctypes.c_uint]),
    ("glVertexAttribPointer", None,
     [ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_ubyte,
      ctypes.c_int, ctypes.c_void_p]),
    ("glViewport", None,
     [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]),
    ("glClearColor", None,
     [ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float]),
    ("glClear", None, [ctypes.c_uint]),
    ("glDrawArrays", None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int]),
    ("glReadPixels", None,
     [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]),
    ("glFinish", None, []),
    ("glGenTextures", None,
     [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
    ("glBindTexture", None, [ctypes.c_uint, ctypes.c_uint]),
    ("glTexImage2D", None,
     [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
      ctypes.c_void_p]),
    ("glTexParameteri", None, [ctypes.c_uint, ctypes.c_uint, ctypes.c_int]),
    ("glDeleteTextures", None,
     [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
    ("glGenFramebuffers", None,
     [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
    ("glBindFramebuffer", None, [ctypes.c_uint, ctypes.c_uint]),
    ("glFramebufferTexture2D", None,
     [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
      ctypes.c_int]),
    ("glCheckFramebufferStatus", ctypes.c_uint, [ctypes.c_uint]),
    ("glDeleteFramebuffers", None,
     [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
]


def _bind_functions(lib, specs):
    """Set restype/argtypes for each (name, restype, argtypes) spec.

    Raises RuntimeError naming the first missing entry point, so a
    half-present library reads as "backend unavailable".
    """
    for name, restype, argtypes in specs:
        func = getattr(lib, name, None)
        if func is None:
            raise RuntimeError("OpenGL ES unavailable: %s is missing"
                               % name)
        func.restype = restype
        func.argtypes = argtypes


class GLESBackend:
    """OpenGL ES 2.0 pixel-buffer backend via stdlib ctypes.

    No new dependency: ``libEGL.so.1`` / ``libGLESv2.so.2`` only.
    One EGL context per instance, created in ``__init__`` on the
    calling (draining) thread and used only there; ``release()``
    destroys it, also on that thread -- never share an instance
    across threads. A cached backend is bound to the thread that
    made it current: reusing one on another thread fails, so
    :func:`acquire_backend` keeps one backend per thread (see
    below) and re-makes its context current on every use.
    Custom fragment programs are cached by source string with a
    per-surface refcount (see ``program``), so a repeated
    ``gpu_program`` with the same source never recompiles. A 1x1
    pbuffer is the default surface (a surfaceless context alone
    has no default framebuffer), and each surface gets its own
    FBO + RGBA8 texture. Rendering is a fullscreen triangle with
    the fixed vertex shader; the fragment shader is the default
    Mandelbrot program unless the surface has a custom program
    (see ``program``). ``fatal_error`` is a string when a mid-drain
    GL failure means the whole queue must be re-run on the CPU
    backend, else None. ``__init__`` raises a clear RuntimeError
    when EGL/GLES cannot be loaded or set up, so the selection
    helper can fall back to the CPU backend.
    """

    def __init__(self):
        self.surfaces = {}
        self.fatal_error = None
        self.has_float_texture = False
        self.gl_version = ""
        self._released = False
        self._egl = None
        self._gles = None
        self._display = None
        self._config = None
        self._context = None
        self._pbuffer = None
        self._default_program = 0
        self._default_uniforms = {}
        self._default_attrib = -1
        self._vbo = 0
        #: Custom programs by source string -> [program, uniforms,
        #: attrib, refcount]. A repeated ``gpu_program`` with the
        #: same source reuses the compiled program; the entry is
        #: deleted (and the GL program destroyed) when the last
        #: surface using it lets go. The default program is not
        #: cached here -- it is compiled once in ``_setup_gl``.
        self._programs = {}
        try:
            self._load_libraries()
            self._setup_egl()
            self._setup_gl()
        except Exception as exc:
            self.fatal_error = "%s: %s" % (type(exc).__name__, exc)
            self._teardown_quietly()
            raise

    # -- setup ------------------------------------------------------
    def _load_libraries(self):
        try:
            egl = ctypes.CDLL(EGL_LIB)
        except OSError as exc:
            raise RuntimeError("OpenGL ES unavailable: cannot load %s "
                               "(%s)" % (EGL_LIB, exc))
        try:
            gles = ctypes.CDLL(GLES_LIB)
        except OSError as exc:
            raise RuntimeError("OpenGL ES unavailable: cannot load %s "
                               "(%s)" % (GLES_LIB, exc))
        _bind_functions(egl, _EGL_FUNCS)
        _bind_functions(gles, _GL_FUNCS)
        self._egl = egl
        self._gles = gles

    def _check_egl(self, what):
        err = self._egl.eglGetError()
        if err != _EGL_SUCCESS:
            raise RuntimeError("EGL error 0x%x at %s" % (err, what))

    def _check_gl(self, what):
        err = self._gles.glGetError()
        if err != 0:
            raise RuntimeError("GL error 0x%x at %s" % (err, what))

    def _setup_egl(self):
        egl = self._egl
        display = egl.eglGetPlatformDisplay(
            _EGL_PLATFORM_SURFACELESS_MESA, None, None)
        if not display:
            egl.eglGetError()  # clear any error before the fallback
            display = egl.eglGetDisplay(None)
        if not display:
            raise RuntimeError("OpenGL ES unavailable: no EGL display")
        major = ctypes.c_int(0)
        minor = ctypes.c_int(0)
        if not egl.eglInitialize(display, ctypes.byref(major),
                                 ctypes.byref(minor)):
            egl.eglGetError()
            display = egl.eglGetDisplay(None)
            if not display or not egl.eglInitialize(
                    display, ctypes.byref(major), ctypes.byref(minor)):
                raise RuntimeError("OpenGL ES unavailable: "
                                   "eglInitialize failed")
        self._display = display
        self._check_egl("eglInitialize")
        attribs = (ctypes.c_int * 13)(
            _EGL_RENDERABLE_TYPE, _EGL_OPENGL_ES2_BIT,
            _EGL_SURFACE_TYPE, _EGL_PBUFFER_BIT,
            _EGL_RED_SIZE, 8, _EGL_GREEN_SIZE, 8, _EGL_BLUE_SIZE, 8,
            _EGL_ALPHA_SIZE, 8, _EGL_NONE)
        configs = (ctypes.c_void_p * 1)()
        count = ctypes.c_int(0)
        if not egl.eglChooseConfig(display, attribs, configs, 1,
                                   ctypes.byref(count)) or count.value < 1:
            raise RuntimeError("OpenGL ES unavailable: eglChooseConfig "
                               "found no config")
        self._check_egl("eglChooseConfig")
        self._config = configs[0]
        ctx_attribs = (ctypes.c_int * 3)(
            _EGL_CONTEXT_CLIENT_VERSION, 2, _EGL_NONE)
        context = egl.eglCreateContext(display, self._config, None,
                                       ctx_attribs)
        if not context:
            raise RuntimeError("OpenGL ES unavailable: "
                               "eglCreateContext failed")
        self._context = context
        self._check_egl("eglCreateContext")
        # A surfaceless context has no default framebuffer, so draw
        # calls fail without a surface: give it a tiny pbuffer.
        pb_attribs = (ctypes.c_int * 5)(
            _EGL_WIDTH, 1, _EGL_HEIGHT, 1, _EGL_NONE)
        surface = egl.eglCreatePbufferSurface(display, self._config,
                                              pb_attribs)
        if not surface:
            raise RuntimeError("OpenGL ES unavailable: "
                               "eglCreatePbufferSurface failed")
        self._pbuffer = surface
        self._check_egl("eglCreatePbufferSurface")
        if not egl.eglMakeCurrent(display, surface, surface, context):
            raise RuntimeError("OpenGL ES unavailable: "
                               "eglMakeCurrent failed")
        self._check_egl("eglMakeCurrent")

    def _setup_gl(self):
        gles = self._gles
        version = gles.glGetString(_GL_VERSION)
        self.gl_version = version.decode(errors="replace") if version else ""
        self._check_gl("glGetString")
        extensions = gles.glGetString(_GL_EXTENSIONS)
        ext_text = extensions.decode(errors="replace") if extensions else ""
        self.has_float_texture = "GL_OES_texture_float" in ext_text.split()
        program, uniforms, attrib = self._compile_program(
            DEFAULT_VERTEX_SHADER, DEFAULT_FRAGMENT_SHADER)
        self._default_program = program
        self._default_uniforms = uniforms
        self._default_attrib = attrib
        verts = (ctypes.c_float * 6)(-1.0, -1.0, 3.0, -1.0, -1.0, 3.0)
        buf = ctypes.c_uint(0)
        gles.glGenBuffers(1, ctypes.byref(buf))
        gles.glBindBuffer(_GL_ARRAY_BUFFER, buf.value)
        gles.glBufferData(_GL_ARRAY_BUFFER, ctypes.sizeof(verts), verts,
                          _GL_STATIC_DRAW)
        self._check_gl("fullscreen triangle VBO")
        self._vbo = buf.value

    # -- shaders ----------------------------------------------------
    def _compile_shader(self, kind, source):
        gles = self._gles
        shader = gles.glCreateShader(kind)
        if not shader:
            raise RuntimeError("glCreateShader returned 0")
        src = ctypes.c_char_p(source.encode("utf-8"))
        gles.glShaderSource(shader, 1, ctypes.byref(src), None)
        gles.glCompileShader(shader)
        status = ctypes.c_int(0)
        gles.glGetShaderiv(shader, _GL_COMPILE_STATUS,
                           ctypes.byref(status))
        if not status.value:
            log = ctypes.create_string_buffer(1024)
            gles.glGetShaderInfoLog(shader, 1024, None, log)
            gles.glDeleteShader(shader)
            kind_name = ("vertex" if kind == _GL_VERTEX_SHADER
                         else "fragment")
            raise RuntimeError("%s shader compile failed: %s"
                               % (kind_name,
                                  log.value.decode(errors="replace")))
        return shader

    def _compile_program(self, vertex_source, fragment_source):
        """Compile + link; returns (program, uniforms, attrib loc)."""
        gles = self._gles
        vertex = fragment = program = 0
        try:
            vertex = self._compile_shader(_GL_VERTEX_SHADER,
                                          vertex_source)
            fragment = self._compile_shader(_GL_FRAGMENT_SHADER,
                                            fragment_source)
            program = gles.glCreateProgram()
            if not program:
                raise RuntimeError("glCreateProgram returned 0")
            gles.glAttachShader(program, vertex)
            gles.glAttachShader(program, fragment)
            gles.glLinkProgram(program)
            status = ctypes.c_int(0)
            gles.glGetProgramiv(program, _GL_LINK_STATUS,
                                ctypes.byref(status))
            if not status.value:
                log = ctypes.create_string_buffer(1024)
                gles.glGetProgramInfoLog(program, 1024, None, log)
                raise RuntimeError("program link failed: %s"
                                   % log.value.decode(errors="replace"))
        except Exception:
            if program:
                gles.glDeleteProgram(program)
            raise
        finally:
            if vertex:
                gles.glDeleteShader(vertex)
            if fragment:
                gles.glDeleteShader(fragment)
        uniforms = {}
        for name in _PROGRAM_UNIFORMS:
            loc = gles.glGetUniformLocation(program, name.encode("utf-8"))
            if loc >= 0:
                uniforms[name] = loc
        attrib = gles.glGetAttribLocation(program, b"a_pos")
        return program, uniforms, attrib

    def _set_uniforms(self, uniforms, params, frame, width, height):
        gles = self._gles
        loc = uniforms.get("u_params")
        if loc is not None:
            arr = (ctypes.c_float * 4)(*params[:4])
            gles.glUniform4fv(loc, 1, arr)
        loc = uniforms.get("u_params2")
        if loc is not None:
            arr = (ctypes.c_float * 2)(*params[4:6])
            gles.glUniform2fv(loc, 1, arr)
        loc = uniforms.get("u_frame")
        if loc is not None:
            gles.glUniform1f(loc, float(frame))
        loc = uniforms.get("u_size")
        if loc is not None:
            gles.glUniform2f(loc, float(width), float(height))

    # -- teardown ---------------------------------------------------
    def _release_surface_program(self, surface):
        """Drop one surface's custom program (refcount -= 1).

        The GL program is destroyed and its cache entry dropped
        only when the last surface using it lets go; a surface
        with no custom program (the default) is a no-op. The
        surface record is cleared either way.
        """
        source = surface.get("program_source")
        prog = surface.get("program") or 0
        surface["program"] = 0
        surface["program_source"] = None
        surface["uniforms"] = {}
        surface["attrib"] = -1
        if not prog:
            return
        entry = self._programs.get(source) if source is not None else None
        if entry is None or entry[0] != prog:
            # Not a cached program: delete it directly.
            try:
                self._gles.glDeleteProgram(prog)
            except Exception:
                pass
            return
        entry[3] -= 1
        if entry[3] <= 0:
            del self._programs[source]
            try:
                self._gles.glDeleteProgram(prog)
            except Exception:
                pass

    def _delete_surface_gl(self, surface):
        if self._gles is None:
            return
        gles = self._gles
        try:
            self._release_surface_program(surface)
            fbo = surface.get("fbo") or 0
            if fbo:
                buf = (ctypes.c_uint * 1)(fbo)
                gles.glDeleteFramebuffers(1, buf)
            texture = surface.get("texture") or 0
            if texture:
                buf = (ctypes.c_uint * 1)(texture)
                gles.glDeleteTextures(1, buf)
        except Exception:
            pass
        surface["program"] = 0
        surface["fbo"] = 0
        surface["texture"] = 0

    def _teardown_quietly(self):
        try:
            self.release()
        except Exception:
            pass

    def release(self):
        """Destroy the EGL context and all GL objects, on this thread.

        Idempotent and best-effort: never raises. Python-side
        surface records (data / pixels) are kept, so a drained
        result can still be inspected after release.
        """
        if self._released:
            return
        self._released = True
        for surface in list(self.surfaces.values()):
            self._delete_surface_gl(surface)
        if self._gles is not None:
            try:
                for entry in list(self._programs.values()):
                    if entry[0]:
                        self._gles.glDeleteProgram(entry[0])
                self._programs.clear()
                if self._default_program:
                    self._gles.glDeleteProgram(self._default_program)
                if self._vbo:
                    buf = (ctypes.c_uint * 1)(self._vbo)
                    self._gles.glDeleteBuffers(1, buf)
            except Exception:
                pass
        self._default_program = 0
        self._vbo = 0
        if self._egl is not None and self._display:
            egl = self._egl
            try:
                egl.eglMakeCurrent(self._display, None, None, None)
            except Exception:
                pass
            try:
                if self._pbuffer:
                    egl.eglDestroySurface(self._display, self._pbuffer)
            except Exception:
                pass
            try:
                if self._context:
                    egl.eglDestroyContext(self._display, self._context)
            except Exception:
                pass
            # No eglTerminate here, deliberately: the surfaceless
            # display is process-global, and terminating it from
            # one backend while another thread's live context
            # still uses it would break that context. The display
            # is released with the process instead.
        self._pbuffer = None
        self._context = None
        self._display = None

    # -- backend protocol -------------------------------------------
    def create_surface(self, handle, width, height):
        key = _handle_key(handle)
        if key is None:
            return
        old = self.surfaces.pop(key, None)
        if old is not None:
            self._delete_surface_gl(old)
        surface = {
            "width": int(width),
            "height": int(height),
            "data": [],
            "params": list(DEFAULT_PARAMS),
            "pixels": None,
            "cap": 0,
            "program_source": None,
            "program": 0,
            "uniforms": {},
            "attrib": -1,
            "texture": 0,
            "fbo": 0,
        }
        self.surfaces[key] = surface
        gles = self._gles
        try:
            texture = ctypes.c_uint(0)
            gles.glGenTextures(1, ctypes.byref(texture))
            gles.glBindTexture(_GL_TEXTURE_2D, texture.value)
            gles.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MIN_FILTER,
                                 _GL_NEAREST)
            gles.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MAG_FILTER,
                                 _GL_NEAREST)
            gles.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_S,
                                 _GL_CLAMP_TO_EDGE)
            gles.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_T,
                                 _GL_CLAMP_TO_EDGE)
            gles.glTexImage2D(_GL_TEXTURE_2D, 0, _GL_RGBA,
                              surface["width"], surface["height"], 0,
                              _GL_RGBA, _GL_UNSIGNED_BYTE, None)
            self._check_gl("glTexImage2D")
            surface["texture"] = texture.value
            fbo = ctypes.c_uint(0)
            gles.glGenFramebuffers(1, ctypes.byref(fbo))
            gles.glBindFramebuffer(_GL_FRAMEBUFFER, fbo.value)
            gles.glFramebufferTexture2D(_GL_FRAMEBUFFER,
                                        _GL_COLOR_ATTACHMENT0,
                                        _GL_TEXTURE_2D, texture.value, 0)
            status = gles.glCheckFramebufferStatus(_GL_FRAMEBUFFER)
            gles.glBindFramebuffer(_GL_FRAMEBUFFER, 0)
            if status != _GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError("incomplete framebuffer: 0x%x" % status)
            surface["fbo"] = fbo.value
        except Exception:
            self._delete_surface_gl(surface)
            self.surfaces.pop(key, None)
            raise

    def upload(self, handle, values):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        surface["data"] = [float(v) for v in values]
        surface["params"] = _sanitize_params(values)

    def program(self, handle, source):
        """Compile ``source`` for the surface, or reuse the cache.

        A cache hit reuses the compiled program (refcount += 1,
        no recompile); a miss compiles and stores it at refcount
        1. The surface's previous custom program is released
        (refcount -= 1, deleted at 0), so two surfaces sharing
        one source keep a single GL program between them.
        Compile failure still sets ``fatal_error`` exactly as
        before (and the failed source is never cached).
        """
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        entry = self._programs.get(source)
        if entry is not None:
            prog, uniforms, attrib = entry[0], entry[1], entry[2]
            entry[3] += 1
        else:
            try:
                prog, uniforms, attrib = self._compile_program(
                    DEFAULT_VERTEX_SHADER, source)
            except Exception as exc:
                self.fatal_error = "custom program failed: %s" % exc
                raise
            self._programs[source] = [prog, uniforms, attrib, 1]
        self._release_surface_program(surface)
        surface["program"] = prog
        surface["uniforms"] = uniforms
        surface["attrib"] = attrib
        surface["program_source"] = source

    def set_cap(self, handle, cap):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        surface["cap"] = int(cap)

    def dispatch(self, handle, frame):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        if surface["cap"] & 1 and not self.has_float_texture:
            surface["pixels"] = None
            raise RuntimeError(
                "dispatch refused: surface cap asks for float32 "
                "textures but OES_texture_float is unavailable")
        if not surface.get("fbo"):
            raise RuntimeError("dispatch refused: surface has no "
                               "framebuffer")
        width = surface["width"]
        height = surface["height"]
        if surface.get("program"):
            program = surface["program"]
            uniforms = surface["uniforms"]
            attrib = surface.get("attrib", -1)
        else:
            program = self._default_program
            uniforms = self._default_uniforms
            attrib = self._default_attrib
        gles = self._gles
        try:
            gles.glBindFramebuffer(_GL_FRAMEBUFFER, surface["fbo"])
            gles.glViewport(0, 0, width, height)
            gles.glUseProgram(program)
            self._set_uniforms(uniforms, surface["params"], frame,
                               width, height)
            gles.glBindBuffer(_GL_ARRAY_BUFFER, self._vbo)
            if attrib >= 0:
                gles.glEnableVertexAttribArray(attrib)
                gles.glVertexAttribPointer(attrib, 2, _GL_FLOAT, 0, 0,
                                           None)
            gles.glClearColor(0.0, 0.0, 0.0, 1.0)
            gles.glClear(_GL_COLOR_BUFFER_BIT)
            gles.glDrawArrays(_GL_TRIANGLES, 0, 3)
            self._check_gl("draw")
            raw = (ctypes.c_ubyte * (width * height * 4))()
            gles.glReadPixels(0, 0, width, height, _GL_RGBA,
                              _GL_UNSIGNED_BYTE, raw)
            self._check_gl("glReadPixels")
            gles.glBindFramebuffer(_GL_FRAMEBUFFER, 0)
        except Exception as exc:
            # A GL error mid-draw means the context is suspect: mark
            # the run fatal so the drainer re-runs it on the CPU.
            # (The cap refusal above raises before this block, so it
            # stays a plain per-surface note, never a re-run.)
            self.fatal_error = "GL dispatch failed: %s" % exc
            raise
        blob = bytes(raw)
        # Strip alpha per row, then reverse the row order: the
        # readback arrives bottom-first, the stored buffer is
        # top-down like the CPU backend.
        rows = []
        for row in range(height):
            line = blob[row * width * 4:(row + 1) * width * 4]
            rgb = bytearray(width * 3)
            rgb[0::3] = line[0::4]
            rgb[1::3] = line[1::4]
            rgb[2::3] = line[2::4]
            rows.append(bytes(rgb))
        surface["pixels"] = bytearray(b"".join(reversed(rows)))

    def read_floats(self, handle, offset, count):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return []
        return surface["data"][offset:offset + count]

    def read_plane(self, handle, plane):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None or surface["pixels"] is None:
            return None
        return _plane_stats(surface["pixels"], plane)

    def read_planes(self, handle, planes):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None or surface["pixels"] is None:
            return [None for _ in planes]
        return [_plane_stats(surface["pixels"], plane)
                for plane in planes]

    def read_window(self, handle, x, y, w, h):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None or surface["pixels"] is None:
            return None
        return _window_stats(surface["pixels"], surface["width"],
                             surface["height"], x, y, w, h)

    def matvec(self, handle, other):
        surface = self.surfaces.get(_handle_key(handle))
        other_surface = self.surfaces.get(_handle_key(other))
        if surface is None or other_surface is None:
            return None
        result = _matvec_compute(list(surface["data"]),
                                 list(other_surface["data"]))
        if result is None:
            return None
        surface["data"] = list(result["result"])
        return result

    def zero(self, handle):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None:
            return
        surface["data"] = [0.0] * len(surface["data"])
        surface["pixels"] = None

    def close(self, handle):
        surface = self.surfaces.pop(_handle_key(handle), None)
        if surface is not None:
            self._delete_surface_gl(surface)

    def image(self, handle):
        surface = self.surfaces.get(_handle_key(handle))
        if surface is None or surface["pixels"] is None or Image is None:
            return None
        return Image.frombytes("RGB", (surface["width"],
                                       surface["height"]),
                               bytes(surface["pixels"]))


def select_backend():
    """Try the GL backend; fall back to CPU on any failure.

    Returns ``(backend, reason)``; ``reason`` is None when the GL
    backend was created, else a short explanation string.
    """
    try:
        return GLESBackend(), None
    except Exception as exc:  # noqa: BLE001 - any failure means CPU
        return CpuPixelBackend(), "%s: %s" % (type(exc).__name__, exc)


#: One cached GL backend per thread (a context is bound to the
#: thread that made it current -- reusing a backend on a new
#: thread fails -- so the cache is thread-local, never shared).
#: ``_cached_backends`` is the registry of every live cached
#: backend, so ``release_all_cached_backends`` can tear them all
#: down; it holds weak refs, so a dropped backend vanishes.
_thread_backends = threading.local()
_cached_backends = weakref.WeakSet()
_cached_backends_lock = threading.Lock()


def _ensure_current(backend):
    """Make the backend's context current on this thread.

    True when the context is current and healthy here (no EGL
    error, ``eglGetCurrentContext`` is this backend's context,
    no fatal error recorded); False otherwise. Measured free
    (~0.0 ms), so it runs on every cached-backend use.
    """
    try:
        if backend._released or backend.fatal_error is not None:
            return False
        egl = backend._egl
        if egl is None or backend._display is None:
            return False
        if backend._context is None or backend._pbuffer is None:
            return False
        if not egl.eglMakeCurrent(backend._display, backend._pbuffer,
                                  backend._pbuffer, backend._context):
            return False
        if egl.eglGetError() != _EGL_SUCCESS:
            return False
        if egl.eglGetCurrentContext() != backend._context:
            return False
        return True
    except Exception:
        return False


def _drop_thread_backend():
    """Release and clear this thread's cached backend, if any."""
    backend = getattr(_thread_backends, "backend", None)
    _thread_backends.backend = None
    if backend is not None:
        with _cached_backends_lock:
            _cached_backends.discard(backend)
        _release_backend(backend)


def acquire_backend():
    """A healthy GLESBackend for the calling thread.

    Creates one on demand (and registers it) or returns the
    thread's cached one with its context ensured current here.
    Raises on failure -- and a failed acquire never caches
    anything, so the caller can fall back to the CPU backend.
    A cached backend whose context cannot be made current on
    this thread (or that recorded a fatal error) is dropped and
    rebuilt once; if the rebuild is unusable, the raise stands.
    """
    backend = getattr(_thread_backends, "backend", None)
    if backend is not None:
        if _ensure_current(backend):
            return backend
        _drop_thread_backend()
        backend = None
    backend = GLESBackend()
    with _cached_backends_lock:
        _cached_backends.add(backend)
    _thread_backends.backend = backend
    if not _ensure_current(backend):
        _drop_thread_backend()
        raise RuntimeError("OpenGL ES unavailable: could not make "
                           "the cached context current on this thread")
    return backend


def release_thread_backend():
    """Release this thread's cached backend and clear its slot."""
    _drop_thread_backend()


def release_all_cached_backends():
    """Best-effort release of every registered cached backend.

    Never raises. Backends belonging to other threads are
    released too (teardown); each ``release()`` is idempotent,
    so a backend released here and later reused from its own
    thread is simply rebuilt on next acquire.
    """
    with _cached_backends_lock:
        backends = list(_cached_backends)
        _cached_backends.clear()
    for backend in backends:
        _release_backend(backend)
    current = getattr(_thread_backends, "backend", None)
    if current is not None and any(b is current for b in backends):
        _thread_backends.backend = None


class _Drainer:
    """Owns one drain run: validation, backend calls, notes."""

    def __init__(self, backend, width, height):
        self.backend = backend
        self.width = width
        self.height = height
        self.open_handles = set()
        self.notes = []
        self.handled = []
        self.unhandled = []
        self.image = None

    def _note(self, text):
        self.notes.append(text)

    def _mark(self, name):
        if name in IMPLEMENTED_COMMANDS:
            if name not in self.handled:
                self.handled.append(name)
        elif name not in self.unhandled:
            self.unhandled.append(name)

    def _call(self, name, method, *args):
        """Call a backend method; a backend error is a note, never a raise."""
        try:
            return getattr(self.backend, method)(*args)
        except Exception as exc:  # noqa: BLE001 - backend must never kill a drain
            self._note("backend error on %s: %s: %s"
                       % (name, type(exc).__name__, exc))
            return None

    def _need_handle(self, name, args):
        """Return (key, raw) for the first arg, or None after noting."""
        if not args:
            self._note("%s: missing handle argument; skipped" % name)
            return None
        key = _handle_key(args[0])
        if key is None:
            self._note("%s: bad handle %r; skipped" % (name, args[0]))
            return None
        return key, args[0]

    def _need_open(self, name, args):
        """Return (key, raw) for a handle that is currently open."""
        got = self._need_handle(name, args)
        if got is None:
            return None
        key, raw = got
        if key not in self.open_handles:
            self._note("%s: unknown or closed handle %r; skipped"
                       % (name, raw))
            return None
        return key, raw

    def run(self, queue):
        for index, entry in enumerate(queue):
            self._run_entry(index, entry)
        return {
            "image": self.image,
            "notes": self.notes,
            "handled": self.handled,
            "unhandled": self.unhandled,
        }

    def _run_entry(self, index, entry):
        if entry is None:
            self._note("entry %d is None; skipped" % index)
            return
        if not isinstance(entry, (list, tuple)) or not entry:
            self._note("entry %d is not a command record %r; skipped"
                       % (index, entry))
            return
        name = entry[0]
        if not isinstance(name, str):
            self._note("entry %d has a non-string command name %r; skipped"
                       % (index, name))
            return
        args = list(entry[1:])
        self._mark(name)
        if name in IMPLEMENTED_COMMANDS:
            self._run_implemented(name, args)
        elif name in CONTRACT:
            self._note("%s: %s -- not executed by this drainer; "
                       "command reported, not dropped"
                       % (name, CONTRACT[name]))
        else:
            self._note("%s: unknown gpu command; ignored (no host "
                       "meaning defined)" % name)

    def _run_implemented(self, name, args):
        if name == "gpu_open":
            self._cmd_open(args)
        elif name == "gpu_open_cap":
            self._cmd_open_cap(args)
        elif name == "gpu_upload_f32":
            self._cmd_upload(args)
        elif name == "gpu_dispatch":
            self._cmd_dispatch(args)
        elif name == "gpu_read_f32":
            self._cmd_read_f32(args)
        elif name == "gpu_read_plane":
            self._cmd_read_plane(args)
        elif name == "gpu_read_planes":
            self._cmd_read_planes(args)
        elif name == "gpu_read_window":
            self._cmd_read_window(args)
        elif name == "gpu_zero":
            self._cmd_zero(args)
        elif name == "gpu_matvec":
            self._cmd_matvec(args)
        elif name == "gpu_program":
            self._cmd_program(args)
        elif name == "gpu_close":
            self._cmd_close(args)

    def _cmd_open(self, args):
        got = self._need_handle("gpu_open", args)
        if got is None:
            return
        key, raw = got
        if len(args) > 1:
            self._note("gpu_open: ignoring %d extra argument(s)" % (len(args) - 1))
        if key in self.open_handles:
            self._note("gpu_open: handle %r already open; surface replaced"
                       % (raw,))
        self._call("gpu_open", "create_surface", raw, self.width, self.height)
        self.open_handles.add(key)
        self._note("gpu_open: surface %r (%dx%d)"
                   % (raw, self.width, self.height))

    def _cmd_open_cap(self, args):
        got = self._need_open("gpu_open_cap", args)
        if got is None:
            return
        key, raw = got
        if len(args) < 2 or not _is_number(args[1]):
            self._note("gpu_open_cap: handle %r needs a numeric cap; "
                       "skipped" % (raw,))
            return
        cap = int(args[1])
        self._call("gpu_open_cap", "set_cap", raw, cap)
        self._note("gpu_open_cap: handle %r cap %d stored" % (raw, cap))

    def _cmd_upload(self, args):
        got = self._need_open("gpu_upload_f32", args)
        if got is None:
            return
        key, raw = got
        if len(args) < 2 or not isinstance(args[1], (list, tuple)):
            self._note("gpu_upload_f32: handle %r needs a list of floats; "
                       "skipped" % (raw,))
            return
        values = []
        dropped = 0
        for value in args[1]:
            if _is_number(value):
                values.append(float(value))
            else:
                dropped += 1
        if dropped:
            self._note("gpu_upload_f32: dropped %d non-numeric value(s)"
                       % dropped)
        if len(values) > MAX_UPLOAD:
            self._note("gpu_upload_f32: capped upload at %d floats "
                       "(got %d)" % (MAX_UPLOAD, len(values)))
            values = values[:MAX_UPLOAD]
        if not values:
            self._note("gpu_upload_f32: handle %r got no usable floats; "
                       "skipped" % (raw,))
            return
        self._call("gpu_upload_f32", "upload", raw, values)
        self._note("gpu_upload_f32: handle %r stored %d float(s)"
                   % (raw, len(values)))

    def _cmd_dispatch(self, args):
        got = self._need_open("gpu_dispatch", args)
        if got is None:
            return
        key, raw = got
        frame = 0
        if len(args) >= 2:
            if not _is_number(args[1]):
                self._note("gpu_dispatch: bad frame %r; skipped" % (args[1],))
                return
            frame = int(args[1])
        if frame < 0:
            self._note("gpu_dispatch: negative frame %d clamped to 0" % frame)
            frame = 0
        self._call("gpu_dispatch", "dispatch", raw, frame)
        shot = self._call("gpu_dispatch", "image", raw)
        if shot is not None:
            self.image = shot
        self._note("gpu_dispatch: handle %r shaded frame %d (%dx%d)"
                   % (raw, frame, self.width, self.height))

    def _cmd_read_f32(self, args):
        got = self._need_open("gpu_read_f32", args)
        if got is None:
            return
        key, raw = got
        offset = 0
        count = 0
        if len(args) >= 2:
            if not _is_number(args[1]):
                self._note("gpu_read_f32: bad offset %r; skipped" % (args[1],))
                return
            offset = max(int(args[1]), 0)
        if len(args) >= 3:
            if not _is_number(args[2]):
                self._note("gpu_read_f32: bad count %r; skipped" % (args[2],))
                return
            count = max(int(args[2]), 0)
        values = self._call("gpu_read_f32", "read_floats", raw, offset, count)
        if values is None:
            values = []
        shown = ", ".join(repr(v) for v in list(values)[:4])
        if len(values) > 4:
            shown += ", ..."
        self._note("gpu_read_f32: handle %r offset %d count %d -> [%s]"
                   % (raw, offset, count, shown))

    def _cmd_read_plane(self, args):
        got = self._need_open("gpu_read_plane", args)
        if got is None:
            return
        key, raw = got
        if len(args) < 2 or not _is_number(args[1]):
            self._note("gpu_read_plane: handle %r needs a plane 0..2; "
                       "skipped" % (raw,))
            return
        plane = int(args[1])
        if plane not in (0, 1, 2):
            self._note("gpu_read_plane: bad plane %r; skipped" % (args[1],))
            return
        stats = self._call("gpu_read_plane", "read_plane", raw, plane)
        if stats is None:
            self._note("gpu_read_plane: handle %r has no shaded frame yet"
                       % (raw,))
            return
        self._note("gpu_read_plane: handle %r plane %d (%s) min %d max %d "
                   "mean %.1f over %d px"
                   % (raw, plane, "RGB"[plane], stats["min"],
                      stats["max"], stats["mean"], stats["count"]))

    def _cmd_read_planes(self, args):
        got = self._need_open("gpu_read_planes", args)
        if got is None:
            return
        key, raw = got
        if len(args) < 2 or not isinstance(args[1], (list, tuple)):
            self._note("gpu_read_planes: handle %r needs a list of "
                       "planes; skipped" % (raw,))
            return
        planes = []
        for value in args[1]:
            if not _is_number(value) or int(value) not in (0, 1, 2):
                self._note("gpu_read_planes: bad plane %r; skipped"
                           % (value,))
                return
            planes.append(int(value))
        if not planes:
            self._note("gpu_read_planes: handle %r got no planes; "
                       "skipped" % (raw,))
            return
        stats_list = self._call("gpu_read_planes", "read_planes", raw,
                                planes)
        if stats_list is None:
            return  # backend error already noted by _call
        if not isinstance(stats_list, (list, tuple)):
            self._note("gpu_read_planes: backend returned no plane "
                       "stats for handle %r" % (raw,))
            return
        for plane, stats in zip(planes, stats_list):
            if stats is None:
                self._note("gpu_read_planes: handle %r plane %d has no "
                           "shaded frame yet" % (raw, plane))
                continue
            self._note("gpu_read_planes: handle %r plane %d (%s) min %d "
                       "max %d mean %.1f over %d px"
                       % (raw, plane, "RGB"[plane], stats["min"],
                          stats["max"], stats["mean"], stats["count"]))

    def _cmd_read_window(self, args):
        got = self._need_open("gpu_read_window", args)
        if got is None:
            return
        key, raw = got
        if len(args) < 5 or any(not _is_number(a) for a in args[1:5]):
            self._note("gpu_read_window: handle %r needs x, y, w, h numbers; "
                       "skipped" % (raw,))
            return
        x, y, w, h = (int(a) for a in args[1:5])
        stats = self._call("gpu_read_window", "read_window", raw, x, y, w, h)
        if stats is None:
            self._note("gpu_read_window: handle %r has no shaded frame yet"
                       % (raw,))
            return
        self._note("gpu_read_window: handle %r (%d,%d %dx%d) mean "
                   "R %.1f G %.1f B %.1f over %d px"
                   % (raw, stats["x"], stats["y"], stats["w"], stats["h"],
                      stats["mean_r"], stats["mean_g"], stats["mean_b"],
                      stats["count"]))

    def _cmd_zero(self, args):
        got = self._need_open("gpu_zero", args)
        if got is None:
            return
        key, raw = got
        self._call("gpu_zero", "zero", raw)
        self._note("gpu_zero: handle %r data buffer zeroed" % (raw,))

    def _cmd_matvec(self, args):
        got = self._need_open("gpu_matvec", args)
        if got is None:
            return
        key, raw = got
        if len(args) < 2:
            self._note("gpu_matvec: handle %r needs a second handle; "
                       "skipped" % (raw,))
            return
        other_key = _handle_key(args[1])
        if other_key is None:
            self._note("gpu_matvec: bad handle %r; skipped" % (args[1],))
            return
        if other_key not in self.open_handles:
            self._note("gpu_matvec: unknown or closed handle %r; skipped"
                       % (args[1],))
            return
        result = self._call("gpu_matvec", "matvec", raw, args[1])
        if not isinstance(result, dict) or "result" not in result:
            self._note("gpu_matvec: handles %r and %r have no usable "
                       "data; skipped" % (raw, args[1]))
            return
        shown = ", ".join(repr(v) for v in list(result["result"])[:4])
        if len(result["result"]) > 4:
            shown += ", ..."
        self._note("gpu_matvec: handle %r (%dx%d) x handle %r -> [%s]"
                   % (raw, result.get("rows"), result.get("cols"),
                      args[1], shown))

    def _cmd_program(self, args):
        got = self._need_open("gpu_program", args)
        if got is None:
            return
        key, raw = got
        if len(args) < 2 or not isinstance(args[1], str):
            self._note("gpu_program: handle %r needs a string source; "
                       "skipped" % (raw,))
            return
        source = args[1]
        if len(source) > MAX_PROGRAM_SOURCE:
            self._note("gpu_program: source too long (%d chars, max %d); "
                       "skipped" % (len(source), MAX_PROGRAM_SOURCE))
            return
        self._call("gpu_program", "program", raw, source)
        if getattr(self.backend, "fatal_error", None):
            return  # compile/link failure already noted by _call
        self._note("gpu_program: handle %r program stored (%d chars)"
                   % (raw, len(source)))

    def _cmd_close(self, args):
        got = self._need_handle("gpu_close", args)
        if got is None:
            return
        key, raw = got
        if key not in self.open_handles:
            self._note("gpu_close: handle %r was not open; nothing to do"
                       % (raw,))
            return
        self._call("gpu_close", "close", raw)
        self.open_handles.discard(key)
        self._note("gpu_close: handle %r released" % (raw,))


def _sanitize_size(value, default):
    """Coerce a requested image dimension.

    Returns ``(size, note)`` where ``note`` is None when the value was
    usable as given, else a human-readable explanation.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default, "invalid %r; using %d" % (value, default)
    if value != value or value in (math.inf, -math.inf):
        return default, "invalid %r; using %d" % (value, default)
    size = int(value)
    if size < 1:
        return 1, "clamped %r up to 1" % (value,)
    if size > MAX_DIMENSION:
        return MAX_DIMENSION, "clamped %r down to %d" % (value, MAX_DIMENSION)
    return size, None


def drain_gpu_commands(queue, *, width=160, height=120, backend=None):
    """Drain a ``script_run`` GPU queue; see the module docstring.

    Returns ``{"image": PIL Image or None, "notes": [str],
    "handled": [names], "unhandled": [names], "backend": str}``.
    ``backend`` is ``"opengl"`` when the GL backend shaded the run,
    ``"cpu"`` when the CPU backend did (fallback or explicit), and
    ``"custom"`` for a caller-supplied backend of another type.
    ``handled`` / ``unhandled`` list command names in
    first-appearance order; ``handled`` is the implemented subset
    that appeared, ``unhandled`` is everything else that appeared
    (defined but not executed, plus unknown names). The first note
    names the backend; on a GL fallback it also names the reason.
    Never raises: garbage input is reported in ``notes`` /
    ``unhandled`` instead.
    """
    try:
        return _drain(queue, width=width, height=height, backend=backend)
    except Exception as exc:  # noqa: BLE001 - the drainer never raises
        return {
            "image": None,
            "notes": ["drainer error: %s: %s"
                      % (type(exc).__name__, exc)],
            "handled": [],
            "unhandled": [],
            "backend": "unknown",
        }


def _backend_name(backend):
    """Short backend name for the result dict ("opengl"/"cpu"/"custom")."""
    if isinstance(backend, GLESBackend):
        return "opengl"
    if isinstance(backend, CpuPixelBackend):
        return "cpu"
    return "custom"


def _release_backend(backend):
    """Call an optional ``backend.release()``; never raises."""
    release = getattr(backend, "release", None)
    if callable(release):
        try:
            release()
        except Exception:
            pass


def _merge_names(first, second):
    """Union of two first-appearance-ordered name lists."""
    merged = list(first)
    for item in second:
        if item not in merged:
            merged.append(item)
    return merged


def _drain(queue, *, width, height, backend):
    width, width_note = _sanitize_size(width, DEFAULT_WIDTH)
    height, height_note = _sanitize_size(height, DEFAULT_HEIGHT)
    notes = []
    if width_note is not None:
        notes.append("width " + width_note)
    if height_note is not None:
        notes.append("height " + height_note)
    reason = None
    cached = False
    if backend is None:
        # Cached backend for this thread: creating a fresh EGL
        # context per drain costs ~50 ms; the cached one drains
        # in ~5 ms. It is NOT released after the drain -- it
        # stays in the thread-local slot for the next frame.
        try:
            backend = acquire_backend()
            cached = True
        except Exception as exc:  # noqa: BLE001 - any failure means CPU
            backend = CpuPixelBackend()
            reason = "%s: %s" % (type(exc).__name__, exc)
    name = _backend_name(backend)
    if name == "opengl":
        backend_note = "GPU backend: OpenGL ES"
    elif reason is not None:
        backend_note = ("GPU backend: CPU (OpenGL ES unavailable: %s)"
                        % reason)
    elif name == "cpu":
        backend_note = "GPU backend: CPU"
    else:
        backend_note = ("GPU backend: custom (%s)"
                        % type(backend).__name__)
    notes.insert(0, backend_note)
    drainer = _Drainer(backend, width, height)
    drainer.notes.extend(notes)
    if queue is None:
        drainer.notes.append("queue is None; treated as empty")
        result = drainer.run([])
    elif not isinstance(queue, (list, tuple)):
        drainer.notes.append("queue is not a list (%r); ignored"
                             % (type(queue).__name__,))
        result = drainer.run([])
    else:
        result = drainer.run(list(queue))
    fatal = getattr(backend, "fatal_error", None)
    if cached:
        # Keep the cached backend for the next drain -- but a
        # backend that just failed fatally is unusable, so drop
        # it (released, slot cleared) before the CPU re-run.
        if fatal:
            _drop_thread_backend()
    else:
        # An explicitly passed backend keeps its old contract:
        # released after the drain, exactly as before.
        _release_backend(backend)
    if fatal:
        # Fatal mid-drain GL failure: the queue is plain data, so
        # re-run it whole on a fresh CPU backend. The GL run's
        # notes are kept, with the fallback note first.
        cpu = CpuPixelBackend()
        redo = _Drainer(cpu, width, height)
        if queue is None:
            redo.notes.append("queue is None; treated as empty")
            cpu_result = redo.run([])
        elif not isinstance(queue, (list, tuple)):
            redo.notes.append("queue is not a list (%r); ignored"
                              % (type(queue).__name__,))
            cpu_result = redo.run([])
        else:
            cpu_result = redo.run(list(queue))
        return {
            "image": cpu_result["image"],
            "notes": (["GL backend failed (%s); fell back to CPU "
                       "backend" % fatal]
                      + result["notes"] + cpu_result["notes"]),
            "handled": _merge_names(result["handled"],
                                    cpu_result["handled"]),
            "unhandled": _merge_names(result["unhandled"],
                                      cpu_result["unhandled"]),
            "backend": "cpu",
        }
    result["backend"] = name
    return result
