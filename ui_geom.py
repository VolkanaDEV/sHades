#!/usr/bin/env python3
"""Pure geometry for canvas-drawn UI icons — NO Kivy, testable headless.

The default Kivy font (Roboto) has no glyphs for ☰/⌕/⚙; they render as boxes
with X's. The icons are therefore drawn on the widget canvas, and the layout
math lives here so it can be unit-tested without a display.

All functions take a widget rect (x, y, w, h) in pixels and return plain
numbers/sequences. Coordinates are bottom-left origin (Kivy convention).
"""
import math

def hamburger_bars(x, y, w, h, bar=None):
    """Three horizontal bars at 72% / 50% / 28% of the height.
    Returns [(px, py, bw, bh), ...] for Rectangle instructions."""
    bar = bar if bar is not None else max(1.5, h * 0.07)
    return [(x + w * 0.22, y + h * fy - bar / 2.0, w * 0.56, bar)
            for fy in (0.72, 0.5, 0.28)]

def search_circle(x, y, w, h):
    """(cx, cy, r) — the magnifier lens, offset up-left of center."""
    return (x + w * 0.42, y + h * 0.58, min(w, h) * 0.22)

def search_handle(x, y, w, h):
    """(x1, y1, x2, y2) — handle from the lens edge to the lower-right."""
    return (x + w * 0.58, y + h * 0.42, x + w * 0.80, y + h * 0.20)

def gear(cx, cy, size):
    """Toothed ring: (r1, teeth) where teeth is 8 (x1, y1, x2, y2) segments
    from the ring radius (r1 = 26% of size) to the tooth tip (r2 = 40%)."""
    r1 = size * 0.26
    r2 = size * 0.40
    teeth = []
    for i in range(8):
        a = math.radians(i * 45.0)
        teeth.append((cx + r1 * math.cos(a), cy + r1 * math.sin(a),
                      cx + r2 * math.cos(a), cy + r2 * math.sin(a)))
    return r1, teeth
