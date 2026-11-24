#!/usr/bin/env python3
"""UIX regression test 4: no non-ASCII glyphs in button labels and no
literal \\u escape sequences in rendered strings. Catches P1-1
(`members \\u25bc` drawn as seven literal characters because Kivy
does not interpret \\uXXXX in KV strings and Roboto has no such glyph).

Two layers:
  - widget tree: every widget with a text attribute on every screen
    must be pure ASCII and contain no literal "\\u"
  - source: kv_md.kv must contain no literal "\\u" sequence at all, so
    a broken escape cannot be reintroduced where the tree walk cannot
    see it (e.g. inside a conditional expression)

Run: xvfb-run -a python tests/test_uix_glyphs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


def walk(widget):
    yield widget
    for child in widget.children:
        yield from walk(child)


def main():
    kc._KV_LOADED = False
    app = kc.sHadesClientApp()
    root = app.build()
    bad_ascii, bad_escape = [], []
    n_texts = 0
    for screen in root.screens:
        for w in walk(screen):
            text = getattr(w, "text", None)
            if not isinstance(text, str) or not text:
                continue
            n_texts += 1
            if any(ord(c) >= 128 for c in text):
                bad_ascii.append("%s: %r" % (type(w).__name__, text))
            if "\\u" in text:
                bad_escape.append("%s: %r" % (type(w).__name__, text))
    check("scanned %d rendered strings across %d screens"
          % (n_texts, len(root.screens)), n_texts > 0)
    check("every rendered string is ASCII", not bad_ascii, bad_ascii[:5])
    check("no rendered string contains a literal \\u sequence",
          not bad_escape, bad_escape[:5])
    with open(os.path.join(REPO, "kv_md.kv"), encoding="utf-8") as f:
        kv_src = f.read()
    check("kv_md.kv contains no literal \\u escape", "\\u" not in kv_src,
          [ln for ln in kv_src.splitlines() if "\\u" in ln][:3])
    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
