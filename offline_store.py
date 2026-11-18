"""Bounded local chat cache, isolated by guild and channel.

This cache is a readable local copy, not an end-to-end encrypted archive.
It deliberately stores metadata only for attachments, never blob bytes.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

_LOCK = threading.RLock()
MAX_PER_CHANNEL = 500


@contextmanager
def _connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.execute("CREATE TABLE IF NOT EXISTS cached_messages ("
               "guild TEXT NOT NULL, channel TEXT NOT NULL, "
               "msg_id TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL, "
               "PRIMARY KEY(guild, channel, msg_id))")
    db.execute("CREATE INDEX IF NOT EXISTS cache_order ON cached_messages "
               "(guild, channel, ts DESC)")
    try:
        with db:
            yield db
    finally:
        db.close()


def _safe_row(message, channel):
    if (not isinstance(message, dict) or not message.get("msg_id")
            or message.get("deleted_at")):
        return None
    row = {key: message.get(key) for key in
           ("msg_id", "author_hash", "username", "content", "ts",
            "edited_at", "deleted_at")}
    row["channel_id"] = channel
    row["attachments"] = [
        {key: att.get(key) for key in ("name", "mime", "size", "path", "kind")}
        for att in (message.get("attachments") or []) if isinstance(att, dict)
    ][:12]
    try:
        row["ts"] = float(row["ts"] or 0)
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return (str(row["msg_id"]), row["ts"], encoded)


def put(path, guild, channel, messages, cap=MAX_PER_CHANNEL):
    if not guild or not channel:
        return 0
    rows = [_safe_row(message, channel) for message in messages]
    rows = [row for row in rows if row]
    deleted_ids = [str(message["msg_id"]) for message in messages
                   if isinstance(message, dict) and message.get("msg_id")
                   and message.get("deleted_at")]
    with _LOCK, _connect(path) as db:
        db.executemany("DELETE FROM cached_messages WHERE guild=? AND channel=? "
                       "AND msg_id=?", [(guild, channel, mid) for mid in deleted_ids])
        db.executemany("INSERT OR REPLACE INTO cached_messages "
                       "(guild,channel,msg_id,ts,data) VALUES (?,?,?,?,?)",
                       [(guild, channel, *row) for row in rows])
        db.execute("DELETE FROM cached_messages WHERE guild=? AND channel=? "
                   "AND msg_id NOT IN (SELECT msg_id FROM cached_messages "
                   "WHERE guild=? AND channel=? ORDER BY ts DESC,msg_id DESC LIMIT ?)",
                   (guild, channel, guild, channel, cap))
    return len(rows)


def recent(path, guild, channel, limit=100):
    if not guild or not channel or not os.path.exists(path):
        return []
    with _LOCK, _connect(path) as db:
        rows = db.execute("SELECT data FROM cached_messages WHERE guild=? "
                          "AND channel=? ORDER BY ts DESC,msg_id DESC LIMIT ?",
                          (guild, channel, max(1, min(limit, MAX_PER_CHANNEL)))).fetchall()
    return [json.loads(row[0]) for row in reversed(rows)]


def all_messages(path, guild):
    if not guild or not os.path.exists(path):
        return []
    with _LOCK, _connect(path) as db:
        rows = db.execute("SELECT data FROM cached_messages WHERE guild=? "
                          "ORDER BY channel,ts,msg_id", (guild,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def apply_update(path, guild, channel, msg_id, content=None, deleted=False):
    if not guild or not channel or not msg_id or not os.path.exists(path):
        return
    with _LOCK, _connect(path) as db:
        key = (guild, channel, msg_id)
        if deleted:
            db.execute("DELETE FROM cached_messages WHERE guild=? AND channel=? "
                       "AND msg_id=?", key)
        else:
            row = db.execute("SELECT data FROM cached_messages WHERE guild=? "
                             "AND channel=? AND msg_id=?", key).fetchone()
            if row:
                message = json.loads(row[0])
                message["content"] = content or ""
                db.execute("UPDATE cached_messages SET data=? WHERE guild=? "
                           "AND channel=? AND msg_id=?",
                           (json.dumps(message, ensure_ascii=False), *key))


def forget_guild(path, guild):
    if guild and os.path.exists(path):
        with _LOCK, _connect(path) as db:
            db.execute("DELETE FROM cached_messages WHERE guild=?", (guild,))


def forget_author(path, guild, author_hash):
    """Remove one identity's cached posts after the guild confirms erasure."""
    if guild and author_hash and os.path.exists(path):
        with _LOCK, _connect(path) as db:
            rows = db.execute("SELECT channel,msg_id,data FROM cached_messages "
                              "WHERE guild=?", (guild,)).fetchall()
            db.executemany(
                "DELETE FROM cached_messages WHERE guild=? AND channel=? AND msg_id=?",
                [(guild, channel, msg_id)
                 for channel, msg_id, data in rows
                 if json.loads(data).get("author_hash") == author_hash])
