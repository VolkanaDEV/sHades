#!/usr/bin/env python3
"""sHades guild storage — SQLite schema, connection, channel/sequence helpers.

Carved out of sHades.py (storage layer, step 1). The functions here are
connection-explicit: they take the sqlite3 connection (and lock) as arguments
and never touch module-global state. sHades.py keeps owning the global `db` /
`db_lock` — and the unit tests keep assigning `sHades.db` directly — so the
thin wrappers there bind the guild connection at call time:

    sHades.ensure_channel(name)  ->  store.ensure_channel(sHades.db, ...)

Nothing in this module imports the server itself, so it stays import-safe for
unit tests (only stdlib + RNS, for the channel-id hash).
"""

import re
import sqlite3
import time

import RNS

# Channel kinds: chat, a NomadNet micron "site", or a terminal stream.
CHANNEL_KINDS = ("chat", "site", "term", "announce")

# ----------------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------------

# Bots table DDL lives in its own constant so SCHEMA (fresh DBs) and
# migrate_schema (already-booted DBs) can never drift apart.
BOTS_DDL = """CREATE TABLE IF NOT EXISTS bots (
    bot_id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, name TEXT NOT NULL,
    key_hash TEXT NOT NULL, perms INTEGER NOT NULL DEFAULT 0,
    created_at REAL, revoked_at REAL, last_seen REAL);"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    id TEXT PRIMARY KEY, name TEXT, node_hash TEXT, delivery_hash TEXT, prop_hash TEXT);
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, created REAL,
    kind TEXT NOT NULL DEFAULT 'chat', manifest TEXT NOT NULL DEFAULT '{}',
    topic TEXT, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS channel_state (
    channel_id TEXT PRIMARY KEY, seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS members (
    channel_id TEXT, member_hash TEXT, last_post REAL,
    PRIMARY KEY (channel_id, member_hash));
CREATE TABLE IF NOT EXISTS users (
    dest_hash TEXT PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE,
    profile_node TEXT, avatar_path TEXT, registered_at REAL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id TEXT UNIQUE,
    channel_id TEXT, ts REAL, author_hash TEXT, content TEXT,
    edited_at REAL, deleted_at REAL);
CREATE TABLE IF NOT EXISTS message_edits (
    msg_id TEXT NOT NULL, edited_at REAL NOT NULL, prior_content TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_message_edits ON message_edits(msg_id, edited_at);
CREATE TABLE IF NOT EXISTS attachments (
    msg_id TEXT, idx INTEGER, name TEXT, mime TEXT, size INTEGER, path TEXT,
    PRIMARY KEY (msg_id, idx));
CREATE TABLE IF NOT EXISTS blobs (
    id TEXT PRIMARY KEY, name TEXT, mime TEXT, size INTEGER, path TEXT,
    ts REAL, by TEXT);
CREATE TABLE IF NOT EXISTS banned (
    dest_hash TEXT PRIMARY KEY, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS muted (
    dest_hash TEXT PRIMARY KEY, reason TEXT, at REAL, by TEXT);
CREATE TABLE IF NOT EXISTS receipts (
    channel_id TEXT, user_hash TEXT, last_id INTEGER NOT NULL DEFAULT 0, ts REAL,
    PRIMARY KEY (channel_id, user_hash));
CREATE TABLE IF NOT EXISTS reactions (
    msg_id TEXT, user_hash TEXT, emoji TEXT, ts REAL,
    PRIMARY KEY (msg_id, user_hash, emoji));
CREATE TABLE IF NOT EXISTS roles (
    role_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE COLLATE NOCASE, color TEXT, position INTEGER DEFAULT 0,
    perms INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_roles (
    user_hash TEXT, role_id INTEGER,
    PRIMARY KEY (user_hash, role_id));
CREATE TABLE IF NOT EXISTS channel_overwrites (
    channel_id TEXT, role_id INTEGER, perms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, role_id));
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, actor TEXT, action TEXT, target TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS server_config (
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY, label TEXT, created_at REAL, expires_at REAL,
    max_uses INTEGER NOT NULL DEFAULT 0, uses INTEGER NOT NULL DEFAULT 0,
    created_by TEXT, channel_id TEXT);
CREATE TABLE IF NOT EXISTS guild_joins (
    user_hash TEXT PRIMARY KEY, joined_at REAL, code TEXT);
CREATE INDEX IF NOT EXISTS idx_reactions_msg ON reactions(msg_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
"""
SCHEMA += "\n" + BOTS_DDL + "\n"


def migrate_schema(conn):
    """Additive migrations for databases written by earlier versions.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so a column added
    to SCHEMA after a guild has already booted has to be added explicitly."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
    for col, decl in (("kind", "TEXT NOT NULL DEFAULT 'chat'"), ("manifest", "TEXT"),
                      ("topic", "TEXT"), ("position", "INTEGER NOT NULL DEFAULT 0")):
        if col not in have:
            conn.execute("ALTER TABLE channels ADD COLUMN %s %s" % (col, decl))
    have_inv = {r["name"] for r in conn.execute("PRAGMA table_info(invites)").fetchall()}
    if "channel_id" not in have_inv:
        conn.execute("ALTER TABLE invites ADD COLUMN channel_id TEXT")
    # Bots (Group 1): a DB created before the bots table existed has no
    # bots row in sqlite_master; CREATE TABLE IF NOT EXISTS adds it.
    has_bots = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='bots'").fetchone()
    if not has_bots:
        conn.execute(BOTS_DDL)


def open_db(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(SCHEMA)
    migrate_schema(conn)
    conn.commit()
    return conn


def channel_id_for(guild_id, name):
    """A channel's id is a hash of its name, so it is case-sensitive."""
    return RNS.hexrep(RNS.Identity.full_hash(f"{guild_id}:{name}".encode()),
                      delimit=False)


def ensure_channel(conn, lock, guild_id, name, kind="chat", emit=None):
    """Create a channel + its revision counter if needed. Returns channel id.

    A channel's id is a hash of its name, so it is case-sensitive, while the
    channels.name unique index is COLLATE NOCASE. The name lookup below is
    therefore NOCASE: '#General' resolves to the existing 'general' channel
    instead of computing a different id that the unique index then refuses.

    `emit`, when given, is called as emit(channel_id, event, payload,
    guild_wide=True) for a newly created channel.
    """
    with lock:
        row = conn.execute("SELECT id FROM channels WHERE name=? COLLATE NOCASE",
                           (name,)).fetchone()
        if row:
            return row["id"]
        cid = channel_id_for(guild_id, name)
        conn.execute("INSERT OR IGNORE INTO channels (id, name, created, kind) "
                     "VALUES (?, ?, ?, ?)",
                     (cid, name, time.time(), kind if kind in CHANNEL_KINDS else "chat"))
        conn.execute("INSERT OR IGNORE INTO channel_state (channel_id, seq) VALUES (?, 0)", (cid,))
        conn.commit()
        # v0.28: a NEW channel must reach stream subscribers (the 3s state
        # poller that used to notice renames/creates is being deleted);
        # name changes ride the same event.
        if emit is not None:
            emit(cid, "channels_changed", {"channel": name}, guild_wide=True)
    return cid


def channel_name(conn, cid):
    row = conn.execute("SELECT name FROM channels WHERE id=?", (cid,)).fetchone()
    return row["name"] if row else "unknown"


def bump_seq(conn, cid):
    conn.execute("UPDATE channel_state SET seq = seq + 1 WHERE channel_id=?", (cid,))


def get_seq(conn, cid):
    row = conn.execute("SELECT seq FROM channel_state WHERE channel_id=?", (cid,)).fetchone()
    return row["seq"] if row else 0

# ----------------------------------------------------------------------------------
# Moderation (banned / muted)
# ----------------------------------------------------------------------------------

def is_banned(conn, lock, sender):
    with lock:
        row = conn.execute("SELECT 1 FROM banned WHERE dest_hash=?",
                           (sender,)).fetchone()
    return row is not None


def ban_user(conn, lock, user, reason="", by=None):
    # `by` is accepted for the sHades.py wrapper's signature; the banned
    # table carries no `by` column.
    with lock:
        conn.execute("INSERT OR REPLACE INTO banned "
                     "(dest_hash, reason, at) VALUES (?, ?, ?)",
                     (user, (reason or "")[:200], time.time()))
        conn.commit()


def unban_user(conn, lock, user):
    with lock:
        conn.execute("DELETE FROM banned WHERE dest_hash=?", (user,))
        conn.commit()


def is_muted(conn, lock, sender):
    with lock:
        row = conn.execute("SELECT 1 FROM muted WHERE dest_hash=?",
                           (sender,)).fetchone()
    return row is not None


def mute_user(conn, lock, user, reason="", by=None):
    with lock:
        conn.execute("INSERT OR REPLACE INTO muted "
                     "(dest_hash, reason, at, by) VALUES (?, ?, ?, ?)",
                     (user, (reason or "")[:200], time.time(), by))
        conn.commit()


def unmute_user(conn, lock, user):
    with lock:
        conn.execute("DELETE FROM muted WHERE dest_hash=?", (user,))
        conn.commit()

# ----------------------------------------------------------------------------------
# Roles and permissions
# ----------------------------------------------------------------------------------

def user_has_role_perm(conn, lock, user, bits):
    """The SQL half of sHades.has_role_perm: does any of the user's roles
    carry any of `bits`? The legacy MODERATORS-set check lives in sHades."""
    if user is None:
        return False
    with lock:
        row = conn.execute(
            "SELECT 1 FROM user_roles ur JOIN roles r ON r.role_id = ur.role_id "
            "WHERE ur.user_hash = ? AND (r.perms & ?) != 0 LIMIT 1",
            (user, bits)).fetchone()
    return row is not None


def effective_perms(conn, lock, user_hash, channel_id, base):
    """The union: base + each of the user's roles' perms + each role's
    per-channel overwrite."""
    if user_hash is None:
        return 0
    with lock:
        rows = conn.execute(
            "SELECT co.perms AS operms, r.perms AS rperms "
            "FROM user_roles ur JOIN roles r ON r.role_id = ur.role_id "
            "LEFT JOIN channel_overwrites co ON co.role_id = ur.role_id "
            "AND co.channel_id=? WHERE ur.user_hash=?",
            (channel_id, user_hash)).fetchall()
    perms = base
    for r in rows:
        perms |= (r["operms"] or 0) | (r["rperms"] or 0)
    return perms


def channel_base_perms(conn, lock, channel_id):
    """The @everyone (role_id=0) overwrite for one channel, or None."""
    with lock:
        row = conn.execute("SELECT perms FROM channel_overwrites "
                           "WHERE channel_id=? AND role_id=0",
                           (channel_id,)).fetchone()
    return row["perms"] if row else None


def ensure_role(conn, lock, name, perms, position=0, color=None):
    """Create a role if missing. Returns id."""
    with lock:
        row = conn.execute("SELECT role_id FROM roles WHERE name=? COLLATE NOCASE",
                           (name,)).fetchone()
        if row:
            return row["role_id"]
        cur = conn.execute("INSERT INTO roles (name, color, position, perms) "
                           "VALUES (?, ?, ?, ?)", (name, color, position, perms))
        conn.commit()
        return cur.lastrowid


def assign_role(conn, lock, user_hash, role_id):
    with lock:
        conn.execute("INSERT OR IGNORE INTO user_roles (user_hash, role_id) "
                     "VALUES (?, ?)", (user_hash, role_id))
        conn.commit()


def count_users(conn, lock):
    with lock:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

# ----------------------------------------------------------------------------------
# Users, receipts, message rows
# ----------------------------------------------------------------------------------

def user_row(conn, lock, author_hash):
    """One users row (username, profile_node) or None."""
    with lock:
        return conn.execute("SELECT username, profile_node FROM users "
                            "WHERE dest_hash=?", (author_hash,)).fetchone()


def get_username(conn, lock, author_hash):
    row = user_row(conn, lock, author_hash)
    return row["username"] if row else "user-" + author_hash[:6]


def receipt_last_id(conn, lock, cid, viewer):
    with lock:
        row = conn.execute("SELECT last_id FROM receipts "
                           "WHERE channel_id=? AND user_hash=?",
                           (cid, viewer)).fetchone()
    return row["last_id"] if row else 0


def unread_count(conn, lock, cid, last_id):
    with lock:
        row = conn.execute("SELECT COUNT(*) AS n FROM messages "
                           "WHERE channel_id=? AND id>?",
                           (cid, last_id)).fetchone()
    return row["n"]


def message_attachments(conn, lock, msg_id):
    with lock:
        return conn.execute("SELECT idx, name, mime, size, path FROM attachments "
                            "WHERE msg_id=? ORDER BY idx", (msg_id,)).fetchall()


def message_reactions(conn, lock, msg_id):
    with lock:
        return conn.execute("SELECT emoji, user_hash FROM reactions "
                            "WHERE msg_id=? ORDER BY ts", (msg_id,)).fetchall()

# ----------------------------------------------------------------------------------
# Audit log, server config, guild joins
# ----------------------------------------------------------------------------------

def audit(conn, lock, actor, action, target=None, detail=None):
    """Append one audit row. Never raises — an audit failure must not break
    the action that was being audited."""
    try:
        with lock:
            conn.execute("INSERT INTO audit_log (ts, actor, action, target, detail) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (time.time(), actor or "-", str(action)[:64],
                          (target or "")[:128], (detail or "")[:400]))
            conn.commit()
    except Exception as e:
        RNS.log("audit write failed: %s" % e, RNS.LOG_ERROR)


def config_get(conn, lock, key, default=None):
    with lock:
        row = conn.execute("SELECT value FROM server_config WHERE key=?",
                           (key,)).fetchone()
    return row["value"] if row else default


def config_set(conn, lock, key, value):
    with lock:
        conn.execute("INSERT INTO server_config (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))
        conn.commit()


def config_all(conn, lock):
    with lock:
        rows = conn.execute("SELECT key, value FROM server_config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def has_joined(conn, lock, user_hash):
    """True if this user already joined with a code or registered a
    username (so turning the join gate on never locks existing members
    out)."""
    if not user_hash:
        return False
    with lock:
        if conn.execute("SELECT 1 FROM guild_joins WHERE user_hash=?",
                        (user_hash,)).fetchone():
            return True
        row = conn.execute("SELECT username FROM users WHERE dest_hash=?",
                           (user_hash,)).fetchone()
    return bool(row and row["username"])

# ----------------------------------------------------------------------------------
# Channel transactions and blobs
# ----------------------------------------------------------------------------------

def create_channel(conn, lock, cid, name, kind, manifest_json, created):
    """Insert a channel + its revision counter if it does not exist.
    Returns (cid, None) or (None, error)."""
    with lock:
        if conn.execute("SELECT 1 FROM channels WHERE id=? OR (name=? COLLATE NOCASE)",
                        (cid, name)).fetchone():
            return None, "channel exists"
        # explicit ordering: new channels sink to the end of the drag list
        nxt = (conn.execute("SELECT COALESCE(MAX(position), 0) + 1 AS nxt "
                            "FROM channels").fetchone()["nxt"])
        conn.execute("INSERT INTO channels (id, name, created, kind, manifest, position) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (cid, name, created, kind, manifest_json, nxt))
        conn.execute("INSERT OR IGNORE INTO channel_state (channel_id, seq) VALUES (?, 0)", (cid,))
        conn.commit()
    return cid, None


def delete_channel_data(conn, lock, cid):
    """Look the channel up, refuse the last one, and cascade-delete its
    rows (messages, attachments, reactions, receipts, members, state).
    Returns (error_or_None, name_or_None)."""
    with lock:
        row = conn.execute("SELECT id, name FROM channels WHERE id=?", (cid,)).fetchone()
        if not row:
            return "unknown channel", None
        n = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
        if n <= 1:
            return "cannot delete the last channel", None
        name = row["name"]
        msg_ids = "SELECT msg_id FROM messages WHERE channel_id=?"
        conn.execute("DELETE FROM attachments WHERE msg_id IN (%s)" % msg_ids, (cid,))
        conn.execute("DELETE FROM reactions WHERE msg_id IN (%s)" % msg_ids, (cid,))
        conn.execute("DELETE FROM messages WHERE channel_id=?", (cid,))
        conn.execute("DELETE FROM receipts WHERE channel_id=?", (cid,))
        conn.execute("DELETE FROM members WHERE channel_id=?", (cid,))
        conn.execute("DELETE FROM channel_state WHERE channel_id=?", (cid,))
        conn.execute("DELETE FROM channels WHERE id=?", (cid,))
        conn.commit()
    return None, name


def record_blob(conn, lock, digest, name, mime, size, fname, by):
    """Insert one blobs metadata row (the database carries metadata only,
    never bytes)."""
    with lock:
        conn.execute("INSERT OR IGNORE INTO blobs (id, name, mime, size, path, ts, by) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (digest, name, mime, size, fname, time.time(), by))
        conn.commit()

# ----------------------------------------------------------------------------------
# H1 — message reads (channels, messages, users, roles, audit)
# ----------------------------------------------------------------------------------

def channel_row(conn, lock, cid):
    """One channels row (all columns) or None."""
    with lock:
        return conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()


def channel_exists(conn, lock, cid):
    with lock:
        return conn.execute("SELECT 1 FROM channels WHERE id=?",
                            (cid,)).fetchone() is not None


def list_channel_names(conn, lock):
    with lock:
        return conn.execute("SELECT id, name FROM channels ORDER BY name ASC").fetchall()


def list_channel_ids(conn, lock):
    with lock:
        return conn.execute("SELECT id FROM channels").fetchall()


def list_channels(conn, lock):
    """All channels with their stream seq, as plain dicts, ordered for
    the drag list. The seq is read under the same lock as the rows."""
    with lock:
        rows = conn.execute("SELECT id, name, kind, created, topic, position FROM channels "
                            "ORDER BY position ASC, name ASC").fetchall()
        return [{"id": r["id"], "name": r["name"], "kind": r["kind"],
                 "created": r["created"], "topic": r["topic"],
                 "position": r["position"], "seq": get_seq(conn, r["id"])}
                for r in rows]


def channel_members(conn, lock, cid):
    with lock:
        return conn.execute(
            "SELECT m.member_hash, m.last_post, u.username FROM members m "
            "LEFT JOIN users u ON u.dest_hash = m.member_hash "
            "WHERE m.channel_id=? ORDER BY m.last_post DESC LIMIT 100", (cid,)).fetchall()


def list_banned(conn, lock):
    with lock:
        return conn.execute(
            "SELECT b.dest_hash, b.reason, b.at, u.username FROM banned b "
            "LEFT JOIN users u ON u.dest_hash = b.dest_hash ORDER BY b.at DESC").fetchall()


def channel_messages(conn, lock, cid, before, limit):
    """Newest-first page of a channel's messages. `before` is an integer
    message id (or None for the latest page)."""
    with lock:
        if before is not None:
            return conn.execute(
                "SELECT * FROM messages WHERE channel_id=? AND id<? "
                "ORDER BY id DESC LIMIT ?", (cid, before, limit)).fetchall()
        return conn.execute(
            "SELECT * FROM messages WHERE channel_id=? "
            "ORDER BY id DESC LIMIT ?", (cid, limit)).fetchall()


def count_messages_before(conn, lock, cid, oldest):
    with lock:
        row = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE channel_id=? AND id<?",
                           (cid, oldest)).fetchone()
    return row["n"]


def message_by_msg_id(conn, lock, msg_id):
    """One full messages row by its msg_id hash, or None."""
    with lock:
        return conn.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()


def latest_message_stamp(conn, lock, cid):
    """The newest message's id and latest edit/delete timestamp, or None."""
    with lock:
        return conn.execute("SELECT msg_id, "
                            "max(coalesce(edited_at,0), coalesce(deleted_at,0)) AS mt "
                            "FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT 1",
                            (cid,)).fetchone()


def fetch_messages(conn, lock, cid, since_id, since_ts, before, limit_raw):
    """The /api/messages delta block, verbatim: channel check, seq, then
    the before / since_id / default branch plus the since_ts updates.
    Returns (seq, new_rows, upd_rows), or None for an unknown channel."""
    with lock:
        if not conn.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return None
        seq = get_seq(conn, cid)
        limit = min(int(limit_raw), 200)
        if before:
            brow = conn.execute("SELECT id FROM messages WHERE msg_id=?", (before,)).fetchone()
            new_rows = conn.execute(
                "SELECT * FROM messages WHERE channel_id=? AND id<? ORDER BY id DESC LIMIT ?",
                (cid, brow["id"] if brow else 0, limit)).fetchall()
            new_rows = list(reversed(new_rows))
        elif isinstance(since_id, int):
            new_rows = conn.execute(
                "SELECT * FROM messages WHERE channel_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (cid, since_id, limit)).fetchall()
        elif isinstance(since_id, str) and since_id:
            # since_id is a msg_id hash string, not the integer PK. Resolve it
            # first so delta polling returns rows after the last seen message.
            bound = conn.execute(
                "SELECT id FROM messages WHERE msg_id=? AND channel_id=?",
                (since_id, cid)).fetchone()
            if bound:
                new_rows = conn.execute(
                    "SELECT * FROM messages WHERE channel_id=? AND id>? ORDER BY id ASC LIMIT ?",
                    (cid, bound["id"], limit)).fetchall()
            else:
                new_rows = []
        else:
            new_rows = conn.execute(
                "SELECT * FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT ?",
                (cid, limit)).fetchall()
            new_rows = list(reversed(new_rows))
        upd_rows = []
        if isinstance(since_ts, (int, float)) and since_ts > 0:
            id_bound = since_id if isinstance(since_id, int) else None
            if id_bound is not None:
                upd_rows = conn.execute(
                    "SELECT * FROM messages WHERE channel_id=? AND id<=? AND "
                    "(coalesce(edited_at,0) > ? OR coalesce(deleted_at,0) > ?) "
                    "ORDER BY id ASC LIMIT ?",
                    (cid, id_bound, since_ts, since_ts, limit)).fetchall()
            else:
                upd_rows = conn.execute(
                    "SELECT * FROM messages WHERE channel_id=? AND "
                    "(coalesce(edited_at,0) > ? OR coalesce(deleted_at,0) > ?) "
                    "ORDER BY id ASC LIMIT ?",
                    (cid, since_ts, since_ts, limit)).fetchall()
    return seq, new_rows, upd_rows


def user_avatar_path(conn, lock, uh):
    with lock:
        return conn.execute("SELECT avatar_path FROM users WHERE dest_hash=?",
                            (uh,)).fetchone()


def user_by_username(conn, lock, token):
    with lock:
        return conn.execute("SELECT dest_hash FROM users WHERE username=? COLLATE NOCASE",
                            (token,)).fetchone()


def search_messages(conn, lock, sql, args):
    """Run a caller-built, parameterized search query (the LIKE pattern
    and channel filter are assembled by the handler)."""
    with lock:
        return conn.execute(sql, args).fetchall()


def user_roles(conn, lock, viewer):
    with lock:
        return conn.execute(
            "SELECT r.role_id, r.name, r.perms, r.color FROM user_roles ur "
            "JOIN roles r ON r.role_id = ur.role_id WHERE ur.user_hash=? "
            "ORDER BY r.position DESC", (viewer,)).fetchall()


def list_audit(conn, lock, limit):
    with lock:
        return conn.execute("SELECT ts, actor, action, target, detail FROM "
                            "audit_log ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()


def public_moderation_log(conn, lock, actions, limit):
    with lock:
        return conn.execute(
            "SELECT ts, actor, action, target FROM audit_log WHERE action IN "
            "(%s) ORDER BY id DESC LIMIT ?" % ",".join("?" for _ in actions),
            (*actions, limit)).fetchall()


def role_members(conn, lock, role_id):
    with lock:
        return conn.execute("SELECT user_hash FROM user_roles WHERE role_id=? "
                            "ORDER BY user_hash LIMIT 64", (role_id,)).fetchall()


def list_roles(conn, lock):
    with lock:
        return conn.execute("SELECT * FROM roles ORDER BY position DESC, name").fetchall()


def message_edits(conn, lock, msg_id):
    with lock:
        return conn.execute("SELECT edited_at, prior_content FROM message_edits "
                            "WHERE msg_id=? ORDER BY edited_at DESC LIMIT 50",
                            (msg_id,)).fetchall()


def receipt_row(conn, lock, cid, viewer):
    """One receipts row, or None (distinct from receipt_last_id's 0)."""
    with lock:
        return conn.execute("SELECT last_id FROM receipts WHERE channel_id=? AND user_hash=?",
                            (cid, viewer)).fetchone()


def catchup_count(conn, lock, cid, last_id, viewer):
    with lock:
        return conn.execute("SELECT count(*) FROM messages WHERE channel_id=? "
                            "AND id>? AND author_hash!=? AND deleted_at IS NULL",
                            (cid, last_id, viewer)).fetchone()[0]


def catchup_samples(conn, lock, cid, last_id, viewer):
    with lock:
        return conn.execute("SELECT author_hash, content, ts FROM messages "
                            "WHERE channel_id=? AND id>? AND author_hash!=? "
                            "AND deleted_at IS NULL ORDER BY id DESC LIMIT 3",
                            (cid, last_id, viewer)).fetchall()

# ----------------------------------------------------------------------------------
# H2 — message writes
# ----------------------------------------------------------------------------------

def message_exists(conn, lock, msg_id):
    with lock:
        return conn.execute("SELECT 1 FROM messages WHERE msg_id=?",
                            (msg_id,)).fetchone() is not None


def ingest_insert(conn, lock, msg_id, channel_id, ts, author_hash, content, att_rows):
    """Insert one message with its attachment rows and membership row, in
    one transaction. `att_rows` is a list of (idx, name, mime, size, fname)
    whose files the caller already stored. Returns False if the msg_id
    already exists (nothing is written)."""
    with lock:
        if conn.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone():
            return False
        # zero-login: auto-register placeholder username on first post
        conn.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) "
                     "VALUES (?, ?, ?)", (author_hash, "user-" + author_hash[:6], time.time()))
        conn.execute(
            "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
            "VALUES (?, ?, ?, ?, ?)", (msg_id, channel_id, ts, author_hash, content))
        for idx, name, mime, size, fname in att_rows:
            conn.execute("INSERT OR REPLACE INTO attachments "
                         "(msg_id, idx, name, mime, size, path) VALUES (?, ?, ?, ?, ?, ?)",
                         (msg_id, idx, name, mime, size, fname))
        conn.execute("INSERT INTO members (channel_id, member_hash, last_post) "
                     "VALUES (?, ?, ?) "
                     "ON CONFLICT(channel_id, member_hash) "
                     "DO UPDATE SET last_post=excluded.last_post",
                     (channel_id, author_hash, ts))
        bump_seq(conn, channel_id)
        conn.commit()
    return True


def mutate_message(conn, lock, msg_id, viewer, action, content, max_len, ts):
    """One edit/delete transaction, verbatim: lookup, author check, then
    the edit branch (history row + update) or the delete branch (tombstone
    + reaction/history cleanup), seq bump, commit. Returns
    (error_or_None, channel_id, seq, event_dict)."""
    with lock:
        row = conn.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
        if not row:
            return "unknown message", None, None, None
        if row["author_hash"] != viewer:
            return "not your message", None, None, None
        if action == "edit":
            if not isinstance(content, str) or not content.strip():
                return "empty content", None, None, None
            new_content = content.strip()[:max_len]
            conn.execute("INSERT INTO message_edits (msg_id, edited_at, prior_content) "
                         "VALUES (?, ?, ?)", (msg_id, ts, row["content"]))
            conn.execute("UPDATE messages SET content=?, edited_at=? WHERE msg_id=?",
                         (new_content, ts, msg_id))
            event = {"msg_id": msg_id, "content": new_content,
                     "edited_at": ts, "deleted_at": None}
        else:
            conn.execute("UPDATE messages SET deleted_at=? WHERE msg_id=?", (ts, msg_id))
            conn.execute("DELETE FROM reactions WHERE msg_id=?", (msg_id,))
            conn.execute("DELETE FROM message_edits WHERE msg_id=?", (msg_id,))
            event = {"msg_id": msg_id, "content": "", "edited_at": None,
                     "deleted_at": ts}
        bump_seq(conn, row["channel_id"])
        conn.commit()
        seq = get_seq(conn, row["channel_id"])
    return None, row["channel_id"], seq, event


def toggle_reaction(conn, lock, msg_id, viewer, emoji, max_emoji, ts):
    """Toggle one reaction (one emoji per user per message). Returns
    (error_or_None, active, counts)."""
    with lock:
        existing = conn.execute("SELECT 1 FROM reactions WHERE msg_id=? AND user_hash=? "
                                "AND emoji=?", (msg_id, viewer, emoji)).fetchone()
        if existing:
            conn.execute("DELETE FROM reactions WHERE msg_id=? AND user_hash=? AND emoji=?",
                         (msg_id, viewer, emoji))
            active = False
        else:
            distinct = conn.execute("SELECT COUNT(DISTINCT emoji) AS n FROM reactions "
                                    "WHERE msg_id=?", (msg_id,)).fetchone()["n"]
            if distinct >= max_emoji:
                return "too many reactions on this message", None, None
            conn.execute("INSERT INTO reactions (msg_id, user_hash, emoji, ts) "
                         "VALUES (?, ?, ?, ?)", (msg_id, viewer, emoji, ts))
            active = True
        counts = {r["emoji"]: r["n"] for r in conn.execute(
            "SELECT emoji, COUNT(*) AS n FROM reactions WHERE msg_id=? "
            "GROUP BY emoji", (msg_id,)).fetchall()}
        conn.commit()
    return None, active, counts

# ----------------------------------------------------------------------------------
# H3 — account lifecycle (register, joins, invites, export, purge, leave)
# ----------------------------------------------------------------------------------

def register_user(conn, lock, user_hash, username, profile_node, registered_at,
                  founder_name, founder_perms):
    """Claim a username (upsert), with the first-member Founder grant.
    Returns True if the username is taken by someone else."""
    with lock:
        clash = conn.execute("SELECT dest_hash FROM users WHERE username=? COLLATE NOCASE "
                             "AND dest_hash<>?", (username, user_hash)).fetchone()
        if clash:
            return True
        conn.execute("INSERT INTO users (dest_hash, username, profile_node, registered_at) "
                     "VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(dest_hash) DO UPDATE SET username=excluded.username, "
                     "profile_node=excluded.profile_node",
                     (user_hash, username, profile_node, registered_at))
        conn.commit()
        n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n == 1:
            rid = ensure_role(conn, lock, founder_name, founder_perms,
                              position=200, color="FB8")
            assign_role(conn, lock, user_hash, rid)
            audit(conn, lock, user_hash, "role.founder", None,
                  "first member granted Founder")
    return False


def record_join_open(conn, lock, viewer, code, joined_at):
    """Open-guild join: record the join and resolve the code's landing
    channel (if any). Returns the channel id or None."""
    with lock:
        conn.execute("INSERT OR IGNORE INTO guild_joins (user_hash, joined_at, "
                     "code) VALUES (?, ?, ?)", (viewer, joined_at, code))
        conn.commit()
        row = conn.execute("SELECT channel_id FROM invites WHERE code=?",
                           (code,)).fetchone()
    return (row["channel_id"] or None) if row else None


def invite_channel(conn, lock, code):
    """The landing channel of one invite code, or None."""
    with lock:
        row = conn.execute("SELECT channel_id FROM invites WHERE code=?",
                           (code,)).fetchone()
    return (row["channel_id"] or None) if row else None


def redeem_invite(conn, lock, viewer, code, ts):
    """Code-guild join: validate the invite, bump its use count and record
    the join. Returns an error string or None."""
    with lock:
        row = conn.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
        if not row:
            return "invalid code"
        if row["expires_at"] and ts > row["expires_at"]:
            return "code expired"
        if row["max_uses"] and row["uses"] >= row["max_uses"]:
            return "code exhausted"
        conn.execute("UPDATE invites SET uses = uses + 1 WHERE code=?", (code,))
        conn.execute("INSERT OR IGNORE INTO guild_joins (user_hash, joined_at, "
                     "code) VALUES (?, ?, ?)", (viewer, ts, code))
        conn.commit()
    return None


def list_invites(conn, lock):
    with lock:
        return conn.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()


def invite_exists(conn, lock, code):
    with lock:
        return conn.execute("SELECT 1 FROM invites WHERE code=?",
                            (code,)).fetchone() is not None


def create_invite(conn, lock, code, label, created_at, expires_at, max_uses,
                  created_by, channel_id):
    with lock:
        conn.execute("INSERT INTO invites (code, label, created_at, "
                     "expires_at, max_uses, uses, created_by, channel_id) "
                     "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                     (code, label, created_at, expires_at, max_uses, created_by,
                      channel_id))
        conn.commit()


def revoke_invite(conn, lock, code):
    """Delete one invite code. Returns the number of rows removed."""
    with lock:
        cur = conn.execute("DELETE FROM invites WHERE code=?", (code,))
        conn.commit()
    return cur.rowcount


def list_own_messages(conn, lock, viewer, after, limit):
    with lock:
        return conn.execute("SELECT * FROM messages WHERE author_hash=? AND id>? "
                            "ORDER BY id LIMIT ?", (viewer, after, limit)).fetchall()


def message_edits_asc(conn, lock, msg_id):
    with lock:
        return conn.execute("SELECT edited_at, prior_content FROM message_edits "
                            "WHERE msg_id=? ORDER BY edited_at ASC",
                            (msg_id,)).fetchall()


def delete_own_posts(conn, lock, viewer, deleted_at):
    """Erase one identity's ledger text and references, retaining
    tombstones. Returns (all_ids, channels, orphaned, orphaned_blobs);
    the caller deletes the orphaned files itself."""
    with lock:
        rows = conn.execute("SELECT msg_id, channel_id FROM messages WHERE "
                            "author_hash=? AND deleted_at IS NULL", (viewer,)).fetchall()
        all_ids = [r["msg_id"] for r in conn.execute(
            "SELECT msg_id FROM messages WHERE author_hash=?", (viewer,)).fetchall()]
        paths = set()
        for msg_id in all_ids:
            paths.update(r["path"] for r in conn.execute(
                "SELECT path FROM attachments WHERE msg_id=?", (msg_id,)).fetchall())
            conn.execute("DELETE FROM attachments WHERE msg_id=?", (msg_id,))
            conn.execute("DELETE FROM reactions WHERE msg_id=?", (msg_id,))
            conn.execute("DELETE FROM message_edits WHERE msg_id=?", (msg_id,))
        conn.execute("UPDATE messages SET content='', deleted_at=coalesce(deleted_at, ?) "
                     "WHERE author_hash=?", (deleted_at, viewer))
        channels = {r["channel_id"] for r in rows}
        for cid in channels:
            bump_seq(conn, cid)
        conn.commit()
        orphaned = [path for path in paths if not conn.execute(
            "SELECT 1 FROM attachments WHERE path=? LIMIT 1", (path,)).fetchone()]
        orphaned_blobs = []
        for path in orphaned:
            if not re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{1,8}", path):
                continue
            digest = path.split(".", 1)[0]
            record = conn.execute("SELECT path FROM blobs WHERE id=? AND by=?",
                                  (digest, viewer)).fetchone()
            if record and not conn.execute(
                    "SELECT 1 FROM attachments WHERE path LIKE ? LIMIT 1",
                    (digest + ".%",)).fetchone():
                orphaned_blobs.append(record["path"])
                conn.execute("DELETE FROM blobs WHERE id=?", (digest,))
        conn.commit()
        # Clear the live WAL and compact historical page versions where possible.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        except sqlite3.Error as exc:
            RNS.log("post compaction deferred: %s" % exc, RNS.LOG_WARNING)
    return all_ids, channels, orphaned, orphaned_blobs


def leave_guild(conn, lock, viewer):
    """Release registration and roles. Returns the avatar path or None."""
    with lock:
        avatar = conn.execute("SELECT avatar_path FROM users WHERE dest_hash=?",
                              (viewer,)).fetchone()
        conn.execute("DELETE FROM guild_joins WHERE user_hash=?", (viewer,))
        conn.execute("DELETE FROM user_roles WHERE user_hash=?", (viewer,))
        conn.execute("DELETE FROM receipts WHERE user_hash=?", (viewer,))
        conn.execute("DELETE FROM members WHERE member_hash=?", (viewer,))
        conn.execute("DELETE FROM users WHERE dest_hash=?", (viewer,))
        conn.commit()
    return avatar["avatar_path"] if avatar else None

# ----------------------------------------------------------------------------------
# H4 — channels, roles, page edits
# ----------------------------------------------------------------------------------

def save_message_edit(conn, lock, msg_id, new_content, prior_content, edited_at,
                      channel_id):
    """Record the prior version and update the message, in one transaction."""
    with lock:
        conn.execute("INSERT INTO message_edits (msg_id, edited_at, prior_content) "
                     "VALUES (?, ?, ?)", (msg_id, edited_at, prior_content))
        conn.execute("UPDATE messages SET content=?, edited_at=? WHERE msg_id=?",
                     (new_content, edited_at, msg_id))
        bump_seq(conn, channel_id)
        conn.commit()


def soft_delete_message(conn, lock, msg_id, deleted_at, channel_id):
    """Tombstone one message, in one transaction."""
    with lock:
        conn.execute("UPDATE messages SET deleted_at=? WHERE msg_id=?", (deleted_at, msg_id))
        bump_seq(conn, channel_id)
        conn.commit()


def reorder_channels(conn, lock, order):
    """Rewrite every channel's position in one transaction. Returns False
    unless `order` covers every channel exactly once."""
    with lock:
        known = {r["id"] for r in conn.execute("SELECT id FROM channels").fetchall()}
        if set(order) != known:
            return False
        for idx, cid in enumerate(order, start=1):
            conn.execute("UPDATE channels SET position=? WHERE id=?", (idx, cid))
        conn.commit()
    return True


def create_role(conn, lock, name, color, position, perms):
    """Insert a role unless the name exists. Returns the new id or None."""
    with lock:
        if conn.execute("SELECT 1 FROM roles WHERE name=? COLLATE NOCASE",
                        (name,)).fetchone():
            return None
        cur = conn.execute("INSERT INTO roles (name, color, position, perms) "
                           "VALUES (?, ?, ?, ?)", (name, color, position, perms))
        conn.commit()
        return cur.lastrowid


def delete_role(conn, lock, role_id):
    """Delete a role and its assignments/overwrites. Returns the role's
    name, or None for an unknown role."""
    with lock:
        row = conn.execute("SELECT name FROM roles WHERE role_id=?", (role_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM user_roles WHERE role_id=?", (role_id,))
        conn.execute("DELETE FROM channel_overwrites WHERE role_id=?", (role_id,))
        conn.execute("DELETE FROM roles WHERE role_id=?", (role_id,))
        conn.commit()
    return row["name"]


def assign_role_checked(conn, lock, user_hash, role_id):
    """Assign a role if it exists (check + insert under one lock hold).
    Returns False for an unknown role."""
    with lock:
        if not conn.execute("SELECT 1 FROM roles WHERE role_id=?",
                            (role_id,)).fetchone():
            return False
        conn.execute("INSERT OR IGNORE INTO user_roles (user_hash, role_id) VALUES (?, ?)",
                     (user_hash, role_id))
        conn.commit()
    return True


def unassign_role(conn, lock, user_hash, role_id):
    with lock:
        conn.execute("DELETE FROM user_roles WHERE user_hash=? AND role_id=?",
                     (user_hash, role_id))
        conn.commit()


def set_channel_topic(conn, lock, cid, topic):
    with lock:
        conn.execute("UPDATE channels SET topic=? WHERE id=?", (topic, cid))
        conn.commit()


def delete_channel_overwrite(conn, lock, cid, rid):
    """Delete one channel overwrite row. No commit: the caller commits
    after the whole overwrite loop (matching the original)."""
    with lock:
        conn.execute("DELETE FROM channel_overwrites "
                     "WHERE channel_id=? AND role_id=?", (cid, rid))


def set_channel_overwrite(conn, lock, cid, rid, perms):
    """Upsert one channel overwrite row. No commit: the caller commits
    after the whole overwrite loop (matching the original)."""
    with lock:
        conn.execute("INSERT INTO channel_overwrites "
                     "(channel_id, role_id, perms) VALUES (?, ?, ?) "
                     "ON CONFLICT(channel_id, role_id) "
                     "DO UPDATE SET perms=excluded.perms", (cid, rid, perms))


def commit(conn, lock):
    """Commit the connection (for callers that stage statements across
    several store calls, like the channel-overwrite loop)."""
    with lock:
        conn.commit()

# ----------------------------------------------------------------------------------
# H5 — tail (dispatch, boot, avatars, blobs, receipts, term)
# ----------------------------------------------------------------------------------

def attachment_channels(conn, lock, fname):
    """Channels holding a visible, non-deleted message with this attachment."""
    with lock:
        return conn.execute(
            "SELECT DISTINCT m.channel_id FROM attachments a "
            "JOIN messages m ON m.msg_id=a.msg_id "
            "WHERE a.path=? AND m.deleted_at IS NULL", (fname,)).fetchall()


def set_avatar_path(conn, lock, viewer, fname):
    with lock:
        conn.execute("UPDATE users SET avatar_path=? WHERE dest_hash=?", (fname, viewer))
        conn.commit()


def blob_path(conn, lock, digest):
    """One blobs row (path) by digest, or None."""
    with lock:
        return conn.execute("SELECT path FROM blobs WHERE id=?",
                            (digest,)).fetchone()


def set_channel_manifest(conn, lock, cid, manifest_json):
    with lock:
        conn.execute("UPDATE channels SET manifest=? WHERE id=?",
                     (manifest_json, cid))
        conn.commit()


def list_channel_name_kind(conn, lock):
    with lock:
        return conn.execute("SELECT name, kind FROM channels ORDER BY name").fetchall()


def channel_id_by_name(conn, lock, name):
    """One channels row (id) by NOCASE name, or None."""
    with lock:
        return conn.execute("SELECT id FROM channels WHERE name=? COLLATE NOCASE",
                            (name,)).fetchone()


def channel_by_name(conn, lock, name):
    """One channels row (id, kind, created, topic) by NOCASE name, or None."""
    with lock:
        return conn.execute("SELECT id, kind, created, topic FROM channels "
                            "WHERE name=? COLLATE NOCASE", (name,)).fetchone()


def channel_name_taken(conn, lock, name, exclude_id):
    """True if another channel already carries this NOCASE name."""
    with lock:
        return conn.execute("SELECT 1 FROM channels WHERE name=? COLLATE NOCASE "
                            "AND id<>?", (name, exclude_id)).fetchone() is not None


def rename_channel(conn, lock, new, cid):
    with lock:
        conn.execute("UPDATE channels SET name=? WHERE id=?", (new, cid))
        conn.commit()


def mark_read(conn, lock, msg_id, cid, viewer, ts):
    """Mark a channel read up to one message. Returns the message id, or
    None if the message is not in the channel."""
    with lock:
        row = conn.execute("SELECT id FROM messages WHERE msg_id=? AND channel_id=?",
                           (msg_id, cid)).fetchone()
        if not row:
            return None
        conn.execute("INSERT INTO receipts (channel_id, user_hash, last_id, ts) "
                     "VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(channel_id, user_hash) DO UPDATE SET "
                     "last_id=MAX(receipts.last_id, excluded.last_id), ts=excluded.ts",
                     (cid, viewer, row["id"], ts))
        conn.commit()
    return row["id"]


def list_attachment_paths(conn, lock):
    with lock:
        return conn.execute("SELECT DISTINCT path FROM attachments").fetchall()


def save_guild(conn, lock, guild_id, name, node_hash, delivery_hash, prop_hash):
    with lock:
        conn.execute("INSERT OR REPLACE INTO guilds "
                     "(id, name, node_hash, delivery_hash, prop_hash) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (guild_id, name, node_hash, delivery_hash, prop_hash))
        conn.commit()


def get_guild(conn, lock, guild_id):
    """The guilds row saved at boot (node/delivery/prop hashes), or None."""
    with lock:
        return conn.execute("SELECT * FROM guilds WHERE id=?",
                            (guild_id,)).fetchone()


# ----------------------------------------------------------------------------------
# Bots (Group 1: registry). Only the SHA-256 hash of an API key is ever
# stored; the raw key is shown to the owner once at creation and is never
# retrievable afterwards (rotation = revoke + create anew).
# ----------------------------------------------------------------------------------

def bot_create(conn, lock, bot_id, owner_hash, name, key_hash, perms, created_at):
    with lock:
        conn.execute("INSERT INTO bots (bot_id, owner_hash, name, key_hash, "
                     "perms, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (bot_id, owner_hash, name, key_hash, perms, created_at))
        conn.commit()
        return conn.execute("SELECT * FROM bots WHERE bot_id=?",
                            (bot_id,)).fetchone()


def bot_list_by_owner(conn, lock, owner_hash):
    with lock:
        return conn.execute("SELECT * FROM bots WHERE owner_hash=? "
                            "ORDER BY created_at", (owner_hash,)).fetchall()


def bot_get(conn, lock, bot_id):
    with lock:
        return conn.execute("SELECT * FROM bots WHERE bot_id=?",
                            (bot_id,)).fetchone()


def bot_revoke(conn, lock, bot_id, revoked_at):
    """Set revoked_at if the bot is still active; an already-revoked bot
    keeps its original revoked_at (revoke is idempotent). Returns the row."""
    with lock:
        conn.execute("UPDATE bots SET revoked_at=? WHERE bot_id=? "
                     "AND revoked_at IS NULL", (revoked_at, bot_id))
        conn.commit()
        return conn.execute("SELECT * FROM bots WHERE bot_id=?",
                            (bot_id,)).fetchone()


def bot_update(conn, lock, bot_id, name=None, perms=None):
    """Rename and/or change perms; a None argument leaves that field alone.
    Returns the updated row, or None for an unknown bot_id."""
    sets, vals = [], []
    if name is not None:
        sets.append("name=?")
        vals.append(name)
    if perms is not None:
        sets.append("perms=?")
        vals.append(perms)
    with lock:
        if sets:
            conn.execute("UPDATE bots SET %s WHERE bot_id=?" % ", ".join(sets),
                         (*vals, bot_id))
            conn.commit()
        return conn.execute("SELECT * FROM bots WHERE bot_id=?",
                            (bot_id,)).fetchone()


def bot_touch_last_seen(conn, lock, bot_id, ts):
    """Record a bot's last authenticated use. Revoked bots are not touched.
    Returns the row, or None for an unknown bot_id."""
    with lock:
        conn.execute("UPDATE bots SET last_seen=? WHERE bot_id=? "
                     "AND revoked_at IS NULL", (ts, bot_id))
        conn.commit()
        return conn.execute("SELECT * FROM bots WHERE bot_id=?",
                            (bot_id,)).fetchone()
