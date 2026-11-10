#!/usr/bin/env python3
"""PFP crop regression tests for the sHades Kivy client.

Bug: the saved circular pfp was ALWAYS cropped from the lower-left of the
source image, whatever circle the user picked.

Root cause: _save_pfp_crop() mixed two coordinate spaces. The crop circle was
converted with `circle_x * editor_w` (editor-relative) while the image rect
(img.pos / img.size) is in absolute window pixels. Subtracting an absolute
image origin from an editor-relative circle centre shifted every crop by the
editor's position inside the popup — always down and to the left.

The tests below drive the real code path (PFPEditorWidget in a Popup ->
Save button -> _save_pfp_crop) over a synthetic 4-quadrant image and assert
the saved pfp contains the quadrant the user actually selected.

Usage:  /usr/bin/python tests/test_pfp_crop.py
"""
import os
import sys
import tempfile
import functools

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


def make_quadrant_image(path, w=800, h=600):
    """red top-left, green top-right, blue bottom-left, yellow bottom-right."""
    from PIL import Image as PILImage, ImageDraw
    im = PILImage.new("RGB", (w, h), "black")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w // 2, h // 2], fill=(255, 0, 0))
    d.rectangle([w // 2, 0, w, h // 2], fill=(0, 255, 0))
    d.rectangle([0, h // 2, w // 2, h], fill=(0, 0, 255))
    d.rectangle([w // 2, h // 2, w, h], fill=(255, 255, 0))
    im.save(path)
    return path


def quadrant(rgb):
    """Map a pixel to a quadrant name using nearest reference colour."""
    ref = {
        (255, 0, 0): "red", (0, 255, 0): "green",
        (0, 0, 255): "blue", (255, 255, 0): "yellow",
    }
    r, g, b = rgb[:3]
    best, bestd = "unknown", 10 ** 9
    for refc, name in ref.items():
        d = sum((a - c) ** 2 for a, c in zip((r, g, b), refc))
        if d < bestd:
            best, bestd = name, d
    return best


class StubGuild:
    def avatar_upload(self, blob):
        return {"ok": True}


class StubApp:
    """Minimal stand-in for sHadesClientApp (no RNS/guild needed)."""

    def __init__(self, kivy_client, data_dir):
        self.kc = kivy_client
        self.guild_api = StubGuild()
        self.my_hash = "a" * 32
        self.my_avatar_orig = None
        self.my_avatar_small = None
        self._msg_widgets = {}
        self.theme_colors = dict(kivy_client.DEFAULT_THEME_COLORS)
        # real crop implementation, bound to this stub
        self._save_pfp_crop = functools.partial(
            kivy_client.sHadesClientApp._save_pfp_crop, self)
        self._save_pfp_crop_impl = functools.partial(
            kivy_client.sHadesClientApp._save_pfp_crop_impl, self)

    def _set_status(self, text):
        pass


def find_popup(window, popup_cls):
    for w in window.children:
        if isinstance(w, popup_cls):
            return w
    return None


def find_editor(popup, editor_cls):
    """The editor is the first child of the popup's vertical content box."""
    content = popup.content
    for child in content.children:
        if isinstance(child, editor_cls):
            return child
    return None


def find_save_button(popup):
    def walk(w):
        yield w
        for c in w.children:
            yield from walk(c)
    for w in walk(popup):
        if getattr(w, "text", "") == "Save PFP":
            return w
    return None


kc_popup_cls = None
kc_editor_cls = None


def main():
    try:
        import kivy_client as kc
    except Exception as e:  # no display / no Kivy -> nothing to test here
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.clock import Clock
    from kivy.core.window import Window
    from kivy.lang import Builder
    from kivy.uix.popup import Popup

    # The KV rules live in kv_md.kv (the app loads that file; there is no
    # inline KV string any more). PFPEditorWidget is defined there.
    Builder.load_file(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "kv_md.kv"))

    # KV rules read app.theme_colors[...], which Kivy resolves through
    # App.get_running_app(); the KV proxy also calls app.bind(). With no app
    # running, building any themed widget raises
    # "'NoneType' object has no attribute 'bind'". sHadesClientApp is an MDApp
    # (KivyMD widgets refuse to build under a plain kivy.app.App), constructs
    # without RNS, and we pin the palette for determinism.
    class _ThemeApp(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)

    _ThemeApp()

    tmp = tempfile.mkdtemp(prefix="shades_pfp_test_")
    kc._app_data_dir = lambda: tmp
    img_path = make_quadrant_image(os.path.join(tmp, "src.png"))

    app = StubApp(kc, tmp)

    def save_with_circle(cx, cy, radius, label):
        """Open the editor, place the circle, press Save, return the quadrant."""
        kc.sHadesClientApp._open_pfp_editor(app, img_path)
        for _ in range(6):
            Clock.tick()
        popup = find_popup(Window, Popup)
        if popup is None:
            check(label + ": popup opened", False, "no popup in window")
            return None
        editor = find_editor(popup, kc.PFPEditorWidget)
        if editor is None:
            check(label + ": editor present", False, "editor not found")
            return None
        editor.circle_x = cx
        editor.circle_y = cy
        editor.circle_radius = radius
        Clock.tick()
        btn = find_save_button(popup)
        if btn is None:
            check(label + ": save button present", False, "button not found")
            return None
        btn.dispatch("on_press")
        for _ in range(6):
            Clock.tick()
        if not app.my_avatar_small:
            check(label + ": pfp written", False, "no output file")
            return None
        from PIL import Image as PILImage
        crop = PILImage.open(app.my_avatar_small).convert("RGBA")
        return quadrant(crop.getpixel((128, 128))), crop

    # --- circle in the top-right of the editor -> top-right of the image ---
    res = save_with_circle(0.75, 0.75, 0.2, "circle top-right")
    if res:
        check("circle top-right crops the top-right quadrant",
              res[0] == "green", res[0])

    # --- circle in the bottom-left of the editor -> bottom-left of image ---
    res = save_with_circle(0.25, 0.25, 0.2, "circle bottom-left")
    if res:
        check("circle bottom-left crops the bottom-left quadrant",
              res[0] == "blue", res[0])

    # --- circle in the middle -> the four quadrants meet in the crop ---
    res = save_with_circle(0.5, 0.5, 0.2, "circle centre")
    if res:
        crop = res[1]
        tl = quadrant(crop.getpixel((40, 40)))
        tr = quadrant(crop.getpixel((215, 40)))
        bl = quadrant(crop.getpixel((40, 215)))
        br = quadrant(crop.getpixel((215, 215)))
        check("circle centre crops the image centre (red/green/blue/yellow)",
              (tl, tr, bl, br) == ("red", "green", "blue", "yellow"),
              (tl, tr, bl, br))

    # --- pure maths: absolute circle vs absolute image rect -----------------
    # Editor at (82, 121) 636x375, image widget at (162.5, 138.875) 475x356.25
    # (the geometry the popup actually produces). A circle at the editor's
    # centre must land on the image's centre.
    class _App:
        guild_api = StubGuild()
        my_hash = ""
        my_avatar_orig = None
        my_avatar_small = None
        _msg_widgets = {}

        def _set_status(self, text):
            pass
        # call the impl directly (the wrapper catches exceptions)
        _save_pfp_crop_impl = kc.sHadesClientApp._save_pfp_crop_impl

    a = _App()
    a._save_pfp_crop_impl(
        path=img_path,
        crop_cx=82 + 0.5 * 636,
        crop_cy=121 + 0.5 * 375,
        crop_r=0.25 * 375,
        image_pos=(162.5, 138.875),
        image_size=(475, 356.25),
    )
    from PIL import Image as PILImage
    c = PILImage.open(a.my_avatar_small).convert("RGBA")
    tl = quadrant(c.getpixel((40, 40)))
    tr = quadrant(c.getpixel((215, 40)))
    bl = quadrant(c.getpixel((40, 215)))
    br = quadrant(c.getpixel((215, 215)))
    check("absolute circle+image rect map to the image centre",
          (tl, tr, bl, br) == ("red", "green", "blue", "yellow"),
          (tl, tr, bl, br))

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
