#!/usr/bin/env python3
"""Icon geometry unit tests (ui_geom) — no Kivy, no display needed.

The top-bar icons are canvas-drawn because the default Kivy font lacks the
glyphs (they rendered as boxes with X's). These tests pin the geometry the
widgets emit: bar count/extent, lens/handle placement, gear tooth count and
angles, and that everything stays inside the widget rect.
"""
import os, sys, math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ui_geom  # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

X, Y, W, H = 10.0, 20.0, 40.0, 40.0

def in_rect(px, py):
    return X <= px <= X + W and Y <= py <= Y + H

def main():
    # ---------- hamburger ----------
    bars = ui_geom.hamburger_bars(X, Y, W, H)
    check("hamburger draws exactly 3 bars", len(bars) == 3, bars)
    centers = sorted((py + bh / 2.0 - Y) / H for _, py, _, bh in bars)
    check("bars centered at 28/50/72% height",
          all(abs(c - e) < 1e-9 for c, e in zip(centers, (0.28, 0.5, 0.72))), centers)
    check("all bars inside the widget rect",
          all(in_rect(px, py) and in_rect(px + bw, py + bh)
              for px, py, bw, bh in bars), bars)
    check("bar width is 56% of widget width",
          all(abs(bw - W * 0.56) < 1e-9 for _, _, bw, _ in bars), bars)
    check("bars are evenly spaced",
          abs((centers[1] - centers[0]) - (centers[2] - centers[1])) < 1e-9, centers)
    custom = ui_geom.hamburger_bars(X, Y, W, H, bar=2.0)
    check("explicit bar thickness honoured",
          all(abs(bh - 2.0) < 1e-9 for _, _, _, bh in custom), custom)

    # ---------- search ----------
    cx, cy, r = ui_geom.search_circle(X, Y, W, H)
    check("lens circle offset up-left of center",
          abs(cx - (X + W * 0.42)) < 1e-9 and abs(cy - (Y + H * 0.58)) < 1e-9, (cx, cy))
    check("lens radius = 22% of min dimension", abs(r - H * 0.22) < 1e-9, r)
    hx1, hy1, hx2, hy2 = ui_geom.search_handle(X, Y, W, H)
    check("handle starts near the lens edge",
          abs(math.hypot(hx1 - cx, hy1 - cy) - r) < W * 0.20, (hx1, hy1))
    check("handle ends at the lower-right of the widget",
          abs(hx2 - (X + W * 0.80)) < 1e-9 and abs(hy2 - (Y + H * 0.20)) < 1e-9,
          (hx2, hy2))
    check("handle points down-right",
          hx2 > hx1 and hy2 < hy1, (hx1, hy1, hx2, hy2))

    # ---------- gear ----------
    cx, cy = X + W / 2.0, Y + H / 2.0
    r1, teeth = ui_geom.gear(cx, cy, min(W, H))
    check("8 teeth", len(teeth) == 8, len(teeth))
    check("ring radius 26% of size", abs(r1 - H * 0.26) < 1e-9, r1)
    angles = sorted(round((math.atan2(ty1 - cy, tx1 - cx) % (2 * math.pi))
                          / math.radians(45.0), 6)
                    for tx1, ty1, _, _ in teeth)
    check("teeth at 45-degree steps", angles == [0.0, 1, 2, 3, 4, 5, 6, 7], angles)
    check("tooth tips at 40% of size",
          all(abs(math.hypot(tx2 - cx, ty2 - cy) - H * 0.40) < 1e-9
              for _, _, tx2, ty2 in teeth), teeth)
    check("teeth start on the ring",
          all(abs(math.hypot(tx1 - cx, ty1 - cy) - r1) < 1e-9
              for tx1, ty1, _, _ in teeth), teeth)

    # ---------- different rect sizes stay proportional ----------
    bars2 = ui_geom.hamburger_bars(0, 0, 80, 60)
    check("geometry scales with the widget rect",
          all(abs(bw - 80 * 0.56) < 1e-9 for _, _, bw, _ in bars2), bars2)
    _, teeth2 = ui_geom.gear(0, 0, 80)
    check("gear scales with size",
          abs(math.hypot(teeth2[0][2], teeth2[0][3]) - 80 * 0.40) < 1e-9, teeth2[0])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
