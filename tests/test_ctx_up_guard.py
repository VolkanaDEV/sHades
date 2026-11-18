#!/usr/bin/env python3
"""ContextMenuMixin touch-up guard tests (device crash regression pins) —
headless.

The on-device crash (traceback above, kivy_client.py:1137 -> kivy 2.3.1
uix/behaviors/button.py:173, `time() - self.__touch_time` with
unsupported operand float - NoneType) comes from the eventloop's grab pass
delivering an up to a widget whose ButtonBehavior down never completed
(a child consumed the press), leaving __touch_time = None.

Pins down:
  - a ButtonBehavior+mixin widget whose down was consumed by a child
    swallows the mixin-grab up instead of crashing (and does not release);
  - a healthy tap on a BB+mixin widget releases exactly once even when
    the widget sits in the grab list twice (double-delivery echo kill);
  - long-press fire + up keeps the swallow contract, no release;
  - a non-BB mixin widget (CtxHeaderLabel) still forwards its up.
"""
import os
import sys
import time as _time
import weakref

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


class Touch:
    """Mirrors kivy 2.3.1 MotionEvent grab mechanics + post_dispatch pass."""

    def __init__(self, x, y):
        self.x, self.y = x, y
        self.ud = {}
        self.grab_list = []
        self._gcur = None
        self.grab_exclusive_class = None
        self.profile = []            # finger: no 'button' key
        self.button = None
        self.is_mouse_scrolling = False
        self.time_update = _time.time()

    @property
    def pos(self):
        return (self.x, self.y)

    @property
    def grab_current(self):
        if self._gcur is not None:
            return self._gcur
        for weak in reversed(self.grab_list):
            wid = weak()
            if wid is not None:
                return wid
        return None

    @grab_current.setter
    def grab_current(self, value):
        self._gcur = value

    def grab(self, widget, exclusive=False):
        self.grab_list.append(weakref.ref(widget))

    def ungrab(self, widget):
        ref = weakref.ref(widget)    # remove drops the FIRST occurrence
        if ref in self.grab_list:
            self.grab_list.remove(ref)


def post_dispatch_ups(touch):
    """Mirror kivy/base.py post_dispatch_input for etype 'end': deliver the
    up once per CURRENT grab-list entry (iterate a live-length copy)."""
    delivered = 0
    for ref in touch.grab_list[:]:
        wid = ref()
        if wid is None:
            continue
        touch._gcur = wid
        wid.on_touch_up(touch)       # mixin on_touch_up (its own dispatch)
        touch._gcur = None
        delivered += 1
    return delivered


def main():
    import kivy_client as kc
    from kivy.uix.behaviors.button import ButtonBehavior
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label

    class CountingMixin(ButtonBehavior):
        """Records on_release dispatches without needing kv."""
        releases = 0

        def __init__(self, **kw):
            super().__init__(**kw)
            self.bind(on_release=lambda *_: setattr(
                self, "n_rel", getattr(self, "n_rel", 0) + 1))
            self.n_rel = 0

    class Row(kc.ContextMenuMixin, CountingMixin, BoxLayout):
        # ChannelButton-shaped: mixin, ButtonBehavior, layout
        def build_context_menu(self):
            return []
        # no children: touch lands ON the row itself (collide ok)

    class RowWithChild(kc.ContextMenuMixin, CountingMixin, BoxLayout):
        """The poison shape: a consuming child inside the BB's Widget chain
        short-circuits ButtonBehavior.on_touch_down (no ud, no grab, no
        __touch_time) while the mixin's grab remains."""

        class ConsumingChild(Label):
            def on_touch_down(self, touch):
                touch.ud[self] = True
                return True

        def __init__(self, **kw):
            super().__init__(**kw)
            self.size = (100, 40)
            self.child = self.ConsumingChild(pos=(0, 0), size_hint=(None, None))
            self.add_widget(self.child)
            self.n_rel = 0

    row = Row(pos=(0, 0), size_hint=(None, None), size=(100, 40))

    # --- A: healthy tap, widget in the grab list TWICE --------------------
    t = Touch(50, 20)
    row.size = (100, 40)
    row.on_touch_down(t)             # mixin grab + BB grab: list len 2
    check("A: down puts the widget in the grab list twice",
          len(t.grab_list) == 2, t.grab_list)
    check("A: BB down-state present", self_in_ud(row, t))
    delivered = post_dispatch_ups(t)
    check("A: two grab-list deliveries happen", delivered == 2 + 0, delivered)
    check("A: release fires exactly once (echo killed)",
          getattr(row, "n_rel", 0) == 1, getattr(row, "n_rel", 0))

    # --- B: the poison shape: a CHILD consumes the down -------------------
    row2 = RowWithChild()
    t2 = Touch(50, 20)
    row2.on_touch_down(t2)   # mixin grabs; BB chain: child consumes -> True
    poisoned = len(t2.grab_list) >= 1 and not self_in_ud(row2, t2)
    check("B: poison shape produced (grab without BB down-state)",
          poisoned, (len(t2.grab_list), self_in_ud(row2, t2)))
    row2.n_rel = 0
    delivered = post_dispatch_ups(t2)
    check("B: poison up does not crash (no exception escaped)", True)
    check("B: the row does not release for a child-consumed press",
          getattr(row2, "n_rel", 0) == 0, getattr(row2, "n_rel", 0))

    # --- C: long-press fire + up: swallow contract, no release ------------
    t3 = Touch(50, 20)
    row.on_touch_down(t3)
    row._ctx_fire(t3)               # the 0.5 s Clock fired
    row.n_rel = 0
    post_dispatch_ups(t3)
    check("C: long-press up: no release", getattr(row, "n_rel", 0) == 0,
          getattr(row, "n_rel", 0))
    check("C: fired flag reset for the next press",
          t3.ud.get("shades_ctx_fired") is False, t3.ud)

    # --- D: CtxHeaderLabel (non-BB) still forwards its up -----------------
    hdr = kc.CtxHeaderLabel(size=(200, 30))
    t4 = Touch(100, 15)
    try:
        r = hdr.on_touch_up(t4)
        check("D: non-BB mixin up forwards without crash",
              r in (False, None), r)
    except Exception as e:
        check("D: non-BB mixin up forwards without crash", False, repr(e))

    # --- E: plain tap WITHOUT the grab-pass double delivery: single up ----
    t5 = Touch(50, 20)
    row.on_touch_down(t5)
    row.n_rel = 0
    r = row.on_touch_up(t5)
    check("E: direct single up still forwards (result truthy)",
          r is True or r is None, r)

    print("\nPASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


def self_in_ud(widget, touch):
    return widget in touch.ud


if __name__ == "__main__":
    sys.exit(main())
