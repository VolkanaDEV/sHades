#!/usr/bin/env python3
"""Invite share-links (U6a): parse/build `shades://<host>:<port>/<node_hash>`.

Pure stdlib, NO Kivy imports — importable headless for tests.

The link carries the RNS *interface* endpoint (hub address + port) and the
guild's 32-hex node hash. This is addressing for Reticulum transport only —
there is no HTTP, no URL fetch, nothing web here: the client dials the hub
through RNS and everything after that rides RNS links (JSON v1 / msgpack /
micron as raw bytes). Both pieces are optional: `shades://<host>:<port>`
(address only, hashes auto-discover from the hub announce) and, on LANs where
AutoInterface announces, a bare 32-hex node hash is enough by itself.
"""
import re

# 32-hex node/delivery hash
_HASH_RE = re.compile(r"^[0-9a-f]{32}$")


def build_invite(host, port, node_hash=None):
    """Canonical invite string. port may be int or numeric string."""
    link = "shades://%s:%s" % (host, port)
    if node_hash:
        link += "/" + str(node_hash)
    return link


def parse_invite(text):
    """Parse an invite string into {host, port, node_hash}.

    Tolerant of surrounding whitespace, SHADES:// case, a missing hash, a
    missing port, and bracketed IPv6 hosts ([::1]:42421/<hash>). Returns
    None when `text` is not an invite at all; fields the link does not
    carry (or that fail validation) come back as None so callers can
    prefill only what is present and leave auto-discovery to the rest.
    """
    if not text:
        return None
    t = text.strip()
    m = re.match(r"^shades://(.+)$", t, re.IGNORECASE)
    if not m:
        return None
    rest = m.group(1).strip()
    # split off the optional /<node_hash> path part
    node_hash = None
    if "/" in rest:
        rest, _, tail = rest.partition("/")
        tail = tail.strip().rstrip("/")   # tolerate a trailing slash
        if _HASH_RE.match(tail.lower()):
            node_hash = tail.lower()
    rest = rest.strip()
    host, port = None, None
    if rest:
        if rest.startswith("["):          # bracketed IPv6 literal
            host_part, _, port_part = rest[1:].partition("]")
            host = host_part.strip()
            if port_part.startswith(":"):
                port = port_part[1:].strip()
        elif ":" in rest:
            host_part, _, port_part = rest.rpartition(":")
            host = host_part.strip()
            port = port_part.strip()
        else:
            host = rest
    if host == "":
        host = None
    if port is not None:
        if not re.match(r"^[0-9]{1,5}$", port) or not (1 <= int(port) <= 65535):
            port = None
        else:
            port = int(port)
    return {"host": host, "port": port, "node_hash": node_hash}
