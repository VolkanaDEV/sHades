#!/usr/bin/env python3
"""Polish (h2): dead fullscreen tap + second SkeletonRow reduced-motion gate.

Follow-on to polish group 8, fixing two defects that group found:

1. MessageEmbed was (AsyncImage, ButtonBehavior): the MRO put
   Widget.on_touch_down ahead of ButtonBehavior's, so the behavior
   never grabbed a real touch and on_release (the fullscreen toggle)
   never fired from a tap. Only a direct emb.on_release() call worked,
   which is why tests/test_embed_features.py never caught it. The fix
   swaps the bases to (ButtonBehavior, AsyncImage). This test proves
   the fix with a genuine touch: a MouseMotionEvent (the concrete
   touch class Kivy's own mouse provider builds) sent through
   EventLoop.post_dispatch_input -- the exact pipeline the SDL provider
   feeds -- so the Window dispatches on_touch_down, ButtonBehavior
   grabs, and the grabbed on_touch_up (grab_current set by the event
   loop, as in real input) fires on_release. Window.dispatch() alone
   is NOT enough: it never sets grab_current, so on_touch_up ignores
   the up-event. No direct on_release() call anywhere in this file.
   (Consequential find, pinned in section 3: _add_attachment_embed
   binds on_release to self._status(...), but the real app has no
   _status method -- only _set_status. The dead MRO hid it, and
   group 8's StubApp masked it with `_status = _set_status`. The
   binding now names _set_status; without that one-token fix a real
   tap raised AttributeError out of the tap dispatch and killed the
   app -- the live probe caught it.)
2. client_widgets.SkeletonRow (history-loading skeleton, used by
   _show_skeleton) started its repeating 1.6s phase animation
   unconditionally. Group 8 gated only ui_fx.SkeletonRow. This file
   gates the second one the same way: WeakSet of live instances +
   start/stop/sync, synced from ui_fx.set_reduced_motion (lazy import
   in ui_fx, since client_widgets imports ui_fx at module level).

Asserts STATE, never wall-clock timing. Deterministic under xvfb-run.
"""
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client_widgets  # noqa: E402
import ui_fx  # noqa: E402
from kivy.animation import Animation  # noqa: E402

PASS, FAIL = [], []

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082")


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.app import App
    from kivy.base import EventLoop
    from kivy.core.window import Window
    from kivy.input.providers.mouse import MouseMotionEvent
    from kivy.lang import Builder
    from kivy.metrics import dp
    from kivy.uix.behaviors import ButtonBehavior
    from kivy.uix.widget import Widget
    from app_site import MessageEmbed

    Builder.load_file("kv_md.kv")
    Window.size = (800, 600)

    # --- 1. MRO: ButtonBehavior must win over Widget ------------------
    mro = MessageEmbed.__mro__
    check("MRO has ButtonBehavior before Widget",
          mro.index(ButtonBehavior) < mro.index(Widget),
          [c.__name__ for c in mro])
    check("MessageEmbed.on_touch_down resolves to ButtonBehavior's",
          MessageEmbed.on_touch_down.__qualname__
          == "ButtonBehavior.on_touch_down",
          MessageEmbed.on_touch_down.__qualname__)
    check("MRO still has AsyncImage for texture loading",
          "AsyncImage" in [c.__name__ for c in mro],
          [c.__name__ for c in mro])

    # Real app instance so the embed's on_release calls the real
    # show/dismiss_fullscreen_image (overlay parented to Window).
    app = kc.sHadesClientApp()

    def tap(x, y):
        """One genuine tap through Kivy's real input pipeline."""
        touch = MouseMotionEvent(
            "test", 1,
            (x / Window.width, y / Window.height, "left"),
            is_touch=True)
        touch.scale_for_screen(Window.width, Window.height)
        EventLoop.post_dispatch_input("begin", touch)
        EventLoop.post_dispatch_input("end", touch)
        return touch

    def overlay():
        return getattr(app, "_fullscreen_overlay", None)
    orig_get_app = App.get_running_app
    App.get_running_app = staticmethod(lambda: app)
    png_path = None
    emb = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(PNG_1PX)
            png_path = f.name
        emb = MessageEmbed(source=png_path, size_hint=(None, None),
                           size=(200, 200), pos=(100, 100))
        check("embed keeps its source (AsyncImage path intact)",
              emb.source == png_path, repr(emb.source))
        check("embed keeps STANDARD_HEIGHT",
              emb.STANDARD_HEIGHT == dp(200), repr(emb.STANDARD_HEIGHT))
        check("embed starts collapsed", emb.is_expanded is False)
        Window.add_widget(emb)

        # A touch that misses the embed entirely does nothing.
        tap(700, 500)
        check("non-colliding touch opens nothing",
              overlay() is None and emb.is_expanded is False,
              "overlay=%r expanded=%r" % (overlay(), emb.is_expanded))

        # A tap on the embed opens fullscreen (via the grab + on_release
        # path, not a direct call): overlay on the Window, flag set.
        tap(200, 200)
        check("tap on embed opens fullscreen overlay",
              overlay() is not None, "overlay=%r" % (overlay(),))
        check("fullscreen overlay is parented to Window",
              overlay() is not None and overlay().parent is Window,
              "parent=%s" % (type(overlay().parent).__name__
                             if overlay() is not None else None))
        check("embed is_expanded after tap", emb.is_expanded is True)

        # A second tap at the same spot -- delivered THROUGH the open
        # overlay, which takes no touch itself -- dismisses it.
        tap(200, 200)
        check("second tap dismisses fullscreen overlay",
              overlay() is None, "overlay=%r" % (overlay(),))
        check("embed is_expanded cleared after dismiss",
              emb.is_expanded is False)

        # The toggle still works after a full open/dismiss cycle.
        tap(200, 200)
        check("tap re-opens fullscreen after dismiss",
              overlay() is not None and emb.is_expanded is True)
        tap(200, 200)
        check("tap dismisses again", overlay() is None
              and emb.is_expanded is False)
    finally:
        try:
            if emb is not None and emb.parent is not None:
                emb.parent.remove_widget(emb)
        except Exception:
            pass
        try:
            if getattr(app, "_fullscreen_overlay", None) is not None:
                app.dismiss_fullscreen_image(
                    getattr(app, "_fullscreen_overlay").embed)
        except Exception:
            pass
        App.get_running_app = orig_get_app
        if png_path and os.path.exists(png_path):
            os.unlink(png_path)

    # --- 3. message-embed tap binding calls a method the app has -----
    # _add_attachment_embed binds on_release to self._status(...), but
    # the real app never had a _status method -- only _set_status. The
    # dead MRO hid this: no real tap ever reached the binding (group 8's
    # StubApp masked it with `_status = _set_status`). With the bases
    # swapped, a tap dispatches on_release to the class handler AND
    # this binding, so the binding must name the real method. Proved
    # here with a real tap on an embed built by the real method, on
    # the real app instance -- before the fix this raised
    # AttributeError out of the tap dispatch.
    from kivy.uix.boxlayout import BoxLayout
    attach_dir = tempfile.mkdtemp(prefix="shades-h2-attach-")
    status_seen = []
    orig_set_status = app._set_status
    app._set_status = lambda text: status_seen.append(text)
    app._attachments_dir = lambda: attach_dir
    App.get_running_app = staticmethod(lambda: app)
    emb3 = None
    try:
        row3 = types.SimpleNamespace(ids=types.SimpleNamespace(
            embed_container=BoxLayout(orientation="vertical")))
        app._add_attachment_embed(row3, "shot.png", PNG_1PX)
        emb3 = row3.ids.embed_container.children[0]
        check("real _add_attachment_embed builds a MessageEmbed",
              isinstance(emb3, MessageEmbed))
        emb3.size_hint = (None, None)
        emb3.size = (200, 200)
        emb3.pos = (100, 100)
        # It was built into the row's container; re-parent to the Window
        # so a real tap through the input pipeline can reach it.
        row3.ids.embed_container.remove_widget(emb3)
        Window.add_widget(emb3)
        tap(200, 200)
        check("tap on real message embed opens fullscreen",
              overlay() is not None and emb3.is_expanded is True,
              "overlay=%r expanded=%r" % (overlay(), emb3.is_expanded))
        check("tap on real message embed writes the status line",
              status_seen == ["shot.png (0 KB)"], repr(status_seen))
        tap(200, 200)
        check("second tap dismisses it again",
              overlay() is None and emb3.is_expanded is False)
    finally:
        try:
            if emb3 is not None and emb3.parent is not None:
                emb3.parent.remove_widget(emb3)
        except Exception:
            pass
        try:
            if overlay() is not None:
                app.dismiss_fullscreen_image(overlay().embed)
        except Exception:
            pass
        app._set_status = orig_set_status
        try:
            del app._attachments_dir
        except Exception:
            pass
        App.get_running_app = orig_get_app
        shutil.rmtree(attach_dir, ignore_errors=True)

    # --- 2. client_widgets.SkeletonRow reduced-motion gate -----------
    check("kivy_client.SkeletonRow is the client_widgets one",
          kc.SkeletonRow is client_widgets.SkeletonRow)
    ui_fx.set_reduced_motion(False)
    try:
        row = client_widgets.SkeletonRow()
        check("skeleton shimmers while motion is on",
              ui_fx.is_animating(row, "phase") is True)
        check("skeleton row keeps its own _anim while shimmering",
              row._anim is not None)
        check("skeleton row is tracked in its WeakSet",
              row in client_widgets.SkeletonRow._instances)

        ui_fx.set_reduced_motion(True)
        check("live skeleton stops under reduced motion",
              ui_fx.is_animating(row, "phase") is False)
        check("stopped skeleton holds no animation",
              row._anim is None)
        row2 = client_widgets.SkeletonRow()
        check("skeleton built under reduced motion never starts",
              ui_fx.is_animating(row2, "phase") is False
              and row2._anim is None)
        check("static skeleton phase stays 0", row2.phase == 0,
              repr(row2.phase))
        # _remove_skeleton cancels animations on rows it drops; a
        # stopped row must survive that call.
        try:
            Animation.cancel_all(row)
            Animation.cancel_all(row2)
            cancelled_ok = True
        except Exception as e:  # noqa: BLE001 - cancel must not raise
            cancelled_ok = False
            print("cancel raised: %r" % (e,))
        check("Animation.cancel_all on stopped rows raises nothing",
              cancelled_ok)

        ui_fx.set_reduced_motion(False)
        check("skeleton restarts when reduced motion flips off",
              ui_fx.is_animating(row, "phase") is True)
        check("skeleton built under reduced motion starts too",
              ui_fx.is_animating(row2, "phase") is True)

        # Group 8's ui_fx.SkeletonRow gate is not regressed by the
        # second row's sync hook.
        ui_fx.set_reduced_motion(True)
        fx_row = ui_fx.SkeletonRow()
        check("ui_fx.SkeletonRow still static under reduced motion",
              ui_fx.is_animating(fx_row, "phase") is False)
        ui_fx.set_reduced_motion(False)
        check("ui_fx.SkeletonRow still restarts",
              ui_fx.is_animating(fx_row, "phase") is True)
    finally:
        ui_fx.set_reduced_motion(False)

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
