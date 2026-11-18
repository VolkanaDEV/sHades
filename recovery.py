"""Portable, passphrase-encrypted identity kit. No network or Kivy imports."""
import base64
import binascii
import json
import os
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT = "shades-recovery-v1"
MAX_KIT_BYTES = 1024 * 1024


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def _decode(value):
    return base64.b64decode(value, validate=True)


def _key(passphrase, salt):
    if not isinstance(passphrase, str) or len(passphrase) < 12:
        raise ValueError("use a recovery phrase of at least 12 characters")
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(
        passphrase.encode("utf-8"))


def create_kit(identity_bytes, passphrase, profile=None):
    """Encrypt private RNS key bytes and small portable profile metadata."""
    if not isinstance(identity_bytes, bytes) or not 32 <= len(identity_bytes) <= 4096:
        raise ValueError("identity file is missing or invalid")
    payload = json.dumps({"identity": _b64(identity_bytes),
                          "profile": profile or {}}, separators=(",", ":")).encode()
    if len(payload) > MAX_KIT_BYTES // 2:
        raise ValueError("profile is too large for a recovery kit")
    salt, nonce = os.urandom(16), os.urandom(12)
    ciphertext = AESGCM(_key(passphrase, salt)).encrypt(
        nonce, payload, FORMAT.encode())
    return json.dumps({"format": FORMAT, "salt": _b64(salt),
                       "nonce": _b64(nonce), "ciphertext": _b64(ciphertext)},
                      separators=(",", ":")).encode("utf-8")


def open_kit(kit_bytes, passphrase):
    """Return (identity bytes, profile) or a safe, user-facing ValueError."""
    try:
        if not isinstance(kit_bytes, bytes) or len(kit_bytes) > MAX_KIT_BYTES:
            raise ValueError("kit is too large")
        envelope = json.loads(kit_bytes)
        if envelope.get("format") != FORMAT:
            raise ValueError("unsupported recovery kit")
        salt, nonce = _decode(envelope["salt"]), _decode(envelope["nonce"])
        if len(salt) != 16 or len(nonce) != 12:
            raise ValueError("invalid recovery kit")
        plaintext = AESGCM(_key(passphrase, salt)).decrypt(
            nonce, _decode(envelope["ciphertext"]), FORMAT.encode())
        payload = json.loads(plaintext)
        identity = _decode(payload["identity"])
        profile = payload.get("profile") or {}
        if not 32 <= len(identity) <= 4096 or not isinstance(profile, dict):
            raise ValueError("invalid recovery kit")
        return identity, profile
    except InvalidTag as exc:
        raise ValueError("wrong phrase or damaged recovery kit") from exc
    except (KeyError, TypeError, UnicodeError, binascii.Error,
            json.JSONDecodeError) as exc:
        raise ValueError("invalid recovery kit") from exc


def write_private(path, data, replace=False):
    """Atomic 0600 write; restoring an identity never silently replaces one."""
    if not replace and os.path.exists(path):
        raise FileExistsError("an identity already exists on this device")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".shades-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if not replace and os.path.exists(path):
            raise FileExistsError("an identity already exists on this device")
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
