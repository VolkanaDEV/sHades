#!/usr/bin/env python3
"""Task 2 group G: the onboarding card scrolls on short windows.

At 640x480 stage 2 ("Your mesh") the fixed stack (512px + the
description) overflowed the 448px card: the title clipped at the top
and the description was squeezed to 6px. The card's middle content
now lives in a ScrollView and the Continue / Restore buttons stay
pinned at the bottom of the card.

Run: xvfb-run -a python tests/test_task2_onboarding_scroll.py
Set T2G_SIZE=1280x720 to check the roomy case instead.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
import kivy_client as kc  # noqa: E402


def walk(widget):
    yield widget
    for child in getattr(widget, "children", []):
        yield from walk(child)


class Probe(kc.sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.passes = 0
        self.my_hash = "a" * 32
        self.username = "probeuser"
        Clock.schedule_once(self.go, 1.8)

    def check(self, condition, message, extra=""):
        print(("PASS " if condition else "FAIL ") + message
              + ((" -- " + str(extra)) if extra and not condition else ""),
              flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def go(self, dt):
        onb = self.root.get_screen("onboarding")
        onb.stage = 2
        self.root.current = "onboarding"
        Clock.schedule_once(self.check_card, 0.9)

    def check_card(self, dt):
        onb = self.root.get_screen("onboarding")
        card = onb.ids.intro_card
        labels = {w.text: w for w in walk(card)
                  if type(w).__name__ == "Label"}
        desc = labels.get(onb.descriptions[2])
        self.check(desc is not None, "stage 2 description label found")
        if desc is not None:
            self.check(abs(desc.height - desc.texture_size[1]) < 1.0,
                       "description height follows its texture (not squeezed)",
                       (desc.height, desc.texture_size[1]))
            self.check(desc.height > 20,
                       "description is tall enough to read", desc.height)
        title = labels.get("Your mesh")
        self.check(title is not None, "stage 2 title label found")
        if title is not None:
            self.check(title.top <= card.top + 0.5
                       and title.y >= card.y - 0.5,
                       "title sits inside the card",
                       (title.top, card.top, title.y, card.y))
        scrolls = [w for w in walk(card)
                   if type(w).__name__ == "ScrollView"]
        self.check(len(scrolls) == 1, "card content is in one ScrollView")
        if scrolls:
            inner = scrolls[0].children[0]
            if Window.height <= 600:
                self.check(inner.height > scrolls[0].height,
                           "content taller than the viewport (scrolls)",
                           (inner.height, scrolls[0].height))
            else:
                self.check(inner.height <= scrolls[0].height + 0.5,
                           "content fits without scrolling at %s"
                           % (Window.size,),
                           (inner.height, scrolls[0].height))
        btns = {w.text: w for w in walk(card)
                if type(w).__name__ == "MDButtonText"}
        cont = btns.get("Protect identity")
        self.check(cont is not None, "stage 2 primary button found")
        if cont is not None:
            btn = cont.parent
            self.check(btn.top <= card.top + 0.5
                       and btn.y >= card.y - 0.5,
                       "primary button stays pinned inside the card",
                       (btn.top, card.top, btn.y, card.y))
            self.check(btn.opacity == 1, "primary button visible")
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    size = os.environ.get("T2G_SIZE", "640x480")
    Window.size = tuple(int(v) for v in size.split("x"))
    probe = Probe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
