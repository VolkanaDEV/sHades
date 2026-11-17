#!/usr/bin/env python3
"""Probe composer bar geometry at desktop and phone window sizes.

Device report (tay): the flat attach plate overlaps the composer pfp
instead of sitting between the pfp and the field. The layout suite pins
the order only at the CI window size, so walk the bar at a phone width
and dump every child's geometry plus the plate's Label child.
"""
import os
import sys

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy.uix.widget import Widget  # noqa: E402
from kivy_client import sHadesClientApp  # noqa: E402


def walk(w):
    yield w
    for c in w.children:
        yield from walk(c)


class Probe(sHadesClientApp):
    def on_start(self):
        Clock.schedule_once(self.step_desktop, 1.0)

    def inspect(self, tag):
        main = self.root.get_screen("main")
        bar = main.ids.msg_input.parent
        print("--- %s (bar w=%s, children=%d)" % (
            tag, round(bar.width), len(bar.children)))
        holder = main.ids.composer_pfp_holder
        pfp = main.ids.composer_pfp
        print("pfp_holder x=%s w=%s | pfp x=%s w=%s" % (
            round(holder.x), round(holder.width),
            round(pfp.x), round(pfp.width)))
        for w in bar.children:
            print("  %s x=%s w=%s size=%sx%s" % (
                type(w).__name__, round(w.x, 1), round(w.width, 1),
                round(w.width, 1), round(w.height, 1)))
        flat = None
        for w in walk(bar):
            if isinstance(w, Widget) and w is not bar and w.width == dp(38):
                flat = w
                break
        if flat is not None:
            print("flat plate x=%s right=%s" % (round(flat.x, 1),
                                                round(flat.right, 1)))
            for c in flat.children:
                print("  glyph %s pos=%s size=%s" % (
                    type(c).__name__, (round(c.x, 1), round(c.y, 1)),
                    (round(c.width, 1), round(c.height, 1))))
        field = main.ids.msg_input
        print("field x=%s right=%s" % (round(field.x, 1),
                                       round(field.right, 1)))
        order = ((None if pfp is None else pfp.right)
                 < (None if flat is None else flat.x)
                 and (None if flat is None else flat.right) <= field.x)
        print("ORDER_OK" if order else "ORDER_BROKEN",
              "pfp", round(pfp.right, 1), "flat", round(flat.x, 1)
              if flat else None, "field", round(field.x, 1))
        print()

    def run_size(self, w, h, next_step):
        def go(dt):
            Window.size = (w, h)
            Clock.schedule_once(lambda dt2: self.finish_size(next_step), 0.4)
        Clock.schedule_once(go, 0.1)

    def finish_size(self, next_step):
        self.inspect("w=%d h=%d" % Window.size)
        if next_step:
            next_step()
        else:
            self.stop()

    def step_desktop(self, dt):
        self.inspect("default window")
        self.run_size(360, 640, None)


if __name__ == "__main__":
    Probe().run()
