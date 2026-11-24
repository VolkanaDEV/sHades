"""Micron site channels: page load/render, the site editor, and micron link/media handling.

Moved verbatim out of kivy_client.py's sHadesClientApp during
the phase-2 client split (methods keep their bodies, decorators and
call order; they reach the rest of the app through ``self`` exactly
as before). The app class inherits this mixin, so every method stays
reachable as ``app.<name>`` and ``kc.sHadesClientApp.<name>``.
"""

import base64
from micron import (micron_media_target, micron_page_bg,
                    micron_page_media, micron_parse,
                    micron_parse_emitted, runs_to_markup)
import queue
import re
import sanguis_script
import threading
import time
import webbrowser
from client_util import _escape
from client_widgets import MicronRefLabel


def _site_media_blob(guild_api, target):
    """Fetch one site-media target's bytes (site_get for /file/sites/
    targets, fetch_file otherwise); None on any failure. Shared by
    the media landings in _add_site_media so every path
    can never disagree about where bytes come from."""
    try:
        if target.startswith("/file/sites/"):
            parts = target.split("/", 4)
            if len(parts) == 5:
                res = guild_api.site_get(parts[3], "/" + parts[4])
                if res.get("ok"):
                    return base64.b64decode(res.get("data_b64") or "")
        else:
            raw = guild_api.fetch_file(target)
            if raw:
                return raw
    except Exception as e:
        print("site media fetch failed:", repr(e))
    return None


# Animated script blocks: target cadence between frames (10 fps).
# A frame that overruns the interval pushes the next tick out instead
# of queuing a second run (see _anim_next_delay).
ANIM_FRAME_INTERVAL = 0.1
ANIM_FRAME_MIN_DELAY = 0.02
# How long an animation worker thread waits idle for its next frame
# job before exiting (see _anim_worker): well above the ~20-100 ms
# gap between a frame landing and the next tick, so the thread --
# and the GL backend cached on it -- survives the steady loop, but
# a stopped loop never leaves a blocked thread behind for long.
ANIM_WORKER_IDLE_TIMEOUT = 0.3

# Safe media access by blob id (direction 4): a page script reads a
# page asset only through the media_blob() builtin, fed by host
# bindings the renderer fetches once per render (see
# _fetch_media_bindings). MEDIA_MAX_BYTES caps one blob; the count
# cap lives in micron_page_media (MICRON_PAGE_MEDIA_MAX).
MEDIA_MAX_BYTES = 8192
MEDIA_MAX_BLOBS = 8

# Kivy (guarded so the module can be imported for linting without Kivy present)
try:
    from kivy.app import App
    from kivy.uix.image import AsyncImage
    from kivy.properties import BooleanProperty
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.behaviors import ButtonBehavior
    from kivy.clock import Clock
    from kivy.graphics import Color
    from kivy.properties import ListProperty
    from kivy.graphics import Rectangle
    from kivy.uix.widget import Widget
    from kivy.metrics import dp
    from kivy.metrics import sp
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e


def _format_script_value(value):
    """A script result value -> display text. Integral floats print
    without the trailing .0 (42.0 -> "42"); lists render recursively
    as [a, b], mirroring the interpreter's val_to_str."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "[" + ", ".join(_format_script_value(v) for v in value) + "]"
    if value is None:
        return ""
    return str(value)


def _resolve_micron_path(base_path, target):
    """Resolve a micron link target against the current page's path.
    Absolute targets are normalized in place; relative targets resolve
    against the current page's directory. ``..`` pops one segment but
    never escapes the root (the server rejects traversal anyway)."""
    if target.startswith("/"):
        combined = target
    else:
        directory = (base_path or "/").rsplit("/", 1)[0] or "/"
        combined = directory + "/" + target
    parts = []
    for seg in combined.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/" + "/".join(parts)


def _anim_next_delay(target, elapsed):
    """Delay before the next animation tick: hold the target cadence
    when the frame was quick; when the drain overran the interval,
    come back almost at once (floored, never zero) so the loop keeps
    moving at the drain's own pace instead of stalling."""
    return max(ANIM_FRAME_MIN_DELAY, target - elapsed)


def _pil_to_png_bytes(image):
    """A PIL Image -> PNG bytes (worker-thread safe)."""
    import io
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _anim_ensure_worker(app, state):
    """Start the block's worker thread, unless one is already up.

    The worker owns the frame queue (see _anim_worker); the slot
    is cleared when the worker exits, so a tick after an
    idle-timeout exit starts a fresh thread (and a fresh cached
    backend) instead of enqueueing into the void.
    """
    if state.get("worker") is not None:
        return
    worker = threading.Thread(
        target=lambda: _anim_worker(app, state), daemon=True)
    state["worker"] = worker
    worker.start()


def _anim_stop(state):
    """Mark the loop stopped and wake the worker so it exits.

    The None sentinel in the frame queue is the worker's exit
    signal (see _anim_worker); a worker that already exited
    (idle timeout) simply never reads it. Idempotent.
    """
    state["stopped"] = True
    job_queue = state.get("queue")
    if job_queue is not None:
        try:
            job_queue.put(None)
        except Exception:
            pass


def _anim_worker(app, state):
    """The block's frame loop: one thread, jobs off a queue.

    Each job is a frame number (None is the stop sentinel). The
    thread blocks on the queue between frames, so the
    thread-local cached GL backend survives from frame to frame
    -- that reuse is the whole point (a fresh EGL context per
    frame costs ~50 ms; the cached drain is ~5 ms). After
    ANIM_WORKER_IDLE_TIMEOUT with no job it exits on its own
    (and clears the worker slot, so the next tick starts a fresh
    thread); a stopped loop also exits promptly on the sentinel
    from _anim_stop.
    """
    me = state.get("worker")
    try:
        while True:
            try:
                job = state["queue"].get(
                    timeout=ANIM_WORKER_IDLE_TIMEOUT)
            except queue.Empty:
                break
            if job is None:
                break
            _anim_run_frame(app, state, job)
    finally:
        # The thread is going away: release the GL backend cached
        # on it, here on its own thread, instead of leaking the
        # EGL context until process exit (a thread-local slot dies
        # with its thread, so nothing else could ever release it).
        # A later tick starts a fresh thread with a fresh backend.
        try:
            from gpu_drain import release_thread_backend
        except Exception:
            release_thread_backend = None
        if release_thread_backend is not None:
            try:
                release_thread_backend()
            except Exception:
                pass
        if state.get("worker") is me:
            state["worker"] = None


def _fetch_media_bindings(app, media):
    """Fetch one page's media blobs as script bindings.

    `media` is micron_page_media's [(digest, target), ...] list.
    For each entry: skip when app.guild_api is None; probe the size
    with app.guild_api.att_get(target, length=1) and read `total`
    (the server clamps length to >= 1, so the probe returns total
    plus one byte); skip when the probe is not ok, total <= 0, or
    total > MEDIA_MAX_BYTES; else a full att_get(target),
    base64-decode data_b64, and use the returned bytes (verifying
    the length matches total when present). Returns the list of
    ["media_" + digest, decimals] bindings, where decimals is the
    bytes as space-separated decimals, no trailing space -- the
    exact format media_blob() parses. NEVER raises: every failure
    skips that blob (the script then sees [] for it).

    Module-level, not a mixin method: the test stubs bind a fixed
    method list, so a new mixin method called from bound code is
    missing on stubs and raises AttributeError.
    """
    bindings = []
    try:
        guild_api = getattr(app, "guild_api", None)
        if guild_api is None:
            return bindings
        att_get = getattr(guild_api, "att_get", None)
        if att_get is None:
            return bindings
        for digest, target in media or []:
            try:
                probe = att_get(target, length=1)
                if not isinstance(probe, dict) or not probe.get("ok"):
                    continue
                try:
                    total = int(probe.get("total"))
                except (TypeError, ValueError):
                    continue
                if total <= 0 or total > MEDIA_MAX_BYTES:
                    continue
                res = att_get(target)
                if not isinstance(res, dict) or not res.get("ok"):
                    continue
                try:
                    data = base64.b64decode(res.get("data_b64") or "")
                except Exception:
                    continue
                full_total = res.get("total")
                if isinstance(full_total, int) \
                        and not isinstance(full_total, bool) \
                        and len(data) != full_total:
                    continue
                bindings.append(["media_" + digest,
                                 " ".join(str(b) for b in data)])
            except Exception:
                continue
    except Exception:
        pass
    return bindings


def _anim_run_frame(app, state, frame):
    """Run one animation frame on the worker thread: re-run the
    script with the `frame` binding, drain the GPU queue, and
    land the raw RGB bytes on the main thread. Animation re-runs
    drain the GPU queue only: audio is a frame-0-landing concern
    (see _add_site_script's work()), so a re-run never drains
    audio and a failing frame never restarts the melody."""
    landed = {"raw": None, "size": None, "error": None}
    source = state["source"]
    bindings = app._script_bindings(state["cid"], frame=float(frame))
    # Media is fetched once per block render and reused every frame
    # (see _anim_maybe_start): never re-fetched per frame.
    bindings = bindings + (state.get("media_bindings") or [])
    try:
        res = app._run_page_script(source, bindings)
    except Exception as e:
        res = {"ok": 0.0, "result": "runner failed: %s" % e,
               "out": [], "gpu": []}
    if not isinstance(res, dict):
        res = {"ok": 0.0, "result": "runner failed: bad result",
               "out": [], "gpu": []}
    if res.get("ok"):
        gpu = res.get("gpu") or []
        if gpu:
            try:
                drained = app._drain_page_gpu(gpu)
            except Exception as e:
                drained = {"image": None,
                           "notes": ["GPU output unavailable: %s" % e],
                           "handled": [], "unhandled": []}
            image = None
            if isinstance(drained, dict):
                image = drained.get("image")
            if image is not None:
                try:
                    landed["raw"] = image.tobytes()
                    landed["size"] = image.size
                except Exception as e:
                    landed["error"] = (
                        "could not encode frame: %s" % e)
            else:
                landed["error"] = "no image drained"
        else:
            landed["error"] = "no gpu commands this frame"
    else:
        landed["error"] = str(res.get("result") or "unknown error")
    Clock.schedule_once(
        lambda dt: app._land_anim_frame(state, frame, landed), 0)


def _interact_maybe_start(app, container, landed, widgets):
    """Start tap interactivity for a landed interact block.

    `container` is the dedicated box _land_site_script wrapped
    the landed widgets in (the re-render target for event
    landings, not the page). The state lives on the box; the tap
    handler is bound on the result label (widgets[0]) only --
    never on the box, so taps on child embeds/links are never
    eaten. The stored state starts at landed["interact_state"]
    (the first run's numeric result, or 0.0). Module-level, not
    a mixin method: the test stubs bind a fixed method list, so
    a new mixin method called from bound code is missing on
    stubs and raises AttributeError (the anim/media pattern).
    """
    if not widgets:
        return
    try:
        start_state = float(landed.get("interact_state") or 0.0)
    except (TypeError, ValueError):
        start_state = 0.0
    state = {"container": container, "cid": landed.get("cid"),
             "source": landed.get("source"),
             "expected": landed.get("expected"),
             "media_bindings": landed.get("media_bindings") or [],
             "state": start_state, "busy": False, "stopped": False,
             "taps": 0, "result_widget": widgets[0]}
    try:
        container._interact_state = state
    except Exception:
        pass
    _interact_bind(app, state)


def _interact_bind(app, state):
    """Bind the tap handler on the block's current result label
    (state["result_widget"]): a tap on the label dispatches one
    re-run via _interact_dispatch. Called once at first landing
    and again after every event landing, when the label is new."""
    label = state.get("result_widget")
    if label is None:
        return
    try:
        label.bind(on_touch_down=lambda inst, touch: (
            _interact_dispatch(app, state, touch)))
    except Exception:
        pass


def _interact_alive(state):
    """True while a tap may still re-run the block: not stopped
    and the box is still on a page. The page check
    (_site_current still the captured page) lives in the callers
    (_interact_dispatch, _land_interact_run), which have the
    app; this stays app-free so the state alone decides the
    rest."""
    if state.get("stopped"):
        return False
    box = state.get("container")
    if box is None or box.parent is None:
        return False
    return True


def _interact_dispatch(app, state, touch):
    """Main-thread tap handler, bound on the result label's
    on_touch_down: drop the tap unless it landed on the label
    and the block is alive, on the current page, and idle;
    otherwise mark busy, count the tap, and spawn a daemon
    thread for the re-run. Returns True only when a run was
    dispatched (the touch is consumed); False lets the touch
    keep propagating (scroll, other handlers).

    Coordinates: the touch arrives in the ScrollView's content
    frame -- the walk applies no per-hop transform (see
    MicronRefLabel) -- so the label's local position is the
    touch minus the label's offset chain up to the ScrollView,
    NOT touch minus label.x/y (label.x/y are relative to the
    interact box only, so that subtraction is off by the
    box/page offsets -- wildly so in y). MicronRefLabel's
    _content_anchor computes exactly that chain; a label
    without it falls back to its own x/y. The same anchor math
    guards the collide check: a bare collide_point would
    compare content-frame coords against box-relative bounds
    and almost never collide."""
    label = state.get("result_widget")
    if label is None:
        return False
    try:
        anchor = label._content_anchor()
    except Exception:
        anchor = None
    if anchor is None:
        try:
            anchor = (label.x, label.y)
        except Exception:
            return False
    try:
        lx = touch.x - anchor[0]
        ly = touch.y - anchor[1]
        width = label.width
        height = label.height
    except Exception:
        return False
    if lx < 0 or ly < 0 or lx > width or ly > height:
        return False
    if not _interact_alive(state):
        return False
    expected = state.get("expected")
    if expected:
        cur = getattr(app, "_site_current", None)
        if cur is not None and tuple(cur[:2]) != expected:
            return False
    if state.get("busy"):
        return False
    state["busy"] = True
    state["taps"] = state.get("taps", 0) + 1
    threading.Thread(
        target=lambda: _interact_run(app, state, lx, ly),
        daemon=True).start()
    return True


def _interact_run(app, state, x, y):
    """One tap re-run, on a worker thread: re-run the script
    with event="tap", the tap position, and the stored state,
    drain the GPU queue (texture-only, like an animation frame)
    and the audio queue (saved + auto-played on landing, like
    the first run), update the stored state by the sticky rule,
    and land on the main thread. Each run is a fresh
    run_script call, so it gets a fresh 200,000-step budget and
    a fresh 10.0 s timeout -- the same limits as the first run.
    Media bindings are the ones fetched at first render, never
    re-fetched per tap (see _add_site_script's work())."""
    landed = {"expected": state.get("expected"), "gpu_notes": [],
              "gpu_fpath": None, "interact": True,
              "source": state.get("source"), "cid": state.get("cid"),
              "media_bindings": state.get("media_bindings") or [],
              "interact_state": state.get("state", 0.0)}
    bindings = app._script_bindings(state.get("cid"))
    bindings = bindings + [["event", "tap"], ["event_x", x],
                           ["event_y", y],
                           ["state", state.get("state", 0.0)]] \
        + (state.get("media_bindings") or [])
    try:
        res = app._run_page_script(state.get("source"), bindings)
    except Exception as e:
        res = {"ok": 0.0, "result": "runner failed: %s" % e,
               "out": [], "gpu": []}
    if not isinstance(res, dict):
        res = {"ok": 0.0, "result": "runner failed: bad result",
               "out": [], "gpu": []}
    landed["result"] = res
    # Sticky state: a numeric result replaces the stored state;
    # a non-numeric result (string, list, bool) leaves it
    # unchanged, so the next tap still sees the last numeric
    # value. A failing run never touches the state either.
    if res.get("ok"):
        val = res.get("result")
        if isinstance(val, (int, float)) \
                and not isinstance(val, bool):
            state["state"] = float(val)
            landed["interact_state"] = float(val)
    gpu = res.get("gpu") or []
    if res.get("ok") and gpu:
        try:
            drained = app._drain_page_gpu(gpu)
        except Exception as e:
            drained = {"image": None,
                       "notes": ["GPU output unavailable: %s" % e],
                       "handled": [], "unhandled": []}
        if not isinstance(drained, dict):
            drained = {"image": None, "notes": [],
                       "handled": [], "unhandled": []}
        notes = list(drained.get("notes") or [])
        backend = drained.get("backend")
        backend_names = {"opengl": "OpenGL ES", "cpu": "CPU"}
        if (isinstance(backend, str) and backend in backend_names
                and not any(isinstance(n, str)
                            and n.startswith("GPU backend:")
                            for n in notes)):
            notes.insert(0, "GPU backend: "
                         + backend_names[backend])
        unhandled = drained.get("unhandled") or []
        if unhandled:
            notes.append("unhandled GPU commands: "
                         + ", ".join(str(n) for n in unhandled))
        landed["gpu_notes"] = notes
        image = drained.get("image")
        if image is not None:
            # Texture-only, like an animated frame: the file
            # save below is for the fullscreen tap only, and a
            # file-source embed could show the loader-cached
            # first image instead of this run's.
            try:
                landed["gpu_raw"] = image.tobytes()
                landed["gpu_size"] = image.size
            except Exception:
                pass
            fpath = app._save_gpu_image(image)
            if fpath:
                landed["gpu_fpath"] = fpath
            else:
                landed["gpu_notes"] = notes + [
                    "GPU output unavailable: could not save image"]
    audio = res.get("audio") or []
    if res.get("ok") and audio:
        try:
            drained_a = app._drain_page_audio(audio)
        except Exception as e:
            drained_a = {"wav": None, "duration": 0.0,
                         "count": 0, "handled": [],
                         "unhandled": [],
                         "notes": ["Audio output unavailable: "
                                   "%s" % e]}
        if not isinstance(drained_a, dict):
            drained_a = {"wav": None, "duration": 0.0,
                         "count": 0, "handled": [],
                         "unhandled": [], "notes": []}
        notes_a = list(drained_a.get("notes") or [])
        unhandled_a = drained_a.get("unhandled") or []
        if unhandled_a:
            notes_a.append("unhandled audio commands: "
                           + ", ".join(str(n)
                                       for n in unhandled_a))
        landed["audio_notes"] = notes_a
        try:
            landed["audio_duration"] = float(
                drained_a.get("duration") or 0.0)
        except (TypeError, ValueError):
            landed["audio_duration"] = 0.0
        try:
            landed["audio_count"] = int(
                drained_a.get("count") or 0)
        except (TypeError, ValueError):
            landed["audio_count"] = 0
        wav = drained_a.get("wav")
        landed["audio_wav"] = wav if isinstance(wav, bytes) \
            else None
        if landed["audio_wav"] is not None:
            fpath_a = app._save_audio_wav(landed["audio_wav"])
            if fpath_a:
                landed["audio_fpath"] = fpath_a
            else:
                landed["audio_notes"] = notes_a + [
                    "Audio output unavailable: could not save audio"]
    Clock.schedule_once(
        lambda dt: _land_interact_run(app, state, landed), 0)


def _land_interact_run(app, state, landed):
    """Main-thread landing for one tap re-run: rebuild the
    block's widgets inside its box from the new run's result.
    The box stays where the first landing put it; only its
    children change. The new result label (the error label on
    a failing run) is re-bound as the tap target, so the block
    stays tappable after an error; audio auto-plays on each
    event landing, like the first landing (a tap that plays a
    note is the point -- the anim "frame 0 only" rule was about
    a looping animation, not user-initiated taps)."""
    state["busy"] = False
    if not _interact_alive(state):
        return
    box = state.get("container")
    if box is None or box.parent is None:
        return
    expected = state.get("expected")
    if expected:
        cur = getattr(app, "_site_current", None)
        if cur is not None and tuple(cur[:2]) != expected:
            return
    res = landed.get("result") or {}
    try:
        widgets = app._script_result_widgets(res, landed)
        box.clear_widgets()
        for w in widgets:
            box.add_widget(w)
        if widgets:
            state["result_widget"] = widgets[0]
            _interact_bind(app, state)
        audio_row = None
        for w in widgets:
            if getattr(w, "_is_script_audio_row", False):
                audio_row = w
                break
        if audio_row is not None:
            fpath = landed.get("audio_fpath")
            if fpath:
                play = getattr(app, "_play_audio", None)
                started = None
                if play is not None:
                    try:
                        started = play(fpath)
                    except Exception:
                        started = None
                try:
                    audio_row._audio_started = started is not None
                except Exception:
                    pass
    except Exception as e:
        print("interact land failed:", repr(e))


def _texture_from_raw(raw, size):
    """A Kivy texture from top-down RGB bytes -- no PNG round-trip.

    ``Texture.create`` + ``blit_buffer`` with the PIL bytes lands
    pixels identical to the CoreImage PNG path (and no flip: the
    stored pixels are already top-down). Raises on failure; the
    caller falls back to the PNG path.
    """
    from kivy.graphics.texture import Texture
    tex = Texture.create(size=size, colorfmt="rgb")
    tex.blit_buffer(raw, colorfmt="rgb", bufferfmt="ubyte")
    return tex


def _rgb_bytes_to_texture(raw, size):
    """A Kivy texture from top-down RGB bytes.

    Raw blit first; if that raises (a driver that refuses the
    blit), fall back to the PNG round-trip (PIL -> PNG bytes ->
    CoreImage), which is the path this replaced. Raises only when
    both paths fail, and the caller then falls back further (the
    file source for frame 0, a stopped loop for later frames).
    """
    try:
        return _texture_from_raw(raw, size)
    except Exception:
        from PIL import Image as _PILImage
        from kivy.core.image import Image as CoreImage
        import io
        image = _PILImage.frombytes("RGB", size, raw)
        return CoreImage(io.BytesIO(_pil_to_png_bytes(image)),
                         ext="png").texture


class SiteMixin:
    """Micron site channels: page load/render, the site editor, and micron link/media handling."""

    def _refresh_site_nav(self, visible=None):
        """Mirror the navigation state into the app properties the KV
        nav bar binds to: visibility, the breadcrumb path, and whether
        Back / Forward have anywhere to go. Call after every navigation,
        render, and error page. visible=None leaves visibility alone."""
        if visible is False:
            self.site_nav_visible = False
            self.site_nav_path = ""
            self.site_nav_can_back = False
            self.site_nav_can_forward = False
            return
        elif visible is True:
            self.site_nav_visible = True
        cur = getattr(self, "_site_current", None)
        if cur:
            cid, path = cur[0], cur[1]
            name = None
            _channel_name = getattr(self, "_channel_name", None)
            if _channel_name is not None:
                try:
                    name = _channel_name(cid)
                except Exception:
                    name = None
            if not name:
                try:
                    name = getattr(self.root.get_screen("main"),
                                   "_current_channel", None)
                except Exception:
                    name = None
            self.site_nav_path = ("#%s %s" % (name, path)) if name else path
        stack = getattr(self, "_site_stack", None) or []
        fwd = getattr(self, "_site_forward", None) or []
        self.site_nav_can_back = bool(stack)
        self.site_nav_can_forward = bool(fwd)

    def _open_site_channel(self, name, cid):
        """Open a site channel: fetch its manifest, then render the index."""
        self._site_stack = []
        self._site_forward = []
        self._site_current = None
        self._site_pending_scroll = 1.0
        self._refresh_site_nav(visible=True)
        self.site_nav_path = "#%s" % name
        self._site_theme = {}
        try:
            main = self.root.get_screen("main")
            grid = main.ids.message_grid
            grid.clear_widgets()
            self._msg_widgets.clear()
            grid.add_widget(self._micron_label("[i]loading site…[/i]"))
        except Exception as e:
            print("site open error:", repr(e))

        def work():
            manifest = {}
            try:
                res = self.guild_api.channels_info(cid)
                if res.get("ok"):
                    manifest = (res.get("channel") or {}).get("manifest") or {}
            except Exception as e:
                print("site manifest error:", repr(e))
            self._site_theme = manifest.get("theme") or {}
            index = manifest.get("index") or "/index.mu"
            Clock.schedule_once(lambda dt: self._site_load_page(cid, index), 0)
        threading.Thread(target=work, daemon=True).start()

    def _site_load_page(self, cid, path, push=False, restore_y=None):
        """Fetch + render one micron page. /page/... targets are the guild's
        own pages (raw request); everything else is a site resource (v1).
        push records the page being left (with its scroll position) on
        the back stack and drops the forward stack; restore_y is the
        scroll position a Back/Forward navigation restores (None = top)."""
        if push and self._site_current:
            self._site_stack.append(
                (self._site_current[0], self._site_current[1],
                 self._site_scroll_y()))
            fwd = getattr(self, "_site_forward", None)
            if fwd is not None:
                fwd.clear()
        self._site_current = (cid, path)
        self._site_pending_scroll = 1.0 if restore_y is None else restore_y
        # (d) the breadcrumb shows the target path at once, even while
        # the fetch is still running.
        _refresh_nav = getattr(self, "_refresh_site_nav", None)
        if _refresh_nav is not None:
            _refresh_nav()
        if not self.guild_api:
            return
        # (b) loading state: show it synchronously, on the main thread,
        # before the worker fetch starts -- otherwise the old page sits
        # unchanged for the whole fetch and the tap reads as dead.
        self._site_show_loading(cid)

        def work():
            text, err, can_edit = None, "unknown", False
            try:
                if path.startswith("/page/"):
                    raw = self.guild_api.fetch_file(path)
                    if raw is not None:
                        text = raw.decode("utf-8", "replace")
                    else:
                        err = "no response"
                else:
                    res = self.guild_api.site_get(cid, path)
                    if res.get("ok"):
                        text = base64.b64decode(
                            res.get("data_b64") or "").decode("utf-8", "replace")
                        can_edit = bool(res.get("can_edit"))
                    else:
                        err = res.get("error", "not found")
                        can_edit = bool(res.get("can_edit"))
            except Exception as e:
                err = repr(e)
                print("site page fetch failed:", err)
            if text is not None:
                Clock.schedule_once(
                    lambda dt: self._render_site_page(cid, text,
                                                      can_edit=can_edit), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._site_empty_page(cid, err,
                                                     can_edit=can_edit), 0)
        threading.Thread(target=work, daemon=True).start()

    def _site_scroll_y(self):
        """The page ScrollView's current position (1.0 = top)."""
        try:
            return float(self.root.get_screen("main").ids.scroll.scroll_y)
        except Exception:
            return 1.0

    def _site_set_scroll(self, y):
        """Restore a scroll position after a render. Set at once, then
        once more on the next clock tick: the layout pass can move the
        ScrollView after the widgets land, undoing a same-tick set."""
        try:
            scroll = self.root.get_screen("main").ids.scroll
        except Exception:
            return
        try:
            scroll.scroll_y = y
            Clock.schedule_once(
                lambda dt, s=scroll, v=y: setattr(s, "scroll_y", v), 0)
        except Exception:
            pass

    @staticmethod
    def _site_entry(entry):
        """Normalize a stack entry to (cid, path, scroll_y); entries
        written before scroll capture carry no third element."""
        cid, path = entry[0], entry[1]
        y = entry[2] if len(entry) > 2 else 1.0
        return cid, path, y

    def _site_go_back(self):
        """Back: pop the back stack, shelve the current page (with its
        scroll position) on the forward stack, render the popped page
        at its recorded scroll position. False when there is nothing
        to go back to."""
        stack = getattr(self, "_site_stack", None) or []
        if not stack:
            return False
        cid, path, y = self._site_entry(stack.pop())
        cur = getattr(self, "_site_current", None)
        if cur:
            fwd = getattr(self, "_site_forward", None)
            if fwd is None:
                fwd = self._site_forward = []
            fwd.append((cur[0], cur[1], self._site_scroll_y()))
        self._site_load_page(cid, path, restore_y=y)
        return True

    def _site_go_forward(self):
        """Forward: the mirror of _site_go_back. False when the forward
        stack is empty (e.g. a fresh navigation already dropped it)."""
        fwd = getattr(self, "_site_forward", None) or []
        if not fwd:
            return False
        cid, path, y = self._site_entry(fwd.pop())
        cur = getattr(self, "_site_current", None)
        if cur:
            stack = getattr(self, "_site_stack", None)
            if stack is None:
                stack = self._site_stack = []
            stack.append((cur[0], cur[1], self._site_scroll_y()))
        self._site_load_page(cid, path, restore_y=y)
        return True

    def site_go_back(self):
        """KV entry point for the persistent Back control."""
        return self._site_go_back()

    def site_go_forward(self):
        """KV entry point for the persistent Forward control."""
        return self._site_go_forward()

    def _site_show_loading(self, cid):
        """Replace the page area with a loading line while a page fetch
        runs. Never clobbers another channel: when the viewer has
        already switched away, the cid no longer matches the open
        channel and the grid is left alone (the stale render is
        dropped by the same guard in _render_site_page)."""
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        try:
            cur = getattr(main, "_current_channel", None)
            if cur and self._channel_cid(cur) != cid:
                return
        except Exception:
            pass
        try:
            grid = main.ids.message_grid
            grid.clear_widgets()
            self._msg_widgets.clear()
            grid.add_widget(self._micron_label("[i]loading…[/i]"))
        except Exception as e:
            print("site loading render failed:", repr(e))

    def _site_empty_page(self, cid, err, can_edit=False):
        """The page fetch failed: replace the 'loading site' placeholder
        with an honest empty page instead of leaving it forever (tay's
        device receipt, 0.36.10 era). When the server says the viewer
        may edit, the empty state IS the creation door: a not-found
        read for a permitted viewer leads with [ Create index page ].
        channels with a default /index.mu, so this is the exceptional
        branch: a site created before the seed, or a deleted target."""
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        cur = getattr(main, "_current_channel", None)
        if cur and self._channel_cid(cur) != cid:
            return   # user switched away while the fetch was in flight
        try:
            grid = main.ids.message_grid
            grid.clear_widgets()
            self._msg_widgets.clear()
            page = SitePage()
            page.add_widget(self._micron_label(
                "[b]This site is empty[/b]"))
            # (a) broken-link dead end: the back stack already holds the
            # previous page, so offer the way back here too -- without
            # it the viewer is stranded on the error page.
            if getattr(self, "_site_stack", None):
                page.add_widget(self._micron_label(
                    "[ref=shades:back][b]< Back[/b][/ref]"))
            if can_edit and (err or "") == "resource not found":
                page.add_widget(self._micron_label(
                    "[ref=shades:site-create][ Create index page ]"
                    "[/ref]"))
            page.add_widget(self._micron_label(
                "The page failed to load: %s" % _escape(err or "unknown")))
            grid.add_widget(page)
            # Polish group 4: the error page is a navigation landing
            # too, so it fades in with the same reveal as a rendered
            # page. reveal() is opacity-only, so layout never moves,
            # and reduced motion lands the page at opacity 1 at once.
            import ui_fx
            ui_fx.reveal(page)
            # getattr: minimal test stubs bind _site_empty_page without
            # the rest of the mixin (see test_link_selfheal).
            _set_scroll = getattr(self, "_site_set_scroll", None)
            if _set_scroll is not None:
                _set_scroll(1.0)
        except Exception as e:
            print("site empty-page render failed:", repr(e))
        # (d) the breadcrumb shows the failed target too, and the
        # Back gate follows the stack (see the Back ref above).
        _refresh_nav = getattr(self, "_refresh_site_nav", None)
        if _refresh_nav is not None:
            try:
                _refresh_nav()
            except Exception:
                pass
        self._set_status("page failed: %s" % (err or "unknown"))

    def _render_site_page(self, cid, text, can_edit=False):
        """Render one micron page into the main grid. Holds the fetched
        source for the P7c editor and, when the server says the caller
        carries SITE_EDIT, leads with the [ Edit page ] affordance."""
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        cur = getattr(main, "_current_channel", None)
        if cur and self._channel_cid(cur) != cid:
            return   # user switched away while the page was in flight
        self._site_src_text = text
        grid = main.ids.message_grid
        grid.clear_widgets()
        self._remove_skeleton(getattr(self, "_skeleton_cid", None))
        self._msg_widgets.clear()
        blocks = micron_parse(text)
        page = SitePage()
        if self._site_stack:
            page.add_widget(self._micron_label(
                "[ref=shades:back][b]< Back[/b][/ref]"))
        if can_edit:
            page.add_widget(self._micron_label(
                "[ref=shades:site-edit][b][ Edit page ][/b][/ref]"))
        self._micron_blocks_into(page, blocks, cid)
        grid.add_widget(page)
        # Polish group 4: every page render fades in. reveal() is
        # opacity-only, so layout never moves, and reduced motion
        # lands the page at opacity 1 at once.
        import ui_fx
        ui_fx.reveal(page)
        # (d) the breadcrumb and the Back/Forward gates follow the render.
        _refresh_nav = getattr(self, "_refresh_site_nav", None)
        if _refresh_nav is not None:
            _refresh_nav()
        # (e) scroll restore: a Back/Forward navigation recorded the
        # position the page was left at; a fresh navigation goes top.
        y = getattr(self, "_site_pending_scroll", 1.0) or 1.0
        self._site_pending_scroll = 1.0
        # getattr: minimal test stubs may bind _render_site_page without
        # the rest of the mixin (same pattern as _site_empty_page).
        _set_scroll = getattr(self, "_site_set_scroll", None)
        if _set_scroll is not None:
            _set_scroll(y)

    def _micron_blocks_into(self, container, blocks, cid, media_ok=True):
        """The micron block loop shared by the main render and the site
        editor's preview. media_ok=False (editor preview) renders media
        runs as text so the preview never reaches for the network."""
        bg = micron_page_bg(blocks, self._site_theme)
        if bg and isinstance(container, SitePage):
            container.bg_color = self._hex6_to_rgba(bg)
        # The page's media set, computed once per render: every
        # script block on the page reads the same fetched blobs.
        media = micron_page_media(blocks)
        for b in blocks:
            t = b["type"]
            if t == "directive":
                continue
            if t == "sep":
                container.add_widget(MicronSep())
                continue
            if t == "heading":
                container.add_widget(self._micron_label(
                    runs_to_markup(b["runs"], _escape), heading=True))
                continue
            if t == "code":
                container.add_widget(self._micron_label(
                    _escape(b["text"]), code=True))
                continue
            if t == "script":
                self._add_site_script(container, cid, b.get("text") or "",
                                      lang=b.get("lang", "sanguis"),
                                      media_ok=media_ok,
                                      anim=bool(b.get("anim")),
                                      markup=bool(b.get("markup")),
                                      interact=bool(b.get("interact")),
                                      media_ids=media)
                continue
            runs = b.get("runs") or []
            if len(runs) == 1 and micron_media_target(runs[0].get("link") or ""):
                if media_ok:
                    self._add_site_media(container, cid, runs[0])
                else:
                    container.add_widget(self._micron_label(
                        "[i][media: %s][/i]" % _escape(runs[0].get("link") or "")))
            else:
                container.add_widget(self._micron_label(
                    runs_to_markup(runs, _escape), center=b.get("center")))

    def _script_bindings(self, cid, frame=None):
        """Bindings injected for a page script: the micron path of the
        page on screen (from _site_current) and gpu_present, plus the
        channel name when it is trivially at hand (the main screen's
        _current_channel is the channel name). The channel binding is
        omitted when no name is available — never guessed. `frame` is
        the animation frame (animated blocks only): frame 0 for the
        first run, N for frame N; one-shot blocks get no `frame`
        binding at all, so their bindings are byte-identical to
        before."""
        page_path = ""
        cur = getattr(self, "_site_current", None)
        if cur and cur[0] == cid and len(cur) > 1:
            page_path = cur[1] or ""
        bindings = [["page", page_path],
                    ["gpu_present", sanguis_script.gpu_present_value()]]
        try:
            name = getattr(self.root.get_screen("main"),
                           "_current_channel", None)
        except Exception:
            name = None
        if isinstance(name, str) and name:
            bindings.append(["channel", name])
        if frame is not None:
            bindings.append(["frame", float(frame)])
        return bindings

    def _script_page_path(self, cid):
        """The micron path of the page on screen for this channel, or
        "" when it is not known (direct _micron_blocks_into calls)."""
        cur = getattr(self, "_site_current", None)
        if cur and cur[0] == cid and len(cur) > 1:
            return cur[1] or ""
        return ""

    def _run_page_script(self, source, bindings):
        """Seam for page-script execution (tests replace this with a
        fake): run one script through the Sanguis runner."""
        return sanguis_script.run_script(source, bindings)

    def _drain_page_gpu(self, queue):
        """Seam for GPU draining (tests replace this with a fake):
        drain the interpreter's queued GPU commands into an image +
        notes. Imported lazily — gpu_drain may not exist on every
        build, and the import must never break module import."""
        from gpu_drain import drain_gpu_commands
        return drain_gpu_commands(queue, width=160, height=120)

    def _drain_page_audio(self, queue):
        """Seam for audio draining (tests replace this with a fake):
        drain the interpreter's queued audio commands into WAV
        bytes + notes. Imported lazily — audio_drain may not exist
        on every build, and the import must never break module
        import."""
        from audio_drain import drain_audio_commands
        return drain_audio_commands(queue)

    def _save_gpu_image(self, image):
        """A drained PIL Image -> PNG bytes -> attachments dir; the
        local path, or None when anything fails. Worker-thread safe
        (same pattern as _add_site_media's _save_attachment_blob)."""
        try:
            save = getattr(self, "_save_attachment_blob", None)
            if save is None:
                return None
            return save(_pil_to_png_bytes(image), "script-gpu.png")
        except Exception:
            return None

    def _save_audio_wav(self, wav_bytes):
        """Drained WAV bytes -> attachments dir as script-audio.wav;
        the local path, or None when anything fails. Worker-thread
        safe (same pattern as _save_gpu_image)."""
        try:
            save = getattr(self, "_save_attachment_blob", None)
            if save is None:
                return None
            return save(wav_bytes, "script-audio.wav")
        except Exception:
            return None

    def _add_site_script(self, container, cid, source, lang="sanguis",
                         media_ok=True, anim=False, markup=False,
                         interact=False, media_ids=()):
        """A `=sanguis script block. Editor preview (media_ok=False)
        shows the source and never executes it — the media_ok
        precedent. A non-sanguis lang renders as code too: only the
        fixed `=sanguis fence ever runs. Live render inserts a
        placeholder, runs the script on a daemon thread (cf.
        _add_site_media), and lands the result on the main thread;
        a stale landing (navigated away, page re-rendered) is a
        no-op. Non-script content renders immediately; script output
        fills in on completion. `anim` (a `=sanguis anim block)
        re-runs the script once per frame with a `frame` binding
        (frame 0 here, then 1, 2, ... via _anim_tick) and swaps each
        drained image into the landed embed's texture in place — the
        page is never re-rendered. The loop only starts when the
        first run actually lands a GPU image; a block whose script
        draws nothing animates nothing and behaves one-shot.
        `markup` (a `=sanguis markup block) re-parses the run's
        printed lines as micron source on a successful landing
        (see _markup_result_widgets): the result widget stays, the
        printed lines become headings, paragraphs, links and media
        instead of code lines, and any nested script block the
        output tries to emit is neutralized to literal code, never
        executed. `interact` (a `=sanguis interact block) re-runs
        the script when the viewer taps its result label: the
        first run carries event="load", event_x/event_y 0.0 and
        state 0.0; each tap re-runs it with event="tap", the tap
        position in the block's local coordinates, and the stored
        numeric state (the previous run's numeric result -- a
        non-numeric result leaves the stored state unchanged, see
        _interact_run). The landed widgets live in a dedicated box
        (see _land_site_script / _interact_maybe_start) that each
        event landing rebuilds in place. A block with any two of
        anim/markup/interact renders as code, never executed --
        the parser never emits the combination, and the loop
        would re-render markup every frame. `media_ids` is the
        page's media set from
        micron_page_media (computed once per render by
        _micron_blocks_into): the worker fetches those blobs once
        via _fetch_media_bindings and passes them as extra
        bindings, so media_blob() can read them; animated blocks
        reuse the same fetched bindings every frame, and interact
        blocks reuse them on every tap."""
        if not media_ok or lang != "sanguis":
            container.add_widget(self._micron_label(
                _escape(source), code=True))
            return
        if not sanguis_script.scripting_available():
            container.add_widget(self._micron_label(
                "[i]scripting unavailable on this platform[/i]"))
            return
        if anim + markup + interact >= 2:
            # Defense in depth: the parser never emits a block
            # with two flags (an opener carrying two suffixes is
            # not a script block at all), so reaching here means a
            # direct caller passed two. Fail closed as code.
            container.add_widget(self._micron_label(
                _escape(source), code=True))
            return
        placeholder = self._micron_label("[i]running script…[/i]")
        container.add_widget(placeholder)
        token = object()
        try:
            placeholder._script_token = token
        except Exception:
            pass
        bindings = self._script_bindings(cid,
                                         frame=0.0 if anim else None)
        if interact:
            # First run: the "load" event, no tap position yet,
            # state 0.0 (see _interact_run for the tap bindings).
            bindings = bindings + [["event", "load"],
                                   ["event_x", 0.0],
                                   ["event_y", 0.0],
                                   ["state", 0.0]]
        page_path = self._script_page_path(cid)
        expected = (cid, page_path) if page_path else None

        def work():
            landed = {"expected": expected, "gpu_notes": [],
                      "gpu_fpath": None, "anim": anim, "markup": markup,
                      "interact": interact, "interact_state": 0.0,
                      "source": source, "cid": cid, "media_bindings": []}
            # Media first: fetch this page's blobs once (worker
            # thread, like the media fetch) and pass them as extra
            # bindings, so media_blob() can read them. Any fetch
            # failure yields no bindings and the script still runs
            # (a missing blob answers [] in the script).
            try:
                media_bindings = _fetch_media_bindings(self, media_ids)
            except Exception:
                media_bindings = []
            landed["media_bindings"] = media_bindings
            try:
                res = self._run_page_script(source,
                                            bindings + media_bindings)
            except Exception as e:
                res = {"ok": 0.0, "result": "runner failed: %s" % e,
                       "out": [], "gpu": []}
            if not isinstance(res, dict):
                res = {"ok": 0.0, "result": "runner failed: bad result",
                       "out": [], "gpu": []}
            landed["result"] = res
            if interact and res.get("ok"):
                # First run's state: a numeric result becomes
                # the stored state; a non-numeric result leaves
                # it at 0.0 (the sticky rule, see _interact_run).
                val = res.get("result")
                if isinstance(val, (int, float)) \
                        and not isinstance(val, bool):
                    landed["interact_state"] = float(val)
            gpu = res.get("gpu") or []
            if res.get("ok") and gpu:
                try:
                    drained = self._drain_page_gpu(gpu)
                except Exception as e:
                    drained = {"image": None,
                               "notes": ["GPU output unavailable: %s" % e],
                               "handled": [], "unhandled": []}
                if not isinstance(drained, dict):
                    drained = {"image": None, "notes": [],
                               "handled": [], "unhandled": []}
                notes = list(drained.get("notes") or [])
                backend = drained.get("backend")
                backend_names = {"opengl": "OpenGL ES", "cpu": "CPU"}
                # The real drainer already leads its notes with a
                # "GPU backend: ..." line (richer on fallback: it
                # names the reason); only add the renderer line
                # when no such line is present, so the page never
                # shows the backend twice.
                if (isinstance(backend, str) and backend in backend_names
                        and not any(isinstance(n, str)
                                    and n.startswith("GPU backend:")
                                    for n in notes)):
                    notes.insert(0, "GPU backend: "
                                 + backend_names[backend])
                unhandled = drained.get("unhandled") or []
                if unhandled:
                    notes.append("unhandled GPU commands: "
                                 + ", ".join(str(n) for n in unhandled))
                landed["gpu_notes"] = notes
                image = drained.get("image")
                if image is not None:
                    if anim or interact:
                        # The first frame's raw bytes ride along so
                        # the landing can build the embed texture
                        # directly (see _script_result_widgets):
                        # a `source` would let the AsyncImage
                        # loader overwrite later frames with this
                        # first one. No PNG encode here -- the
                        # file save below still encodes one, for
                        # the fullscreen tap. An interact block
                        # is texture-only for the same reason: a
                        # tap re-run must show its own image, not
                        # the loader-cached first one.
                        try:
                            landed["gpu_raw"] = image.tobytes()
                            landed["gpu_size"] = image.size
                        except Exception:
                            pass
                    fpath = self._save_gpu_image(image)
                    if fpath:
                        landed["gpu_fpath"] = fpath
                    else:
                        landed["gpu_notes"] = notes + [
                            "GPU output unavailable: "
                            "could not save image"]
            # Audio drains ONLY here, on the frame-0 landing path:
            # _anim_tick re-runs the script and drains the GPU
            # queue per frame but never touches the audio queue, so
            # a melody plays once when the block lands and a later
            # (or failing) animation frame never restarts it. A
            # failing script produces no audio at all, and an empty
            # queue drains nothing (no note line, no control, no
            # file). res.get("audio") (never res["audio"]) so a
            # runner that predates the audio field still works.
            audio = res.get("audio") or []
            if res.get("ok") and audio:
                try:
                    drained_a = self._drain_page_audio(audio)
                except Exception as e:
                    drained_a = {"wav": None, "duration": 0.0,
                                 "count": 0, "handled": [],
                                 "unhandled": [],
                                 "notes": ["Audio output unavailable: "
                                           "%s" % e]}
                if not isinstance(drained_a, dict):
                    drained_a = {"wav": None, "duration": 0.0,
                                 "count": 0, "handled": [],
                                 "unhandled": [], "notes": []}
                notes_a = list(drained_a.get("notes") or [])
                unhandled_a = drained_a.get("unhandled") or []
                if unhandled_a:
                    notes_a.append("unhandled audio commands: "
                                   + ", ".join(str(n)
                                               for n in unhandled_a))
                landed["audio_notes"] = notes_a
                try:
                    landed["audio_duration"] = float(
                        drained_a.get("duration") or 0.0)
                except (TypeError, ValueError):
                    landed["audio_duration"] = 0.0
                try:
                    landed["audio_count"] = int(
                        drained_a.get("count") or 0)
                except (TypeError, ValueError):
                    landed["audio_count"] = 0
                wav = drained_a.get("wav")
                landed["audio_wav"] = wav if isinstance(wav, bytes) \
                    else None
                if landed["audio_wav"] is not None:
                    fpath_a = self._save_audio_wav(landed["audio_wav"])
                    if fpath_a:
                        landed["audio_fpath"] = fpath_a
                    else:
                        landed["audio_notes"] = notes_a + [
                            "Audio output unavailable: "
                            "could not save audio"]
            Clock.schedule_once(
                lambda dt: self._land_site_script(
                    container, placeholder, token, landed), 0)
        threading.Thread(target=work, daemon=True).start()

    def _land_site_script(self, container, placeholder, token, landed):
        """Replace a script placeholder with the run's result, on the
        main thread. No-ops when the page is stale: the viewer
        navigated away (_site_current moved on from the (cid, path)
        captured at render), the page re-rendered (the placeholder
        left its container), or another block's landing already
        claimed this placeholder (per-block token)."""
        try:
            seen = getattr(placeholder, "_script_token", token)
            if seen is not token:
                return
            if placeholder.parent is not container:
                return
            expected = landed.get("expected")
            if expected:
                cur = getattr(self, "_site_current", None)
                if cur is not None and tuple(cur[:2]) != expected:
                    return
            try:
                idx = container.children.index(placeholder)
            except ValueError:
                return
            container.remove_widget(placeholder)
            res = landed.get("result") or {}
            widgets = self._script_result_widgets(res, landed)
            # Interact blocks land inside a dedicated box at the
            # placeholder's index: one re-render target the event
            # landings rebuild in place (_land_interact_run).
            # The box sizes to its content like the page itself
            # (size_hint_y None + minimum_height binding).
            box = None
            if landed.get("interact"):
                box = BoxLayout(orientation="vertical",
                                size_hint_y=None, spacing=dp(4))
                box.bind(minimum_height=lambda w, h: setattr(
                    w, "height", h))
                for w in widgets:
                    box.add_widget(w)
                container.add_widget(box, index=idx)
            else:
                # children is newest-first; inserting each widget at the
                # placeholder's index, in visual (top-to-bottom) order,
                # puts the group back exactly where the placeholder was.
                for w in widgets:
                    container.add_widget(w, index=idx)
            # Auto-play landed script audio. This landing runs on
            # the main thread (it arrives via Clock.schedule_once
            # from the worker), so _play_audio is never called from
            # the worker thread. _play_audio returns None headless
            # or with no audio device; the page keeps the note line
            # and the stop button either way, and the row only
            # counts as started when playback actually began.
            audio_row = None
            for w in widgets:
                if getattr(w, "_is_script_audio_row", False):
                    audio_row = w
                    break
            if audio_row is not None:
                fpath = landed.get("audio_fpath")
                if fpath:
                    play = getattr(self, "_play_audio", None)
                    started = None
                    if play is not None:
                        try:
                            started = play(fpath)
                        except Exception:
                            started = None
                    try:
                        audio_row._audio_started = started is not None
                    except Exception:
                        pass
            if landed.get("anim"):
                self._anim_maybe_start(container, landed, widgets)
            if landed.get("interact"):
                _interact_maybe_start(self, box, landed, widgets)
        except Exception as e:
            print("site script land failed:", repr(e))

    def _anim_maybe_start(self, container, landed, widgets):
        """Start the frame loop for an animated block whose first run
        landed a GPU image. No image -> no loop: the block stays
        one-shot. The loop state lives on the embed widget, so the
        live probe and tests can read frames landed and timing;
        the first tick is scheduled at the target interval."""
        emb_cls = globals().get("MessageEmbed")
        if emb_cls is None:
            return
        emb = None
        for w in widgets:
            if isinstance(w, emb_cls):
                emb = w
                break
        if emb is None:
            return
        state = {"container": container, "cid": landed.get("cid"),
                 "source": landed.get("source"),
                 "expected": landed.get("expected"),
                 "media_bindings": landed.get("media_bindings") or [],
                 "embed": emb, "frame": 0, "busy": False,
                 "stopped": False, "landed_frames": 0,
                 "target": ANIM_FRAME_INTERVAL,
                 "first_land": time.time(), "last_land": None,
                 "tick_started": None}
        try:
            emb._anim_state = state
        except Exception:
            pass
        self._anim_start(state)

    def _anim_start(self, state):
        """Arm the loop: create the frame queue and schedule the
        first tick at the target interval.

        The worker thread itself starts on the first tick (see
        _anim_tick -> _anim_ensure_worker), not here: a loop
        whose viewer navigates away before the first tick must
        never spawn a thread at all, and one daemon thread per
        animated block then lives as long as frames keep coming,
        so the GL backend cached on it (thread-local, see
        gpu_drain.acquire_backend) is reused every frame instead
        of rebuilt.
        """
        state["queue"] = queue.Queue()
        state["worker"] = None
        Clock.schedule_once(lambda dt: self._anim_tick(state),
                            state["target"])

    def _anim_alive(self, state):
        """True while the loop may keep running: the embed is still
        in the container it landed in and the viewer is still on
        the page the block was rendered for. Either check failing
        means the page was left or re-rendered, so the loop stops."""
        if state.get("stopped"):
            return False
        embed = state.get("embed")
        if embed is None or embed.parent is not state.get("container"):
            return False
        expected = state.get("expected")
        if expected:
            cur = getattr(self, "_site_current", None)
            if cur is not None and tuple(cur[:2]) != expected:
                return False
        return True

    def _anim_tick(self, state):
        """One Clock tick: queue the next frame on the block's
        worker thread, unless the previous frame is still running
        — then skip this tick and try again at the target interval.
        Runs never overlap and never queue up; a slow drain just
        makes the animation run slower. The worker (started once
        by _anim_start) re-runs the script with the `frame`
        binding and lands on the main thread; a tick after the
        worker exited idle starts a fresh one first, and a busy
        tick does the same check so an orphaned job can never
        strand the loop with busy set and no worker to run it."""
        if not self._anim_alive(state):
            _anim_stop(state)
            return
        if state["busy"]:
            _anim_ensure_worker(self, state)
            Clock.schedule_once(lambda dt: self._anim_tick(state),
                                state["target"])
            return
        frame = state["frame"] + 1
        state["frame"] = frame
        state["busy"] = True
        state["tick_started"] = time.time()
        _anim_ensure_worker(self, state)
        state["queue"].put(frame)

    def _land_anim_frame(self, state, frame, landed):
        """Main-thread landing for one animation frame: swap the new
        image into the embed's texture in place. The page is not
        re-rendered; the result/out widgets keep frame 0's text.
        The embed has no `source` (texture-only, so the AsyncImage
        loader never runs and can never overwrite a later frame
        with the first one — a late `on_load` clobbers any texture
        set before it completes); the frame-0 file path lives in
        `_anim_source` for the fullscreen tap. A frame that fails
        (runner error, no GPU image) stops the loop and leaves a
        visible note under the last good frame."""
        state["busy"] = False
        if not self._anim_alive(state):
            _anim_stop(state)
            return
        raw = landed.get("raw")
        if raw is None:
            _anim_stop(state)
            err = landed.get("error")
            if err:
                try:
                    label = self._micron_label(
                        "[i]animation stopped: %s[/i]"
                        % _escape(str(err)))
                    container = state["container"]
                    idx = container.children.index(state["embed"])
                    container.add_widget(label, index=idx)
                except Exception:
                    pass
            return
        try:
            tex = _rgb_bytes_to_texture(raw, landed.get("size"))
            state["embed"].texture = tex
        except Exception:
            _anim_stop(state)
            return
        now = time.time()
        state["landed_frames"] += 1
        state["last_land"] = now
        elapsed = now - (state.get("tick_started") or now)
        delay = _anim_next_delay(state["target"], elapsed)
        Clock.schedule_once(lambda dt: self._anim_tick(state), delay)

    def _script_result_widgets(self, res, landed):
        """Widgets replacing a script placeholder after a run: the
        result on success, the error string on failure, the script's
        printed lines either way, then the drained GPU image (when
        there is one) and drain notes. Never blank, never a crash:
        a bad script lands as an error block, not an empty page. An
        animated block's image lands texture-only (no `source`), so
        the AsyncImage loader can never overwrite later frames with
        the first one; the file path is kept in `_anim_source` for
        the fullscreen tap. An interact block's image lands
        texture-only for the same reason (a tap re-run must show
        its own image), and an interact landing appends a hint
        line advertising the result as tappable."""
        widgets = []
        if res.get("ok"):
            widgets.append(self._micron_label(
                "[b]" + _escape(_format_script_value(res.get("result")))
                + "[/b]"))
        else:
            err = res.get("result")
            widgets.append(self._micron_label(
                "[b]script error:[/b] "
                + _escape(str(err) if err is not None
                          else "unknown error")))
        if landed.get("markup") and res.get("ok"):
            # A markup block's printed lines are micron source, not
            # literal text: re-parse and render them in place of
            # the code lines (see _markup_result_widgets). The
            # result widget above stays, and the GPU/audio widgets
            # below are unaffected. A failing script keeps the code
            # lines (the error block + lines printed before the
            # error), so a half-printed emit is never rendered as
            # markup.
            widgets.extend(self._markup_result_widgets(res, landed))
        else:
            for line in res.get("out") or []:
                widgets.append(self._micron_label(
                    _escape(str(line)), code=True))
        fpath = landed.get("gpu_fpath")
        if fpath:
            emb_cls = globals().get("MessageEmbed")
            if emb_cls is not None:
                try:
                    emb = emb_cls()
                    raw = (landed.get("gpu_raw")
                           if (landed.get("anim")
                               or landed.get("interact")) else None)
                    textured = False
                    if raw is not None:
                        try:
                            emb.texture = _rgb_bytes_to_texture(
                                raw, landed.get("gpu_size"))
                            textured = True
                        except Exception:
                            textured = False
                    if textured:
                        try:
                            emb._anim_source = fpath
                        except Exception:
                            pass
                    else:
                        emb.source = fpath
                    widgets.append(emb)
                except Exception as e:
                    widgets.append(self._micron_label(
                        "[i]GPU output unavailable: %s[/i]"
                        % _escape(str(e))))
            else:
                widgets.append(self._micron_label(
                    "[i]GPU output unavailable: no image widget[/i]"))
        for note in landed.get("gpu_notes") or []:
            widgets.append(self._micron_label(
                "[i]" + _escape(str(note)) + "[/i]"))
        # Script audio: only when the frame-0 run drained the audio
        # queue ("audio_count" in landed). The summary line plus
        # any drainer notes land like the GPU notes; the stop row
        # only appears when a WAV file was actually saved (nothing
        # to stop otherwise). There is deliberately no play button:
        # playback starts automatically on landing (see
        # _land_site_script) and stop is the only control.
        if "audio_count" in landed:
            try:
                count = int(landed.get("audio_count") or 0)
            except (TypeError, ValueError):
                count = 0
            try:
                duration = float(landed.get("audio_duration") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            widgets.append(self._micron_label(
                "[i]audio: %d notes, %.1f s[/i]" % (count, duration)))
            for note in landed.get("audio_notes") or []:
                widgets.append(self._micron_label(
                    "[i]" + _escape(str(note)) + "[/i]"))
            if landed.get("audio_fpath"):
                widgets.append(self._script_audio_row())
        if landed.get("interact"):
            # The block advertises itself as tappable; the hint
            # is rebuilt on every event landing along with the
            # rest of the widgets (see _land_interact_run).
            widgets.append(self._micron_label(
                "[i]tap the result to interact[/i]"))
        return widgets

    def _markup_result_widgets(self, res, landed):
        """Widgets for a markup block's successful run: the printed
        lines joined and re-parsed as micron source, rendered into
        a throwaway container and harvested in visual order. Script
        blocks in the emitted source are neutralized by
        micron_parse_emitted (shown as literal code, never
        executed), so an emitted script can never re-enter the
        renderer. The throwaway is a plain BoxLayout, not a
        SitePage, so an emitted `#!bg=` directive cannot restyle
        the page; links, media and headings otherwise follow the
        same rules as authored markup. Any failure falls back to
        the plain code lines, so a bad emit never blanks the
        landing; empty output renders nothing extra."""
        lines = res.get("out") or []
        text = "\n".join(str(line) for line in lines)
        if not text.strip():
            return []
        try:
            blocks = micron_parse_emitted(text)
            temp = BoxLayout(orientation="vertical")
            self._micron_blocks_into(temp, blocks, landed.get("cid"),
                                     media_ok=True)
            harvested = list(reversed(temp.children))
            # Kivy's add_widget refuses a widget that already has a
            # parent, so detach the harvest from the throwaway
            # before the landing inserts it into the page.
            temp.clear_widgets()
            return harvested
        except Exception:
            return [self._micron_label(_escape(text), code=True)]

    def _script_audio_row(self):
        """The stop control row for landed script audio: a status
        label plus a stop button in the _AudioAttachRow style. The
        button stops the currently playing script audio with the
        AudioRow stop idiom (app._audio_media: stop() + release()
        if present, then _audio_media None) and resets the row's
        own labels to their stopped state. When the row leaves its
        parent (page re-rendered / navigated away) playback stops
        too, if this row started it — script audio never keeps
        playing on a page the viewer left. No play button: playback
        is automatic on landing."""
        row = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=dp(40), spacing=dp(6))
        row._is_script_audio_row = True
        row._audio_started = False
        label = self._micron_label("audio playing")
        row._audio_label = label
        row.add_widget(label)
        try:
            from client_widgets import _fancy_button
            btn = _fancy_button("secondary", "stop", font_size=sp(11),
                                size_hint_x=None, width=dp(48))
        except Exception:
            from kivy.uix.button import Button
            btn = Button(text="stop", size_hint_x=None, width=dp(48))
        row._stop_button = btn

        def _stop_audio(pressed=None):
            mp = getattr(self, "_audio_media", None)
            if mp is not None:
                try:
                    mp.stop()
                    rel = getattr(mp, "release", None)
                    if rel is not None:
                        rel()
                except Exception:
                    pass
                try:
                    self._audio_media = None
                except Exception:
                    pass
            try:
                row._audio_started = False
            except Exception:
                pass
            try:
                row._audio_label.text = "audio stopped"
            except Exception:
                pass
            target = pressed if pressed is not None \
                else getattr(row, "_stop_button", None)
            if target is not None:
                try:
                    fancy = getattr(target, "_fancy_label", None)
                    if fancy is not None:
                        fancy.text = "stopped"
                    elif hasattr(target, "text"):
                        target.text = "stopped"
                except Exception:
                    pass

        row._stop_script_audio = _stop_audio
        btn.bind(on_release=_stop_audio)
        row.add_widget(btn)

        def _on_row_parent(inst, parent):
            # The row left its container: the page was re-rendered
            # or navigated away. Stop playback if this row started
            # it; a row that never started anything is a no-op.
            if parent is None and getattr(inst, "_audio_started",
                                          False):
                _stop_audio(getattr(inst, "_stop_button", None))

        row.bind(parent=_on_row_parent)
        return row

    def _open_site_editor(self):
        """Enter the micron editor for the page currently on screen."""
        cid, path = self._site_current or (None, None)
        if not cid or not path:
            self._set_status("no page on screen to edit")
            return
        src = getattr(self, "_site_src_text", None)
        if src is None:
            self._set_status("fetching page source…")

            def work():
                txt, err = None, "fetch failed"
                try:
                    res2 = self.guild_api.site_get(cid, path)
                    if res2.get("ok"):
                        txt = base64.b64decode(res2.get("data_b64") or "").decode(
                            "utf-8", "replace")
                    else:
                        err = res2.get("error", "fetch failed")
                except Exception as e2:
                    err = repr(e2)
                Clock.schedule_once(
                    lambda dt: self._enter_site_editor(cid, path, txt,
                                                       err=err), 0)
            threading.Thread(target=work, daemon=True).start()
            return
        self._enter_site_editor(cid, path, src)

    def _enter_site_editor(self, cid, path, src, err=None):
        if src is None:
            self._set_status("page source unavailable: %s" % (err or "unknown"))
            return
        self._edit_cid, self._edit_path = cid, path
        scr = self.root.get_screen("site_editor")
        scr._src.text = src
        scr._status.text = "editing %s" % path
        holder = scr._prev_holder
        holder.clear_widgets()
        holder.parent.scroll_y = 1.0
        self.root.current = "site_editor"

    def _open_new_site_page(self):
        """Empty-state creation door (tay's device report, 0.36.24): a
        permitted viewer lands on a not-found read of a site with no
        stored index; enter the editor with the same starter template
        the server seeds, keyed to the not-found target (_site_current)."""
        cid, path = getattr(self, "_site_current", None) or (None, None)
        if not cid or not path:
            self._set_status("no page target")
            return
        self._edit_cid, self._edit_path = cid, path
        scr = self.root.get_screen("site_editor")
        scr._src.text = self.SITE_STARTER_INDEX_MU
        scr._status.text = "creating %s" % path
        holder = scr._prev_holder
        holder.clear_widgets()
        holder.parent.scroll_y = 1.0
        self.root.current = "site_editor"

    def _site_editor_back(self):
        """Return to the rendered page; _site_current is still live."""
        if self.root is not None:
            self.root.current = "main"

    def _site_editor_preview(self):
        """Render the editor's source into the in-screen preview column;
        media runs stay text so the preview never reaches the network."""
        try:
            scr = self.root.get_screen("site_editor")
        except Exception:
            return
        blocks = micron_parse(scr._src.text)
        holder = scr._prev_holder
        holder.clear_widgets()
        page = SitePage()
        holder.add_widget(page)
        self._micron_blocks_into(page, blocks,
                                 getattr(self, "_edit_cid", None),
                                 media_ok=False)
        scr._status.text = "preview: %d blocks" % len(blocks)

    def _site_editor_publish_done(self, ok, err):
        try:
            scr = self.root.get_screen("site_editor")
        except Exception:
            return
        if ok:
            scr._status.text = "published"
            if self.root is not None:
                self.root.current = "main"
            self._site_load_page(self._edit_cid, self._edit_path)
        else:
            scr._status.text = "publish failed: %s" % err

    def _micron_label(self, markup, heading=False, center=False, code=False):
        # MicronRefLabel (fold-18, verbatim from the pre-split base): the
        # page labels are 2+ levels under the messages ScrollView, where
        # kivy's own Label ref check reads the delivered touch in the
        # scroll's content frame and misses the zones - the pre-split
        # MessageLabel carried the offset-chain conversion; the plain
        # label returned here after the split made every [ Edit page ]
        # ref dead (lane receipt: downs=1, refs=0).
        lbl = MicronRefLabel(markup=True, text=markup)
        lbl.size_hint_y = None
        lbl.font_size = sp(18) if heading else (sp(13) if code else sp(15))
        lbl.halign = "center" if center else "left"
        lbl.color = (0.62, 0.65, 0.7, 1) if code else (0.88, 0.9, 0.93, 1)
        lbl.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
        lbl.bind(texture_size=lambda inst, ts: setattr(
            inst, "height", max(dp(18), ts[1] if ts else 0)))
        lbl.bind(on_ref_press=lambda inst, ref: self._micron_link_tapped(ref))
        return lbl

    @staticmethod
    def _hex6_to_rgba(h):
        """'#88aa88' -> (r, g, b, 1.0) floats."""
        h = (h or "#").lstrip("#")
        if len(h) == 3:
            h = "".join(c + c for c in h)
        if len(h) != 6:
            return (0, 0, 0, 0)
        return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0, 1.0)

    def _add_site_media(self, container, cid, run):
        """A media-only line: images render inline (MessageEmbed, bytes
        fetched over the link), every other extension stays a tappable
        [ref]audio:[/ref] link label, exactly as before."""
        target = (run.get("link") or "").split("`")[0].strip()
        name = target.rsplit("/", 1)[-1] or "media"
        ext = micron_media_target(target)
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            container.add_widget(self._micron_label(
                "[ref=%s]audio: %s[/ref]" % (_escape(target), _escape(name))))
            return
        emb = MessageEmbed()
        emb.source = ""    # placeholder box until the bytes arrive
        container.add_widget(emb)

        def work():
            blob = _site_media_blob(self.guild_api, target)
            if not blob:
                return
            fpath = self._save_attachment_blob(blob, name)

            def _land(dt):
                emb.source = fpath
                # Polish 2 item 3: fade only once the texture has
                # landed. reveal() starts the fade at once, so on a
                # cold load it finished on the empty box and the real
                # image popped in after it. reveal_on_texture() holds
                # the embed at opacity 0 until on_load fires (or the
                # Loader-cached texture is already there).
                import ui_fx
                ui_fx.reveal_on_texture(emb)

            Clock.schedule_once(_land, 0)
        threading.Thread(target=work, daemon=True).start()

    def _micron_link_tapped(self, target):
        """Dispatch a micron link ref press: back/forward stack, the
        editor affordances, media download, intra-site pages, guild
        /page/... pages, external links, profile-node hashes, and
        relative targets resolved against the current page. Anything
        with no meaning here falls through to a status line."""
        target = (target or "").split("`")[0].strip()
        if target == "shades:back":
            self._site_go_back()
            return
        if target == "shades:forward":
            self._site_go_forward()
            return
        if target == "shades:site-edit":
            self._open_site_editor()
            return
        if target == "shades:site-create":
            self._open_new_site_page()
            return
        if not target:
            self._set_status("link: (empty)")
            return
        # Anchors: micron has no anchor syntax and nothing in the
        # codebase emits '#' targets (no '#' target in sHades.py,
        # micron.py, or md_to_micron) -- there is nothing to scroll
        # to, so an anchor target is an unknown link, not a feature.
        if target.startswith("#"):
            self._set_status("link: %s" % target)
            return
        if target.startswith(("http://", "https://")):
            try:
                webbrowser.open(target)
                self._set_status("opened %s" % target)
            except Exception as e:
                self._set_status("could not open link: %s" % e)
            return
        if re.match(r"^[0-9a-f]{32}$", target):
            self._open_profile_node(target)
            return
        if not target.startswith("/"):
            # Relative target: resolve against the current page, then
            # dispatch the absolute result through the same branches.
            cur = getattr(self, "_site_current", None)
            if not cur:
                self._set_status("link: %s" % target)
                return
            target = _resolve_micron_path(cur[1], target)
        else:
            target = _resolve_micron_path("/", target)
        if micron_media_target(target):
            self._site_download_media(target)
            return
        if target.startswith("/file/sites/") and target.endswith(".mu"):
            parts = target.split("/", 4)
            if len(parts) == 5 and re.match(r"^[0-9a-f]{64}$", parts[3]):
                self._site_load_page(parts[3], "/" + parts[4], push=True)
                return
        if target.startswith("/file/"):
            # A non-media file link (blob .pdf, site doc, attachment):
            # download it like media instead of a dead status line.
            self._site_download_media(target)
            return
        if target.startswith("/page/"):
            try:
                main = self.root.get_screen("main")
                name = getattr(main, "_current_channel", "general")
                self._site_load_page(self._channel_cid(name), target, push=True)
            except Exception:
                pass
            return
        # Any other absolute path is a resource of the site on screen.
        cur = getattr(self, "_site_current", None)
        if cur:
            if target.endswith(".mu"):
                self._site_load_page(cur[0], target, push=True)
                return
            self._site_download_site_resource(cur[0], target)
            return
        self._set_status("link: %s" % target)

    def _open_profile_node(self, target_hash):
        """A bare 32-hex link target is a profile *node* hash -- the
        guild's username_fragment emits exactly these as the target of
        a linked username. Resolve it through presence to a user hash
        + name when the member is known; otherwise hand the node hash
        itself to open_profile, which falls back to it as the lookup
        hash. An unknown node surfaces as open_profile's 'Node not
        found' status, not a dead status line."""
        username = ""
        user_hash = None
        try:
            for uh, info in (getattr(self, "_active_users", None)
                             or {}).items():
                if (info or {}).get("node_hash") == target_hash:
                    user_hash, username = uh, info.get("username") or ""
                    break
        except Exception:
            pass
        try:
            self.open_profile(user_hash or target_hash, username)
        except Exception as e:
            self._set_status("profile link failed: %s" % e)

    def _site_download_site_resource(self, cid, path):
        """Download a site resource addressed by site path (a relative
        link, or an absolute site path that is not a .mu page)."""
        name = path.rsplit("/", 1)[-1] or "file"
        self._set_status("downloading %s…" % name)

        def work():
            blob = None
            try:
                res = self.guild_api.site_get(cid, path)
                if res.get("ok"):
                    blob = base64.b64decode(res.get("data_b64") or "")
            except Exception as e:
                print("site resource download failed:", repr(e))
            if blob:
                fpath = self._save_attachment_blob(blob, name)
                Clock.schedule_once(
                    lambda dt: self._set_status("saved %s" % fpath), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._set_status("download failed: %s" % name),
                    0)
        threading.Thread(target=work, daemon=True).start()

    def _site_download_media(self, target):
        """Download a media target to the local attachments dir."""
        name = target.rsplit("/", 1)[-1] or "media"
        self._set_status("downloading %s…" % name)

        def work():
            blob = None
            try:
                if target.startswith("/file/sites/"):
                    parts = target.split("/", 4)
                    if len(parts) == 5:
                        res = self.guild_api.site_get(parts[3], "/" + parts[4])
                        if res.get("ok"):
                            blob = base64.b64decode(res.get("data_b64") or "")
                else:
                    raw = self.guild_api.fetch_file(target)
                    if raw:
                        blob = raw
            except Exception as e:
                print("site media download failed:", repr(e))
            if blob:
                fpath = self._save_attachment_blob(blob, name)
                Clock.schedule_once(
                    lambda dt: self._set_status("saved %s" % fpath), 0)
            else:
                Clock.schedule_once(
                    lambda dt: self._set_status("download failed: %s" % name), 0)
        threading.Thread(target=work, daemon=True).start()


# Widget classes that moved with this feature: they were
# defined under `if KIVY:` in kivy_client.py and stay that
# way here, so they only exist when Kivy does.
if KIVY:
    class MessageEmbed(ButtonBehavior, AsyncImage):
        """Clickable image embed - click to expand to full screen, click again to minimize.

        ButtonBehavior comes FIRST: with (AsyncImage, ButtonBehavior) the
        MRO put Widget.on_touch_down ahead of ButtonBehavior's, so the
        behavior never grabbed the touch and on_release (the fullscreen
        toggle) never fired from a real tap. Polish group 8b.
        """
        STANDARD_HEIGHT = dp(200)
        is_expanded = BooleanProperty(False)

        def on_release(self):
            app = App.get_running_app()
            if not app:
                return True
            if self.is_expanded:
                app.dismiss_fullscreen_image(self)
            else:
                # Animated embeds carry no `source` (texture-only, so
                # the loader cannot clobber later frames); the
                # frame-0 file path lives in _anim_source instead.
                source = self.source or getattr(self, "_anim_source", "")
                if not source:
                    return True
                app.show_fullscreen_image(source, embed=self)
                self.is_expanded = True
            return True

    class SitePage(BoxLayout):
        """One rendered micron page (site channels). bg_color paints the page
        canvas; the blocks are added as children by _render_site_page."""
        bg_color = ListProperty([0, 0, 0, 0])

        def __init__(self, **kw):
            super().__init__(**kw)
            self.orientation = "vertical"
            self.size_hint_y = None
            self.spacing = dp(4)
            self.padding = [dp(8), dp(8), dp(8), dp(8)]
            self.bind(minimum_height=lambda w, h: setattr(w, "height", h))
            with self.canvas.before:
                self._bg_instr = Color(rgba=self.bg_color)
                self._bg_rect = Rectangle(pos=self.pos, size=self.size)
            self.bind(pos=self._update_bg, size=self._update_bg,
                      bg_color=self._update_bg)

        def _update_bg(self, *a):
            self._bg_instr.rgba = self.bg_color
            self._bg_rect.pos = self.pos
            self._bg_rect.size = self.size

    class MicronSep(Widget):
        """A thin micron separator line."""

        def __init__(self, color=None, **kw):
            super().__init__(**kw)
            self.size_hint_y = None
            self.height = dp(2)
            app = App.get_running_app()
            if color is None:
                color = app.theme_colors.get(
                    'text_dim', (0.35, 0.37, 0.42, 1)) if app else (0.35, 0.37, 0.42, 1)
            with self.canvas:
                Color(*color)
                self._rect = Rectangle(pos=self.pos, size=self.size)
            self.bind(pos=self._up, size=self._up)

        def _up(self, *a):
            self._rect.pos = self.pos
            self._rect.size = self.size
