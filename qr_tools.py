"""QR payloads for an invite or a minimal encrypted identity transfer."""
import base64
import binascii

PREFIX = "shades-kit:"


def encode_kit(kit):
    if not isinstance(kit, bytes) or len(kit) > 1400:
        raise ValueError("identity kit is too large for one QR; use file export")
    return PREFIX + base64.urlsafe_b64encode(kit).decode("ascii")


def decode_kit(text):
    if not isinstance(text, str) or not text.startswith(PREFIX) or len(text) > 2000:
        raise ValueError("not a recovery QR")
    try:
        return base64.b64decode(text[len(PREFIX):], altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid recovery QR") from exc


def make_qr(value, path):
    if not isinstance(value, str) or not value or len(value) > 2000:
        raise ValueError("QR content is too long")
    import qrcode
    code = qrcode.make(value, border=3, box_size=8,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.save(path)
    return path
