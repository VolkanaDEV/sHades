"""Signed release manifests for the update channel (trust model, step 1).

The .sha256 asset answers "do these bytes match what GitHub served".
This module answers the different question: "did the project sign these
bytes". It is deliberately transport-free and release-process-free:

- Signature scheme: ed25519 over the RAW manifest bytes (the exact bytes
  published as sHades-server-<V>.manifest.json). No canonicalization is
  attempted; sign what you fetch.
- The signature travels as the raw 64-byte signature, hex- or base64-
  encoded (64 bytes hex = 128 chars; anything else is tried as base64).
- The manifest is JSON with this shape:

    {"version": "0.36.25",
     "min_version": "0.30.0",
     "channel": "general",
     "issued_at": "2026-11-22T00:00:00Z",
     "expires_at": "2026-12-22T00:00:00Z",
     "artifact": {"name": "sHades-server-0.36.25.tar.gz",
                  "sha256": "<64 hex>",
                  "size": 123456}}

- Nothing here decides WHO holds the release key or WHEN enforcement
  flips. Policy (off|log|enforce) and the key are the caller's concern;
  updater.py wires both from config. The shipped default is off.
"""
import base64
import binascii
import datetime
import json


class ManifestError(Exception):
    pass


def _parse_time(s, field):
    if not isinstance(s, str):
        raise ManifestError("%s missing or not a string" % field)
    try:
        t = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ManifestError("%s is not ISO-8601: %r" % (field, s))
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t


def load_public_key(key_material):
    """Accept PEM text, base64 of 32 raw bytes, or hex of 32 raw bytes."""
    if not key_material:
        raise ManifestError("no release public key configured")
    key_material = key_material.strip()
    try:
        from cryptography.hazmat.primitives.serialization import (
            load_pem_public_key, Encoding, PublicFormat)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
    except ImportError:
        raise ManifestError("cryptography package is required for "
                            "manifest verification")
    if "BEGIN PUBLIC KEY" in key_material:
        pem = load_pem_public_key(key_material.encode("utf-8"))
        if not isinstance(pem, Ed25519PublicKey):
            raise ManifestError("PEM public key is not ed25519")
        return pem
    raw = None
    for decoder in (
            lambda s: base64.b64decode(s, validate=True),
            lambda s: bytes.fromhex(s)):
        try:
            cand = decoder(key_material)
        except (ValueError, binascii.Error):
            continue
        if len(cand) == 32:
            raw = cand
            break
    if raw is None:
        raise ManifestError("release public key is neither PEM nor a "
                            "32-byte base64/hex raw key")
    return Ed25519PublicKey.from_public_bytes(raw)


def parse_manifest(manifest_bytes):
    try:
        m = json.loads(manifest_bytes)
    except ValueError as e:
        raise ManifestError("manifest is not JSON: %s" % e)
    if not isinstance(m, dict):
        raise ManifestError("manifest is not a JSON object")
    art = m.get("artifact")
    if not isinstance(art, dict):
        raise ManifestError("manifest missing artifact object")
    try:
        version = str(m["version"])
        min_version = str(m["min_version"])
        channel = str(m["channel"])
        name = str(art["name"])
        sha = str(art["sha256"]).lower()
        size = int(art["size"])
    except (KeyError, TypeError, ValueError):
        raise ManifestError("manifest missing/invalid required fields")
    return {
        "version": version,
        "min_version": min_version,
        "channel": channel,
        "issued_at": _parse_time(m.get("issued_at"), "issued_at"),
        "expires_at": _parse_time(m.get("expires_at"), "expires_at"),
        "artifact": {"name": name, "sha256": sha, "size": size},
    }


def _decode_sig(sig_bytes):
    s = sig_bytes.strip()
    if len(s) == 128 and all(c in b"0123456789abcdefABCDEF" for c in s):
        try:
            return bytes.fromhex(s.decode("ascii"))
        except ValueError:
            pass
    try:
        return base64.b64decode(s, validate=True)
    except (ValueError, binascii.Error):
        raise ManifestError("signature is neither hex nor base64")


def verify_manifest(manifest_bytes, sig_bytes, key_material,
                    current_version=None, channel=None, now=None):
    """Verify signature and freshness. Returns the parsed manifest dict.
    Raises ManifestError on any failure. Enforcement policy is the
    caller's decision; this function only answers yes/no, loudly."""
    if not manifest_bytes:
        raise ManifestError("manifest fetch came back empty")
    m = parse_manifest(manifest_bytes)
    pub = load_public_key(key_material)
    sig = _decode_sig(sig_bytes)
    try:
        pub.verify(sig, manifest_bytes)
    except Exception:
        raise ManifestError("signature does not match the manifest")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now > m["expires_at"]:
        raise ManifestError("manifest expired at %s" % m["expires_at"]
                            .isoformat())
    if now < m["issued_at"] - datetime.timedelta(hours=24):
        raise ManifestError("manifest issued_at is in the future")
    if current_version is not None:
        from updater import parse_version
        cur = parse_version(current_version)
        minv = parse_version(m["min_version"])
        if minv is None:
            raise ManifestError("manifest min_version unparseable: %r"
                                % m["min_version"])
        if cur is not None and cur < minv:
            raise ManifestError(
                "running %s is below the manifest's min_version %s; "
                "this release may not install on this build"
                % (current_version, m["min_version"]))
    if channel is not None and m["channel"] != channel:
        raise ManifestError("manifest channel %r does not match this "
                            "client's channel %r" % (m["channel"], channel))
    return m


def check_artifact(manifest, data):
    """Bind the downloaded bytes to the manifest. Raises ManifestError."""
    import hashlib
    art = manifest["artifact"]
    if len(data) != art["size"]:
        raise ManifestError("artifact size %d != manifest size %d"
                            % (len(data), art["size"]))
    got = hashlib.sha256(data).hexdigest()
    if got != art["sha256"]:
        raise ManifestError("artifact sha256 mismatch vs manifest")
    return True
