#!/usr/bin/env python3
"""Profile contact card payload + QR rendering (v1 spec, tay 2026-11-18).

The collectible profile card carries a QR of the member's Reticulum
public keyset with their avatar composited mid-code. Any other client
holding the public key can derive the member's 32-hex lxmf.delivery
address offline (RNS.Destination.hash(identity, "lxmf", "delivery")),
which is the guild's canonical user key (see dms.py), so the payload is
by construction "enough info to DM".

Wire format (versioned so it can evolve without silent drift):

    shades-contact1:<urlsafe-b64(pub64)>.<urlsafe-b64(alias-utf8)>

- pub64: exactly 64 bytes of RNS.Identity.get_public_key() data
- alias: UTF-8, max 64 bytes, no padding stripping (both parts keep
  their '=' padding so the round-trip is deterministic)

Reticulum routing note (tay: hubs interconnect via gateways): the
payload deliberately carries NO hub address. Cross-hub DMs ride
existing gateway routes; only an unreachable-hub fallback would ever
need more, and that would be a v2 field.

Rendering: python-qrcode at ERROR_CORRECT_H (tolerates the ~30%
occlusion of the mid-code avatar) into a PNG; composition with the
avatar uses pillow, which buildozer already pins.
"""

import base64
import io

PREFIX = "shades-contact1:"
PUB_BYTES = 64
ALIAS_MAX_BYTES = 64


class CardPayloadError(ValueError):
    """Raised when a scanned or pasted payload is not a v1 card."""


def build_payload(pub_key, alias):
    """Encode a card payload from a 64-byte public keyset + alias."""
    if not isinstance(pub_key, (bytes, bytearray)):
        raise CardPayloadError("public key must be bytes")
    if len(pub_key) != PUB_BYTES:
        raise CardPayloadError(
            "public key must be %d bytes, got %d" % (PUB_BYTES, len(pub_key)))
    alias = (alias or "").strip()
    if not alias:
        raise CardPayloadError("alias must not be empty")
    alias_bytes = alias.encode("utf-8")
    if len(alias_bytes) > ALIAS_MAX_BYTES:
        raise CardPayloadError("alias exceeds %d bytes" % ALIAS_MAX_BYTES)
    pub = base64.urlsafe_b64encode(bytes(pub_key)).decode("ascii")
    name = base64.urlsafe_b64encode(alias_bytes).decode("ascii")
    return PREFIX + pub + "." + name


def parse_payload(text):
    """Decode a card payload; returns (pub64, alias). Raises CardPayloadError."""
    if not isinstance(text, str):
        raise CardPayloadError("payload must be text")
    text = text.strip()
    if not text.startswith(PREFIX):
        raise CardPayloadError("not a shades-contact1 payload")
    body = text[len(PREFIX):]
    parts = body.split(".")
    if len(parts) != 2:
        raise CardPayloadError("malformed payload body")
    try:
        pub = base64.urlsafe_b64decode(parts[0].encode("ascii"))
        alias = base64.urlsafe_b64decode(
            parts[1].encode("ascii")).decode("utf-8")
    except Exception as e:
        raise CardPayloadError("payload encoding failed: %s" % e)
    if len(pub) != PUB_BYTES:
        raise CardPayloadError(
            "public key must be %d bytes, got %d" % (PUB_BYTES, len(pub)))
    alias = alias.strip()
    if not alias:
        raise CardPayloadError("alias must not be empty")
    if len(alias.encode("utf-8")) > ALIAS_MAX_BYTES:
        raise CardPayloadError("alias exceeds %d bytes" % ALIAS_MAX_BYTES)
    return pub, alias


def derive_user_hash(pub_key):
    """Derive the 32-hex lxmf.delivery user hash from a public keyset.

    Mirrors the client's own identity boot (kivy_client: client_dest =
    Destination(identity, OUT, SINGLE, "lxmf", "delivery")), so a
    scanned pubkey yields exactly the address the guild keys members
    by. Lazy RNS import keeps payload-only testing dependency-free.
    """
    import RNS

    if not isinstance(pub_key, (bytes, bytearray)):
        raise CardPayloadError("public key must be bytes")
    if len(pub_key) != PUB_BYTES:
        raise CardPayloadError(
            "public key must be %d bytes, got %d" % (PUB_BYTES, len(pub_key)))
    empty = RNS.Identity(create_keys=False)
    # RNS 1.1.3 load_public_key returns None even on success; it populates
    # self.pub/self.sig_pub, which stay None when the bytes are invalid.
    empty.load_public_key(bytes(pub_key))
    if empty.pub is None or empty.sig_pub is None:
        raise CardPayloadError("public key invalid for RNS")
    dest = RNS.Destination(empty, RNS.Destination.OUT,
                           RNS.Destination.SINGLE, "lxmf", "delivery")
    return dest.hash.hex()


def render_qr_png(payload, box_size=8, border=2, fill="#000000", back="#FFFFFF"):
    """Render a payload as a QR PNG at ECC level H. Returns PNG bytes."""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_H
    except Exception as e:
        raise CardPayloadError("qrcode library unavailable: %s" % e)
    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_H, box_size=box_size, border=border)
    qr.add_data(payload.encode("utf-8"))
    qr.make(fit=True)
    img = qr.make_image(fill_color=fill, back_color=back)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def composite_center(qr_png, avatar_png, out_px=512):
    """Composite an avatar mid-code with margin-aware clearance.

    ECC-H tolerates up to ~30% occlusion; the avatar is scaled to fit
    inside ~23% of the code width with padding, then pasted at the
    exact center over a white plate. Returns PNG bytes.
    """
    try:
        from PIL import Image
    except Exception as e:
        raise CardPayloadError("pillow unavailable: %s" % e)
    code = Image.open(io.BytesIO(qr_png)).convert("RGBA")
    plate = int(code.width * 0.28)
    cover = Image.new("RGBA", (plate, plate), (255, 255, 255, 255))
    pos = ((code.width - plate) // 2, (code.height - plate) // 2)
    if avatar_png is None:
        code.paste(cover, pos)
    else:
        avatar = Image.open(io.BytesIO(avatar_png)).convert("RGBA")
        inner = int(plate * 0.86)
        avatar = avatar.resize((inner, inner), Image.Resampling.LANCZOS)
        cover.paste(avatar, ((plate - inner) // 2,) * 2, avatar)
        code.paste(cover, pos)
    if code.width != out_px or code.height != out_px:
        code = code.resize((out_px, out_px), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    code.save(buf, format="PNG")
    return buf.getvalue()
