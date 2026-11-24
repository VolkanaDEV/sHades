"""sHades visual-effects layer: motion + atmosphere.

Everything here is cosmetic and fails soft: if an effect cannot run
(headless tests, odd GPU) the UI simply renders without it.

  * soft_texture()      cached radial-glow texture used by every orb/halo
  * AuroraBackground    three slow-drifting colour orbs behind a screen
  * reveal()            fade-in used for messages and screen sections
  * reveal_on_texture() fade an AsyncImage in once its texture lands
  * stagger()           reveal a list of widgets one after another
  * tap_feedback()      quick opacity dip on press
  * slide_property()    animate one numeric property (drawer widths)
  * popup_entrance()    open a Popup with a short opacity fade
  * ease constants      one motion language across the whole app
"""
from kivy.animation import Animation
from kivy.clock import Clock
from kivy.graphics import Color, Ellipse, Line, Rectangle, RoundedRectangle
from kivy.graphics.texture import Texture
from kivy.metrics import dp
from kivy.properties import ListProperty, NumericProperty, StringProperty
from kivy.uix.widget import Widget
from weakref import WeakSet
import random

EASE_OUT = "out_quint"       # things arriving
EASE_IN_OUT = "in_out_cubic"  # things drifting
EASE_SPRING = "out_back"     # small, playful settles

_TEX = {}
REDUCED_MOTION = False

# tay 2026-11-24: the popup opacity fade hides sheet button labels on
# the Android GLES path (recovery/restore sheets render clean on the
# desktop GL stack - probe shots z5/z6 - and the fade is the only
# phone-specific paint path in them). The fade stays off on Android
# entirely; desktop keeps it.
try:
    from kivy.utils import platform as _kivy_platform
    POPUP_FADE_SKIP = (_kivy_platform == "android")
except ImportError:
    POPUP_FADE_SKIP = False

# Screen fades use the same language as reveal(): short, out_quint ease.
SCREEN_TRANSITION_DURATION = 0.18

# Drawer slide + popup fade (polish group 3): same language, same rules
# (opacity/width only, fail-soft, instant under REDUCED_MOTION).
DRAWER_SLIDE_DURATION = 0.22
POPUP_FADE_DURATION = 0.15

# ScreenManagers that opted in via register_screen_manager(); WeakSet so a
# discarded manager never keeps the module (or itself) alive.
_SCREEN_MANAGERS = WeakSet()


def register_screen_manager(manager):
    """Track a ScreenManager so set_reduced_motion() retimes its fade.

    The manager's transition duration is set to the current motion state
    on registration, so a manager built while reduced motion is on starts
    instant instead of fading.
    """
    _SCREEN_MANAGERS.add(manager)
    _sync_manager_transition(manager)


def _sync_manager_transition(manager):
    transition = getattr(manager, "transition", None)
    if transition is not None and hasattr(transition, "duration"):
        transition.duration = 0 if REDUCED_MOTION else SCREEN_TRANSITION_DURATION


def set_reduced_motion(enabled):
    global REDUCED_MOTION
    REDUCED_MOTION = bool(enabled)
    for manager in list(_SCREEN_MANAGERS):
        _sync_manager_transition(manager)
    # Polish group 8: live SkeletonRows follow the flag too -- a row
    # built while motion was on stops shimmering the moment the flag
    # flips, and one built while it is off starts again.
    for row in list(SkeletonRow._instances):
        row._sync_motion()
    # Polish group 8b: the near-duplicate SkeletonRow in client_widgets
    # (history-loading skeleton, kivy_client._show_skeleton) follows the
    # flag the same way. Lazy import: client_widgets imports ui_fx at
    # module level, so a top-level import here would cycle.
    try:
        import client_widgets
        for row in list(client_widgets.SkeletonRow._instances):
            row._sync_motion()
    except Exception:
        pass


def drawer_open_width(screen_width, side):
    """Open width of a drawer for a screen of `screen_width`.

    Mirrors the KV formula the drawers used before the slide: the left
    (channels) drawer is 284dp capped at 80% of the screen, the right
    (members) drawer 200dp capped at 72%.
    """
    if side == "right":
        return min(dp(200), screen_width * 0.72)
    return min(dp(284), screen_width * 0.8)


def _anim_for(widget, prop):
    """Running Animation for `prop` on `widget`, or None."""
    try:
        uid = widget.uid
    except Exception:
        return None
    for anim in list(Animation._instances):
        entry = getattr(anim, "_widgets", {}).get(uid)
        if entry is not None and prop in entry.get("properties", {}):
            return anim
    return None


def is_animating(widget, prop):
    """True while an Animation is driving `prop` on `widget`."""
    return _anim_for(widget, prop) is not None


def slide_property(widget, prop, target, duration=DRAWER_SLIDE_DURATION):
    """Animate one numeric property (drawer widths live on MainScreen).

    REDUCED_MOTION lands at the target at once. A new slide cancels
    the running one for the same property, so a mid-slide toggle
    restarts from the current value and can never wedge half-open.
    Returns the Animation, or None when the value landed at once.
    """
    if REDUCED_MOTION:
        try:
            Animation.cancel_all(widget, prop)
        except Exception:
            pass
        try:
            setattr(widget, prop, target)
        except Exception:
            pass
        return None
    try:
        Animation.cancel_all(widget, prop)
        anim = Animation(**{prop: target}, duration=duration, t=EASE_OUT)
        anim.start(widget)
        return anim
    except Exception:
        try:
            setattr(widget, prop, target)
        except Exception:
            pass
        return None


def slide_width(widget, target, duration=DRAWER_SLIDE_DURATION):
    """Animate a widget's own width (convenience over slide_property)."""
    return slide_property(widget, "width", target, duration=duration)


def popup_entrance(popup, duration=POPUP_FADE_DURATION):
    """Open a Popup with a short opacity fade (ModalView: opacity only).

    The popup is set to opacity 0 before open() so no full-opacity
    frame flashes, then fades to 1. REDUCED_MOTION opens at opacity 1
    at once. A failed open() restores opacity 1 and re-raises, so a
    broken popup still fails loudly instead of failing silently.

    POPUP_FADE_SKIP (Android) opens at opacity 1 with no animation:
    on the phone GLES path the fade hides sheet button labels
    (tay 2026-11-24); the desktop probes z5/z6 show the same sheets
    render clean without any phone-specific path left but the fade.

    Every sheet/popup open also emits the draw-order receipt line
    (client_diag.report) and runs the texture-rebuild medicine, so the
    device log pins the paint order at the exact moment each sheet
    opens (tay's order verdict, tarball-2).
    """
    try:
        from kivy.app import App
        import client_diag
        app = App.get_running_app()
        if app is not None:
            client_diag.report(app, "sheet-open " + type(popup).__name__)
            client_diag.medicine(app, "sheet-open")
    except Exception:
        pass
    if REDUCED_MOTION or POPUP_FADE_SKIP:
        popup.opacity = 1
        popup.open()
        return
    popup.opacity = 0
    try:
        popup.open()
    except Exception:
        popup.opacity = 1
        raise
    try:
        Animation(opacity=1, duration=duration, t=EASE_OUT).start(popup)
    except Exception:
        popup.opacity = 1


def brand_wordmark(accent, primary):
    """Six small color steps give Kivy's text a restrained brand gradient."""
    name = "sHades"
    parts = []
    for index, letter in enumerate(name):
        t = index / (len(name) - 1)
        rgb = tuple(max(0, min(255, int(255 * (a * (1 - t) + b * t))))
                    for a, b in zip(accent[:3], primary[:3]))
        parts.append("[color=%02x%02x%02x]%s[/color]" % (*rgb, letter))
    return "".join(parts)


def gradient_texture():
    """Small reusable teal-to-amber brand accent, not a full-screen fill."""
    if "gradient" not in _TEX:
        buf = bytearray()
        for i in range(128):
            t = i / 127
            buf.extend((int(88 + 157 * t), int(214 - 31 * t),
                        int(195 - 100 * t), 255))
        tex = Texture.create(size=(128, 1), colorfmt="rgba")
        tex.blit_buffer(bytes(buf), colorfmt="rgba", bufferfmt="ubyte")
        tex.mag_filter = tex.min_filter = "linear"
        _TEX["gradient"] = tex
    return _TEX["gradient"]


def grain_texture():
    """Deterministic tiny tile to soften dark gradient banding."""
    if "grain" not in _TEX:
        rng = random.Random(0x5ADE5)
        buf = bytearray()
        for _ in range(64 * 64):
            value = rng.randrange(64, 192)
            buf.extend((value, value, value, 255))
        tex = Texture.create(size=(64, 64), colorfmt="rgba")
        tex.blit_buffer(bytes(buf), colorfmt="rgba", bufferfmt="ubyte")
        tex.wrap = "repeat"
        tex.mag_filter = tex.min_filter = "nearest"
        _TEX["grain"] = tex
    return _TEX["grain"]


def soft_texture(size=128):
    """White radial falloff (alpha 1 centre -> 0 edge), cached."""
    tex = _TEX.get(size)
    if tex is not None:
        return tex
    half = size / 2.0
    buf = bytearray()
    for y in range(size):
        for x in range(size):
            d = (((x - half) ** 2 + (y - half) ** 2) ** 0.5) / half
            a = max(0.0, 1.0 - d)
            a = int(255 * a * a * (3 - 2 * a))  # smoothstep
            buf += bytes((255, 255, 255, a))
    tex = Texture.create(size=(size, size), colorfmt="rgba")
    tex.blit_buffer(bytes(buf), colorfmt="rgba", bufferfmt="ubyte")
    tex.mag_filter = tex.min_filter = "linear"
    _TEX[size] = tex
    return tex


class AuroraBackground(Widget):
    """Ambient glow; only the visible, active screen drifts.

    Colours come from the theme (aurora_a/b/c) so a custom theme recolours it.
    """
    colors = ListProperty([(0.96, 0.72, 0.37, 0.16),
                           (0.35, 0.84, 0.77, 0.13),
                           (0.50, 0.40, 0.95, 0.14)])
    phase = NumericProperty(0.0)
    _instances = WeakSet()
    _motion_enabled = True
    _battery_saver = False

    def __init__(self, **kw):
        super().__init__(**kw)
        self._orbs = []
        self._anim = None
        self._instances.add(self)
        with self.canvas:
            for rgba in self.colors:
                c = Color(*rgba)
                e = Ellipse(texture=soft_texture(), size=(1, 1))
                self._orbs.append((c, e))
        with self.canvas.after:
            Color(1, 1, 1, 0.025)
            self._grain = Rectangle(texture=grain_texture(), pos=self.pos,
                                    size=self.size)
        self.bind(pos=self._layout, size=self._layout, phase=self._layout,
                  colors=self._recolor)
        Clock.schedule_once(lambda dt: self._sync(), 0)

    @classmethod
    def set_motion(cls, enabled, battery_saver=False):
        cls._motion_enabled = enabled
        cls._battery_saver = battery_saver
        for instance in list(cls._instances):
            instance._sync()

    def on_parent(self, *_args):
        Clock.schedule_once(lambda dt: self._sync(), 0)

    def _visible_screen(self):
        node = self.parent
        while node is not None:
            manager = getattr(node, "manager", None)
            if manager is not None and hasattr(node, "name"):
                return manager.current == node.name
            node = node.parent
        return False

    def _sync(self):
        self.opacity = 0 if self._battery_saver else 1
        if self._motion_enabled and not self._battery_saver and self._visible_screen():
            if self._anim is None:
                self._start()
        else:
            self._stop()

    # orbit centres as fractions of the widget, per orb: (cx, cy, rx, ry, k)
    _PATHS = ((0.22, 0.80, 0.12, 0.07, 1.0),
              (0.82, 0.22, 0.10, 0.09, -0.8),
              (0.55, 0.50, 0.16, 0.10, 0.6))

    def _layout(self, *a):
        import math
        w, h = self.size
        self._grain.pos = self.pos
        self._grain.size = self.size
        self._grain.tex_coords = (0, 0, w / 64, 0, w / 64, h / 64, 0, h / 64)
        if w <= 1 or h <= 1:
            return
        r = max(w, h) * 0.62
        for (c, e), (cx, cy, rx, ry, k) in zip(self._orbs, self._PATHS):
            t = self.phase * 6.2831853 * k
            x = self.x + w * (cx + rx * math.cos(t))
            y = self.y + h * (cy + ry * math.sin(t * 1.3))
            e.size = (r, r)
            e.pos = (x - r / 2, y - r / 2)

    def _recolor(self, *a):
        for (c, _e), rgba in zip(self._orbs, self.colors):
            c.rgba = rgba

    def _start(self):
        if not self._motion_enabled or self._battery_saver or not self._visible_screen():
            self._stop()
            return
        self._stop()
        self.phase = 0.0
        self._anim = Animation(phase=1.0, duration=48, t="linear")
        self._anim.bind(on_complete=lambda *_: self._start())
        self._anim.start(self)

    def _stop(self):
        if self._anim is not None:
            self._anim.cancel(self)
            self._anim = None


def reveal(widget, delay=0.0, duration=0.42):
    """Fade a widget in. Layout-safe: only opacity moves."""
    if REDUCED_MOTION:
        widget.opacity = 1
        return
    try:
        widget.opacity = 0

        def go(dt):
            Animation(opacity=1, duration=duration,
                      t=EASE_OUT).start(widget)
        if delay:
            Clock.schedule_once(go, delay)
        else:
            go(0)
    except Exception:
        widget.opacity = 1


def reveal_on_texture(widget, delay=0.0, duration=0.42):
    """Fade a widget in once its image texture has actually landed.

    Plain reveal() starts the fade the moment it is called, but an
    AsyncImage loads its texture on a background thread: the fade can
    finish on the empty box (or the 32x32 loading placeholder) and
    the real image then pops in after it. This waits for the load
    instead: the widget sits at opacity 0 until the texture arrives,
    then fades, so the fade and the image land together.

    Why on_load, not on_texture: setting an AsyncImage source fires
    on_texture at once for the loading-placeholder texture, so
    binding the fade to on_texture would still fade the placeholder.
    on_load fires only when the real texture is assigned (AsyncImage
    dispatches it from _on_source_load), and ProxyImage.loaded tells
    whether the texture is already there -- a Loader-cached source
    hands back a loaded proxy and never fires on_load again.

    Already loaded (cached source, plain Image, empty/missing source
    with no proxy to wait on) fades at once, exactly like reveal().
    A load error also fades at once -- a failed image must not leave
    a permanently invisible hole. REDUCED_MOTION lands at opacity 1
    at once, like reveal(). Returns None; when the fade is deferred
    there is no Animation yet to return.
    """
    if REDUCED_MOTION:
        try:
            widget.opacity = 1
        except Exception:
            pass
        return None
    try:
        proxy = getattr(widget, "_coreimage", None)
        waiting = (proxy is not None
                   and not getattr(proxy, "loaded", True)
                   and getattr(widget, "source", None))
    except Exception:
        waiting = False
    if not waiting:
        return reveal(widget, delay=delay, duration=duration)
    try:
        widget.opacity = 0
    except Exception:
        return reveal(widget, delay=delay, duration=duration)
    state = {"done": False}

    def _fire(*args):
        if state["done"]:
            return
        state["done"] = True
        handler = getattr(widget, "_ui_fx_reveal_handler", None)
        if handler is not None:
            try:
                widget.unbind(on_load=handler, on_error=handler)
            except Exception:
                pass
            try:
                del widget._ui_fx_reveal_handler
            except Exception:
                pass
        reveal(widget, delay=delay, duration=duration)

    try:
        old = getattr(widget, "_ui_fx_reveal_handler", None)
        if old is not None:
            try:
                widget.unbind(on_load=old, on_error=old)
            except Exception:
                pass
        widget._ui_fx_reveal_handler = _fire
        widget.bind(on_load=_fire, on_error=_fire)
    except Exception:
        return reveal(widget, delay=delay, duration=duration)
    return None


def stagger(widgets, step=0.06, start=0.05, duration=0.45):
    for i, w in enumerate(widgets):
        reveal(w, delay=start + i * step, duration=duration)


def tap_feedback(widget):
    """Quick dip-and-return so every press feels acknowledged."""
    if REDUCED_MOTION:
        return
    try:
        Animation.cancel_all(widget, "opacity")
        (Animation(opacity=0.55, duration=0.06) +
         Animation(opacity=1, duration=0.28, t=EASE_OUT)).start(widget)
    except Exception:
        pass


def haptic():
    """One short Android tap; desktop and unavailable vibrator fail quietly."""
    try:
        from kivy.utils import platform
        if platform != "android":
            return
        from jnius import autoclass
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        service = autoclass("android.content.Context").VIBRATOR_SERVICE
        vibrator = activity.getSystemService(service)
        if vibrator and vibrator.hasVibrator():
            vibrator.vibrate(14)
    except Exception:
        pass


class MeshPulse(Widget):
    """A path-state ring; pace and colour reflect the observed guild link."""
    mode = StringProperty("offline")
    phase = NumericProperty(0)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.size_hint = (None, None)
        self.size = (dp(40), dp(40))
        self.bind(pos=self._draw, size=self._draw, mode=self._draw,
                  phase=self._draw)
        self._pulse_clock = Clock.schedule_interval(self._tick, 0.1)

    def _tick(self, dt):
        if REDUCED_MOTION:
            return
        speed = 0.0 if self.mode == "offline" else (0.11 if self.mode == "relay" else 0.25)
        self.phase = (self.phase + speed * dt) % 1.0

    def _draw(self, *a):
        from math import sin, pi
        palette = {"direct": (0.35, 0.84, 0.77),
                   "relay": (0.96, 0.72, 0.37),
                   "offline": (0.38, 0.43, 0.48)}
        rgb = palette.get(self.mode, palette["offline"])
        wave = 0.0 if self.mode == "offline" else (sin(self.phase * 2 * pi) + 1) / 2
        self.canvas.clear()
        with self.canvas:
            Color(*rgb, 0.17 + 0.20 * wave)
            Line(circle=(self.center_x, self.center_y, min(self.width, self.height) * 0.42),
                 width=dp(4))
            Color(*rgb, 0.85)
            Line(circle=(self.center_x, self.center_y, min(self.width, self.height) * 0.34),
                 width=dp(1.4))
            Color(*rgb, 0.8)
            Ellipse(pos=(self.center_x - dp(3), self.center_y - dp(3)),
                    size=(dp(6), dp(6)))


class MeshConstellation(Widget):
    """A compact path diagram. Dots are capped when the route is longer."""
    mode = StringProperty("offline")
    hops = NumericProperty(0)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.size_hint = (None, None)
        self.size = (dp(52), dp(20))
        self.bind(pos=self._draw, size=self._draw, mode=self._draw,
                  hops=self._draw)

    def _draw(self, *a):
        count = max(2, min(5, int(self.hops) + 1))
        rgb = ((0.35, 0.84, 0.77) if self.mode == "direct"
               else (0.96, 0.72, 0.37) if self.mode == "relay"
               else (0.38, 0.43, 0.48))
        points = [(self.x + dp(5) + i * (self.width - dp(10)) / (count - 1),
                   self.center_y + (dp(3) if i % 2 else -dp(3)))
                  for i in range(count)]
        self.canvas.clear()
        with self.canvas:
            Color(*rgb, 0.4 if self.mode != "offline" else 0.2)
            Line(points=[c for p in points for c in p], width=dp(1))
            for i, (x, y) in enumerate(points):
                Color(*rgb, 0.85 if self.mode != "offline" or i == 0 else 0.3)
                Ellipse(pos=(x - dp(2.5), y - dp(2.5)), size=(dp(5), dp(5)))


class SkeletonRow(Widget):
    """Two quiet shimmer bars while a history page is on its way.

    Polish group 8: the shimmer honours REDUCED_MOTION -- with it on,
    the bars render statically (no repeating phase animation), the
    same treatment reveal() and AuroraBackground give the flag. A row
    built while reduced motion is on never starts the animation; one
    already shimmering stops the moment set_reduced_motion() flips.
    """
    phase = NumericProperty(0)
    _instances = WeakSet()

    def __init__(self, **kw):
        super().__init__(**kw)
        self.size_hint_y = None
        self.height = dp(70)
        self.bind(pos=self._draw, size=self._draw, phase=self._draw)
        self._anim = None
        self._instances.add(self)
        self._sync_motion()

    def _sync_motion(self):
        if REDUCED_MOTION:
            self._stop()
        else:
            self._start()

    def _start(self):
        if REDUCED_MOTION:
            return
        self._stop()
        self._anim = Animation(phase=1, duration=1.6, t="linear")
        self._anim.repeat = True
        self._anim.start(self)

    def _stop(self):
        if self._anim is not None:
            self._anim.cancel(self)
            self._anim = None

    def _draw(self, *a):
        self.canvas.clear()
        width = max(dp(40), self.width - dp(72))
        with self.canvas:
            Color(0.64, 0.75, 0.76, 0.12)
            Ellipse(pos=(self.x + dp(12), self.y + dp(18)), size=(dp(34), dp(34)))
            RoundedRectangle(pos=(self.x + dp(58), self.y + dp(44)),
                             size=(width * 0.45, dp(10)), radius=[dp(5)] * 4)
            RoundedRectangle(pos=(self.x + dp(58), self.y + dp(21)),
                             size=(width * 0.8, dp(10)), radius=[dp(5)] * 4)
            # Short highlight moves within the body bar.
            Color(0.70, 0.92, 0.89, 0.13)
            RoundedRectangle(pos=(self.x + dp(58) + width * 0.64 * self.phase,
                                  self.y + dp(21)),
                             size=(width * 0.16, dp(10)), radius=[dp(5)] * 4)
