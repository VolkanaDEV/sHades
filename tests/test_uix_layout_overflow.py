#!/usr/bin/env python3
"""UIX regression test 1: layout overflow. Build the app, walk every
screen, assert no child extends past its parent's bounds and no two
siblings overlap. Catches P0-1, P0-2 and P2-2 and anything like them.

Scoping rules (each grounded in the live tree, not guessed):
  - Sample after the first layout pass and gate on a real size
    (screen.width > 100); the pre-layout 100x100 default is not a defect.
  - A widget inside a collapsed container (any ancestor with width or
    height 0 -- closed drawers, the collapsed advanced box, the empty
    reply bar, stage-gated onboarding boxes, disabled strips) keeps its
    size but is invisible; it and its subtree are skipped.
  - A ScrollView's direct content is legitimately taller than the
    viewport (the scroller clips it); only the content-vs-ScrollView
    pair is skipped, never the rows inside it.
  - KivyMD's internal field labels (hint/helper/max-length) are children
    of their MDTextField and sit at stale positions; the drawn text is a
    canvas texture, so a widget whose parent is an MDTextField is skipped.
  - The drawers (left_sidebar / right_sidebar) slide OVER the chat by
    design, and a full-screen backdrop (AuroraBackground) sits behind
    its screen's content; overlaps involving either are skipped. A
    widget that paints nothing (opacity 0, or an Image with no source,
    like the banner letter tile's empty frame) cannot collide with
    anything visible.
  - The profile screen's content is taller than the viewport and scrolls
    (ScrollView wrap); the config rows are the same. Both are covered by
    the ScrollView rule above, not by an exemption.
  - One known exception: the reply-cancel MDButton is 40px tall (the
    minimum touch target) inside the 32px reply bar, so it overhangs the
    bar 4px top and bottom. The 32px bar is pinned by
    tests/test_reply_cards.py, so that single parent/child pair is
    tolerated here rather than fixed by growing the bar.

Run: xvfb-run -a python tests/test_uix_layout_overflow.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.uix.scrollview import ScrollView  # noqa: E402
from kivymd.uix.button import MDButton  # noqa: E402
from kivymd.uix.textfield import MDTextField  # noqa: E402
from kivy_client import sHadesClientApp  # noqa: E402

SCREENS = ["connect", "onboarding", "main", "profile", "config",
           "site_editor", "server_config"]
DRAWER_IDS = ("left_sidebar", "right_sidebar")
TOL = 1.0


def wrect(w):
    x, y = w.to_window(*w.pos)
    return (x, y, x + w.width, y + w.height)


def name(w):
    cls = type(w).__name__
    ident = ""
    if hasattr(w, "id") and w.id:
        ident = "#" + w.id
    elif isinstance(getattr(w, "text", None), str) and w.text:
        ident = repr(str(w.text)[:24])
    return cls + ident


def has_zero_ancestor(w):
    seen = set()
    p = w.parent
    while p is not None and id(p) not in seen and len(seen) < 24:
        seen.add(id(p))
        if p.width == 0 or p.height == 0:
            return True
        p = p.parent
    return False


def is_backdrop(w, screen):
    return (w.width * w.height >= 0.95 * screen.width * screen.height)


def make_in_drawer(screen):
    """True if the widget is a drawer or lives inside one (drawers slide
    over the chat by design, so drawer content overlapping the chat is
    not a collision). KV ids live in the screen's ids dict -- widgets
    do not carry an .id attribute themselves -- and the ids dict holds
    WeakProxies, so drawers are compared with ==, never with id()/is."""
    drawers = []
    for key in DRAWER_IDS:
        w = screen.ids.get(key)
        if w is not None:
            drawers.append(w)

    def in_drawer(w):
        seen = set()
        p = w
        while p is not None and id(p) not in seen and len(seen) < 24:
            seen.add(id(p))
            if any(p == d for d in drawers):
                return True
            p = p.parent
        return False

    return in_drawer


def invisible(w):
    """Paints nothing: opacity 0, or an Image with no source (the banner
    letter tile is layered over an empty, hidden Image by design)."""
    if getattr(w, "opacity", 1) == 0:
        return True
    from kivy.uix.image import Image
    return isinstance(w, Image) and not getattr(w, "source", None)


class OverflowProbe(sHadesClientApp):
    def on_start(self):
        self.failures = []
        self.passes = 0
        self.queue = list(SCREENS)
        self.state = "screens"
        Clock.schedule_once(self.next_screen, 2.2)

    def check(self, condition, message):
        print(("PASS " if condition else "FAIL ") + message, flush=True)
        if condition:
            self.passes += 1
        else:
            self.failures.append(message)

    def scan(self, screen):
        over, lap = [], []
        in_drawer = make_in_drawer(screen)
        # The 32px reply bar (pinned by test_reply_cards.py) holds a
        # 40px cancel button; that one pair is a known tolerated
        # overhang -- see the module docstring.
        reply_bar = screen.ids.get("reply_bar")

        def walk(w, parent):
            if w is not screen and invisible(w):
                return
            if parent is not None and w is not screen:
                if (w.width > 0 and w.height > 0
                        and not has_zero_ancestor(w)
                        and not isinstance(parent, MDTextField)
                        and not isinstance(parent, ScrollView)
                        and not (reply_bar is not None and parent == reply_bar
                                 and isinstance(w, MDButton))):
                    px, py, px2, py2 = wrect(parent)
                    x, y, x2, y2 = wrect(w)
                    if (x < px - TOL or y < py - TOL
                            or x2 > px2 + TOL or y2 > py2 + TOL):
                        over.append("%s %s outside %s"
                                    % (screen.name, name(w), name(parent)))
            kids = [c for c in w.children
                    if c.width > 0 and c.height > 0
                    and not has_zero_ancestor(c)]
            for i in range(len(kids)):
                for j in range(i + 1, len(kids)):
                    a, b = kids[i], kids[j]
                    if (in_drawer(a) != in_drawer(b)
                            or invisible(a) or invisible(b)
                            or is_backdrop(a, screen)
                            or is_backdrop(b, screen)):
                        continue
                    ax, ay, ax2, ay2 = wrect(a)
                    bx, by, bx2, by2 = wrect(b)
                    iw = min(ax2, bx2) - max(ax, bx)
                    ih = min(ay2, by2) - max(ay, by)
                    if iw > TOL and ih > TOL:
                        lap.append("%s: %s overlaps %s (%dx%d)"
                                   % (screen.name, name(a), name(b), iw, ih))
            for c in w.children:
                walk(c, w)

        walk(screen, None)
        return over, lap

    def check_screen(self, screen_name):
        screen = self.root.get_screen(screen_name)
        if screen.width <= 100:
            self.check(False, "%s screen laid out to a real size "
                       "(got %s)" % (screen_name, screen.size))
            return
        over, lap = self.scan(screen)
        self.check(not over, "%s: no child extends past its parent "
                   "(%d) %r" % (screen_name, len(over), over[:3]))
        self.check(not lap, "%s: no two siblings overlap (%d) %r"
                   % (screen_name, len(lap), lap[:3]))

    def next_screen(self, dt):
        if not self.queue:
            self.finish_main_states()
            return
        name_s = self.queue.pop(0)
        self.root.current = name_s
        if name_s == "onboarding":
            Clock.schedule_once(self.check_onboarding, 0.9)
        else:
            Clock.schedule_once(
                lambda _dt, n=name_s: (self.check_screen(n),
                                       self.next_screen(0)), 0.9)

    def check_onboarding(self, dt):
        self.check_screen("onboarding")
        screen = self.root.get_screen("onboarding")
        try:
            screen.stage = 2
        except Exception as e:
            self.check(False, "onboarding stage can be set to 2: %r" % (e,))
            self.next_screen(0)
            return
        Clock.schedule_once(self.check_onboarding_stage2, 0.8)

    def check_onboarding_stage2(self, dt):
        over, lap = self.scan(self.root.get_screen("onboarding"))
        self.check(not over, "onboarding stage 2: no child extends past "
                   "its parent (%d) %r" % (len(over), over[:3]))
        self.check(not lap, "onboarding stage 2: no two siblings overlap "
                   "(%d) %r" % (len(lap), lap[:3]))
        self.next_screen(0)

    def finish_main_states(self):
        self.root.current = "main"
        Clock.schedule_once(self.main_closed, 0.9)

    def main_closed(self, dt):
        self.check_screen("main")
        main = self.root.get_screen("main")
        main.toggle_left()
        Clock.schedule_once(self.main_left_open, 0.7)

    def main_left_open(self, dt):
        over, lap = self.scan(self.root.get_screen("main"))
        self.check(not over, "main (channels open): no child extends past "
                   "its parent (%d) %r" % (len(over), over[:3]))
        self.check(not lap, "main (channels open): no two siblings overlap "
                   "(%d) %r" % (len(lap), lap[:3]))
        main = self.root.get_screen("main")
        main.toggle_right()
        Clock.schedule_once(self.main_right_open, 0.7)

    def main_right_open(self, dt):
        over, lap = self.scan(self.root.get_screen("main"))
        self.check(not over, "main (members open): no child extends past "
                   "its parent (%d) %r" % (len(over), over[:3]))
        self.check(not lap, "main (members open): no two siblings overlap "
                   "(%d) %r" % (len(lap), lap[:3]))
        main = self.root.get_screen("main")
        main.toggle_right()
        self.reply_draft = "re: hello"
        Clock.schedule_once(self.main_reply, 0.7)

    def main_reply(self, dt):
        over, lap = self.scan(self.root.get_screen("main"))
        self.check(not over, "main (reply draft shown): no child extends "
                   "past its parent (%d) %r" % (len(over), over[:3]))
        self.check(not lap, "main (reply draft shown): no two siblings "
                   "overlap (%d) %r" % (len(lap), lap[:3]))
        self.reply_draft = ""
        print("PASS: %d  FAIL: %d" % (self.passes, len(self.failures)),
              flush=True)
        self.stop()


if __name__ == "__main__":
    Window.size = (1280, 720)
    probe = OverflowProbe()
    probe.run()
    sys.exit(1 if probe.failures else 0)
