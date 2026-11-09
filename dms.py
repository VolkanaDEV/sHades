#!/usr/bin/env python3
"""Friends roster + client-local DM history (U7). Pure stdlib, NO Kivy
imports — importable headless for tests.

Friends live in app data as friends.json: [{"user_hash", "nickname"}, ...].
DM threads live as dms/<user_hash>.json: [{"ts", "dir", "text"}, ...] with
dir "in" (from peer) or "out" (sent by me). The guild never sees either —
DM transport is direct LXMF client-to-client; the roster is a local file.

A user_hash is the 32-hex lxmf.delivery destination hash (the guild's
canonical user key), NOT the 64-hex channel-id length.
"""
import json
import os
import re
import threading

_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
HISTORY_CAP = 200

_lock = threading.Lock()


def is_user_hash(h):
    return bool(h) and bool(_HASH_RE.match(h))


def load_friends(path):
    """Load friends.json; missing/corrupt -> [] (never raises)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for e in data:
        if isinstance(e, dict) and is_user_hash(e.get("user_hash")):
            out.append({"user_hash": e["user_hash"],
                        "nickname": str(e.get("nickname") or "")[:32]})
    return out


def save_friends(path, friends):
    with _lock:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(friends, f, indent=1)
        os.replace(tmp, path)


def is_friend(friends, user_hash):
    return any(f["user_hash"] == user_hash for f in friends)


def add_friend(friends, user_hash, nickname):
    """Add or update (re-nickname) by hash; returns the new list."""
    if not is_user_hash(user_hash):
        raise ValueError("user_hash must be 32 hex")
    out = [f for f in friends if f["user_hash"] != user_hash]
    out.append({"user_hash": user_hash,
                "nickname": (nickname or "")[:32]})
    out.sort(key=lambda f: f["nickname"].lower())
    return out


def remove_friend(friends, user_hash):
    return [f for f in friends if f["user_hash"] != user_hash]


def dm_history_path(app_data_dir, user_hash):
    if not is_user_hash(user_hash):
        raise ValueError("user_hash must be 32 hex")
    return os.path.join(app_data_dir, "dms", user_hash + ".json")


def load_dm_history(path):
    """Load a DM thread; missing/corrupt -> [] (never raises)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data
            if isinstance(e, dict) and isinstance(e.get("text"), str)
            and e.get("dir") in ("in", "out")]


def append_dm(path, direction, text, cap=HISTORY_CAP):
    """Append one DM row atomically, trimming to the newest `cap` rows."""
    if direction not in ("in", "out"):
        raise ValueError("direction must be 'in' or 'out'")
    row = {"ts": __import__("time").time(), "dir": direction,
           "text": str(text)[:4000]}
    with _lock:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        rows = load_dm_history(path)
        rows.append(row)
        rows = rows[-cap:]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rows, f, indent=1)
        os.replace(tmp, path)
    return row
