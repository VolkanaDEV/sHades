#!/usr/bin/env python3
"""Polish round 2, item 1: the volume slider is themed.

_AudioAttachRow.vol stays a plain Kivy Slider (the audio-row test
pins the type) but is themed in place: a 6dp track in input_bg over
the default 36sp grey bar, a primary-amber fill bar to value_pos,
and a primary-amber knob Ellipse (primary-hover, slightly larger,
while pressed) over the shrunken default cursor image. The slider
still drives the player's volume, stays 40px tall, and re-tints live
when theme_colors is reassigned (DESIGN.md hex fallbacks when a key
is missing).

Asserts STATE, never wall-clock timing: configuration, canvas.after
instructions and colours, geometry after layout, value propagation
to both media backends, pressed state via a real dispatched touch,
and live re-tint / fallback. Harness modelled on
tests/test_polish_i_audio_row.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.metrics import dp  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from kivy.uix.slider import Slider  # noqa: E402
from kivy.utils import get_color_from_hex  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "  | " + str(extra)))


def _close(a, b, tol=0.01):
    try:
        return len(a) == len(b) and all(
            abs(float(x) - float(y)) <= tol for x, y in zip(a, b))
    except Exception:
        return False


class StubMedia(object):
    """Media with setVolume (Android MediaPlayer path)."""

    def __init__(self):
        self.calls = []

    def setVolume(self, left, right):
        self.calls.append((left, right))

    def stop(self):
        pass

    def release(self):
        pass


class VolumeAttrMedia(object):
    """Media with only a volume attribute (desktop SoundLoader path)."""

    def __init__(self):
        self.volume = None

    def stop(self):
        pass

    def release(self):
        pass


class StubPlayer(object):
    """Minimal stand-in for sHadesClientApp's audio side."""

    def __init__(self, media=None):
        self._audio_media = None
        self._media = media if media is not None else StubMedia()
        self.played = []

    def _play_audio(self, path):
        self.played.append(path)
        self._audio_media = self._media
        return self._media


def _canvas_kinds(widget):
    return [type(c).__name__ for c in widget.canvas.after.children]


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.core.window import Window  # noqa: F401  (touch tests need it)
    from kivy.lang import Builder
    from kivy.tests.common import UnitTestTouch
    from client_widgets import _AudioAttachRow

    Builder.load_file("kv_md.kv")

    class _ThemeApp(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)

    app = _ThemeApp()
    theme = kc.DEFAULT_THEME_COLORS

    player = StubPlayer()
    parent = BoxLayout(orientation="vertical", size=(400, 200),
                       size_hint=(None, None))
    row = _AudioAttachRow(path="/tmp/song.mp3", name="song.mp3",
                          size=50000, player=player)
    parent.add_widget(row)
    parent.do_layout()
    # The parent's layout only positions the row; the row's own
    # layout (which sizes the slider to the 40px row) is a deferred
    # Clock trigger, so run it synchronously here.
    row.do_layout()
    vol = row.vol

    # --- 1. still a plain Kivy Slider, same geometry -------------------
    check("volume control is still a plain Kivy Slider",
          isinstance(vol, Slider) and type(vol).__name__ == "Slider",
          repr(type(vol).__name__))
    check("volume slider keeps its 80dp width",
          vol.width == dp(80), repr(vol.width))
    check("audio row stays 40px tall", row.height == dp(40),
          repr(row.height))
    check("slider fills the 40px row height (touch target)",
          vol.height == dp(40), repr(vol.height))
    check("volume slider keeps its 0.8 default value",
          vol.value == 0.8, repr(vol.value))

    # --- 2. themed configuration: no default blue ----------------------
    check("track bar shrinks from the 36sp default to 6dp",
          vol.background_width == dp(6),
          repr(vol.background_width))
    check("default 32sp cursor image shrinks to 12dp",
          list(vol.cursor_size) == [dp(12), dp(12)],
          repr(list(vol.cursor_size)))
    kinds = _canvas_kinds(vol)
    check("canvas.after holds 3 Color instructions",
          kinds.count("Color") == 3, repr(kinds))
    check("canvas.after holds 2 RoundedRectangle instructions",
          kinds.count("RoundedRectangle") == 2, repr(kinds))
    check("canvas.after holds 1 Ellipse knob",
          kinds.count("Ellipse") == 1, repr(kinds))
    check("track colour is the theme input_bg",
          _close(row._vol_track_color.rgba, theme["input_bg"]),
          repr(row._vol_track_color.rgba))
    check("fill colour is the theme primary",
          _close(row._vol_fill_color.rgba, theme["primary"]),
          repr(row._vol_fill_color.rgba))
    check("knob colour is the theme primary at rest",
          _close(row._vol_knob_color.rgba, theme["primary"]),
          repr(row._vol_knob_color.rgba))
    check("knob is amber, not the default blue (red beats blue)",
          row._vol_knob_color.rgba[0] > row._vol_knob_color.rgba[2],
          repr(row._vol_knob_color.rgba))
    check("knob covers the shrunken cursor image (corner to corner)",
          row._vol_knob.size[0] >= vol.cursor_width * 1.414,
          repr((row._vol_knob.size, vol.cursor_width)))

    # --- 3. geometry after layout --------------------------------------
    pad = vol.padding
    left = vol.x + pad
    track_w = vol.width - pad * 2
    check("track rect spans the slider minus padding",
          _close(row._vol_track_rect.pos,
                 (left, vol.center_y - dp(6) / 2), 0.6)
          and _close(row._vol_track_rect.size, (track_w, dp(6)), 0.6),
          repr((row._vol_track_rect.pos, row._vol_track_rect.size)))
    check("fill rect runs from the track start to value_pos at 0.8",
          _close(row._vol_fill_rect.pos,
                 (left, vol.center_y - dp(6) / 2), 0.6)
          and abs(row._vol_fill_rect.size[0] - 0.8 * track_w) < 1.0,
          repr((row._vol_fill_rect.pos, row._vol_fill_rect.size)))
    check("knob is 20dp, centred on value_pos",
          _close(row._vol_knob.size, (dp(20), dp(20)), 0.6)
          and _close(row._vol_knob.pos,
                     (vol.value_pos[0] - dp(10),
                      vol.center_y - dp(10)), 0.6),
          repr((row._vol_knob.pos, row._vol_knob.size)))

    # --- 4. value still drives the volume ------------------------------
    player._audio_media = player._media
    vol.value = 0.3
    check("setting value 0.3 calls setVolume(0.3, 0.3)",
          player._media.calls == [(0.3, 0.3)],
          repr(player._media.calls))
    check("fill shrinks with the value",
          abs(row._vol_fill_rect.size[0] - 0.3 * track_w) < 1.0,
          repr(row._vol_fill_rect.size))
    check("knob follows the value",
          abs(row._vol_knob.pos[0] + dp(10) - vol.value_pos[0]) < 1.0,
          repr((row._vol_knob.pos, vol.value_pos)))
    vol.value = 0.0
    check("value 0 empties the fill",
          row._vol_fill_rect.size[0] == 0,
          repr(row._vol_fill_rect.size))
    vol.value = 1.0
    check("value 1 fills the whole track",
          abs(row._vol_fill_rect.size[0] - track_w) < 1.0,
          repr(row._vol_fill_rect.size))
    check("knob stays inside the slider at value 1",
          row._vol_knob.pos[0] + dp(20) <= vol.right + 1.0,
          repr(row._vol_knob.pos))
    vol.value = 0.8

    player2 = StubPlayer(media=VolumeAttrMedia())
    row2 = _AudioAttachRow(path="/tmp/tune.mp3", name="tune.mp3",
                           size=1000, player=player2)
    parent.add_widget(row2)
    parent.do_layout()
    row2.do_layout()
    player2._audio_media = player2._media
    row2.vol.value = 0.45
    check("volume-attribute media gets volume 0.45",
          player2._media.volume == 0.45,
          repr(player2._media.volume))

    row3 = _AudioAttachRow(path="/tmp/orphan.mp3", name="orphan.mp3",
                           size=1000, player=None)
    try:
        row3.vol.value = 0.5
        check("no-player row tolerates a value change",
              row3.vol.value == 0.5)
    except Exception as e:
        check("no-player row tolerates a value change", False, repr(e))

    # --- 5. pressed state ------------------------------------------------
    row._set_vol_pressed(True)
    check("pressed knob turns primary-hover",
          _close(row._vol_knob_color.rgba,
                 get_color_from_hex("#FFD090")),
          repr(row._vol_knob_color.rgba))
    check("pressed knob grows to 22dp",
          _close(row._vol_knob.size, (dp(22), dp(22)), 0.6),
          repr(row._vol_knob.size))
    row._set_vol_pressed(False)
    check("released knob returns to primary",
          _close(row._vol_knob_color.rgba, theme["primary"]),
          repr(row._vol_knob_color.rgba))
    check("released knob returns to 20dp",
          _close(row._vol_knob.size, (dp(20), dp(20)), 0.6),
          repr(row._vol_knob.size))

    # a real dispatched touch on the knob: pressed + value follow.
    # UnitTestTouch.pos stays (0, 0) until the touch goes through the
    # Window's dispatch (scale_for_screen), so scale it here first --
    # a direct dispatch never scales it. The grab is asserted via the
    # grab list: grab_current is only set by post_dispatch_input for
    # the direct delivery to the grabbed widget, never during a
    # direct dispatch.
    kx, ky = vol.value_pos[0], vol.center_y
    touch = UnitTestTouch(kx, ky)
    touch.scale_for_screen(Window.width, Window.height)
    row.dispatch("on_touch_down", touch)
    check("touch down on the knob marks the slider pressed",
          row._vol_pressed is True)
    check("touch down grabs the slider",
          any(w() is vol for w in touch.grab_list),
          repr(touch.grab_list))
    check("touch down on the knob keeps the value",
          abs(vol.value - 0.8) < 0.03, repr(vol.value))
    check("touch down brightens the knob",
          _close(row._vol_knob_color.rgba,
                 get_color_from_hex("#FFD090")),
          repr(row._vol_knob_color.rgba))
    row._vol_pressed = False  # cleared mid-drag (second-finger up)
    row.on_touch_move(touch)
    check("move while grabbed re-arms the pressed knob",
          row._vol_pressed is True)
    row.dispatch("on_touch_up", touch)
    check("touch up clears the pressed knob",
          row._vol_pressed is False)
    check("touch up restores the primary knob",
          _close(row._vol_knob_color.rgba, theme["primary"]),
          repr(row._vol_knob_color.rgba))

    # touch down on the track start moves the value there
    touch2 = UnitTestTouch(left + 2, vol.center_y)
    touch2.scale_for_screen(Window.width, Window.height)
    row.dispatch("on_touch_down", touch2)
    check("touch down near the track start drops the value",
          vol.value < 0.1, repr(vol.value))
    row.dispatch("on_touch_up", touch2)
    vol.value = 0.8

    # --- 6. live re-tint + DESIGN.md fallbacks --------------------------
    old = dict(app.theme_colors)
    new = dict(old)
    new["primary"] = (1, 0, 0, 1)
    new["input_bg"] = (0, 1, 0, 1)
    app.theme_colors = new
    check("fill re-tints when theme primary changes",
          _close(row._vol_fill_color.rgba, (1, 0, 0, 1)),
          repr(row._vol_fill_color.rgba))
    check("track re-tints when theme input_bg changes",
          _close(row._vol_track_color.rgba, (0, 1, 0, 1)),
          repr(row._vol_track_color.rgba))
    check("knob re-tints when theme primary changes",
          _close(row._vol_knob_color.rgba, (1, 0, 0, 1)),
          repr(row._vol_knob_color.rgba))
    app.theme_colors = old
    check("restoring the theme restores the amber fill",
          _close(row._vol_fill_color.rgba, theme["primary"]),
          repr(row._vol_fill_color.rgba))
    app.theme_colors = {}
    check("empty theme falls back to DESIGN.md input_bg for the track",
          _close(row._vol_track_color.rgba,
                 get_color_from_hex("#172231")),
          repr(row._vol_track_color.rgba))
    check("empty theme falls back to DESIGN.md primary for the fill",
          _close(row._vol_fill_color.rgba,
                 get_color_from_hex("#F5B75F")),
          repr(row._vol_fill_color.rgba))
    check("empty theme falls back to DESIGN.md primary for the knob",
          _close(row._vol_knob_color.rgba,
                 get_color_from_hex("#F5B75F")),
          repr(row._vol_knob_color.rgba))
    row._set_vol_pressed(True)
    check("pressed knob falls back to DESIGN.md primary-hover",
          _close(row._vol_knob_color.rgba,
                 get_color_from_hex("#FFD090")),
          repr(row._vol_knob_color.rgba))
    row._set_vol_pressed(False)
    app.theme_colors = old

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
