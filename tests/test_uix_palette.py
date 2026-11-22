#!/usr/bin/env python3
"""UIX regression test 5: tokens that must be visually distinct share
no value. Catches P1-2 (own-message bubble invisible against the chat
surface) and P2-5 (top bar indistinguishable from the chat surface),
plus any future palette edit that collapses the elevation ladder.

Pairs are the full RGBA tuples from DEFAULT_THEME_COLORS. DESIGN.md
pairs topbar with panel_darker deliberately (same #131E2B header /
composer value), so that pair is NOT asserted distinct here.

Run: xvfb-run -a python tests/test_uix_palette.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []

# (token A, token B, why they must differ)
PAIRS = [
    ("msg_own", "panel", "own-message bubble vs chat surface (P1-2)"),
    ("msg_own", "panel_darker", "own-message bubble vs composer"),
    ("topbar", "bg_main", "top bar vs app background (P2-5)"),
    ("topbar", "panel", "top bar vs chat surface"),
    ("border", "border_subtle", "field boundary vs divider (P2-4)"),
    ("bg_main", "bg_sidebar", "elevation ladder: background vs sidebar"),
    ("bg_sidebar", "panel", "elevation ladder: sidebar vs chat surface"),
    ("panel", "panel_darker", "elevation ladder: chat surface vs composer"),
    ("input_bg", "input_bg_focus", "input fill vs focused fill"),
    ("text", "text_muted", "body text vs secondary text"),
    ("text_muted", "text_dim", "secondary text vs timestamps"),
    ("text", "text_dim", "body text vs timestamps"),
    ("primary", "accent", "amber signal vs teal live state"),
    ("sidebar_ctrl", "bg_sidebar", "selection surface vs navigation"),
    ("btn_text", "primary", "text on amber vs amber itself"),
    ("aurora_a", "aurora_b", "amber orb vs teal orb"),
    ("aurora_b", "aurora_c", "teal orb vs violet orb"),
    ("aurora_a", "aurora_c", "amber orb vs violet orb"),
]


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


def main():
    colors = kc.DEFAULT_THEME_COLORS
    for a, b, why in PAIRS:
        check("%s != %s (%s)" % (a, b, why),
              tuple(colors[a]) != tuple(colors[b]),
              "%r == %r" % (colors[a], colors[b]))
    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
