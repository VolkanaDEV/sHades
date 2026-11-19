#!/usr/bin/env python3
"""Launcher icon contract (v0.36.8, tay's sHades wordmark art).

The icon file icon_512.png carries tay's wordmark art centered inside
Android's circular adaptive safe zone. Regenerate with (from the repo
root, after placing the source jpeg path it expects):

    python - <<'PY'
    (the build recipe's body -- SAFE=0.56, OUT_SIZE=512, luma>100 scale)
    PY

Contract:
  1. icon_512.png exists and is a 512x512 RGB PNG.
  2. Corners match the art's own background color (2, 2, 2) -- the
     icon is full-bleed art, not a p4a default.
  3. The bright lettering spans < 0.60 of the width: inside the 0.61
     circular adaptive safe band with halo margin.
  4. buildozer.spec's icon.filename points at this exact file.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


def main():
    icon = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "icon_512.png")
    check("icon file exists", os.path.isfile(icon), icon)
    if not os.path.isfile(icon):
        return 1
    try:
        from PIL import Image
    except Exception as e:
        print("SKIP  pillow missing: %s" % e)
        return 0

    with Image.open(icon) as im:
        im = im.convert("RGB")
        w, h = im.size
        check("icon is 512x512", (w, h) == (512, 512), (w, h))
        corners = [im.getpixel(p) for p in
                   ((0, 0), (511, 0), (0, 511), (511, 511))]
        check("corners are the art background",
              all(all(abs(c[i] - 2) <= 1 for i in range(3))
                  for c in corners), corners)

        g = im.convert("L")
        px = g.load()
        x0, x1 = w, 0
        for y in range(h):
            for x in range(w):
                if px[x, y] > 100:
                    x0 = min(x0, x)
                    x1 = max(x1, x)
        span = (x1 - x0) / w
        check("lettering inside the circular safe band (span < 0.60)",
              span < 0.60, "%.3f (x %d..%d)" % (span, x0, x1))
        # centered: neither side hugs the edge
        check("lettering horizontally centered",
              abs(min(x0, w - 1 - x1) - max(x0, w - 1 - x1)) < 8,
              "%d/%d" % (x0, w - 1 - x1))

    spec = open(os.path.join(os.path.dirname(icon),
                             "buildozer.spec"), "rb").read()
    check("buildozer icon.filename wired",
          b"icon.filename = icon_512.png" in spec)
    print("RESULT: %d PASS, %d FAIL" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
