#!/usr/bin/env python3
"""Polish 2 item 3: image-reveal texture pop -- fade on texture load.

Before this fix _add_site_media landed the embed source and called
ui_fx.reveal() at once. reveal() fades opacity 0 -> 1 over 0.42s, but
an AsyncImage loads its texture on a background thread, so on a cold
load the fade finished on the empty box (or the 32x32 loading
placeholder) and the real image popped in after it. Measured before
the fix, cold cache: fade pumped to opacity 1 while the texture was
still the 32x32 placeholder.

The fix is ui_fx.reveal_on_texture(), used by _add_site_media: the
embed holds at opacity 0 with no animation until the texture lands,
then fades, so the fade and the image land together.

Why on_load, not on_texture: setting an AsyncImage source fires
on_texture at once for the loading-placeholder texture, so binding
the fade to on_texture would still fade the placeholder. on_load
fires only when the real texture is assigned, and ProxyImage.loaded
tells whether the texture is already there (a Loader-cached source
hands back a loaded proxy and never fires on_load again).

Asserts STATE, never wall-clock timing. The load event is driven
deterministically by dispatching on_load on the embed (the event
Kivy fires when the real texture lands); one bounded setup loop
lets the real Loader finish so the test can also prove the real
texture -- not the placeholder -- is present when the fade runs,
and that a second embed for the same file takes the cached,
already-loaded branch. The fetch thread is deferred (never started)
until the test releases it, so the placeholder is observable
mid-flight. The animation is pumped by explicit _update(dt) calls,
so no real time has to elapse for the fade itself.
"""
import binascii
import functools
import os
import struct
import sys
import tempfile
import time
import types
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_site  # noqa: E402
import ui_fx  # noqa: E402
from kivy.animation import Animation  # noqa: E402

PASS, FAIL = [], []

def _make_png(width, height, rgb=(200, 30, 40)):
    """A small valid PNG, stdlib only (zlib + CRCs).

    The 1px fixture other embed tests share is undecodable -- PIL
    reports "broken data stream" and SDL2 "Unable to load image",
    so the Loader never delivers a texture for it and any check
    that needs a real load must not use it.
    """
    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", binascii.crc32(tag + data)
                                 & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (sig + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG_VALID = _make_png(16, 12)
PNG_SIZE = (16, 12)

PATH = "/file/attachments/abc.png"


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


class _NowClock(object):
    @staticmethod
    def schedule_once(fn, dt=0):
        fn(0)


class _DeferredThread(object):
    """A thread that never runs until the test releases it."""
    pending = []

    def __init__(self, target=None, daemon=False, **k):
        self._target = target

    def start(self):
        _DeferredThread.pending.append(self._target)


def _release_threads():
    pending, _DeferredThread.pending = _DeferredThread.pending, []
    for target in pending:
        target()


def _anim_for(widget, prop):
    for anim in list(Animation._instances):
        entry = getattr(anim, "_widgets", {}).get(widget.uid)
        if entry is not None and prop in entry.get("properties", {}):
            return anim
    return None


def _step(widget, prop, dt):
    anim = _anim_for(widget, prop)
    if anim is None:
        return False
    anim._update(dt)
    return True


def _pump_to_end(widget, prop):
    for _ in range(10):
        if _anim_for(widget, prop) is None:
            break
        _step(widget, prop, 0.15)
    return widget.opacity


def _tex_size(widget):
    tex = getattr(widget, "texture", None)
    return None if tex is None else tex.size


def _wait_loaded(widget, tries=100):
    """Bounded setup wait for the real Loader to finish a texture.

    State assertions only -- this waits for the load to happen, it
    never asserts anything about how long it took.
    """
    from kivy.clock import Clock
    for _ in range(tries):
        proxy = getattr(widget, "_coreimage", None)
        if proxy is not None and getattr(proxy, "loaded", False):
            return True
        Clock.tick()
        time.sleep(0.05)
    return False


class FakeRow(object):
    def __init__(self, msg_id):
        from kivy.uix.boxlayout import BoxLayout
        self.msg_id = msg_id
        self.ids = types.SimpleNamespace(
            embed_container=BoxLayout(orientation="vertical"))
        self._loading_placeholder = None


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.clock import Clock
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label
    from kivy.uix.widget import Widget
    from app_site import MessageEmbed

    png_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    png_file.write(PNG_VALID)
    png_file.close()
    png_path = png_file.name
    extra_pngs = [png_path]

    def fresh_png():
        """A new temp file with the same bytes: a cold Loader cache."""
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.write(PNG_VALID)
        f.close()
        extra_pngs.append(f.name)
        return f.name
    attach_dir = tempfile.mkdtemp(prefix="shades-reveal-test-")
    holder = types.SimpleNamespace(
        _attachments_dir=lambda: attach_dir,
        _attachment_kind=kc.sHadesClientApp._attachment_kind,
        _set_status=lambda text: None)
    holder._save_attachment_blob = functools.partial(
        kc.sHadesClientApp._save_attachment_blob, holder)
    holder._add_loading_placeholder = functools.partial(
        kc.sHadesClientApp._add_loading_placeholder, holder)
    holder._loading_placeholder_text = (
        kc.sHadesClientApp._loading_placeholder_text)
    holder._remove_placeholder = functools.partial(
        kc.sHadesClientApp._remove_placeholder, holder)

    def site_ns():
        site = types.SimpleNamespace(
            guild_api=types.SimpleNamespace(
                fetch_file=lambda p: PNG_VALID if p == PATH else None),
            _save_attachment_blob=holder._save_attachment_blob,
            _set_status=lambda text: None)
        site._add_site_media = types.MethodType(
            app_site.SiteMixin._add_site_media, site)
        return site

    real_site_clock, real_site_thread = (app_site.Clock,
                                         app_site.threading)
    app_site.Clock = _NowClock
    app_site.threading = types.SimpleNamespace(Thread=_DeferredThread)
    try:
        ui_fx.set_reduced_motion(False)

        # --- 1. non-image widget: behaves exactly like reveal() -----
        plain = Widget()
        ui_fx.reveal_on_texture(plain)
        check("plain widget starts at opacity 0",
              plain.opacity == 0, repr(plain.opacity))
        check("plain widget has a running opacity animation",
              _anim_for(plain, "opacity") is not None)
        check("plain widget lands at opacity 1",
              _pump_to_end(plain, "opacity") == 1, repr(plain.opacity))

        # --- 2. unloaded texture: waits, then fades on on_load -------
        # A fresh path is a genuinely unloaded proxy: the Loader only
        # marks it loaded on a Clock tick, and this section ticks
        # none, so the wait is deterministic without stubbing.
        emb = MessageEmbed(source=fresh_png(), size_hint=(None, None),
                           size=(200, 200))
        check("fresh embed proxy is not loaded yet",
              getattr(getattr(emb, "_coreimage", None), "loaded", None)
              is False)
        ui_fx.reveal_on_texture(emb)
        check("unloaded embed holds at opacity 0",
              emb.opacity == 0, repr(emb.opacity))
        check("unloaded embed starts no animation while waiting",
              _anim_for(emb, "opacity") is None)
        check("pumping a waiting embed leaves it at opacity 0",
              _pump_to_end(emb, "opacity") == 0, repr(emb.opacity))
        emb.dispatch("on_load")
        check("on_load starts the fade",
              _anim_for(emb, "opacity") is not None)
        _step(emb, "opacity", 0.21)  # first update arms; opacity still 0
        check("embed still 0 after the arming update",
              emb.opacity == 0, repr(emb.opacity))
        _step(emb, "opacity", 0.21)  # halfway through the 0.42s fade
        check("embed is mid-fade (0 < opacity < 1)",
              0 < emb.opacity < 1, repr(emb.opacity))
        check("embed lands at opacity 1",
              _pump_to_end(emb, "opacity") == 1, repr(emb.opacity))
        check("embed animation finished after landing",
              _anim_for(emb, "opacity") is None)
        emb.dispatch("on_load")
        check("a second on_load does not restart the fade",
              emb.opacity == 1
              and _anim_for(emb, "opacity") is None,
              repr(emb.opacity))

        # --- 3. load error: fades anyway, never stays invisible -----
        bad = MessageEmbed(source=fresh_png(), size_hint=(None, None),
                           size=(200, 200))
        ui_fx.reveal_on_texture(bad)
        check("failed embed holds at opacity 0 while waiting",
              bad.opacity == 0
              and _anim_for(bad, "opacity") is None,
              repr(bad.opacity))
        bad.dispatch("on_error", Exception("boom"))
        check("on_error starts the fade",
              _anim_for(bad, "opacity") is not None)
        check("failed embed lands at opacity 1",
              _pump_to_end(bad, "opacity") == 1, repr(bad.opacity))

        # --- 4. already-loaded texture: fades at once ----------------
        # Let section 2's embed really load, then a second embed for
        # the same file gets the Loader-cached, already-loaded proxy.
        check("section 2 embed really loads",
              _wait_loaded(emb),
              repr(getattr(getattr(emb, "_coreimage", None),
                           "loaded", None)))
        cached = MessageEmbed(source=emb.source, size_hint=(None, None),
                              size=(200, 200))
        check("cached embed proxy is already loaded",
              getattr(getattr(cached, "_coreimage", None), "loaded", None)
              is True)
        ui_fx.reveal_on_texture(cached)
        check("loaded embed starts at opacity 0",
              cached.opacity == 0, repr(cached.opacity))
        check("loaded embed has a running opacity animation",
              _anim_for(cached, "opacity") is not None)
        check("loaded embed lands at opacity 1",
              _pump_to_end(cached, "opacity") == 1,
              repr(cached.opacity))

        # --- 5. no proxy (empty source): fades at once ---------------
        empty = MessageEmbed()
        check("empty embed has no proxy to wait on",
              getattr(empty, "_coreimage", None) is None)
        ui_fx.reveal_on_texture(empty)
        check("empty embed starts at opacity 0",
              empty.opacity == 0, repr(empty.opacity))
        check("empty embed has a running opacity animation",
              _anim_for(empty, "opacity") is not None)
        check("empty embed lands at opacity 1",
              _pump_to_end(empty, "opacity") == 1, repr(empty.opacity))

        # --- 6. REDUCED_MOTION: lands at once, never waits -----------
        ui_fx.set_reduced_motion(True)
        try:
            still = MessageEmbed(source=fresh_png(),
                                 size_hint=(None, None),
                                 size=(200, 200))
            ui_fx.reveal_on_texture(still)
            check("reduced-motion embed is opacity 1 at once",
                  still.opacity == 1, repr(still.opacity))
            check("reduced-motion embed starts no animation",
                  _anim_for(still, "opacity") is None)
            check("reduced-motion embed waits on nothing",
                  getattr(still, "_ui_fx_reveal_handler", None) is None)
            still.dispatch("on_load")
            check("reduced-motion embed ignores a late on_load",
                  still.opacity == 1
                  and _anim_for(still, "opacity") is None,
                  repr(still.opacity))
        finally:
            ui_fx.set_reduced_motion(False)

        # --- 7. called twice while waiting: still one fade -----------
        twice = MessageEmbed(source=fresh_png(), size_hint=(None, None),
                             size=(200, 200))
        ui_fx.reveal_on_texture(twice)
        ui_fx.reveal_on_texture(twice)
        check("double call still holds at opacity 0",
              twice.opacity == 0
              and _anim_for(twice, "opacity") is None,
              repr(twice.opacity))
        twice.dispatch("on_load")
        check("double call fades on a single on_load",
              _anim_for(twice, "opacity") is not None)
        check("double call lands at opacity 1",
              _pump_to_end(twice, "opacity") == 1,
              repr(twice.opacity))

        # --- 8. real site media, cold cache: waits for the texture ---
        site = site_ns()
        container = BoxLayout(orientation="vertical")
        site._add_site_media(container, "c" * 64, {"link": PATH})
        semb = container.children[0]
        check("site embed starts as an empty box",
              semb.source == "", repr(semb.source))
        check("site embed starts at opacity 1 (no reveal yet)",
              semb.opacity == 1, repr(semb.opacity))
        _release_threads()
        check("site embed lands its source",
              semb.source.endswith("abc.png")
              and os.path.exists(semb.source),
              repr(semb.source))
        check("site embed holds at opacity 0 on landing",
              semb.opacity == 0, repr(semb.opacity))
        check("site embed starts no animation before the texture",
              _anim_for(semb, "opacity") is None)
        check("site embed still shows the loading placeholder",
              _tex_size(semb) == (32, 32), repr(_tex_size(semb)))
        check("site embed proxy is not loaded yet",
              getattr(getattr(semb, "_coreimage", None), "loaded", None)
              is False)
        check("pumping before the texture leaves opacity 0",
              _pump_to_end(semb, "opacity") == 0, repr(semb.opacity))
        semb.dispatch("on_load")
        check("site embed fades once the texture arrives",
              _anim_for(semb, "opacity") is not None)
        _step(semb, "opacity", 0.21)
        _step(semb, "opacity", 0.21)
        check("site embed is mid-fade after the texture arrives",
              0 < semb.opacity < 1, repr(semb.opacity))
        check("site embed lands at opacity 1",
              _pump_to_end(semb, "opacity") == 1, repr(semb.opacity))
        check("real Loader finishes the texture",
              _wait_loaded(semb),
              repr(getattr(getattr(semb, "_coreimage", None),
                           "loaded", None)))
        check("real texture is the image, not the placeholder",
              _tex_size(semb) == PNG_SIZE, repr(_tex_size(semb)))
        check("real load does not restart the fade",
              semb.opacity == 1
              and _anim_for(semb, "opacity") is None,
              repr(semb.opacity))

        # --- 9. real site media, cached: fades at once ---------------
        container2 = BoxLayout(orientation="vertical")
        site._add_site_media(container2, "c" * 64, {"link": PATH})
        semb2 = container2.children[0]
        _release_threads()
        check("cached site embed lands its source",
              semb2.source.endswith("abc.png"), repr(semb2.source))
        check("cached site embed already holds the real texture",
              _tex_size(semb2) == PNG_SIZE, repr(_tex_size(semb2)))
        check("cached site embed starts at opacity 0",
              semb2.opacity == 0, repr(semb2.opacity))
        check("cached site embed fades at once",
              _anim_for(semb2, "opacity") is not None)
        check("cached site embed lands at opacity 1",
              _pump_to_end(semb2, "opacity") == 1,
              repr(semb2.opacity))

        # --- 10. real site media, reduced motion lands at once -------
        ui_fx.set_reduced_motion(True)
        try:
            container3 = BoxLayout(orientation="vertical")
            site._add_site_media(container3, "c" * 64, {"link": PATH})
            semb3 = container3.children[0]
            _release_threads()
            check("reduced-motion site embed lands at opacity 1",
                  semb3.opacity == 1, repr(semb3.opacity))
            check("reduced-motion site embed starts no animation",
                  _anim_for(semb3, "opacity") is None)
        finally:
            ui_fx.set_reduced_motion(False)

        # --- 11. skeleton placeholder behaviour is kept --------------
        row = FakeRow("m-ph")
        holder._add_loading_placeholder(row, "shot.png")
        ph = row._loading_placeholder
        check("image placeholder is still a 200px skeleton box",
              ph.height == 200
              and _skeleton_of(ph) is not None,
              "%s h=%r" % (type(ph).__name__,
                           getattr(ph, "height", None)))
        holder._remove_placeholder(row)
        check("image placeholder still removes cleanly",
              row._loading_placeholder is None
              and row.ids.embed_container.children == [])
        row2 = FakeRow("m-ph-aud")
        holder._add_loading_placeholder(row2, "song.mp3")
        ph2 = row2._loading_placeholder
        check("audio placeholder is still the text label",
              isinstance(ph2, Label)
              and ph2.text == "[loading audio…]",
              repr(getattr(ph2, "text", None)))
        holder._remove_placeholder(row2)
        check("audio placeholder still removes cleanly",
              row2._loading_placeholder is None
              and row2.ids.embed_container.children == [])
    finally:
        app_site.Clock = real_site_clock
        app_site.threading = real_site_thread
        ui_fx.set_reduced_motion(False)
        for p in extra_pngs:
            try:
                os.unlink(p)
            except Exception:
                pass
        # Let the real Loader thread finish before the process exits.
        for _ in range(20):
            Clock.tick()
            time.sleep(0.01)

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


def _skeleton_of(placeholder):
    """The SkeletonRow inside an image placeholder box, or None."""
    if isinstance(placeholder, ui_fx.SkeletonRow):
        return placeholder
    for kid in getattr(placeholder, "children", []):
        if isinstance(kid, ui_fx.SkeletonRow):
            return kid
    return None


if __name__ == "__main__":
    sys.exit(main())
