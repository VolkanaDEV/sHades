#!/usr/bin/env python3
"""Profile card payload + QR render tests (v1 spec, tay 2026-11-18).

The decisive check: derive_user_hash(pub) must equal the lxmf.delivery
destination hash a client derives from the SAME identity at boot
(kivy_client: client_dest = Destination(identity, OUT, SINGLE,
"lxmf", "delivery")) — otherwise scanned cards would DM to nowhere.
"""

import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profile_card as pc  # noqa: E402

PASS = []
FAIL = []


def b64s(b):
    return base64.urlsafe_b64encode(b).decode("ascii")


def check(ok, message, detail=""):
    print(("PASS " if ok else "FAIL ") + message, flush=True)
    (PASS if ok else FAIL).append(message if ok else message + " " + str(detail))


def main():
    try:
        import RNS
    except Exception as e:
        print("SKIP  RNS unavailable: %s" % e)
        return 0

    # 1. round-trip: build -> parse restores the same bytes and alias
    identity = RNS.Identity()
    pub = identity.get_public_key()
    payload = pc.build_payload(pub, "tay")
    pub2, alias = pc.parse_payload(payload)
    check(bytes(pub2) == bytes(pub) and alias == "tay",
          "payload round-trip restores pubkey and alias", (pub2, alias))
    check(payload.startswith("shades-contact1:"),
          "payload carries the versioned prefix", payload[:24])

    # 2. the decisive cross-check: scan-side derive == client-side hash
    dest = RNS.Destination(identity, RNS.Destination.OUT,
                           RNS.Destination.SINGLE, "lxmf", "delivery")
    mine = dest.hash.hex()
    derived = pc.derive_user_hash(pub)
    check(derived == mine,
          "derive_user_hash equals the client-side lxmf.delivery hash",
          dict(derived=derived, client=mine))

    # 3. malformed payloads rejected (each case can lose)
    bad_cases = {
        "no prefix": "lxmf://deadbeef.tay",
        "three parts": pc.PREFIX + "abc.def.ghi",
        "short pub": pc.PREFIX + b64s(b"x" * 8) + ".dGF5",
        "not b64": pc.PREFIX + "@@@@.dGF5",
        "empty alias": pc.PREFIX + b64s(pub) + ".",
        "alias too long": pc.PREFIX + b64s(pub) + "."
        + b64s(b"x" * 65),
    }
    for name, bad in bad_cases.items():
        try:
            pc.parse_payload(bad)
            ok = False
            detail = "accepted"
        except pc.CardPayloadError:
            ok = True
            detail = "rejected"
        check(ok, "rejects %s" % name, detail)

    # 4. alias length cap and non-bytes pubkey raise, not corrupt
    try:
        pc.build_payload(pub, "y" * 65)
        ok = False
    except pc.CardPayloadError:
        ok = True
    check(ok, "build rejects an oversized alias")
    try:
        pc.build_payload("not-bytes", "tay")
        ok = False
    except pc.CardPayloadError:
        ok = True
    check(ok, "build rejects a non-bytes pubkey")

    # 5. QR render: real PNG, ECC-H square, parses back via PIL
    try:
        png = pc.render_qr_png(payload)
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        check(img.format == "PNG" and img.size[0] == img.size[1]
              and len(png) > 500,
              "QR renders as a square PNG", (img.format, img.size))
    except pc.CardPayloadError as e:
        png = None
        check(False, "QR renders as a square PNG", e)

    # 6. composite: avatar over the code center, still a square PNG
    try:
        if png is None:
            raise pc.CardPayloadError("no QR from step 5")
        avatar = pc.render_qr_png(pc.PREFIX + "smoke", box_size=4)
        out = pc.composite_center(png, avatar, out_px=512)
        img2 = Image.open(io.BytesIO(out))
        check(img2.format == "PNG" and img2.size == (512, 512),
              "composite lands the avatar and resizes to out_px", img2.size)
        plate = img2.crop((256 - 70, 256 - 70, 256 + 70, 256 + 70))
        colors = plate.convert("RGB").getcolors(maxcolors=100000)
        white_dom = sorted(colors, key=lambda c: -c[0])[0][1] == (255, 255, 255)
        check(white_dom, "center plate is dominated by the avatar plate")
    except pc.CardPayloadError as e:
        check(False, "composite lands the avatar and resizes to out_px", e)

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
