#!/usr/bin/env python3
"""UIX regression test 3: WCAG 2.1 contrast for the text pairs in
DEFAULT_THEME_COLORS. Catches P2-3 and any future palette edit that
drops a text pair below AA (4.5:1 for normal text).

Pairs (per DESIGN.md): text / text_muted / text_dim on each dark
surface (panel, bg_main, bg_sidebar, panel_darker); accent on bg_main;
btn_text on primary. Colours are the shipped RGBA tuples; alpha is
ignored (all text pairs are opaque).

Run: xvfb-run -a python tests/test_uix_contrast.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []
MIN_TEXT = 4.5

PAIRS = ([(fg, bg) for fg in ("text", "text_muted", "text_dim")
          for bg in ("panel", "bg_main", "bg_sidebar", "panel_darker")]
         + [("accent", "bg_main"), ("btn_text", "primary")])


def _lin(c):
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    r, g, b = (_lin(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    l1, l2 = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


def main():
    colors = kc.DEFAULT_THEME_COLORS
    for fg_key, bg_key in PAIRS:
        r = ratio(colors[fg_key], colors[bg_key])
        check("%s on %s is >= %.1f:1 (got %.2f:1)"
              % (fg_key, bg_key, MIN_TEXT, r), r >= MIN_TEXT)
    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
