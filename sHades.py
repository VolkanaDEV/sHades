#!/usr/bin/env python3
# sHades guild server — implements plan.md v2.2 (phases 1-6).
#
# One process, one RNS identity, three destinations:
#   lxmf.delivery      — members post to channels by messaging the guild (#channel first token)
#   lxmf.propagation   — mesh reachability (enable_propagation); guild-bound traffic rides
#                        the propagation network and is delivered locally on arrival
#   nomadnetwork.node  — the Micron chatroom UI + /api/* endpoints
#
# Every API call used here is verified against the installed stack:
#   rns 1.1.3 (/usr/local/lib/python3.10/dist-packages/RNS)
#   lxmf 0.9.4 (~/.local/lib/python3.10/site-packages/LXMF)
#   nomadnet 0.9.8 (~/.local/lib/python3.10/site-packages/nomadnet)
# See plan.md for file:line citations and tests/ for the verification suite.

import os
import re
import sys
import json
import time
import base64
import shutil
import argparse
import sqlite3
import hashlib
import secrets
import threading
import updater
from collections import defaultdict, deque

import RNS
import LXMF
from RNS.vendor import umsgpack as msgpack

# ----------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------

DEFAULT_STORAGE = os.path.expanduser("~/.sHades")
GUILD_NAME      = "sHades Hub"

DEBUG           = False   # set via --debug; enables verbose attachment/ingest logging

MAX_CONTENT_LEN   = 2000          # per-message character cap (Discord-style)
MAX_ATTACHMENTS   = 4             # files per message
MAX_ATTACHMENT_SZ = 10 * 1024 * 1024   # 10 MB per attachment (payload law)
MAX_AVATAR_SZ     = 64 * 1024
RATE_MAX_POSTS    = 10            # per sender per channel
RATE_WINDOW_S     = 60
PAGE_SIZE         = 50
TOKEN_TTL_S       = 900
ATTACHMENT_EXT    = {"png", "jpg", "jpeg", "gif", "webp", "mp3", "ogg", "wav",
                     "flac", "mp4", "webm", "pdf", "txt"}

# Channel kinds: chat, a NomadNet micron "site", or a terminal stream.
CHANNEL_KINDS     = ("chat", "site", "term", "announce")

# Permission bitmask (U5). Guild-wide bits (MANAGE_*/ADMIN) live on roles;
# channel-scoped bits (VIEW/POST) live in channel_overwrites keyed by role
# (role_id 0 = @everyone). Missing @everyone row defaults to open reading
# and posting, which preserves pre-roles behaviour.
PERMS = {"VIEW": 1, "POST": 2, "MANAGE_MESSAGES": 4, "MANAGE_CHANNEL": 8,
         "MANAGE_ROLES": 16, "ADMIN": 32, "SITE_EDIT": 64}
PERM_ALL = 127
DEFAULT_CHANNEL_PERMS = PERMS["VIEW"] | PERMS["POST"]
_GUILD_MANAGE_BITS = (PERMS["MANAGE_MESSAGES"] | PERMS["MANAGE_CHANNEL"] |
                      PERMS["MANAGE_ROLES"] | PERMS["ADMIN"])

# Blobs + site resources (JSON API v1 phases 3-4): content-addressed media,
# uploaded once and referenced by id. Chunked get keeps memory bounded.
BLOB_CHUNK_DEFAULT = 256 * 1024   # /api/v1/blobs/get default slice
BLOB_CHUNK_MAX     = 1024 * 1024  # ... and hard cap per request
BLOB_UPLOAD_CHUNK  = 256 * 1024   # chunked-upload default slice; the server
# is what dictates chunk_size per session anyway
MAX_BLOB_SZ        = 10 * 1024 * 1024   # payloads max 10 MB (tay, v0.29-c)
BLOB_UPLOAD_IDLE_S = 600          # staged upload idle this long -> reaped
SITE_PATH_RE       = re.compile(r"^/[a-zA-Z0-9][a-zA-Z0-9._/\-]{0,127}$")
EXT_MIME           = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                      "gif": "image/gif", "webp": "image/webp", "mp3": "audio/mpeg",
                      "ogg": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac",
                      "mp4": "video/mp4", "webm": "video/webm", "pdf": "application/pdf",
                      "txt": "text/plain", "mu": "text/micron"}

# JSON API v1 stream state
EVENT_LOG_MAX     = 200    # per-channel event ring; a cursor older than this resyncs
STREAM_WAIT_S     = 4.0    # default long-poll wait (server side)
STREAM_WAIT_MAX   = 10.0   # hard cap on one poll's wait
SUB_TTL_S         = 120.0  # a subscription not polled for this long is dropped
TYPING_TTL_S      = 5.0    # a "typing" flag expires this long after its last ping

# Term channels (JSON API v1 phase 5): a guild console over the stream — a
# line-based REPL executed in-process. Never a pty, never a subprocess, never
# a shell: the command table below is the whole attack surface.
TERM_IDLE_S       = 300.0  # session closed after this much inactivity
TERM_MAX_PER_USER = 4      # concurrent sessions per user
TERM_MAX_TOTAL    = 32     # concurrent sessions guild-wide
TERM_MAX_INPUT    = 2000   # bytes per term/data payload
TERM_IN_RATE      = (30, 60)  # commands per window (uses rate_ok)

# ----------------------------------------------------------------------------------
# Globals (set up by run(); import-safe for unit tests)
# ----------------------------------------------------------------------------------

db        = None
db_lock   = threading.RLock()
router          = None
guild_delivery  = None            # lxmf.delivery destination
node_destination  = None          # nomadnetwork.node destination
guild_identity    = None
STORAGE         = None
GUILD_ID        = "main_guild"
ANNOUNCE_DELAY  = LXMF.LXMRouter.NODE_ANNOUNCE_DELAY

_tokens       = {}                # token -> (user_hash, scope, expires_at)
_tokens_lock  = threading.Lock()
_rate_windows = defaultdict(deque)  # (sender, channel) -> deque[timestamps]
_rate_lock    = threading.Lock()
MODERATORS    = set()             # canonical user hashes; seeded from moderators.txt at boot
ADMIN_USERS   = set()             # canonical user hashes; seeded from admin.txt at boot
NODE_REGISTRY = {}                # node_hash -> {address, port, last_seen}
NODE_REGISTRY_LOCK = threading.Lock()
# Presence is link-derived (v0.29): a user is online iff at least one of their
# guild links is ACTIVE. RNS monitors link health with its own keepalives and
# teardowns -- the "pings baked in" -- so no application-layer heartbeat runs
# on top. ONLINE_LINKS maps link_id -> user_hash, bound on every API request
# (the request's remote_identity is the proof of who is behind the link).
ONLINE_LINKS = {}                 # link_id -> user_hash
ONLINE_LINKS_LOCK = threading.Lock()

# Stream state for the JSON API. One subscription per link: subscribing to a
# channel replaces whatever that link was streaming, which is what makes
# "only the active channel flows" a server-side rule rather than a convention.
EVENT_COND    = threading.Condition()   # guards EVENT_LOG and wakes stream pollers
EVENT_LOG     = {}                      # channel_id -> deque[(seq, event)], maxlen EVENT_LOG_MAX
EVENT_SEQ     = 0                       # single global sequence; cursors are per-channel views of it
SUBSCRIPTIONS = {}                      # link_id -> {user_hash, channel, cursor, at}
SUB_BY_USER   = {}                      # user_hash -> {channel, link_id, at}
SUB_LOCK      = threading.Lock()
TYPING        = {}                      # channel_id -> {user_hash: expires_at}
TYPING_LOCK   = threading.Lock()

FIELD_ATTACHMENTS = LXMF.FIELD_FILE_ATTACHMENTS  # 0x05 — envelope ours (plan §14)

# P11 version bridge: the server knows its own version. Lockstep with
# buildozer.spec and kivy_client.APP_VERSION (tests/test_update_meta.py).
SERVER_VERSION = "0.36.25"

# ----------------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------------

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

def _migrate_schema(conn):
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

def open_db(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    conn.commit()
    return conn

def ensure_channel(name, kind="chat"):
    """Create a channel + its revision counter if needed. Returns channel id.

    A channel's id is a hash of its name, so it is case-sensitive, while the
    channels.name unique index is COLLATE NOCASE. The name lookup below is
    therefore NOCASE: '#General' resolves to the existing 'general' channel
    instead of computing a different id that the unique index then refuses.
    """
    with db_lock:
        row = db.execute("SELECT id FROM channels WHERE name=? COLLATE NOCASE",
                         (name,)).fetchone()
        if row:
            return row["id"]
        cid = hexstr(RNS.Identity.full_hash(f"{GUILD_ID}:{name}".encode()))
        db.execute("INSERT OR IGNORE INTO channels (id, name, created, kind) "
                   "VALUES (?, ?, ?, ?)",
                   (cid, name, time.time(), kind if kind in CHANNEL_KINDS else "chat"))
        db.execute("INSERT OR IGNORE INTO channel_state (channel_id, seq) VALUES (?, 0)", (cid,))
        db.commit()
        # v0.28: a NEW channel must reach stream subscribers (the 3s state
        # poller that used to notice renames/creates is being deleted);
        # name changes ride the same event.
        _emit_event(cid, "channels_changed", {"channel": name}, guild_wide=True)
    return cid

def channel_name(cid):
    row = db.execute("SELECT name FROM channels WHERE id=?", (cid,)).fetchone()
    return row["name"] if row else "unknown"

def bump_seq(cid):
    db.execute("UPDATE channel_state SET seq = seq + 1 WHERE channel_id=?", (cid,))

def get_seq(cid):
    row = db.execute("SELECT seq FROM channel_state WHERE channel_id=?", (cid,)).fetchone()
    return row["seq"] if row else 0

# ----------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------

def hexstr(b):
    return RNS.hexrep(b, delimit=False)

def now():
    return time.time()


def dlog(msg):
    """Debug log — only prints when --debug is passed."""
    if DEBUG:
        print(f"[DEBUG] {msg}", flush=True)

# ----------------------------------------------------------------------------------
# Micron rendering (verified grammar: nomadnet/ui/textui/MicronParser.py, Browser.py)
# ----------------------------------------------------------------------------------

def page_header(bg="223"):
    # 3-hex colors only (Browser.py:1094-1096 ignores 6-digit); #!c=0 defeats the
    # 12h peer cache — mandatory on every dynamic page.
    return "#!c=0\n#!bg=%s\n" % bg

def esc_micron_text(s):
    """Escape a plain-text run. In text mode only \\ and ` are special (MicronParser
    make_output text branch); [ < { are literal there."""
    return s.replace("\\", "\\\\").replace("`", "\\`")

def fmt_time(ts):
    return time.strftime("%H:%M", time.localtime(ts))

_MD_TOKEN = re.compile(
    r"`(?P<code>[^`\n]{1,256})`"
    r"|(?P<bold>\*\*[^*\n]{1,256}\*\*)"
    r"|(?P<under>__[^_\n]{1,256}__)"
    r"|\[(?P<label>[^\]\n]{1,128})\]\((?P<target>[^)\s]{1,256})\)"
    r"|(?P<italic>\*[^*\n]{1,256}\*)"
)
_LINK_TARGET = re.compile(r"^(/[a-zA-Z0-9/._-]{1,128}|[0-9a-f]{32}(/[a-zA-Z0-9/._-]{0,128})?)$")

def md_to_micron_line(line):
    """Translate the message markdown subset to Micron; escape everything else.
    Subset: **bold**, *italic*, __underline__, `code`, [label](target).
    Single-pass token scan: independent per-rule finditer() passes would eat each
    other's delimiters (e.g. the italic rule pairing '**'s closing asterisk with
    a later opening one). Leftmost token wins; code content renders inert."""
    out = []
    pos = 0
    for m in _MD_TOKEN.finditer(line):
        out.append(esc_micron_text(line[pos:m.start()]))
        g = m.lastgroup
        if g == "code":
            out.append("`F8a8" + esc_micron_text(m.group("code")) + "`f")
        elif g == "bold":
            out.append("`!" + esc_micron_text(m.group("bold")[2:-2]) + "!`")
        elif g == "under":
            out.append("`_" + esc_micron_text(m.group("under")[2:-2]) + "_`")
        elif g == "italic":
            out.append("`*" + esc_micron_text(m.group("italic")[1:-1]) + "*`")
        else:  # link
            target = m.group("target")
            if _LINK_TARGET.match(target):
                # Micron link form: `[label`target] (backtick-separated; link parse
                # happens in formatting mode, i.e. always after a backtick)
                out.append("`[" + esc_micron_text(m.group("label")) + "`" + target + "]")
            else:
                out.append(esc_micron_text(m.group(0)))
        pos = m.end()
    out.append(esc_micron_text(line[pos:]))
    return "".join(out)

def render_content(content):
    """Line-start escapes are decided from the ORIGINAL line's first character
    (translated output legitimately begins with formatting backticks)."""
    out = []
    for l in (content or "").split("\n"):
        rendered = md_to_micron_line(l)
        if l and l[0] in ">-#`<\\{":
            rendered = "\\" + rendered
        out.append(rendered)
    return "\n".join(out)

def get_username(author_hash):
    with db_lock:
        row = db.execute("SELECT username FROM users WHERE dest_hash=?", (author_hash,)).fetchone()
    return row["username"] if row else "user-" + author_hash[:6]

def username_fragment(author_hash):
    """§5 rendering rule: linked username when the user registered a profile node,
    otherwise a BOLD plain name. (A micron link cannot be nested inside a bold
    formatting run — the parser would break — so links render unbolded.)"""
    with db_lock:
        row = db.execute("SELECT username, profile_node FROM users WHERE dest_hash=?",
                         (author_hash,)).fetchone()
    name = (row["username"] if row else None) or ("user-" + author_hash[:6])
    if row and row["profile_node"] and re.match(r"^[0-9a-f]{32}$", row["profile_node"]):
        # bare 32-hex target = that node's home page (Browser.py parse_url)
        return "`[" + esc_micron_text(name) + "`" + row["profile_node"] + "]"
    return "`!" + esc_micron_text(name) + "!`"

# ----------------------------------------------------------------------------------
# CSRF tokens: every mutating PAGE action requires a server-issued one-time token.
# A message containing a micron link can otherwise perform actions as whoever clicks
# it (link field constants carry the attacker's values). Tokens are minted per page
# render, bound to (viewer, scope), consumed on use — a forger can craft step-1 links
# but never step-2 (the token is only ever rendered to the requester).
# API endpoints are not forgeable from micron links: the browser submits
# field_/var_-prefixed keys, while api handlers read unprefixed keys (plan §14).
# ----------------------------------------------------------------------------------

def issue_token(user, scope, ttl=TOKEN_TTL_S):
    token = secrets.token_hex(16)
    with _tokens_lock:
        cutoff = now()
        for t in [t for t, v in _tokens.items() if v[2] < cutoff]:
            del _tokens[t]
        _tokens[token] = (user, scope, now() + ttl)
    return token

def check_token(user, scope, token):
    if not token:
        return False
    with _tokens_lock:
        v = _tokens.get(token)
        if v and v[0] == user and v[1] == scope and v[2] >= now():
            del _tokens[token]
            return True
    return False

# ----------------------------------------------------------------------------------
# Rate limiting + moderation
# ----------------------------------------------------------------------------------

def rate_ok(sender, channel_id, max_posts=None, window_s=None):
    key = (sender, channel_id)
    max_posts = max_posts if max_posts is not None else RATE_MAX_POSTS
    window_s = window_s if window_s is not None else RATE_WINDOW_S
    with _rate_lock:
        w = _rate_windows[key]
        cutoff = now() - window_s
        while w and w[0] < cutoff:
            w.popleft()
        if len(w) >= max_posts:
            return False
        w.append(now())
        return True

def is_banned(sender):
    with db_lock:
        return db.execute("SELECT 1 FROM banned WHERE dest_hash=?", (sender,)).fetchone() is not None

def can_delete(viewer, row):
    """Author or guild moderator (plan §7)."""
    return viewer is not None and (row["author_hash"] == viewer or is_moderator(viewer))

def ban_user(user, reason="", by=None):
    with db_lock:
        db.execute("INSERT OR REPLACE INTO banned (dest_hash, reason, at) VALUES (?, ?, ?)",
                   (user, (reason or "")[:200], now()))
        db.commit()

def unban_user(user):
    with db_lock:
        db.execute("DELETE FROM banned WHERE dest_hash=?", (user,))
        db.commit()

def is_muted(sender):
    with db_lock:
        return db.execute("SELECT 1 FROM muted WHERE dest_hash=?", (sender,)).fetchone() is not None

def mute_user(user, reason="", by=None):
    with db_lock:
        db.execute("INSERT OR REPLACE INTO muted (dest_hash, reason, at, by) VALUES (?, ?, ?, ?)",
                   (user, (reason or "")[:200], now(), by))
        db.commit()

def unmute_user(user):
    with db_lock:
        db.execute("DELETE FROM muted WHERE dest_hash=?", (user,))
        db.commit()

def load_moderators(path):
    """moderators.txt: one canonical (lxmf.delivery) user hash per line, '#' comments.
    The file is the boot-time source of truth; MODERATORS is the live set."""
    MODERATORS.clear()
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and re.match(r"^[0-9a-f]{32}$", line):
                    MODERATORS.add(line)
    return len(MODERATORS)

def load_admins(path):
    """admin.txt: same format as moderators.txt. ADMIN_USERS is the live
    set; boot seeding also assigns the Admin role to every hash here."""
    ADMIN_USERS.clear()
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and re.match(r"^[0-9a-f]{32}$", line):
                    ADMIN_USERS.add(line)
    return len(ADMIN_USERS)

def is_moderator(user):
    """Guild-wide moderator check: any role carrying MANAGE_MESSAGES or
    ADMIN, or the legacy MODERATORS set (moderators.txt, kept for backward
    compatibility with existing deployments and tests)."""
    if user is None:
        return False
    if user in MODERATORS:
        return True
    return has_role_perm(user, PERMS["MANAGE_MESSAGES"] | PERMS["ADMIN"])

def has_role_perm(user, bits):
    """Guild-wide: does any of the user's roles carry any of `bits`?
    The legacy MODERATORS set (moderators.txt without roles) counts as
    carrying MANAGE_MESSAGES and MANAGE_CHANNEL."""
    if user is None:
        return False
    if user in MODERATORS and bits & (PERMS["MANAGE_MESSAGES"] |
                                      PERMS["MANAGE_CHANNEL"]):
        return True
    with db_lock:
        row = db.execute(
            "SELECT 1 FROM user_roles ur JOIN roles r ON r.role_id = ur.role_id "
            "WHERE ur.user_hash = ? AND (r.perms & ?) != 0 LIMIT 1",
            (user, bits)).fetchone()
    return row is not None

def _effective_perms(user_hash, channel_id, base):
    """The union: base + each of the user's roles' perms + each role's
    per-channel overwrite. The admin short-circuit lives in has_perm."""
    if user_hash is None:
        return 0
    with db_lock:
        rows = db.execute(
            "SELECT co.perms AS operms, r.perms AS rperms "
            "FROM user_roles ur JOIN roles r ON r.role_id = ur.role_id "
            "LEFT JOIN channel_overwrites co ON co.role_id = ur.role_id "
            "AND co.channel_id=? WHERE ur.user_hash=?",
            (channel_id, user_hash)).fetchall()
    perms = base
    for r in rows:
        perms |= (r["operms"] or 0) | (r["rperms"] or 0)
    return perms

def _announce_post_ok(user_hash, channel_id):
    """kind=='announce' POST gate (P8): moderators/admins always; everyone
    else only with an EXPLICIT POST grant -- an @everyone overwrite row
    carrying POST, a role perm, or a role overwrite. The open channel
    default confers VIEW here, never POST."""
    if is_moderator(user_hash):
        return True
    if user_hash is None:
        return False
    with db_lock:
        base_row = db.execute("SELECT perms FROM channel_overwrites "
                              "WHERE channel_id=? AND role_id=0",
                              (channel_id,)).fetchone()
    perms = _effective_perms(user_hash, channel_id,
                             base_row["perms"] if base_row else 0)
    return (perms & PERMS["POST"]) != 0

def has_perm(user, channel_id, bit):
    """Effective permission for one channel. VIEW/POST = the @everyone
    overwrite (or the open default) UNION each of the user's roles' own
    perms UNION each role's per-channel overwrite. Guild-wide bits
    (MANAGE_*/ADMIN) delegate to the user's roles; ADMIN short-circuits."""
    if user is None:
        return False
    if bit & _GUILD_MANAGE_BITS:
        return has_role_perm(user, bit)
    with db_lock:
        base = db.execute("SELECT perms FROM channel_overwrites "
                          "WHERE channel_id=? AND role_id=0",
                          (channel_id,)).fetchone()
        is_admin = db.execute(
            "SELECT 1 FROM user_roles ur JOIN roles r ON r.role_id = ur.role_id "
            "WHERE ur.user_hash=? AND (r.perms & ?) != 0 LIMIT 1",
            (user, PERMS["ADMIN"])).fetchone()
    perms = _effective_perms(user, channel_id,
                             base["perms"] if base else DEFAULT_CHANNEL_PERMS)
    if is_admin:
        return True
    return (perms & bit) != 0

def page_can_view(user, channel_id):
    """Anonymous NomadNet readers see only channels whose @everyone
    overwrite (or the open default) grants VIEW; identified readers take
    their role grants. has_perm itself rejects user=None, which is what
    makes the anonymous lane a separate question. (tay fork tier-1)"""
    if user is not None:
        return has_perm(user, channel_id, PERMS["VIEW"])
    with db_lock:
        row = db.execute("SELECT perms FROM channel_overwrites "
                         "WHERE channel_id=? AND role_id=0",
                         (channel_id,)).fetchone()
    perms = row["perms"] if row else DEFAULT_CHANNEL_PERMS
    return bool(perms & PERMS["VIEW"])

def attachment_visible(viewer, fname):
    """A stored attachment is readable only through a visible, non-deleted
    message. Serving a hash-keyed file with no message linkage would let
    anyone with a node link pull every stored blob by name (or after its
    message was deleted). (tay fork tier-1)"""
    with db_lock:
        rows = db.execute(
            "SELECT DISTINCT m.channel_id FROM attachments a "
            "JOIN messages m ON m.msg_id=a.msg_id "
            "WHERE a.path=? AND m.deleted_at IS NULL", (fname,)).fetchall()
    return any(page_can_view(viewer, row["channel_id"]) for row in rows)

def _ensure_role(name, perms, position=0, color=None):
    """Create a role if missing (boot seeding / role editor). Returns id."""
    with db_lock:
        row = db.execute("SELECT role_id FROM roles WHERE name=? COLLATE NOCASE",
                         (name,)).fetchone()
        if row:
            return row["role_id"]
        cur = db.execute("INSERT INTO roles (name, color, position, perms) "
                         "VALUES (?, ?, ?, ?)", (name, color, position, perms))
        db.commit()
        return cur.lastrowid

def _assign_role(user_hash, role_id):
    with db_lock:
        db.execute("INSERT OR IGNORE INTO user_roles (user_hash, role_id) "
                   "VALUES (?, ?)", (user_hash, role_id))
        db.commit()

FOUNDER_ROLE_NAME = "Founder"

def _maybe_founder(viewer):
    """Grant the Founder role to the first member of a server (the creator).

    tay: "add roles. first person into a server (the creator) should be
    able to add/delete channels and have founder role." The Founder role
    carries every permission bit, so add/delete channels (and role
    management) flow through the existing U5 gates with no new gates.
    Called after every users upsert; grants exactly once, when this
    upsert created the users table's first row — no later registration
    can ever reach it, and existing deployments with members are never
    touched.
    """
    with db_lock:
        n = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n != 1:
            return
        rid = _ensure_role(FOUNDER_ROLE_NAME, PERM_ALL, position=200, color="FB8")
        _assign_role(viewer, rid)
        _audit(viewer, "role.founder", None, "first member granted Founder")

def load_role_seeds(storage):
    """Boot-time role seeding (U5): admin.txt hashes get the Admin role
    (ADMIN + all manage bits), moderators.txt hashes the Moderator role
    (MANAGE_MESSAGES|MANAGE_CHANNEL). Same one-hash-per-line format as
    moderators.txt; '#' comments allowed."""
    admin_id = _ensure_role("Admin", PERM_ALL, position=100, color="F8a8")
    mod_id = _ensure_role("Moderator",
                          PERMS["MANAGE_MESSAGES"] | PERMS["MANAGE_CHANNEL"],
                          position=50, color="8a8")
    for fname, role_id in (("admin.txt", admin_id), ("moderators.txt", mod_id)):
        path = os.path.join(storage, fname)
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and \
                        re.match(r"^[0-9a-f]{32}$", line):
                    _assign_role(line, role_id)

def _reap_stale_nodes():
    """Presence sweep (v0.29): derive presence from link liveness.

    A binding whose link is no longer in RNS.Transport.active_links is
    dropped; directory entries for users with no live link go with them
    (the node is unreachable). Presence transitions fire exactly once --
    comparing the derived online set around the prune -- and stream as
    guild-wide presence events. Previously every client had to re-register
    every 4 seconds so an 8s timer could guess liveness; RNS keepalives
    and teardowns already do that job."""
    before = set(online_users())
    active = {l.link_id for l in list(RNS.Transport.active_links)
              if l.status == RNS.Link.ACTIVE}
    with ONLINE_LINKS_LOCK:
        for lid in [lid for lid in ONLINE_LINKS if lid not in active]:
            del ONLINE_LINKS[lid]
        live_users = set(ONLINE_LINKS.values())
    with SUB_LOCK:
        for lid in [lid for lid in SUBSCRIPTIONS if lid not in active]:
            s = SUBSCRIPTIONS.pop(lid)
            cur = SUB_BY_USER.get(s["user_hash"])
            if cur and cur.get("link_id") == lid:
                del SUB_BY_USER[s["user_hash"]]
    with NODE_REGISTRY_LOCK:
        stale = [h for h, e in list(NODE_REGISTRY.items())
                 if e.get("user_hash") not in live_users]
        if stale:
            for h in stale:
                del NODE_REGISTRY[h]
                RNS.log("dropped node %s (link down)" % h[:12], RNS.LOG_NOTICE)
    after = set(online_users())
    if after != before:
        _emit_event(None, "presence", {"users": _presence_payload()},
                    guild_wide=True)
    return stale

def _node_reaper():
    while True:
        time.sleep(1.0)
        try:
            _reap_stale_nodes()
        except Exception as e:
            RNS.log("node reaper error: %s" % e, RNS.LOG_ERROR)
        try:
            if BLOB_UPLOADS:
                _reap_stale_uploads()
        except Exception as e:
            RNS.log("upload reaper error: %s" % e, RNS.LOG_ERROR)
        try:
            if _update_maybe_tick():
                pass
        except Exception as e:
            RNS.log("update tick error: %s" % e, RNS.LOG_ERROR)
        try:
            if ATT_RAM:
                _att_ram_sweep()
        except Exception as e:
            RNS.log("att RAM reaper error: %s" % e, RNS.LOG_ERROR)

def online_users():
    """The active user list, derived from guild link liveness (v0.29).

    A user is online iff at least one of their guild links is in
    RNS.Transport.active_links (RNS Link.ACTIVE -- keepalives and teardown
    monitor the link natively). ONLINE_LINKS binds link_id -> user_hash on
    every API request; the remote_identity on the request is the proof of
    who is behind the link, so a binding cannot be forged.

    Keyed by canonical user hash (the lxmf.delivery destination hash) so the
    result joins directly against messages.author_hash / members.member_hash.
    A user with several registered nodes keeps only their most recent entry.

    Returns {user_hash: {user_hash, username, node_hash, last_seen}}.
    """
    with ONLINE_LINKS_LOCK:
        live_users = set(ONLINE_LINKS.values())
    with NODE_REGISTRY_LOCK:
        entries = [(e.get("user_hash"), h, e.get("last_seen", 0.0))
                   for h, e in NODE_REGISTRY.items()
                   if e.get("user_hash") in live_users]
    users = {}
    for user_hash, node_hash, last_seen in entries:
        prev = users.get(user_hash)
        if prev is None or last_seen > prev["last_seen"]:
            users[user_hash] = {"user_hash": user_hash, "node_hash": node_hash,
                                "last_seen": last_seen,
                                "username": get_username(user_hash)}
    return users

# ----------------------------------------------------------------------------------
# Attachments (content-addressed on disk; one request handler per file, the
# nomadnet/Node.py register_files pattern)
# ----------------------------------------------------------------------------------

def safe_ext(name, mime):
    m = re.search(r"\.([a-zA-Z0-9]{1,8})$", name or "")
    if m and m.group(1).lower() in ATTACHMENT_EXT:
        return m.group(1).lower()
    mm = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "audio/mpeg": "mp3"}
    return mm.get((mime or "").lower(), "bin")

def store_attachment(name, mime, data, by=None):
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    if len(data) > MAX_ATTACHMENT_SZ:
        return None
    digest = hashlib.sha256(data).hexdigest()[:32]
    fname = f"{digest}.{safe_ext(name, mime)}"
    if _attachment_mode() == "ephemeral":
        # P2: the message row persists, the attachment bytes live in
        # the RAM registry until the TTL; the /file/attachments/<fname>
        # handler below stays registered so remote fetches route and
        # land in the RAM branch (serve + att/get resolve RAM-first).
        _att_ram_put(digest, bytes(data), str(name)[:128],
                     str(mime)[:64], by)
    else:
        adir = os.path.join(STORAGE, "attachments")
        os.makedirs(adir, exist_ok=True)
        path = os.path.join(adir, fname)
        try:
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(data)
        except Exception as e:
            RNS.log(f"store_attachment write failed for {fname}: {e}",
                    RNS.LOG_ERROR)
            return None
    dlog(f"store_attachment: name={name!r} mime={mime!r} size={len(data)} fname={fname}")
    if node_destination is not None:
        node_destination.register_request_handler(
            "/file/attachments/" + fname, serve_attachment_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
        dlog(f"store_attachment: registered handler /file/attachments/{fname}")
    return fname

def serve_attachment_file(path, data, request_id, link_id, remote_identity, requested_at):
    fname = path.rsplit("/", 1)[-1]
    dlog(f"serve_attachment_file: requested fname={fname!r}")
    if not re.match(r"^[0-9a-f]{32}\.[a-z0-9]{1,8}$", fname):
        dlog(f"serve_attachment_file: invalid fname {fname!r}")
        return b"not found"
    if not attachment_visible(viewer_of(remote_identity), fname):
        dlog(f"serve_attachment_file: gated {fname!r}")
        return b"not found"
    entry = _att_ram_get(fname.split(".")[0])
    if entry is not None:
        blob = bytes(entry["bytes"])
        dlog(f"serve_attachment_file: served {fname} from RAM "
             f"({len(blob)} bytes)")
        return blob
    fpath = os.path.join(STORAGE, "attachments", fname)
    if os.path.isfile(fpath):
        with open(fpath, "rb") as f:
            blob = f.read()
        dlog(f"serve_attachment_file: served {fname} ({len(blob)} bytes)")
        return blob
    dlog(f"serve_attachment_file: file not found {fpath}")
    return b"not found"

# ----------------------------------------------------------------------------------
# Ingest (plan §4): both LXMF delivery and page compose converge here.
# One transaction: user placeholder, message row, attachments, members, seq bump.
# ----------------------------------------------------------------------------------

_last_post_at = {}              # user_hash -> ts of last accepted post (slowmode)
_last_post_lock = threading.Lock()

def _slow_ok(user_hash, seconds):
    """Slowmode gate: True if the user may post now (and records the
    post). Uses its own lock so it never deadlocks with db_lock."""
    t = now()
    with _last_post_lock:
        last = _last_post_at.get(user_hash, 0.0)
        if t - last < seconds:
            return False
        _last_post_at[user_hash] = t
    return True

def ingest(channel_id, author_hash, content, ts=None, attachments=None):
    dlog(f"ingest: channel={channel_id[:12]} author={author_hash[:12]} content_len={len(content or '')} attachments={len(attachments or [])}")
    if is_banned(author_hash):
        return None
    if is_muted(author_hash):
        return None  # muted: post silently dropped
    with db_lock:
        _kind_row = db.execute("SELECT kind FROM channels WHERE id=?",
                               (channel_id,)).fetchone()
    _chn_kind = _kind_row["kind"] if _kind_row else "chat"
    if _chn_kind == "announce":
        # P8: announcements only -- explicit grants (or mods); the open
        # default never confers POST on an announce channel
        if not _announce_post_ok(author_hash, channel_id):
            RNS.log("ingest: announce POST denied for %s in %s"
                    % (author_hash[:12], channel_id[:12]), RNS.LOG_WARNING)
            return None
    elif not has_perm(author_hash, channel_id, PERMS["POST"]):
        RNS.log("ingest: POST denied for %s in %s" % (author_hash[:12],
                                                      channel_id[:12]),
                RNS.LOG_WARNING)
        return None  # no POST permission in this channel
    # guild-wide slowmode (admin setting): one post per N seconds per user
    slow = 0
    try:
        slow = int(_cfg_get("slowmode", "0") or 0)
    except (TypeError, ValueError):
        slow = 0
    if slow > 0 and not _slow_ok(author_hash, slow) \
            and not has_role_perm(author_hash, PERMS["ADMIN"]):
        RNS.log("ingest: slowmode drop for %s" % author_hash[:12],
                RNS.LOG_DEBUG)
        return None
    content = (content or "").strip()
    if not content and not attachments:
        dlog("ingest: empty content and no attachments, rejecting")
        return None
    if len(content) > MAX_CONTENT_LEN:
        content = content[:MAX_CONTENT_LEN]
    ts = ts if isinstance(ts, (int, float)) and ts > 0 else now()
    msg_id = hexstr(RNS.Identity.full_hash(
        f"{channel_id}|{author_hash}|{ts}|{content}".encode()))
    att_meta = []
    with db_lock:
        if db.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone():
            dlog(f"ingest: duplicate msg_id {msg_id[:12]}")
            return None  # duplicate
        # zero-login: auto-register placeholder username on first post
        db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) "
                   "VALUES (?, ?, ?)", (author_hash, "user-" + author_hash[:6], now()))
        db.execute(
            "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
            "VALUES (?, ?, ?, ?, ?)", (msg_id, channel_id, ts, author_hash, content))
        for idx, att in enumerate((attachments or [])[:MAX_ATTACHMENTS]):
            try:
                name, mime, blob = att
            except Exception:
                dlog(f"ingest: malformed attachment at idx {idx}, skipping")
                continue
            fname = store_attachment(str(name)[:128], str(mime)[:64],
                                     blob, by=author_hash)
            if fname:
                dlog(f"ingest: stored attachment idx={idx} name={name} fname={fname}")
                db.execute("INSERT OR REPLACE INTO attachments "
                           "(msg_id, idx, name, mime, size, path) VALUES (?, ?, ?, ?, ?, ?)",
                           (msg_id, idx, str(name)[:128], str(mime)[:64], len(blob), fname))
                att_meta.append({"name": str(name)[:128], "mime": str(mime)[:64],
                                 "size": len(blob), "path": "/file/attachments/" + fname})
            else:
                dlog(f"ingest: store_attachment returned None for idx={idx} name={name}")
        db.execute("INSERT INTO members (channel_id, member_hash, last_post) VALUES (?, ?, ?) "
                   "ON CONFLICT(channel_id, member_hash) DO UPDATE SET last_post=excluded.last_post",
                   (channel_id, author_hash, ts))
        bump_seq(channel_id)
        db.commit()
    RNS.log(f"ingest #{channel_name(channel_id)} msg {msg_id[:12]} from {author_hash[:12]}",
            RNS.LOG_DEBUG)
    # Stream event: subscribers of this channel get the full message row.
    _emit_event(channel_id, "msg", {"msg": {
        "msg_id": msg_id, "channel_id": channel_id, "ts": ts,
        "author_hash": author_hash, "username": get_username(author_hash),
        "content": content, "edited_at": None, "deleted_at": None,
        "attachments": att_meta, "reactions": [],
    }})
    return msg_id

def extract_channel(content):
    """Leading '#channel' token → (channel_id, body). No prefix → general."""
    m = re.match(r"^\s*#([a-zA-Z0-9_\-]{1,32})[ \t]?\n?", content or "")
    if m:
        return ensure_channel(m.group(1)), (content or "")[m.end():].lstrip()
    return ensure_channel("general"), (content or "")

# ----------------------------------------------------------------------------------
# LXMF delivery callback (verified: fires per accepted inbound LXM, LXMRouter.py:1805)
# ----------------------------------------------------------------------------------

def on_message(lxm):
    try:
        sender = hexstr(lxm.source_hash)
        content = lxm.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        channel_id, body = extract_channel(content)
        if not rate_ok(sender, channel_id):
            RNS.log(f"rate limit: dropping post from {sender[:12]}", RNS.LOG_DEBUG)
            return
        attachments = []
        fields = getattr(lxm, "fields", None) or {}
        env = fields.get(FIELD_ATTACHMENTS)
        if isinstance(env, list):
            for entry in env[:MAX_ATTACHMENTS]:
                if isinstance(entry, (list, tuple)) and len(entry) == 3:
                    attachments.append(entry)
        ingest(channel_id, sender, body, ts=lxm.timestamp, attachments=attachments)
    except Exception as e:
        RNS.log(f"on_message error: {e}", RNS.LOG_ERROR)

# ----------------------------------------------------------------------------------
# Page renderers (plan §6). Handler signature verified:
# response_generator(path, data, request_id, link_id, remote_identity, requested_at)
# data carries browser fields as "field_<name>" / "var_<name>".
# ----------------------------------------------------------------------------------

PAGE_ROUTES = {}   # filled by @route at import time
API_ROUTES  = {}

def route(path):
    def deco(fn):
        PAGE_ROUTES[path] = fn
        return fn
    return deco

def api_route(path):
    def deco(fn):
        API_ROUTES[path] = fn
        return fn
    return deco

def viewer_of(remote_identity):
    if remote_identity is None:
        return None
    # Canonical user key = the LXMF delivery hash derived from the identified
    # identity — the SAME hash their LXMF posts carry as source_hash. A link
    # identifies with the raw identity (hash of the public key), while LXMF
    # attributes messages to the lxmf.delivery destination hash; deriving one
    # from the other makes page-compose and client-post attributions converge.
    return hexstr(RNS.Destination(
        remote_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
        "lxmf", "delivery").hash)

def get_data(data, name, default=None):
    if not isinstance(data, dict):
        return default
    return data.get("field_" + name, data.get("var_" + name, default))

PAGE_IDENTITY_REQUIRED = (page_header() + "> Identification needed for this action\n-\n"
    "cReading is open, but posting and settings need your identity.\n"
    "cIn NomadNet: enable 'identify on connect' for this node\n"
    "cin your directory, or simply post via LXMF instead\n"
    "c(message the guild address with a #channel first token).")

def msg_row_json(row):
    with db_lock:
        atts = db.execute("SELECT idx, name, mime, size, path FROM attachments "
                          "WHERE msg_id=? ORDER BY idx", (row["msg_id"],)).fetchall()
        rx = db.execute("SELECT emoji, user_hash FROM reactions "
                        "WHERE msg_id=? ORDER BY ts", (row["msg_id"],)).fetchall()
    summary = {}
    for r in rx:
        e = summary.setdefault(r["emoji"],
                               {"emoji": r["emoji"], "count": 0, "users": []})
        e["count"] += 1
        if len(e["users"]) < 32:
            e["users"].append(r["user_hash"])
    return {
        "id": row["id"], "msg_id": row["msg_id"], "channel_id": row["channel_id"],
        "ts": row["ts"], "author_hash": row["author_hash"],
        "username": get_username(row["author_hash"]),
        "content": "" if row["deleted_at"] else row["content"],
        "edited_at": row["edited_at"], "deleted_at": row["deleted_at"],
        "attachments": [{"name": a["name"], "mime": a["mime"], "size": a["size"],
                         "path": "/file/attachments/" + a["path"]} for a in atts],
        "reactions": list(summary.values()),
    }

@route("/page/index.mu")
def page_index(path, data, request_id, link_id, remote_identity, requested_at):
    out = [page_header()]
    out.append(f"> {GUILD_NAME}\n")
    out.append("`F8a8Reticulum-native chat`f  `[Settings`:/page/sHades/settings.mu]\n")
    out.append("-\n")
    out.append("> Channels\n")
    with db_lock:
        chans = db.execute("SELECT id, name FROM channels ORDER BY name ASC").fetchall()
    viewer = viewer_of(remote_identity)
    # anonymous / insufficient-VIEW lanes never even see the channel names
    chans = [c for c in chans if page_can_view(viewer, c["id"])]
    for c in chans:
        out.append("`[#" + esc_micron_text(c["name"]) +
                   "`:/page/sHades/channel.mu`channel=" + c["id"] + "]\n")
    if not chans:
        out.append("cNo channels yet.\n")
    return "".join(out).encode("utf-8")

@route("/page/sHades/channel.mu")
def page_channel(path, data, request_id, link_id, remote_identity, requested_at):
    cid = get_data(data, "channel")
    with db_lock:
        chan = db.execute("SELECT id, name FROM channels WHERE id=?", (cid,)).fetchone()
    if not chan:
        return (page_header() + "> Unknown channel\n"
                "cPick one from `[Channels`:/page/index.mu]\n").encode("utf-8")
    viewer = viewer_of(remote_identity)
    if not page_can_view(viewer, cid):
        return (page_header() + "> Channel unavailable\n").encode("utf-8")
    out = [page_header()]
    out.append("> #" + esc_micron_text(chan["name"]) + "\n")
    out.append("-\n")
    # live subframe: auto-refresh partial (verified partial grammar + parse_url)
    out.append("`{:/page/sHades/messages.mu`8`channel=" + chan["id"] + "}\n")
    out.append("-\n")
    token = issue_token(viewer, f"post:{chan['id']}") if viewer else ""
    out.append("`B333`<msg`>`b  `[Send`:/page/sHades/update.mu`msg|channel=" +
               chan["id"] + "|token=" + token + "]\n")
    out.append("`[Channels`:/page/index.mu]  ")
    out.append("`[Members`:/page/sHades/members.mu`channel=" + chan["id"] + "]\n")
    return "".join(out).encode("utf-8")

@route("/page/sHades/messages.mu")
def page_messages(path, data, request_id, link_id, remote_identity, requested_at):
    cid = get_data(data, "channel")
    before = get_data(data, "before")
    viewer = viewer_of(remote_identity)
    with db_lock:
        chan = db.execute("SELECT id, name FROM channels WHERE id=?", (cid,)).fetchone()
        if not chan:
            return b"cUnknown channel."
        if before and str(before).isdigit():
            rows = db.execute(
                "SELECT * FROM messages WHERE channel_id=? AND id<? "
                "ORDER BY id DESC LIMIT ?", (cid, int(before), PAGE_SIZE)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM messages WHERE channel_id=? "
                "ORDER BY id DESC LIMIT ?", (cid, PAGE_SIZE)).fetchall()
        oldest = rows[-1]["id"] if rows else None
        total_before = (db.execute("SELECT COUNT(*) AS n FROM messages WHERE channel_id=? AND id<?",
                                   (cid, oldest)).fetchone()["n"] if oldest else 0)
    if not page_can_view(viewer, cid):
        return (page_header() + "> Channel unavailable\n").encode("utf-8")
    rows = list(reversed(rows))  # newest last, Discord order
    out = []
    for r in rows:
        out.append(username_fragment(r["author_hash"]))
        out.append(" `F555" + fmt_time(r["ts"]) + "`f")
        if r["edited_at"]:
            out.append(" `F555(edited)`f")
        out.append("\n")
        if r["deleted_at"]:
            out.append("`F555<message deleted>`f\n")
        else:
            out.append(render_content(r["content"]) + "\n")
            with db_lock:
                atts = db.execute("SELECT name, path FROM attachments WHERE msg_id=? ORDER BY idx",
                                  (r["msg_id"],)).fetchall()
            for a in atts:
                out.append("`F8a8[attachment]`f `[Download " + esc_micron_text(a["name"]) +
                           "`/file/attachments/" + a["path"] + "]\n")
            if viewer and (viewer == r["author_hash"] or is_moderator(viewer)):
                # owner (or mod) controls: one-time tokens minted per render (§7)
                dt = issue_token(viewer, f"del:{r['msg_id']}")
                if viewer == r["author_hash"]:
                    et = issue_token(viewer, f"edit:{r['msg_id']}")
                    out.append("`F555`[edit`:/page/sHades/edit.mu`msg=" + r["msg_id"] +
                               "|token=" + et + "]`f")
                    out.append(" ")
                out.append("`F555`[del`:/page/sHades/delete.mu`msg=" + r["msg_id"] +
                           "|token=" + dt + "]`f")
                out.append("\n")
        out.append("\n")
    if total_before > 0 and oldest:
        out.append("`[Load older`:/page/sHades/messages.mu`channel=" + cid +
                   "|before=" + str(oldest) + "]\n")
    if not rows:
        out.append("cNo messages yet. Say something.\n")
    return "".join(out).encode("utf-8")

@route("/page/sHades/update.mu")
def page_update(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    cid = get_data(data, "channel")
    body = get_data(data, "msg") or ""
    token = get_data(data, "token") or ""
    with db_lock:
        chan = db.execute("SELECT id FROM channels WHERE id=?", (cid,)).fetchone()
    if not chan or not isinstance(body, str) or not body.strip():
        return (page_header() + "> Nothing to send\ncEmpty message.\n").encode("utf-8")
    if not check_token(viewer, f"post:{cid}", token):
        return (page_header() + "> Expired form\n"
                "cReload the channel and try again (one-time token).\n").encode("utf-8")
    if not rate_ok(viewer, cid):
        return (page_header() + "> Slow down\ncRate limit: "
                f"{RATE_MAX_POSTS} posts / {RATE_WINDOW_S}s.\n").encode("utf-8")
    ingest(cid, viewer, body)
    return page_messages(path, {"var_channel": cid},
                         request_id, link_id, remote_identity, requested_at)

@route("/page/sHades/edit.mu")
def page_edit(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    msg_id = get_data(data, "msg") or ""
    with db_lock:
        row = db.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
    if not row or row["deleted_at"] or row["author_hash"] != viewer:
        return (page_header() + "> Not allowed\ncYou can only edit your own messages.\n"
                ).encode("utf-8")
    token = issue_token(viewer, f"edit:{msg_id}")
    out = [page_header()]
    out.append("> Edit message\n-\n")
    out.append("Current (micron literal block):\n`=\n")
    out.append(esc_micron_text(row["content"]) + "\n`=\n-\n")
    # Field defaults are single-line in Micron; type the replacement below.
    out.append("Replacement message:\n`B333`<content`>`b\n")
    out.append("`[Save`:/page/sHades/save_edit.mu`msg=" + msg_id + "|token=" + token + "]\n")
    out.append("`[Cancel`:/page/sHades/channel.mu`channel=" + row["channel_id"] + "]\n")
    return "".join(out).encode("utf-8")

@route("/page/sHades/save_edit.mu")
def page_save_edit(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    msg_id = get_data(data, "msg") or ""
    token = get_data(data, "token") or ""
    content = get_data(data, "content") or ""
    with db_lock:
        row = db.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
    if not row or row["author_hash"] != viewer:
        return (page_header() + "> Not allowed\n").encode("utf-8")
    if not check_token(viewer, f"edit:{msg_id}", token):
        return (page_header() + "> Expired form\ncReload and retry.\n").encode("utf-8")
    if not isinstance(content, str) or not content.strip():
        return (page_header() + "> Empty edit\ncUse delete instead.\n").encode("utf-8")
    with db_lock:
        edited_at = now()
        db.execute("INSERT INTO message_edits (msg_id, edited_at, prior_content) "
                   "VALUES (?, ?, ?)", (msg_id, edited_at, row["content"]))
        db.execute("UPDATE messages SET content=?, edited_at=? WHERE msg_id=?",
                   (content.strip()[:MAX_CONTENT_LEN], edited_at, msg_id))
        bump_seq(row["channel_id"])
        db.commit()
    return page_messages(path, {"var_channel": row["channel_id"]},
                         request_id, link_id, remote_identity, requested_at)

@route("/page/sHades/delete.mu")
def page_delete(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    msg_id = get_data(data, "msg") or ""
    with db_lock:
        row = db.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
    if not row or row["deleted_at"] or not can_delete(viewer, row):
        return (page_header() + "> Not allowed\n"
                "cOnly the author or a guild moderator can delete this.\n").encode("utf-8")
    token = issue_token(viewer, f"del:{msg_id}")
    out = [page_header()]
    out.append("> Delete message\n-\n")
    out.append(render_content(row["content"]) + "\n-\n")
    out.append("`[Confirm delete`:/page/sHades/do_delete.mu`msg=" + msg_id +
               "|token=" + token + "]\n")
    out.append("`[Cancel`:/page/sHades/channel.mu`channel=" + row["channel_id"] + "]\n")
    return "".join(out).encode("utf-8")

@route("/page/sHades/do_delete.mu")
def page_do_delete(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    msg_id = get_data(data, "msg") or ""
    token = get_data(data, "token") or ""
    with db_lock:
        row = db.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
    if not row or not page_can_view(viewer, row["channel_id"]):
        return (page_header() + "> Not allowed\n").encode("utf-8")
    if not can_delete(viewer, row):
        return (page_header() + "> Not allowed\n").encode("utf-8")
    if not check_token(viewer, f"del:{msg_id}", token):
        return (page_header() + "> Expired form\ncReload and retry.\n").encode("utf-8")
    with db_lock:
        db.execute("UPDATE messages SET deleted_at=? WHERE msg_id=?", (now(), msg_id))
        bump_seq(row["channel_id"])
        db.commit()
    return page_messages(path, {"var_channel": row["channel_id"]},
                         request_id, link_id, remote_identity, requested_at)

@route("/page/sHades/members.mu")
def page_members(path, data, request_id, link_id, remote_identity, requested_at):
    cid = get_data(data, "channel")
    with db_lock:
        chan = db.execute("SELECT id, name FROM channels WHERE id=?", (cid,)).fetchone()
        if not chan:
            return b"cUnknown channel."
        rows = db.execute(
            "SELECT m.member_hash, m.last_post, u.username FROM members m "
            "LEFT JOIN users u ON u.dest_hash = m.member_hash "
            "WHERE m.channel_id=? ORDER BY m.last_post DESC LIMIT 100", (cid,)).fetchall()
    viewer = viewer_of(remote_identity)
    if not page_can_view(viewer, cid):
        return (page_header() + "> Channel unavailable\n").encode("utf-8")
    online = online_users()
    out = [page_header()]
    out.append("> Members of #" + esc_micron_text(chan["name"]) + "\n-\n")
    # Presence is link-derived (v0.29): everyone with a live link is here;
    # the presence keeper prunes the list the moment a link dies.
    out.append("`!Online now`!  `F555" + str(len(online)) +
               " online (guild-wide)`f\n")
    if online:
        for u in sorted(online.values(), key=lambda u: u["username"].lower()):
            # "+" and not "*"/"_": those are Micron formatting controls, so
            # inside a formatting run they toggle style instead of printing.
            out.append("`F2a2+`f " + username_fragment(u["user_hash"]))
            if u["user_hash"] == viewer:
                out.append(" `F555(you)`f")
            out.append("\n")
    else:
        out.append("`F555(nobody online)`f\n")
    out.append("\n`!Channel history`!\n")
    for r in rows:
        out.append(("`F2a2+`f " if r["member_hash"] in online else "  ") +
                   username_fragment(r["member_hash"]) +
                   " `F555last post " + fmt_time(r["last_post"]) + "`f")
        if is_moderator(viewer) and r["member_hash"] != viewer:
            bt = issue_token(viewer, f"ban:{r['member_hash']}")
            out.append(" `F555`[ban`:/page/sHades/do_ban.mu`member=" +
                       r["member_hash"] + "|token=" + bt + "]`f")
            if is_muted(r["member_hash"]):
                ut = issue_token(viewer, f"unmute:{r['member_hash']}")
                out.append(" `F555`[unmute`:/page/sHades/do_unmute.mu`member=" +
                           r["member_hash"] + "|token=" + ut + "]`f")
            else:
                mt = issue_token(viewer, f"mute:{r['member_hash']}")
                out.append(" `F555`[mute`:/page/sHades/do_mute.mu`member=" +
                           r["member_hash"] + "|token=" + mt + "]`f")
        out.append("\n")
    out.append("`[Back`:/page/sHades/channel.mu`channel=" + chan["id"] + "]\n")
    if is_moderator(viewer):
        out.append("`[Moderation`:/page/sHades/mod.mu]\n")
    return "".join(out).encode("utf-8")

def _require_mod(viewer):
    return viewer is not None and is_moderator(viewer)

@route("/page/sHades/mod.mu")
def page_mod(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    if not _require_mod(viewer):
        return (page_header() + "> Not a moderator\ncAsk a guild operator to add your\n"
                "chash to moderators.txt on the guild host.\n").encode("utf-8").replace(
                    b"chash", b"hash")
    with db_lock:
        rows = db.execute(
            "SELECT b.dest_hash, b.reason, b.at, u.username FROM banned b "
            "LEFT JOIN users u ON u.dest_hash = b.dest_hash ORDER BY b.at DESC").fetchall()
    out = [page_header()]
    out.append("> Moderation\n-\n")
    out.append("You are a guild moderator.\n\n")
    out.append("`!Banned users`\n")
    if not rows:
        out.append("`F555(none)`f\n")
    for r in rows:
        name = r["username"] or ("user-" + r["dest_hash"][:6])
        ut = issue_token(viewer, f"unban:{r['dest_hash']}")
        out.append(esc_micron_text(name) + " `F555" + fmt_time(r["at"]) + "`f " +
                   "`[unban`:/page/sHades/do_unban.mu`member=" + r["dest_hash"] +
                   "|token=" + ut + "]\n")
    out.append("\n`!How to ban`\n")
    out.append("`F555Open a channel's Members page; mods see a [ban] link next to each poster.`f\n")
    out.append("`[Back to guild`:/page/index.mu]\n")
    return "".join(out).encode("utf-8")

@route("/page/sHades/do_ban.mu")
def page_do_ban(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    if not _require_mod(viewer):
        return (page_header() + "> Not a moderator\n").encode("utf-8")
    member = get_data(data, "member") or ""
    token = get_data(data, "token") or ""
    if not re.match(r"^[0-9a-f]{32}$", member):
        return (page_header() + "> Bad member hash\n").encode("utf-8")
    if not check_token(viewer, f"ban:{member}", token):
        return (page_header() + "> Expired form\ncReload and retry.\n").encode("utf-8")
    ban_user(member, reason=f"banned by {viewer[:12]}")
    RNS.log(f"moderation: {viewer[:12]} banned {member[:12]}", RNS.LOG_NOTICE)
    return page_mod(path, data, request_id, link_id, remote_identity, requested_at)

@route("/page/sHades/do_unban.mu")
def page_do_unban(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    if not _require_mod(viewer):
        return (page_header() + "> Not a moderator\n").encode("utf-8")
    member = get_data(data, "member") or ""
    token = get_data(data, "token") or ""
    if not check_token(viewer, f"unban:{member}", token):
        return (page_header() + "> Expired form\ncReload and retry.\n").encode("utf-8")
    unban_user(member)
    RNS.log(f"moderation: {viewer[:12]} unbanned {member[:12]}", RNS.LOG_NOTICE)
    return page_mod(path, data, request_id, link_id, remote_identity, requested_at)

@route("/page/sHades/do_mute.mu")
def page_do_mute(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    if not _require_mod(viewer):
        return (page_header() + "> Not a moderator\n").encode("utf-8")
    member = get_data(data, "member") or ""
    token = get_data(data, "token") or ""
    if not re.match(r"^[0-9a-f]{32}$", member):
        return (page_header() + "> Bad member hash\n").encode("utf-8")
    if not check_token(viewer, f"mute:{member}", token):
        return (page_header() + "> Expired form\ncReload and retry.\n").encode("utf-8")
    mute_user(member, reason=f"muted by {viewer[:12]}")
    RNS.log(f"moderation: {viewer[:12]} muted {member[:12]}", RNS.LOG_NOTICE)
    return page_mod(path, data, request_id, link_id, remote_identity, requested_at)

@route("/page/sHades/do_unmute.mu")
def page_do_unmute(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    if not _require_mod(viewer):
        return (page_header() + "> Not a moderator\n").encode("utf-8")
    member = get_data(data, "member") or ""
    token = get_data(data, "token") or ""
    if not check_token(viewer, f"unmute:{member}", token):
        return (page_header() + "> Expired form\ncReload and retry.\n").encode("utf-8")
    unmute_user(member)
    RNS.log(f"moderation: {viewer[:12]} unmuted {member[:12]}", RNS.LOG_NOTICE)
    return page_mod(path, data, request_id, link_id, remote_identity, requested_at)

@route("/page/sHades/settings.mu")
def page_settings(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    with db_lock:
        row = db.execute("SELECT username, profile_node FROM users WHERE dest_hash=?",
                         (viewer,)).fetchone()
    username = (row["username"] if row and row["username"] else "")
    profile_node = (row["profile_node"] if row and row["profile_node"] else "")
    token = issue_token(viewer, f"settings:{viewer}")
    out = [page_header()]
    out.append("> Settings\n-\n")
    out.append("Username (shown in chat):\n")
    out.append("`B333`<username`" + esc_micron_text(username) + ">`b\n\n")
    out.append("Profile node hash (32 hex chars, optional — makes your name clickable):\n")
    out.append("`B333`<profile_node`" + esc_micron_text(profile_node) + ">`b\n\n")
    out.append("`[Save`:/page/sHades/save_settings.mu`token=" + token + "]\n")
    out.append("`[Back`:/page/index.mu]\n")
    return "".join(out).encode("utf-8")

@route("/page/sHades/save_settings.mu")
def page_save_settings(path, data, request_id, link_id, remote_identity, requested_at):
    viewer = viewer_of(remote_identity)
    if viewer is None:
        return PAGE_IDENTITY_REQUIRED.encode("utf-8")
    token = get_data(data, "token") or ""
    username = (get_data(data, "username") or "").strip()
    profile_node = (get_data(data, "profile_node") or "").strip()
    if not check_token(viewer, f"settings:{viewer}", token):
        return (page_header() + "> Expired form\ncReload settings and retry.\n").encode("utf-8")
    if not username or not re.match(r"^[A-Za-z0-9_\-\. ]{2,32}$", username):
        return (page_header() + "> Invalid username\n"
                "c2-32 chars: letters, digits, space, _ - .\n").encode("utf-8")
    if profile_node and not re.match(r"^[0-9a-fA-F]{32}$", profile_node):
        return (page_header() + "> Invalid profile node hash\nc32 hex characters.\n"
                ).encode("utf-8")
    profile_node = profile_node.lower() if profile_node else None
    with db_lock:
        clash = db.execute("SELECT dest_hash FROM users WHERE username=? COLLATE NOCASE "
                           "AND dest_hash<>?", (username, viewer)).fetchone()
        if clash:
            return (page_header() + "> Username taken\ncPick another one.\n").encode("utf-8")
        db.execute("INSERT INTO users (dest_hash, username, profile_node, registered_at) "
                   "VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(dest_hash) DO UPDATE SET username=excluded.username, "
                   "profile_node=excluded.profile_node",
                   (viewer, username, profile_node, now()))
        db.commit()
        _maybe_founder(viewer)
    out = [page_header()]
    out.append("> Saved\n-\n")
    out.append("Username: `!" + esc_micron_text(username) + "!`\n")
    if profile_node:
        out.append("Profile: `[your node`" + profile_node + "]\n")
    out.append("`[Back to guild`:/page/index.mu]\n")
    return "".join(out).encode("utf-8")

# ----------------------------------------------------------------------------------
# API endpoints (plan §14 groundwork): msgpack in/out, UNPREFIXED keys.
# Unreachable from forged micron links (browser submits field_/var_ keys only).
# Auth = link identity (remote_identity). /api/messages implements the §8 delta
# protocol: since_id for new rows, since_ts for edits/deletes.
# ----------------------------------------------------------------------------------

def api_error(msg, code=0):
    return msgpack.packb({"ok": 0, "error": msg, "code": code})

def wrap_api(fn):
    def handler(path, data, request_id, link_id, remote_identity, requested_at):
        viewer = viewer_of(remote_identity)
        if viewer is None:
            return api_error("identified link required", 401)
        # v0.29: every identified request binds presence -- this link belongs
        # to this user until the link dies (the presence keeper prunes it).
        if link_id:
            with ONLINE_LINKS_LOCK:
                ONLINE_LINKS[link_id] = viewer
        try:
            if not isinstance(data, dict):
                return api_error("dict payload required", 400)
            return fn(viewer, data)
        except Exception as e:
            RNS.log(f"api error {path}: {e}", RNS.LOG_ERROR)
            return api_error("internal error", 500)
    return handler

@api_route("/api/state")
@wrap_api
def api_state(viewer, data):
    cid = data.get("channel")
    with db_lock:
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return api_error("unknown channel", 404)
        seq = get_seq(cid)
        row = db.execute("SELECT msg_id, max(coalesce(edited_at,0), coalesce(deleted_at,0)) AS mt "
                         "FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    return msgpack.packb({"ok": 1, "seq": seq,
                          "last": row["msg_id"] if row else None,
                          "last_ts": row["mt"] if row else 0.0})

@api_route("/api/messages")
@wrap_api
def api_messages(viewer, data):
    cid = data.get("channel")
    if not has_perm(viewer, cid, PERMS["VIEW"]):
        return api_error("view permission required", 403)
    with db_lock:
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return api_error("unknown channel", 404)
        seq = get_seq(cid)
        since_id = data.get("since_id")
        since_ts = data.get("since_ts")
        before   = data.get("before")
        limit = min(int(data.get("limit", PAGE_SIZE)), 200)
        if before:
            brow = db.execute("SELECT id FROM messages WHERE msg_id=?", (before,)).fetchone()
            new_rows = db.execute(
                "SELECT * FROM messages WHERE channel_id=? AND id<? ORDER BY id DESC LIMIT ?",
                (cid, brow["id"] if brow else 0, limit)).fetchall()
            new_rows = list(reversed(new_rows))
        elif isinstance(since_id, int):
            new_rows = db.execute(
                "SELECT * FROM messages WHERE channel_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (cid, since_id, limit)).fetchall()
        elif isinstance(since_id, str) and since_id:
            # since_id is a msg_id hash string, not the integer PK. Resolve it
            # first so delta polling returns rows after the last seen message.
            bound = db.execute(
                "SELECT id FROM messages WHERE msg_id=? AND channel_id=?",
                (since_id, cid)).fetchone()
            if bound:
                new_rows = db.execute(
                    "SELECT * FROM messages WHERE channel_id=? AND id>? ORDER BY id ASC LIMIT ?",
                    (cid, bound["id"], limit)).fetchall()
            else:
                new_rows = []
        else:
            new_rows = db.execute(
                "SELECT * FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT ?",
                (cid, limit)).fetchall()
            new_rows = list(reversed(new_rows))
        upd_rows = []
        if isinstance(since_ts, (int, float)) and since_ts > 0:
            id_bound = since_id if isinstance(since_id, int) else None
            if id_bound is not None:
                upd_rows = db.execute(
                    "SELECT * FROM messages WHERE channel_id=? AND id<=? AND "
                    "(coalesce(edited_at,0) > ? OR coalesce(deleted_at,0) > ?) "
                    "ORDER BY id ASC LIMIT ?",
                    (cid, id_bound, since_ts, since_ts, limit)).fetchall()
            else:
                upd_rows = db.execute(
                    "SELECT * FROM messages WHERE channel_id=? AND "
                    "(coalesce(edited_at,0) > ? OR coalesce(deleted_at,0) > ?) "
                    "ORDER BY id ASC LIMIT ?",
                    (cid, since_ts, since_ts, limit)).fetchall()
    return msgpack.packb({
        "ok": 1, "seq": seq,
        "messages": [msg_row_json(r) for r in new_rows],
        "updates": [{"msg_id": r["msg_id"],
                     "content": "" if r["deleted_at"] else r["content"],
                     "edited_at": r["edited_at"], "deleted_at": r["deleted_at"]}
                    for r in upd_rows],
    })

@api_route("/api/post")
@wrap_api
def api_post(viewer, data):
    cid = data.get("channel")
    content = data.get("content")
    atts = data.get("attachments") or []
    if not isinstance(atts, list):
        atts = []
    dlog(f"api_post: viewer={viewer[:12]} channel={cid} content_len={len(content or '')} attachments={len(atts)}")
    with db_lock:
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return api_error("unknown channel", 404)
    if not isinstance(content, str):
        return api_error("empty content", 400)
    # A message may be attachment-only (no text); require at least one.
    if not content.strip() and not atts:
        return api_error("empty content", 400)
    if not rate_ok(viewer, cid):
        return api_error("rate limited", 429)
    msg_id = ingest(cid, viewer, content, attachments=atts[:MAX_ATTACHMENTS])
    if msg_id is None:
        dlog(f"api_post: ingest rejected for channel={cid}")
        return api_error("rejected", 400)
    dlog(f"api_post: ingest ok msg_id={msg_id[:12]}")
    # Return stored attachment metadata so the client knows it's server-stored.
    with db_lock:
        att_row = db.execute(
            "SELECT name, mime, size, path FROM attachments WHERE msg_id=? ORDER BY idx LIMIT 1",
            (msg_id,),
        ).fetchone()
    resp = {"ok": 1, "msg_id": msg_id}
    if att_row:
        dlog(f"api_post: attachment metadata name={att_row['name']} path={att_row['path']}")
        resp["attachment"] = {
            "name": att_row["name"],
            "mime": att_row["mime"],
            "size": att_row["size"],
            "path": "/file/attachments/" + att_row["path"],
        }
    return msgpack.packb(resp)

def _mutate_msg(viewer, data, action):
    msg_id = data.get("msg")
    with db_lock:
        row = db.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
        if not row:
            return api_error("unknown message", 404)
        if row["author_hash"] != viewer:
            return api_error("not your message", 403)
        if action == "edit":
            content = data.get("content")
            if not isinstance(content, str) or not content.strip():
                return api_error("empty content", 400)
            new_content = content.strip()[:MAX_CONTENT_LEN]
            edited_at = now()
            db.execute("INSERT INTO message_edits (msg_id, edited_at, prior_content) "
                       "VALUES (?, ?, ?)", (msg_id, edited_at, row["content"]))
            db.execute("UPDATE messages SET content=?, edited_at=? WHERE msg_id=?",
                       (new_content, edited_at, msg_id))
            event = {"msg_id": msg_id, "content": new_content,
                     "edited_at": edited_at, "deleted_at": None}
        else:
            deleted_at = now()
            db.execute("UPDATE messages SET deleted_at=? WHERE msg_id=?", (deleted_at, msg_id))
            db.execute("DELETE FROM reactions WHERE msg_id=?", (msg_id,))
            db.execute("DELETE FROM message_edits WHERE msg_id=?", (msg_id,))
            event = {"msg_id": msg_id, "content": "", "edited_at": None,
                     "deleted_at": deleted_at}
        bump_seq(row["channel_id"])
        db.commit()
        seq = get_seq(row["channel_id"])
    _emit_event(row["channel_id"], action, event)
    return msgpack.packb({"ok": 1, "seq": seq})

@api_route("/api/edit")
@wrap_api
def api_edit(viewer, data):
    return _mutate_msg(viewer, data, "edit")

@api_route("/api/delete")
@wrap_api
def api_delete(viewer, data):
    return _mutate_msg(viewer, data, "delete")

@api_route("/api/register")
@wrap_api
def api_register(viewer, data):
    username = (data.get("username") or "").strip()
    profile_node = (data.get("profile_node") or "").strip()
    # join gate (U6b): with the 'code' policy a member must present a
    # valid invite code (/api/v1/join) before they can claim a username.
    # Existing members (already registered) are never locked out.
    if _registration_policy() == "code" and not _has_joined(viewer):
        return api_error("join code required", 403)
    if not re.match(r"^[A-Za-z0-9_\-\. ]{2,32}$", username):
        return api_error("invalid username", 400)
    if profile_node and not re.match(r"^[0-9a-fA-F]{32}$", profile_node):
        return api_error("invalid profile node hash", 400)
    with db_lock:
        clash = db.execute("SELECT dest_hash FROM users WHERE username=? COLLATE NOCASE "
                           "AND dest_hash<>?", (username, viewer)).fetchone()
        if clash:
            return api_error("username taken", 409)
        db.execute("INSERT INTO users (dest_hash, username, profile_node, registered_at) "
                   "VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(dest_hash) DO UPDATE SET username=excluded.username, "
                   "profile_node=excluded.profile_node",
                   (viewer, username, profile_node.lower() or None, now()))
        db.commit()
        _maybe_founder(viewer)
    return msgpack.packb({"ok": 1})

@api_route("/api/register_node")
@wrap_api
def api_register_node(viewer, data):
    """Client registers its node destination with the guild index (v0.29: a
    ONE-SHOT directory write at connect, and on reconnect).

    Presence is NOT this call: presence is link liveness (the guild derives
    it from RNS.Transport.active_links, where RNS keepalives and teardowns
    already monitor health). data: {node_hash} — the RNS destination hash of
    the member's nomadnetwork/node. This network is cryptographically
    addressed: members reach a node by hash (Identity.recall + RNS.Link),
    never by ip:port — that pair is TCP-hub transport config, not a
    directory concern. If an older client still sends address/port they are
    ignored. The node_hash is what profiles and the node index join on.

    The user identity is taken from the *identified link* the request arrived
    on (viewer = the lxmf.delivery hash derived from the requester's identity),
    NOT from anything in the payload. A client-supplied identity could be
    forged — one client could report a victim as online, or claim a name that
    isn't theirs — whereas the link identity is bound by the RNS handshake.
    """
    node_hash = (data.get("node_hash") or "").strip().lower()
    if not node_hash or not re.match(r"^[0-9a-f]{32}$", node_hash):
        return api_error("invalid node_hash", 400)
    before = set(online_users())
    with NODE_REGISTRY_LOCK:
        NODE_REGISTRY[node_hash] = {
            "last_seen": now(),
            "user_hash": viewer,
        }
    # A first register (or a re-register after a prune) changes the online
    # set — streamers get a guild-wide presence event so member lists live.
    _emit_presence_if_changed(before)
    return msgpack.packb({"ok": 1})

@api_route("/api/lookup_node")
@wrap_api
def api_lookup_node(viewer, data):
    """Look up a node by hash. The hash is the address (RNS is
    cryptographically addressed); the registry row exists to join the node
    destination to the user who runs it and to state that it is registered.
    data: {node_hash}
    Returns: {ok, node_hash, user_hash, last_seen} or
             {ok: 0, error: "node not found"}
    """
    node_hash = (data.get("node_hash") or "").strip().lower()
    if not node_hash:
        return api_error("node_hash required", 400)
    with NODE_REGISTRY_LOCK:
        entry = NODE_REGISTRY.get(node_hash)
    if not entry:
        return msgpack.packb({"ok": 0, "error": "node not found"})
    return msgpack.packb({"ok": 1, "node_hash": node_hash,
                          "user_hash": entry.get("user_hash"),
                          "last_seen": entry.get("last_seen")})

@api_route("/api/nodes")
@wrap_api
def api_nodes(viewer, data):
    """List all registered nodes (for directory browsing)."""
    with NODE_REGISTRY_LOCK:
        nodes = {k: v for k, v in NODE_REGISTRY.items()}
    return msgpack.packb({"ok": 1, "nodes": nodes})

@api_route("/api/presence")
@wrap_api
def api_presence(viewer, data):
    """The active user list (v0.29: link-derived presence).

    Being able to make a request has always been strictly necessary but never
    sufficient: presence follows the link, not the traffic. A user is online
    iff their link to the guild is in RNS.Transport.active_links; the link
    identity binds it to a user. A client that vanishes drops off when its
    link dies (RNS teardown), within at most the keeper's 1s sweep, and fires
    a presence event for every streamer when it does.

    Returns {ok, mode: "link", users: [{user_hash, username, node_hash,
    last_seen}], now}.
    """
    users = online_users()
    return msgpack.packb({
        "ok": 1,
        "mode": "link",
        "users": sorted(users.values(), key=lambda u: u["username"].lower()),
        "now": now(),
    })

@api_route("/api/avatar")
@wrap_api
def api_avatar(viewer, data):
    blob = data.get("avatar")
    if not isinstance(blob, (bytes, bytearray)):
        return api_error("avatar bytes required", 400)
    if len(blob) > MAX_AVATAR_SZ:
        return api_error("avatar too large", 413)
    adir = os.path.join(STORAGE, "avatars")
    os.makedirs(adir, exist_ok=True)
    fname = viewer + ".img"
    with open(os.path.join(adir, fname), "wb") as f:
        f.write(blob)
    with db_lock:
        db.execute("UPDATE users SET avatar_path=? WHERE dest_hash=?", (fname, viewer))
        db.commit()
    return msgpack.packb({"ok": 1})

@api_route("/api/avatar_get")
@wrap_api
def api_avatar_get(viewer, data):
    uh = data.get("user") or viewer
    with db_lock:
        row = db.execute("SELECT avatar_path FROM users WHERE dest_hash=?", (uh,)).fetchone()
    if not row or not row["avatar_path"]:
        return msgpack.packb({"ok": 1, "avatar": None})
    fpath = os.path.join(STORAGE, "avatars", row["avatar_path"])
    if not os.path.isfile(fpath):
        return msgpack.packb({"ok": 1, "avatar": None})
    with open(fpath, "rb") as f:
        return msgpack.packb({"ok": 1, "avatar": f.read()})

# ----------------------------------------------------------------------------------
# JSON API v1 (msgpack /api/* stays for the legacy client; v1 speaks JSON).
# Same transport as every other endpoint: RNS Link requests on nomadnetwork.node,
# auth = identified link identity. RNS wraps request/response payloads in its own
# internal msgpack envelope, but the payload itself passes through opaquely — so
# the wire format the API sees is plain JSON (utf-8), never HTML/HTTP.
#
# Stream model: one subscription per link (subscribing replaces), cursor = last
# event seq seen, long-poll wait capped at STREAM_WAIT_MAX. Event kinds:
#   msg / edit / del   — channel events (from ingest/_mutate_msg)
#   typing             — channel event (transitions only, TTL TYPING_TTL_S)
#   presence           — guild-wide event, fanned into every channel's ring
#   site_patch         — reserved for site-channel resource updates
# ----------------------------------------------------------------------------------

def _json_resp(d):
    return json.dumps(d).encode("utf-8")

def _json_args(data):
    """Accept JSON bytes/str payloads; tolerate a plain dict (tests, clients
    that let RNS msgpack-encode a dict). Anything else is a bad request."""
    if isinstance(data, (bytes, bytearray)):
        try:
            args = json.loads(data.decode("utf-8"))
        except Exception:
            return None
    elif isinstance(data, str):
        try:
            args = json.loads(data)
        except Exception:
            return None
    elif isinstance(data, dict):
        args = data
    else:
        return None
    return args if isinstance(args, dict) else None

def wrap_json_api(fn):
    """v1 handlers are fn(viewer, args, link_id) -> dict; the wrapper turns the
    dict into a JSON response and maps every failure to {ok:0, error, code}."""
    def handler(path, data, request_id, link_id, remote_identity, requested_at):
        viewer = viewer_of(remote_identity)
        if viewer is None:
            return _json_resp({"ok": 0, "error": "identified link required", "code": 401})
        # v0.29: every identified request binds presence -- this link belongs
        # to this user until the link dies (the presence keeper prunes it).
        if link_id:
            with ONLINE_LINKS_LOCK:
                ONLINE_LINKS[link_id] = viewer
        args = _json_args(data)
        if args is None:
            return _json_resp({"ok": 0, "error": "invalid JSON payload", "code": 400})
        try:
            resp = fn(viewer, args, link_id)
        except Exception as e:
            RNS.log(f"api v1 error {path}: {e}", RNS.LOG_ERROR)
            resp = {"ok": 0, "error": "internal error", "code": 500}
        return _json_resp(resp)
    return handler

# ---- stream internals -------------------------------------------------------------

def _emit_event(channel_id, kind, data, guild_wide=False):
    """Append an event to the rings that should carry it. A single global
    EVENT_SEQ keeps cursors ordered; each channel's deque is a window over
    that channel's events (plus, for guild-wide events only, a copy for every
    channel that currently has a subscriber — presence must reach everyone,
    while a channel's own events never leak into another channel's stream)."""
    global EVENT_SEQ
    with SUB_LOCK:
        subscribed = {s["channel"] for s in SUBSCRIPTIONS.values()}
    if guild_wide:
        targets = subscribed
    else:
        targets = {channel_id} if channel_id else set()
    if not targets:
        RNS.log("emit %s dropped: no targets (guild_wide=%s, ch=%s)"
                % (kind, guild_wide, channel_id), RNS.LOG_NOTICE)
        return  # nobody is streaming; presence history is served on demand
    with EVENT_COND:
        EVENT_SEQ += 1
        ev = {"seq": EVENT_SEQ, "kind": kind, "ts": now(), "data": data}
        for cid in targets:
            ring = EVENT_LOG.setdefault(cid, deque(maxlen=EVENT_LOG_MAX))
            ring.append((EVENT_SEQ, ev))
        EVENT_COND.notify_all()
    RNS.log("emit %s seq %d targets %d" % (kind, ev["seq"], len(targets)),
            RNS.LOG_DEBUG)
    # v0.28 push: hand each event to its subscribers directly. Runs after
    # the cond block releases -- the sender never holds the event lock.
    for cid in targets:
        try:
            _push_stream(cid)
        except Exception as e:
            RNS.log("stream push failed: %s" % e, RNS.LOG_ERROR)

def _push_stream(cid):
    """v0.28 push: deliver the subscribed channel's new events down the
    link the moment they exist, instead of holding them for the client's
    long-poll. RNS links are full duplex; RNS.Packet(link, bytes) sends a
    raw frame over an established link and the client's packet callback
    receives it. The poll endpoint stays for one-shot gap recovery.
    """
    with SUB_LOCK:
        subs = [(lid, s["cursor"], s.get("user_hash")) for lid, s in SUBSCRIPTIONS.items()
                if s["channel"] == cid]
    for lid, cursor, user_hash in subs:
        if not has_perm(user_hash, cid, PERMS["VIEW"]):
            # a revoked or never-granted VIEW must not keep a live push
            # flowing down the link (tay fork tier-1)
            _drop_subscription(lid)
            continue
        events, head, reset = _stream_collect(cid, cursor)
        # v0.31 ticket protocol: the push frame carries REFERENCES, never
        # bodies. The frame rides a raw RNS.Packet with the 500-byte link
        # MTU; a single serialized message row (say 400 chars of content,
        # or any message plus attachment metadata) crossed that line and
        # the push died with "packet size of X exceeds mtu of 500" -- the
        # receipt is in the operator console, twice. The event log and
        # the long-poll endpoint are untouched: they serve on the
        # request/resource path, which chunks. The ticket says "seq is
        # now N on this channel"; the client delta-fetches immediately.
        # Bounded by construction: k + cid + seq + reset + a typing
        # snapshot capped at eight identities always packs under MTU.
        frame = {"k": "stream", "channel": cid, "seq": head,
                 "reset": reset, "ticket": True,
                 "typing": _typing_snapshot(cid)[:8]}
        out_cursor = cursor
        packed = msgpack.packb(frame, raw=False)
        sent = False
        for link in list(RNS.Transport.active_links):
            if link.link_id == lid and link.status == RNS.Link.ACTIVE:
                try:
                    RNS.Packet(link, packed).send()
                    sent = True
                except Exception as e:
                    RNS.log("stream push to link failed: %s" % e,
                            RNS.LOG_ERROR)
                break
        with SUB_LOCK:
            s = SUBSCRIPTIONS.get(lid)
            if s and s["channel"] == cid:
                if sent:
                    s["cursor"] = out_cursor   # mirror the poll's advance
                s["at"] = now()                # push keeps the sub alive

def _emit_presence_if_changed(before):
    """Emit a guild-wide presence event when the online set changed."""
    if set(online_users()) != before:
        _emit_event(None, "presence", {"users": _presence_payload()}, guild_wide=True)

def _purge_subs():
    """Drop subscriptions that have not been polled within SUB_TTL_S — the
    server-side stand-in for link teardown, which request handlers can't see."""
    cutoff = now() - SUB_TTL_S
    with SUB_LOCK:
        dead = [lid for lid, s in SUBSCRIPTIONS.items() if s["at"] < cutoff]
    for lid in dead:
        _drop_subscription(lid)

def _drop_subscription(link_id):
    """Remove one subscription by link id, including its per-user index.
    Lock owner: callers must not hold SUB_LOCK. (tay fork tier-1)"""
    with SUB_LOCK:
        sub = SUBSCRIPTIONS.pop(link_id, None)
        if sub:
            cur = SUB_BY_USER.get(sub["user_hash"])
            if cur and cur.get("link_id") == link_id:
                del SUB_BY_USER[sub["user_hash"]]
    return sub

def _stream_collect(cid, cursor):
    """(events after cursor, head seq, gap?) for one channel's ring.
    A gap means the ring rotated past the client's cursor: it must resync."""
    with EVENT_COND:
        head = EVENT_SEQ
        ring = EVENT_LOG.get(cid)
        if not ring:
            return [], head, cursor < head
        oldest = ring[0][0]
        if cursor < oldest - 1:
            return [], head, True
        return [ev for seq, ev in ring if seq > cursor], head, False

def _typing_snapshot(cid):
    cutoff = now()
    with TYPING_LOCK:
        return [{"user_hash": uh, "username": get_username(uh)}
                for uh, exp in TYPING.get(cid, {}).items() if exp >= cutoff]

def _active_channels_by_user():
    with SUB_LOCK:
        return {uh: s["channel"] for uh, s in SUB_BY_USER.items()
                if now() - s["at"] <= SUB_TTL_S}

def _presence_payload():
    """online_users() enriched with the channel each user is streaming."""
    active = _active_channels_by_user()
    return [dict(u, active_channel=active.get(u["user_hash"]))
            for u in sorted(online_users().values(),
                            key=lambda x: x["username"].lower())]

# ---- channels ---------------------------------------------------------------------

@api_route("/api/v1/channels/list")
@wrap_json_api
def api_v1_channels_list(viewer, data, link_id):
    with db_lock:
        rows = db.execute("SELECT id, name, kind, created, topic, position FROM channels "
                          "ORDER BY position ASC, name ASC").fetchall()
        chans = [{"id": r["id"], "name": r["name"], "kind": r["kind"],
                  "created": r["created"], "topic": r["topic"],
                  "position": r["position"], "seq": get_seq(r["id"])} for r in rows]
    # VIEW permission: channels the viewer cannot read are absent entirely
    chans = [c for c in chans if has_perm(viewer, c["id"], PERMS["VIEW"])]
    for c in chans:
        if c["kind"] == "announce":
            # P8: the composer's truth rides the read the client already
            # fetches (the site-edit verdict shape)
            c["can_post"] = bool(_announce_post_ok(viewer, c["id"]))
        else:
            c["can_post"] = bool(has_perm(viewer, c["id"], PERMS["POST"]))
        last_id = _receipt_last_id(c["id"], viewer)
        c["unread"] = _unread_count(c["id"], last_id)
    return {"ok": 1, "channels": chans}

@api_route("/api/v1/channels/reorder")
@wrap_json_api
def api_v1_channels_reorder(viewer, data, link_id):
    """Persist the channel list order (drag result). {order: [cid, ...]} must
    list EVERY channel exactly once; the position column is rewritten in one
    transaction. Needs MANAGE_CHANNEL (guild-wide) — ADMIN carries it."""
    if not has_role_perm(viewer, PERMS["MANAGE_CHANNEL"]):
        return {"ok": 0, "error": "manage channel permission required", "code": 403}
    order = data.get("order")
    if not isinstance(order, list) or not order \
            or not all(isinstance(c, str) for c in order) \
            or len(set(order)) != len(order):
        return {"ok": 0, "error": "order must be a unique cid list", "code": 400}
    with db_lock:
        known = {r["id"] for r in db.execute("SELECT id FROM channels").fetchall()}
        if set(order) != known:
            return {"ok": 0, "error": "order must cover every channel exactly once",
                    "code": 400}
        for idx, cid in enumerate(order, start=1):
            db.execute("UPDATE channels SET position=? WHERE id=?", (idx, cid))
        db.commit()
    # every subscriber sees the new order through their own channel's stream
    _audit(viewer, "channel.reorder", None, "channels=%d" % len(order))
    _emit_event(None, "channels_changed", {"order": order}, guild_wide=True)
    return {"ok": 1, "order": order}

def _create_channel_txn(viewer, name, kind, manifest=None):
    """Shared channel creation (API + slash command). Returns
    (cid, None) or (None, error)."""
    name = (name or "").strip()
    kind = (kind or "chat").strip().lower()
    if not re.match(r"^[a-zA-Z0-9_\-]{1,32}$", name):
        return None, "invalid channel name"
    if kind not in CHANNEL_KINDS:
        return None, "kind must be one of: " + ", ".join(CHANNEL_KINDS)
    if kind in ("site", "term") and not is_moderator(viewer):
        return None, "moderator required to create site/term channels"
    if manifest is not None and not isinstance(manifest, dict):
        return None, "manifest must be an object"
    manifest_json = json.dumps(manifest)[:4096] if manifest else "{}"
    cid = hexstr(RNS.Identity.full_hash(f"{GUILD_ID}:{name}".encode()))
    with db_lock:
        if db.execute("SELECT 1 FROM channels WHERE id=? OR (name=? COLLATE NOCASE)",
                      (cid, name)).fetchone():
            return None, "channel exists"
        # explicit ordering: new channels sink to the end of the drag list
        nxt = (db.execute("SELECT COALESCE(MAX(position), 0) + 1 AS nxt "
                          "FROM channels").fetchone()["nxt"])
        db.execute("INSERT INTO channels (id, name, created, kind, manifest, position) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (cid, name, now(), kind, manifest_json, nxt))
        db.execute("INSERT OR IGNORE INTO channel_state (channel_id, seq) VALUES (?, 0)", (cid,))
        db.commit()
    if kind == "site":
        # outside db_lock: the seed is a file write + handler registration
        _seed_default_site(cid)
    _audit(viewer, "channel.create", cid, "%s (%s)" % (name, kind))
    return cid, None

@api_route("/api/v1/channels/create")
@wrap_json_api
def api_v1_channels_create(viewer, data, link_id):
    cid, err = _create_channel_txn(viewer, data.get("name"), data.get("kind") or "chat",
                                   data.get("manifest"))
    if err:
        return {"ok": 0, "error": err,
                "code": 403 if "moderator" in err else (409 if "exists" in err else 400)}
    kind = (data.get("kind") or "chat").strip().lower()
    manifest = data.get("manifest")
    return {"ok": 1, "channel": {"id": cid, "name": (data.get("name") or "").strip(),
                                 "kind": kind if kind in CHANNEL_KINDS else "chat",
                                 "manifest": manifest or {}, "seq": 0}}

@api_route("/api/v1/channels/info")
@wrap_json_api
def api_v1_channels_info(viewer, data, link_id):
    cid = data.get("channel")
    with db_lock:
        r = db.execute("SELECT id, name, kind, manifest, created, topic FROM channels "
                       "WHERE id=?", (cid,)).fetchone()
        if not r:
            return {"ok": 0, "error": "unknown channel", "code": 404}
        seq = get_seq(cid)
        try:
            manifest = json.loads(r["manifest"] or "{}")
        except Exception:
            manifest = {}
    return {"ok": 1, "channel": {"id": r["id"], "name": r["name"], "kind": r["kind"],
                                 "manifest": manifest, "created": r["created"],
                                 "topic": r["topic"], "seq": seq}}

# ---- stream -----------------------------------------------------------------------

@api_route("/api/v1/stream/subscribe")
@wrap_json_api
def api_v1_stream_subscribe(viewer, data, link_id):
    cid = data.get("channel")
    with db_lock:
        chan = db.execute("SELECT id, kind FROM channels WHERE id=?", (cid,)).fetchone()
    if not chan:
        return {"ok": 0, "error": "unknown channel", "code": 404}
    if not has_perm(viewer, cid, PERMS["VIEW"]):
        return {"ok": 0, "error": "view permission required", "code": 403}
    _purge_subs()
    with EVENT_COND:
        head = EVENT_SEQ
    cursor = data.get("cursor")
    cursor = int(cursor) if isinstance(cursor, (int, float)) and cursor >= 0 else head
    # One subscription per link: this replaces whatever the link streamed before,
    # which is what makes "only the active channel flows" a server-side rule.
    with SUB_LOCK:
        old = SUBSCRIPTIONS.get(link_id)
        if old and old["user_hash"] == viewer:
            cur = SUB_BY_USER.get(viewer)
            if cur and cur.get("link_id") == link_id:
                del SUB_BY_USER[viewer]
        SUBSCRIPTIONS[link_id] = {"user_hash": viewer, "channel": cid,
                                  "cursor": cursor, "at": now()}
        SUB_BY_USER[viewer] = {"channel": cid, "link_id": link_id, "at": now()}
    return {"ok": 1, "channel": cid, "kind": chan["kind"],
            "seq": head, "cursor": cursor, "typing": _typing_snapshot(cid)}

@api_route("/api/v1/stream/poll")
@wrap_json_api
def api_v1_stream_poll(viewer, data, link_id):
    wait_s = data.get("timeout_s", STREAM_WAIT_S)
    try:
        wait_s = min(max(float(wait_s), 0.0), STREAM_WAIT_MAX)
    except (TypeError, ValueError):
        wait_s = STREAM_WAIT_S
    deadline = now() + wait_s
    while True:
        with SUB_LOCK:
            sub = SUBSCRIPTIONS.get(link_id)
        if not sub:
            return {"ok": 0, "error": "not subscribed", "code": 409}
        cid = sub["channel"]
        if sub["user_hash"] != viewer or not has_perm(viewer, cid, PERMS["VIEW"]):
            # identity swap on this link, or a permission revoke: the sub
            # must not outlive the VIEW grant (tay fork tier-1)
            _drop_subscription(link_id)
            return {"ok": 0, "error": "view permission required", "code": 403}
        cursor = sub["cursor"]
        pc = data.get("cursor")
        if isinstance(pc, (int, float)) and pc >= 0:
            cursor = int(pc)          # client override (reconnect/resync)
        events, head, reset = _stream_collect(cid, cursor)
        if events or reset or now() >= deadline:
            out_cursor = events[-1]["seq"] if events else (head if reset else cursor)
            with SUB_LOCK:
                s = SUBSCRIPTIONS.get(link_id)
                # don't clobber a newer subscription that raced us on this link
                if s and s["channel"] == cid:
                    s["cursor"] = out_cursor
                    s["at"] = now()
            if reset:
                return {"ok": 1, "channel": cid, "events": [], "cursor": head,
                        "seq": head, "reset": True}
            return {"ok": 1, "channel": cid, "events": events, "cursor": out_cursor,
                    "reset": False, "typing": _typing_snapshot(cid)}
        # Long-poll: sleep on the event condition in short slices so a
        # resubscribe from this link is honoured promptly.
        with EVENT_COND:
            EVENT_COND.wait(min(0.5, max(0.0, deadline - now())))

@api_route("/api/v1/stream/unsubscribe")
@wrap_json_api
def api_v1_stream_unsubscribe(viewer, data, link_id):
    with SUB_LOCK:
        sub = SUBSCRIPTIONS.pop(link_id, None)
        if sub:
            cur = SUB_BY_USER.get(sub["user_hash"])
            if cur and cur.get("link_id") == link_id:
                del SUB_BY_USER[sub["user_hash"]]
    return {"ok": 1}

# ---- typing -----------------------------------------------------------------------

@api_route("/api/v1/typing")
@wrap_json_api
def api_v1_typing(viewer, data, link_id):
    cid = data.get("channel")
    active = bool(data.get("active"))
    with db_lock:
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return {"ok": 0, "error": "unknown channel", "code": 404}
    with TYPING_LOCK:
        bucket = TYPING.setdefault(cid, {})
        was_active = bucket.get(viewer, 0) >= now()
        if active:
            bucket[viewer] = now() + TYPING_TTL_S
        elif viewer in bucket:
            del bucket[viewer]
    if active != was_active:
        # transitions only: a held-down typing ping refreshes the TTL silently
        _emit_event(cid, "typing", {"user_hash": viewer,
                                    "username": get_username(viewer),
                                    "active": active})
    return {"ok": 1}

# ---- presence ---------------------------------------------------------------------

@api_route("/api/v1/presence")
@wrap_json_api
def api_v1_presence(viewer, data, link_id):
    """Link-derived active users (v0.29) plus the channel each user is
    currently streaming."""
    return {"ok": 1, "mode": "link", "users": _presence_payload(), "now": now()}

# ---- blobs (phase 3): content-addressed image/media get+put ------------------------

def _blob_dir():
    return os.path.join(STORAGE, "blobs")

def _b64_decode(data):
    """Strict base64 (whitespace tolerated around the payload). None on error."""
    if not isinstance(data, str):
        return None
    try:
        blob = base64.b64decode(data.strip(), validate=True)
    except Exception:
        return None
    return blob or None

def _blob_fname(digest):
    """Find a stored blob by its 32-hex digest (ext is content-derived)."""
    if not re.match(r"^[0-9a-f]{32}$", digest or ""):
        return None
    bdir = _blob_dir()
    try:
        for fname in os.listdir(bdir):
            if fname.startswith(digest + "."):
                return fname
    except OSError:
        pass
    # P2 (ephemeral "attachments" mode): the bytes live in the RAM
    # registry, so the disk scan misses -- but the blobs metadata row
    # persists in both modes, and its path is exactly the fname the
    # serve paths (RAM-first, then disk) use.
    with db_lock:
        row = db.execute("SELECT path FROM blobs WHERE id=?",
                         (digest,)).fetchone()
    if row and row["path"]:
        return row["path"]
    return None

# ---- ephemeral attachment mode (P2): RAM registry, disk untouched -------
# tay (plan_admin v1.1): the "attachments" config governs where blob
# bytes land. "saved" = today's law (content-addressed files on disk,
# DB carries only metadata). "ephemeral" = bytes live in this RAM
# registry keyed by digest {bytes, ts, name, mime, by}, TTL-swept by the
# 1 s reaper, hard-capped with LRU eviction; serve paths resolve
# RAM-first, then disk. Flipping back to "saved" restores disk commits
# for NEW uploads; existing stored blobs keep serving untouched.

ATT_RAM_TTL = 6 * 3600          # seconds an ephemeral blob lives
ATT_RAM_MAX_BYTES = 32 * 1024 * 1024
ATT_RAM = {}                    # digest -> {bytes, ts, name, mime, by}
ATT_RAM_LOCK = threading.Lock()


def _attachment_mode():
    """Live config read: every upload/commit checks the CURRENT value,
    so a flip binds on the next request with no restart."""
    try:
        return ("ephemeral" if _cfg_get("attachments", "saved")
                == "ephemeral" else "saved")
    except Exception:
        return "saved"


def _att_ram_sweep():
    """Drop TTL-expired RAM blobs; runs in the 1 s reaper loop."""
    try:
        deadline = now() - ATT_RAM_TTL
    except Exception:
        return 0
    with ATT_RAM_LOCK:
        stale = [k for k, v in ATT_RAM.items() if v["ts"] < deadline]
        for k in stale:
            del ATT_RAM[k]
    return len(stale)


def _att_ram_put(digest, blob, name, mime, by):
    """Insert an ephemeral blob, LRU-evicting to stay under the cap."""
    with ATT_RAM_LOCK:
        deadline = now() - ATT_RAM_TTL
        for k in [k for k, v in ATT_RAM.items() if v["ts"] < deadline]:
            del ATT_RAM[k]
        if digest not in ATT_RAM:
            while (sum(len(v["bytes"]) for v in ATT_RAM.values())
                   + len(blob) > ATT_RAM_MAX_BYTES and ATT_RAM):
                oldest = min(ATT_RAM, key=lambda k: ATT_RAM[k]["ts"])
                del ATT_RAM[oldest]
        ATT_RAM[digest] = {"bytes": bytearray(blob), "ts": now(),
                           "name": name, "mime": mime, "by": by}


def _att_ram_get(digest):
    """Return the entry (refreshing its LRU clock) or None."""
    with ATT_RAM_LOCK:
        v = ATT_RAM.get(digest)
        if v is not None:
            v["ts"] = now()
        return v


def _read_slice(fpath, offset, length):
    """Read one chunk of a file. Returns (total, chunk) or (None, None)."""
    try:
        total = os.path.getsize(fpath)
        with open(fpath, "rb") as f:
            f.seek(offset)
            chunk = f.read(length)
        return total, chunk
    except OSError:
        return None, None

def _chunk_params(data):
    """Common offset/length parsing for blob and site gets."""
    try:
        offset = int(data.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        length = int(data.get("len", BLOB_CHUNK_DEFAULT))
    except (TypeError, ValueError):
        length = BLOB_CHUNK_DEFAULT
    offset = max(0, offset)
    length = min(max(1, length), BLOB_CHUNK_MAX)
    return offset, length

@api_route("/api/v1/blobs/put")
@wrap_json_api
def api_v1_blobs_put(viewer, data, link_id):
    """Upload one content-addressed blob. Returns {blob_id, size, path} —
    reference it from chat posts (attachments: [{blob_id}]) or site pages.
    Rate-limited like posting: 10 uploads / 60 s per user."""
    if not rate_ok(viewer, "_blobs"):
        return {"ok": 0, "error": "rate limited", "code": 429}
    blob = _b64_decode(data.get("data_b64"))
    if blob is None:
        return {"ok": 0, "error": "data_b64 must be valid base64", "code": 400}
    if len(blob) > MAX_ATTACHMENT_SZ:
        return {"ok": 0, "error": "blob too large", "code": 413}
    name = (data.get("name") or "").strip()[:128]
    mime = (data.get("mime") or "").strip()[:64]
    res = _blob_commit(blob, name, mime, by=viewer)
    if res is None:
        return {"ok": 0, "error": "storage failed", "code": 500}
    return res

@api_route("/api/v1/blobs/get")
@wrap_json_api
def api_v1_blobs_get(viewer, data, link_id):
    """Chunked blob read: {blob_id, offset?, len?} ->
    {data_b64, total, offset, eof}. len is capped at BLOB_CHUNK_MAX."""
    digest = (data.get("blob_id") or "").strip().lower()
    fname = _blob_fname(digest)
    if not fname:
        return {"ok": 0, "error": "unknown blob_id", "code": 404}
    offset, length = _chunk_params(data)
    entry = _att_ram_get(digest)
    if entry is not None:
        total = len(entry["bytes"])
        chunk = bytes(entry["bytes"][offset:offset + length])
    else:
        total, chunk = _read_slice(os.path.join(_blob_dir(), fname),
                                   offset, length)
        if total is None:
            return {"ok": 0, "error": "blob unreadable", "code": 500}
    return {"ok": 1, "blob_id": digest, "total": total, "offset": offset,
            "eof": offset + len(chunk) >= total,
            "data_b64": base64.b64encode(chunk).decode("ascii")}

@api_route("/api/v1/att/get")
@wrap_json_api
def api_v1_att_get(viewer, data, link_id):
    """Chunked attachment read: {path, offset?, len?} -> {data_b64,
    total, offset, eof}. The chunked-reader shape of /api/v1/blobs/get,
    keyed by the attachment's /file/attachments/<fname> path.

    Receipt: single-shot attachment fetches hold the entire payload on
    one link receipt with a 15s deadline; a real multi-MB attachment
    from a remote hub overruns the deadline and the embed spins with no
    error surfaced. Stored attachments carry no blob_id in their
    metadata (ingest copies the blob into the attachments dir), so a
    path-keyed slice endpoint is what the receive path can use."""
    fname = ((data.get("path") or "").rsplit("/", 1)[-1]).strip()
    if not re.match(r"^[0-9a-f]{32}\.[a-z0-9]{1,8}$", fname):
        return {"ok": 0, "error": "unknown path", "code": 404}
    if not attachment_visible(viewer, fname):
        return {"ok": 0, "error": "attachment unavailable", "code": 404}
    offset, length = _chunk_params(data)
    entry = _att_ram_get(fname.split(".")[0])
    if entry is not None:
        total = len(entry["bytes"])
        chunk = bytes(entry["bytes"][offset:offset + length])
    else:
        total, chunk = _read_slice(
            os.path.join(STORAGE, "attachments", fname), offset, length)
        if total is None:
            return {"ok": 0, "error": "attachment unreadable", "code": 404}
    return {"ok": 1, "path": "/file/attachments/" + fname, "total": total,
            "offset": offset, "eof": offset + len(chunk) >= total,
            "data_b64": base64.b64encode(chunk).decode("ascii")}

def serve_blob_file(path, data, request_id, link_id, remote_identity, requested_at):
    """Raw /file/blobs/<fname> handler (one per stored blob, attachments
    pattern). Path is the registration key; validate before touching disk."""
    fname = path.rsplit("/", 1)[-1]
    if not re.match(r"^[0-9a-f]{32}\.[a-z0-9]{1,8}$", fname):
        return b"not found"
    entry = _att_ram_get(fname.split(".")[0])
    if entry is not None:
        return bytes(entry["bytes"])
    fpath = os.path.join(_blob_dir(), fname)
    if os.path.isfile(fpath):
        with open(fpath, "rb") as f:
            return f.read()
    return b"not found"


def _blob_commit(blob, name, mime, by=None):
    """Content-address the blob, store it, register its /file/blobs handler
    and return the standard response dict. Shared by the legacy single-shot
    put and the chunked-upload finish; None on storage failure.

    Storage law (tay): files live native on disk as content-addressed
    <sha256>.<ext>; the database carries only name/mime/size/path metadata
    (blobs table), never bytes. Ephemeral "attachments" mode (P2) keeps
    the same law but holds the bytes in ATT_RAM under a TTL instead of
    the disk; serving resolves RAM-first, metadata persists normally."""
    digest = hashlib.sha256(blob).hexdigest()[:32]
    ext = safe_ext(name, mime)
    fname = f"{digest}.{ext}"
    if _attachment_mode() == "ephemeral":
        # P2: bytes stay in RAM under the same law (DB carries metadata
        # only, never bytes); no disk write. Serve paths resolve
        # RAM-first for the digest, while the /file/blobs/<fname>
        # handler below stays registered so remote fetches route and
        # land in the RAM branch -- until the TTL (or the admin flips
        # back to "saved").
        _att_ram_put(digest, blob, name, mime, by)
    else:
        bdir = _blob_dir()
        os.makedirs(bdir, exist_ok=True)
        fpath = os.path.join(bdir, fname)
        if not os.path.exists(fpath):
            try:
                with open(fpath, "wb") as f:
                    f.write(blob)
            except OSError as e:
                RNS.log(f"blob store failed for {fname}: {e}",
                        RNS.LOG_ERROR)
                return None
    if node_destination is not None:
        node_destination.register_request_handler(
            "/file/blobs/" + fname, serve_blob_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
    with db_lock:
        db.execute(
            "INSERT OR IGNORE INTO blobs (id, name, mime, size, path, ts, by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (digest, name or fname,
             EXT_MIME.get(ext, mime or "application/octet-stream"),
             len(blob), fname, now(), by))
        db.commit()
    return {"ok": 1, "blob_id": digest, "size": len(blob),
            "mime": EXT_MIME.get(ext, mime or "application/octet-stream"),
            "name": name or fname, "path": "/file/blobs/" + fname}


# ---- chunked blob uploads (v0.29-c): max payload + automatic chunking ------
# tay: "The payload should be maximum of 10mb & automatically chunked,
# not timer based." The session is byte-offset addressed and every ack
# carries next_offset, so the sender is driven by data — never by a timer.

# upload_id -> {"path","name","mime","size","offset","user","ts"}
BLOB_UPLOADS = {}
BLOB_UPLOADS_LOCK = threading.Lock()


def _blob_staging_dir():
    return os.path.join(_blob_dir(), "staging")


def _reap_stale_uploads():
    """Drop staged uploads with no chunk activity for BLOB_UPLOAD_IDLE_S
    (hygiene for abandoned transfers; not part of the transfer mechanism)."""
    cutoff = now() - BLOB_UPLOAD_IDLE_S
    dead = []
    with BLOB_UPLOADS_LOCK:
        for uid, up in BLOB_UPLOADS.items():
            if up["ts"] < cutoff:
                dead.append((uid, up["path"]))
    for uid, path in dead:
        with BLOB_UPLOADS_LOCK:
            BLOB_UPLOADS.pop(uid, None)
        try:
            os.remove(path)
        except OSError:
            pass
    return [u for u, _ in dead]


@api_route("/api/v1/blobs/put/init")
@wrap_json_api
def api_v1_blobs_put_init(viewer, data, link_id):
    """Open a chunked upload session: {name, mime?, size} ->
    {ok, upload_id, chunk_size, size}. Rejects payloads above MAX_BLOB_SZ
    (10 MB) up front — named failure, no bytes moved."""
    if not rate_ok(viewer, "_blobs"):
        return {"ok": 0, "error": "rate limited", "code": 429}
    try:
        size = int(data.get("size", 0))
    except (TypeError, ValueError):
        size = -1
    if size <= 0:
        return {"ok": 0, "error": "size must be a positive int", "code": 400}
    if size > MAX_BLOB_SZ:
        return {"ok": 0, "error": "payload too large: limit is 10 MB",
                "code": 413}
    name = (data.get("name") or "").strip()[:128]
    mime = (data.get("mime") or "").strip()[:64]
    upload_id = os.urandom(16).hex()
    if _attachment_mode() == "ephemeral":
        # P2: staging stays in RAM too -- init/chunk touch no disk;
        # finish commits the assembled bytes per the mode AT THAT MOMENT
        staging = None
    else:
        sdir = _blob_staging_dir()
        os.makedirs(sdir, exist_ok=True)
        part = os.path.join(sdir, upload_id + ".part")
        with open(part, "wb"):
            pass
        staging = part
    with BLOB_UPLOADS_LOCK:
        BLOB_UPLOADS[upload_id] = {
            "path": staging, "mem": None if staging else bytearray(),
            "name": name, "mime": mime, "size": size,
            "offset": 0, "user": viewer, "ts": now(),
        }
    return {"ok": 1, "upload_id": upload_id, "chunk_size": BLOB_UPLOAD_CHUNK,
            "size": size}


@api_route("/api/v1/blobs/put/chunk")
@wrap_json_api
def api_v1_blobs_put_chunk(viewer, data, link_id):
    """Write one offset-addressed slice of a staged upload:
    {upload_id, offset, data_b64} -> {ok, received, next_offset, eof}.
    Rewriting the same offset is safe, so a retried request cannot corrupt
    the stream."""
    upload_id = (data.get("upload_id") or "").strip()
    with BLOB_UPLOADS_LOCK:
        up = BLOB_UPLOADS.get(upload_id)
    if not up:
        return {"ok": 0, "error": "unknown upload_id", "code": 404}
    if up["user"] != viewer:
        return {"ok": 0, "error": "not your upload", "code": 403}
    chunk = _b64_decode(data.get("data_b64"))
    if chunk is None:
        return {"ok": 0, "error": "data_b64 must be valid base64", "code": 400}
    try:
        offset = int(data.get("offset", -1))
    except (TypeError, ValueError):
        offset = -1
    if offset < 0 or offset > up["size"]:
        return {"ok": 0, "error": "offset out of range", "code": 400}
    if offset + len(chunk) > up["size"]:
        return {"ok": 0, "error": "chunk overruns declared size", "code": 400}
    if up.get("mem") is not None:
        with BLOB_UPLOADS_LOCK:
            cur = BLOB_UPLOADS.get(upload_id)
            if cur is None:
                return {"ok": 0, "error": "unknown upload_id", "code": 404}
            cur["mem"][offset:offset + len(chunk)] = chunk
        up = cur
    else:
        try:
            with open(up["path"], "r+b") as f:
                f.seek(offset)
                f.write(chunk)
        except OSError as e:
            RNS.log(f"blob chunk write failed ({up['name']}@{offset}): {e}",
                    RNS.LOG_ERROR)
            return {"ok": 0, "error": "storage failed", "code": 500}
    received = offset + len(chunk)
    with BLOB_UPLOADS_LOCK:
        if received > up["offset"]:
            up["offset"] = received
        up["ts"] = now()
    return {"ok": 1, "received": received, "next_offset": received,
            "eof": received >= up["size"], "size": up["size"]}


@api_route("/api/v1/blobs/put/finish")
@wrap_json_api
def api_v1_blobs_put_finish(viewer, data, link_id):
    """Commit a staged upload: {upload_id} -> same shape as blobs/put.
    Content-addresses the assembled bytes (sha256), so the returned
    blob_id is identical to a legacy single-shot put of the same payload."""
    upload_id = (data.get("upload_id") or "").strip()
    with BLOB_UPLOADS_LOCK:
        up = BLOB_UPLOADS.pop(upload_id, None)
    if not up:
        return {"ok": 0, "error": "unknown upload_id", "code": 404}
    if up["user"] != viewer:
        with BLOB_UPLOADS_LOCK:
            BLOB_UPLOADS[upload_id] = up
        return {"ok": 0, "error": "not your upload", "code": 403}
    if up["offset"] < up["size"]:
        return {"ok": 0, "error": "incomplete upload "
                f"({up['offset']}/{up['size']} bytes)", "code": 400,
                "received": up["offset"]}
    if up.get("mem") is not None:
        with BLOB_UPLOADS_LOCK:
            blob = bytes(up["mem"])
    else:
        try:
            with open(up["path"], "rb") as f:
                blob = f.read()
        except OSError as e:
            RNS.log(f"blob finish read failed ({up['name']}): {e}",
                    RNS.LOG_ERROR)
            return {"ok": 0, "error": "storage failed", "code": 500}
    if len(blob) != up["size"]:
        return {"ok": 0, "error": "staged bytes do not match declared size",
                "code": 400}
    res = _blob_commit(blob, up["name"], up["mime"], by=up["user"])
    try:
        if up.get("path"):
            os.remove(up["path"])
    except OSError:
        pass
    if res is None:
        return {"ok": 0, "error": "storage failed", "code": 500}
    return res


# ---- chat posts over v1 JSON (phase 3): inline or blob-referenced attachments ------

def _resolve_v1_attachments(atts):
    """v1 attachment entries: {name, mime, data_b64} (inline) or
    {name?, mime?, blob_id} (reference an earlier blobs/put upload).
    Returns (triples, error) — triples are the [name, mime, bytes] lists
    ingest() expects."""
    triples = []
    for att in (atts or [])[:MAX_ATTACHMENTS]:
        if not isinstance(att, dict):
            return None, "each attachment must be an object"
        name = (att.get("name") or "").strip()[:128]
        mime = (att.get("mime") or "").strip()[:64]
        if att.get("data_b64") is not None:
            blob = _b64_decode(att.get("data_b64"))
            if blob is None:
                return None, "attachment data_b64 must be valid base64"
        elif att.get("blob_id"):
            bid = (att.get("blob_id") or "").strip().lower()
            fname = _blob_fname(bid)
            if not fname:
                return None, "unknown blob_id: " + bid[:12]
            entry = _att_ram_get(bid)
            if entry is not None:
                blob = bytes(entry["bytes"])
                # the RAM registry carries the upload's original
                # metadata; prefer it over the content-derived fname
                name = name or (entry.get("name") or None)
                mime = mime or (entry.get("mime") or None)
            else:
                try:
                    with open(os.path.join(_blob_dir(), fname), "rb") as f:
                        blob = f.read()
                except OSError:
                    return None, "blob unreadable"
            ext = fname.rsplit(".", 1)[-1]
            mime = mime or EXT_MIME.get(ext, "application/octet-stream")
            name = name or fname
        else:
            return None, "attachment needs data_b64 or blob_id"
        if len(blob) > MAX_ATTACHMENT_SZ:
            return None, "attachment too large (max %d MB)" % (
                MAX_ATTACHMENT_SZ // (1024 * 1024),)
        triples.append([name or "attachment.bin", mime or "application/octet-stream", blob])
    return triples, None

@api_route("/api/v1/post")
@wrap_json_api
def api_v1_post(viewer, data, link_id):
    """JSON chat post: {channel, content, attachments?: [{data_b64|blob_id, ...}]}.
    Same ingest path as the msgpack /api/post (rate limit, moderation, ledger)."""
    cid = data.get("channel")
    content = data.get("content")
    with db_lock:
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return {"ok": 0, "error": "unknown channel", "code": 404}
    if not isinstance(content, str):
        return {"ok": 0, "error": "empty content", "code": 400}
    triples, err = _resolve_v1_attachments(data.get("attachments"))
    if err:
        return {"ok": 0, "error": err, "code": 400}
    if not content.strip() and not triples:
        return {"ok": 0, "error": "empty content", "code": 400}
    if not rate_ok(viewer, cid):
        return {"ok": 0, "error": "rate limited", "code": 429}
    msg_id = ingest(cid, viewer, content, attachments=triples)
    if msg_id is None:
        return {"ok": 0, "error": "rejected", "code": 400}
    with db_lock:
        atts = db.execute("SELECT name, mime, size, path FROM attachments "
                          "WHERE msg_id=? ORDER BY idx", (msg_id,)).fetchall()
    return {"ok": 1, "msg_id": msg_id,
            "attachments": [{"name": a["name"], "mime": a["mime"], "size": a["size"],
                             "path": "/file/attachments/" + a["path"]} for a in atts]}

# ---- site channels (phase 4): micron pages + media under a manifest ----------------

def _site_root():
    return os.path.join(STORAGE, "sites")

def _site_safe_rel(path):
    """Validate a site resource path. Returns the relative path (no leading
    slash) or None. Rejects traversal, backslashes and empty segments."""
    if not path or not SITE_PATH_RE.match(path):
        return None
    rel = path.lstrip("/")
    if not rel or ".." in rel.split("/") or any(not seg for seg in rel.split("/")):
        return None
    return rel

def _site_file_path(cid, rel):
    """Resolve a site resource only if it stays beneath this channel's root.
    commonpath, not realpath.startswith: a realpath prefix also matches
    sibling directories (sites/ab accepts sites/abc/x that startswith(root)),
    and differing drives raise ValueError on Windows. (tay fork tier-1)"""
    root = os.path.realpath(os.path.join(_site_root(), cid))
    path = os.path.realpath(os.path.join(root, rel))
    try:
        inside = os.path.commonpath((root, path)) == root
    except ValueError:
        inside = False
    if not inside:
        return None
    return path

# Starter page for fresh site channels (tay: creating a site used to
# leave the channel rendering "loading site..." forever, because no
# resource existed to fetch). Micron grammar per micron.py: heading
# lines start with '>', separators '-', directives '#!k=v', links
# [label`target]. Headings parse via micron.py:184.
DEFAULT_SITE_INDEX_MU = (
    "#!c=0\n"
    "> A fresh sHades site\n"
    "-\n"
    "This channel is a micron site. Pages live as site resources\n"
    "served from this guild; the default template at /index.mu\n"
    "stands in until you publish your own pages.\n"
    "-\n"
    "Quick micron form:\n"
    "`!bold!` `*italic*` `_underline_` `F8a8`code`f`\n"
    "[publish with site/put `/index.mu]\n"
)


def _seed_default_site(cid):
    """Seed a fresh site channel with the default template /index.mu,
    through the same storage path and handler registration as
    site/put. Best-effort: the channel row already committed, so a
    seed failure is logged, not fatal -- the client renders its
    empty-state page instead (that leg covers this branch)."""
    fpath = _site_file_path(cid, "index.mu")
    if fpath is None:
        return
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    try:
        with open(fpath, "wb") as f:
            f.write(DEFAULT_SITE_INDEX_MU.encode("utf-8"))
    except OSError as e:
        RNS.log(f"default site seed failed {cid}: {e}", RNS.LOG_ERROR)
        return
    if node_destination is not None:
        node_destination.register_request_handler(
            f"/file/sites/{cid}/index.mu", serve_site_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
    RNS.log(f"site channel seeded: {cid}/index.mu", RNS.LOG_INFO)

def _site_channel(cid):
    with db_lock:
        r = db.execute("SELECT id, kind FROM channels WHERE id=?", (cid,)).fetchone()
    return r if (r and r["kind"] == "site") else None

@api_route("/api/v1/site/put")
@wrap_json_api
def api_v1_site_put(viewer, data, link_id):
    """Store one site resource (micron page, image, audio, video).
    {channel, path, data_b64, mime?} -> registers /file/sites/<cid>/<path> and
    emits a site_patch event on the channel stream. P7b: moderator OR the
    caller carries the channel-scoped PERMS["SITE_EDIT"] (channel_overwrites).
    Wikipedia-style: the granted bits, not the moderator list, write the
    channel's wiki."""
    cid = data.get("channel")
    if not _site_channel(cid):
        return {"ok": 0, "error": "unknown or non-site channel", "code": 404}
    if not (is_moderator(viewer) or has_perm(viewer, cid, PERMS["SITE_EDIT"])):
        return {"ok": 0, "error": "site edit permission required", "code": 403}
    rel = _site_safe_rel(data.get("path"))
    if not rel:
        return {"ok": 0, "error": "invalid resource path", "code": 400}
    if not rate_ok(viewer, cid):
        return {"ok": 0, "error": "rate limited", "code": 429}
    blob = _b64_decode(data.get("data_b64"))
    if blob is None:
        return {"ok": 0, "error": "data_b64 must be valid base64", "code": 400}
    if len(blob) > MAX_ATTACHMENT_SZ:
        return {"ok": 0, "error": "resource too large", "code": 413}
    fpath = _site_file_path(cid, rel)
    if fpath is None:
        return {"ok": 0, "error": "invalid resource path", "code": 400}
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    try:
        with open(fpath, "wb") as f:
            f.write(blob)
    except OSError as e:
        RNS.log(f"site store failed {cid}/{rel}: {e}", RNS.LOG_ERROR)
        return {"ok": 0, "error": "storage failed", "code": 500}
    reg_path = f"/file/sites/{cid}/{rel}"
    if node_destination is not None:
        node_destination.register_request_handler(
            reg_path, serve_site_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    mime = (data.get("mime") or "").strip()[:64] or EXT_MIME.get(ext, "application/octet-stream")
    _emit_event(cid, "site_patch", {"path": "/" + rel, "mime": mime, "size": len(blob)})
    return {"ok": 1, "channel": cid, "path": "/" + rel, "size": len(blob),
            "mime": mime, "file_path": reg_path}

@api_route("/api/v1/site/get")
@wrap_json_api
def api_v1_site_get(viewer, data, link_id):
    """Chunked site resource read: {channel, path, offset?, len?}."""
    cid = data.get("channel")
    if not _site_channel(cid):
        return {"ok": 0, "error": "unknown or non-site channel", "code": 404}
    if not has_perm(viewer, cid, PERMS["VIEW"]):
        return {"ok": 0, "error": "view permission required", "code": 403}
    rel = _site_safe_rel(data.get("path"))
    if not rel:
        return {"ok": 0, "error": "invalid resource path", "code": 400}
    fpath = _site_file_path(cid, rel)
    if fpath is None:
        return {"ok": 0, "error": "invalid resource path", "code": 400}
    # The read verdict rides the not-found response too (tay's device
    # receipt: an admin on an empty site had no widget anywhere). The
    # empty-state leg is the creation door; the viewer must know here
    # whether they may enter it.
    can_edit = is_moderator(viewer) or has_perm(viewer, cid, PERMS["SITE_EDIT"])
    offset, length = _chunk_params(data)
    total, chunk = _read_slice(fpath, offset, length)
    if total is None:
        return {"ok": 0, "error": "resource not found", "code": 404,
                "can_edit": bool(can_edit)}
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    return {"ok": 1, "channel": cid, "path": "/" + rel, "total": total,
            "offset": offset, "eof": offset + len(chunk) >= total,
            "mime": EXT_MIME.get(ext, "application/octet-stream"),
            "can_edit": bool(can_edit),
            "data_b64": base64.b64encode(chunk).decode("ascii")}

@api_route("/api/v1/site/manifest")
@wrap_json_api
def api_v1_site_manifest(viewer, data, link_id):
    """Get a site channel's manifest: {channel} -> {manifest}."""
    cid = data.get("channel")
    with db_lock:
        r = db.execute("SELECT kind, manifest FROM channels WHERE id=?", (cid,)).fetchone()
    if not r or r["kind"] != "site":
        return {"ok": 0, "error": "unknown or non-site channel", "code": 404}
    try:
        manifest = json.loads(r["manifest"] or "{}")
    except Exception:
        manifest = {}
    return {"ok": 1, "channel": cid, "manifest": manifest}

@api_route("/api/v1/site/manifest_put")
@wrap_json_api
def api_v1_site_manifest_put(viewer, data, link_id):
    """Update a site manifest: {channel, manifest} — emits a site_patch
    event carrying the manifest so open clients re-theme. P7b: moderator OR
    the channel-scoped PERMS["SITE_EDIT"], same gate as site/put."""
    cid = data.get("channel")
    if not _site_channel(cid):
        return {"ok": 0, "error": "unknown or non-site channel", "code": 404}
    if not (is_moderator(viewer) or has_perm(viewer, cid, PERMS["SITE_EDIT"])):
        return {"ok": 0, "error": "site edit permission required", "code": 403}
    manifest = data.get("manifest")
    if not isinstance(manifest, dict):
        return {"ok": 0, "error": "manifest must be an object", "code": 400}
    with db_lock:
        db.execute("UPDATE channels SET manifest=? WHERE id=?",
                   (json.dumps(manifest)[:4096], cid))
        db.commit()
    _emit_event(cid, "site_patch", {"manifest": manifest})
    return {"ok": 1, "channel": cid, "manifest": manifest}

def serve_site_file(path, data, request_id, link_id, remote_identity, requested_at):
    """Raw /file/sites/<cid>/<rel> handler. The registration key IS the path,
    but re-validate anyway: the resolved file must stay inside sites/<cid>."""
    m = re.match(r"^/file/sites/([0-9a-f]{64})/(.+)$", path or "")
    if not m:
        return b"not found"
    cid, rel = m.group(1), m.group(2)
    if not _site_safe_rel("/" + rel):
        return b"not found"
    if not page_can_view(viewer_of(remote_identity), cid):
        return b"not found"
    fpath = _site_file_path(cid, rel)
    if fpath and os.path.isfile(fpath):
        with open(fpath, "rb") as f:
            return f.read()
    return b"not found"

# ---- term channels (phase 5): guild console sessions over the stream ---------------

TERM_SESSIONS = {}                     # session_id -> session dict
TERM_SESSIONS_LOCK = threading.Lock()
_GUILD_BOOT = now()                    # updated in run(); uptime command

_TERM_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"                 # CSI sequences (ESC [ ... letter)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"    # OSC sequences (ESC ] ... BEL)
    r"|\x1b.")                                # remaining single-char escapes
_TERM_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1c\x7f]")   # keep \t (09) and \n

def _term_sanitize(s):
    """Strip whole ANSI escape sequences first, then any other control
    characters. Keeps printable text and tabs only."""
    return _TERM_CTRL_RE.sub("", _TERM_ANSI_RE.sub("", s))

def _term_emit(session, text, clear=False):
    data = {"session": session["id"],
            "text": _term_sanitize(text)[:4000]}
    if clear:
        # structured clear (the client wipes its buffer) — never ANSI
        data["clear"] = True
    _emit_event(session["channel"], "term_data", data)

def _term_purge_idle():
    """Close sessions idle past TERM_IDLE_S. Called on every term request."""
    cutoff = now() - TERM_IDLE_S
    with TERM_SESSIONS_LOCK:
        dead = [sid for sid, s in TERM_SESSIONS.items() if s["last_seen"] < cutoff]
        for sid in dead:
            s = TERM_SESSIONS.pop(sid)
            _term_emit(s, "\r\n[session closed: idle]\r\n")
    return dead

def _term_prompt():
    return "\r\n> "

def _resolve_user(token):
    """32-hex hash passthrough, else exact-username (NOCASE) lookup."""
    token = (token or "").strip()
    if re.match(r"^[0-9a-f]{32}$", token):
        return token
    with db_lock:
        row = db.execute("SELECT dest_hash FROM users WHERE username=? COLLATE NOCASE",
                         (token,)).fetchone()
    return row["dest_hash"] if row else None

def _dispatch_command(viewer, line):
    """Run one console/slash command as `viewer`. Returns
    {"out": str, "clear": bool, "quit": bool} — no emission, no session side
    effects; the term console and /api/v1/command decide how to deliver.
    Moderator denials come back as plain output (Discord-style ephemeral)."""
    cmd, _, arg = line.strip().partition(" ")
    cmd = cmd.strip().lower()
    arg = arg.strip()
    out = ""
    if cmd == "":
        pass
    elif cmd in ("help", "?"):
        out = ("\r\n".join([
            "commands:",
            "  help                this list",
            "  who                 who is online (and where they are)",
            "  channels            channel index",
            "  msg <channel> <text>   post <text> to an existing channel",
            "  info <channel>      channel details",
            "  presence            the active user list",
            "  uptime              guild uptime",
            "  create <name> [kind]   new channel (site/term: moderator)",
            "  delete <channel>    delete a channel (moderator)",
            "  rename <channel> <name>   rename (moderator)",
            "  topic <channel> <text>    set the channel topic (moderator)",
            "  ban <user> / unban <user>        moderator",
            "  mute <user> / unmute <user>      moderator",
            "  clear               clear the terminal",
            "  quit                close this session",
        ]))
    elif cmd == "who":
        users = online_users()
        if not users:
            out = "nobody online"
        else:
            active = _active_channels_by_user()
            out = "\r\n".join(
                "  %s  node %s  in %s" % (
                    u["username"], u["node_hash"][:12],
                    channel_name(active.get(u["user_hash"], "")) or "-")
                for u in sorted(users.values(), key=lambda x: x["username"].lower()))
    elif cmd == "channels":
        with db_lock:
            rows = db.execute("SELECT name, kind FROM channels ORDER BY name").fetchall()
        out = "\r\n".join("  #%s (%s)" % (r["name"], r["kind"]) for r in rows) or "  (none)"
    elif cmd == "msg":
        chan, _, text = arg.partition(" ")
        chan = chan.lstrip("#").strip()
        text = text.strip()
        if not chan or not text:
            out = "usage: msg <channel> <text>"
        else:
            with db_lock:
                row = db.execute("SELECT id FROM channels WHERE name=? COLLATE NOCASE",
                                 (chan,)).fetchone()
            if not row:
                out = "unknown channel: #%s" % chan
            elif not rate_ok(viewer, row["id"]):
                out = "rate limited"
            else:
                mid = ingest(row["id"], viewer, text)
                out = "posted to #%s" % chan if mid else "rejected"
    elif cmd == "info":
        chan = arg.lstrip("#").strip()
        with db_lock:
            row = db.execute("SELECT id, kind, created, topic FROM channels "
                             "WHERE name=? COLLATE NOCASE", (chan,)).fetchone()
        if not row:
            out = "unknown channel: #%s" % chan
        else:
            out = "  #%s  %s  seq %d%s" % (chan, row["kind"], get_seq(row["id"]),
                                           ("  topic: " + row["topic"])
                                           if row["topic"] else "")
    elif cmd == "presence":
        users = _presence_payload()
        out = "\r\n".join(
            "  %s%s" % (u["username"],
                       "  in " + channel_name(u["active_channel"])
                       if u.get("active_channel") else "")
            for u in users) or "  nobody online"
    elif cmd == "uptime":
        secs = int(now() - _GUILD_BOOT)
        out = "up %dd %dh %dm" % (secs // 86400, (secs % 86400) // 3600,
                                  (secs % 3600) // 60)
    elif cmd == "create":
        name, _, kind = arg.partition(" ")
        cid, err = _create_channel_txn(viewer, name.strip(), kind.strip().lower() or "chat")
        out = ("created #%s" % name.strip()) if cid else err
    elif cmd == "delete":
        chan = arg.lstrip("#").strip()
        with db_lock:
            row = db.execute("SELECT id FROM channels WHERE name=? COLLATE NOCASE",
                             (chan,)).fetchone()
        if not row:
            out = "unknown channel: #%s" % chan
        else:
            err, name = _delete_channel_txn(row["id"], viewer)
            out = "deleted #%s" % (name or chan) if not err else err
    elif cmd == "rename":
        chan, _, new = arg.partition(" ")
        new = new.strip()
        if not re.match(r"^[a-zA-Z0-9_\-\s]{1,32}$", chan) or \
                not re.match(r"^[a-zA-Z0-9_\-]{1,32}$", new):
            out = "usage: rename <channel> <newname>"
        else:
            with db_lock:
                row = db.execute("SELECT id FROM channels WHERE name=? COLLATE NOCASE",
                                 (chan.lstrip("#").strip(),)).fetchone()
            if not row:
                out = "unknown channel: #%s" % chan
            elif not has_perm(viewer, row["id"], PERMS["MANAGE_CHANNEL"]):
                out = "manage channel permission required"
            elif db.execute("SELECT 1 FROM channels WHERE name=? COLLATE NOCASE "
                            "AND id<>?", (new, row["id"])).fetchone():
                out = "name already taken"
            else:
                with db_lock:
                    db.execute("UPDATE channels SET name=? WHERE id=?",
                               (new, row["id"]))
                    db.commit()
                _emit_event(row["id"], "channels_changed",
                            {"channel": new}, guild_wide=True)
                out = "renamed #%s -> #%s" % (chan, new)
    elif cmd == "topic":
        chan, _, text = arg.partition(" ")
        text = text.strip()[:200]
        with db_lock:
            row = db.execute("SELECT id FROM channels WHERE name=? COLLATE NOCASE",
                             (chan.lstrip("#").strip(),)).fetchone()
        if not row:
            out = "unknown channel: #%s" % chan
        elif not has_perm(viewer, row["id"], PERMS["MANAGE_CHANNEL"]):
            out = "manage channel permission required"
        else:
            with db_lock:
                db.execute("UPDATE channels SET topic=? WHERE id=?",
                           (text or None, row["id"]))
                db.commit()
            _emit_event(row["id"], "channels_changed",
                        {"channel": chan.lstrip("#").strip()},
                        guild_wide=True)
            out = ("topic set on #%s: %s" % (chan, text)) if text \
                else "topic cleared on #%s" % chan
    elif cmd in ("ban", "mute", "unban", "unmute"):
        target = _resolve_user(arg)
        if not is_moderator(viewer):
            out = "moderator required"
        elif not target:
            out = "unknown user: %s" % arg
        elif target == viewer:
            out = "cannot moderate yourself"
        else:
            name = get_username(target)
            if cmd == "ban":
                ban_user(target, "banned by %s" % viewer[:12])
            elif cmd == "mute":
                mute_user(target, "muted by %s" % viewer[:12], viewer)
            elif cmd == "unban":
                unban_user(target)
            else:
                unmute_user(target)
            out = "%s %s" % ({"ban": "banned", "mute": "muted",
                              "unban": "unbanned", "unmute": "unmuted"}[cmd], name)
    elif cmd == "clear":
        return {"out": "", "clear": True, "quit": False}
    elif cmd in ("quit", "exit"):
        return {"out": "[session closed]", "clear": False, "quit": True}
    else:
        out = "unknown command: %s (try help)" % cmd
    return {"out": out, "clear": False, "quit": False}

def _term_execute(session, line):
    """Console wrapper: run one command, emit output (+ prompt) as term_data."""
    res = _dispatch_command(session["user_hash"], line)
    if res["quit"]:
        with TERM_SESSIONS_LOCK:
            TERM_SESSIONS.pop(session["id"], None)
        _term_emit(session, "\r\n[session closed]")
        return
    if res["clear"]:
        _term_emit(session, "", clear=True)
        _term_emit(session, _term_prompt())
        return
    _term_emit(session, res["out"] + _term_prompt())

@api_route("/api/v1/term/open")
@wrap_json_api
def api_v1_term_open(viewer, data, link_id):
    """Open a console session on a term channel. {channel, rows?, cols?} ->
    {session}. The banner + prompt arrive as term_data stream events."""
    cid = data.get("channel")
    with db_lock:
        row = db.execute("SELECT id, kind FROM channels WHERE id=?", (cid,)).fetchone()
    if not row or row["kind"] != "term":
        return {"ok": 0, "error": "unknown or non-term channel", "code": 404}
    if not has_perm(viewer, cid, PERMS["VIEW"]):
        return {"ok": 0, "error": "view permission required", "code": 403}
    _term_purge_idle()
    with TERM_SESSIONS_LOCK:
        mine = sum(1 for s in TERM_SESSIONS.values() if s["user_hash"] == viewer)
        if mine >= TERM_MAX_PER_USER:
            return {"ok": 0, "error": "too many sessions", "code": 429}
        if len(TERM_SESSIONS) >= TERM_MAX_TOTAL:
            return {"ok": 0, "error": "guild session limit reached", "code": 429}
        sid = secrets.token_hex(12)
        TERM_SESSIONS[sid] = {
            "id": sid, "user_hash": viewer, "link_id": link_id, "channel": cid,
            "created": now(), "last_seen": now(), "rows": 24, "cols": 80, "line": "",
        }
    banner = ("sHades guild console\r\ntype 'help' for commands")
    _term_emit(TERM_SESSIONS[sid], banner + _term_prompt())
    return {"ok": 1, "session": sid, "channel": cid}

@api_route("/api/v1/term/data")
@wrap_json_api
def api_v1_term_data(viewer, data, link_id):
    """Feed console input: {session, data_b64}. Lines execute on newline;
    output rides term_data events on the channel stream."""
    with TERM_SESSIONS_LOCK:
        session = TERM_SESSIONS.get(data.get("session") or "")
    if not session:
        return {"ok": 0, "error": "unknown session", "code": 404}
    if session["user_hash"] != viewer:
        return {"ok": 0, "error": "not your session", "code": 403}
    raw = _b64_decode(data.get("data_b64"))
    if raw is None:
        return {"ok": 0, "error": "data_b64 must be valid base64", "code": 400}
    if len(raw) > TERM_MAX_INPUT:
        return {"ok": 0, "error": "input too long", "code": 400}
    if not rate_ok(viewer, "_term", *TERM_IN_RATE):
        return {"ok": 0, "error": "rate limited", "code": 429}
    _term_purge_idle()
    with TERM_SESSIONS_LOCK:
        if session["id"] not in TERM_SESSIONS:
            return {"ok": 0, "error": "session closed", "code": 404}
        session["last_seen"] = now()
        session["line"] += _term_sanitize(raw.decode("utf-8", "replace"))
    # Execute complete lines OUTSIDE the session lock: a command may close
    # the session (quit) and other handlers need the same lock.
    closed = False
    while "\n" in session["line"]:
        line, _, session["line"] = session["line"].partition("\n")
        _term_execute(session, line.rstrip("\r"))
        with TERM_SESSIONS_LOCK:
            if session["id"] not in TERM_SESSIONS:
                closed = True
                break
    return {"ok": 1, "closed": closed}

@api_route("/api/v1/term/resize")
@wrap_json_api
def api_v1_term_resize(viewer, data, link_id):
    with TERM_SESSIONS_LOCK:
        session = TERM_SESSIONS.get(data.get("session") or "")
    if not session:
        return {"ok": 0, "error": "unknown session", "code": 404}
    if session["user_hash"] != viewer:
        return {"ok": 0, "error": "not your session", "code": 403}
    try:
        session["rows"] = max(1, min(int(data.get("rows", 24)), 200))
        session["cols"] = max(1, min(int(data.get("cols", 80)), 500))
    except (TypeError, ValueError):
        return {"ok": 0, "error": "bad rows/cols", "code": 400}
    return {"ok": 1}

@api_route("/api/v1/term/close")
@wrap_json_api
def api_v1_term_close(viewer, data, link_id):
    with TERM_SESSIONS_LOCK:
        session = TERM_SESSIONS.pop(data.get("session") or "", None)
    if not session:
        return {"ok": 0, "error": "unknown session", "code": 404}
    if session["user_hash"] != viewer:
        # not ours: put it back rather than letting a stranger close it
        with TERM_SESSIONS_LOCK:
            TERM_SESSIONS.setdefault(session["id"], session)
        return {"ok": 0, "error": "not your session", "code": 403}
    _term_emit(session, "\r\n[session closed]")
    return {"ok": 1}

# ---- receipts, reactions, search (phase 6) -----------------------------------------

_EMOJI_RE = re.compile(r"^[a-z0-9_+\-]{1,16}$")
# Unicode emoji gate: BMP symbols (arrows/misc/dingbats incl 2705/2764) and
# supplementary-plane atoms (1F000-1FAFF), with variation selectors / ZWJs as
# joiners between-or-after atoms only. NOTE the 8-digit \U escapes: \u1F000
# in a regex class is five hex digits (\u1F00 + "00"), which silently widens
# the class to every codepoint below U+1FAF — accepted letters and punctuation.
_EMOJI_CORE_RE = re.compile(
    r"^(?:[\u2190-\u2BFF\U0001F000-\U0001FAFF]"
    r"(?:[\uFE0F\u200D]*[\u2190-\u2BFF\U0001F000-\U0001FAFF])*)"
    r"[\uFE0F\u200D]*$")
MAX_EMOJI_PER_MSG = 16   # distinct shortcodes/emoji per message

def _emoji_ok(emoji):
    """"+1" and friends; or unicode emoji atoms with ZWJ/VS joiners;
    byte-capped so nothing absurd reaches the PK column."""
    if _EMOJI_RE.match(emoji):
        return True
    return len(emoji.encode("utf-8")) <= 32 and bool(_EMOJI_CORE_RE.match(emoji))

def _receipt_last_id(cid, viewer):
    with db_lock:
        row = db.execute("SELECT last_id FROM receipts WHERE channel_id=? AND user_hash=?",
                         (cid, viewer)).fetchone()
    return row["last_id"] if row else 0

def _unread_count(cid, last_id):
    with db_lock:
        row = db.execute("SELECT COUNT(*) AS n FROM messages WHERE channel_id=? AND id>?",
                         (cid, last_id)).fetchone()
    return row["n"]

@api_route("/api/v1/receipts")
@wrap_json_api
def api_v1_receipts(viewer, data, link_id):
    """Read watermarks. With msg_id: mark read up to that message (it must
    belong to the channel). Without: report {last_read_id, unread}."""
    cid = data.get("channel")
    with db_lock:
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return {"ok": 0, "error": "unknown channel", "code": 404}
    if not has_perm(viewer, cid, PERMS["VIEW"]):
        return {"ok": 0, "error": "view permission required", "code": 403}
    msg_id = data.get("msg_id")
    if msg_id:
        with db_lock:
            row = db.execute("SELECT id FROM messages WHERE msg_id=? AND channel_id=?",
                             (msg_id, cid)).fetchone()
            if not row:
                return {"ok": 0, "error": "unknown message in channel", "code": 404}
            db.execute("INSERT INTO receipts (channel_id, user_hash, last_id, ts) "
                       "VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(channel_id, user_hash) DO UPDATE SET "
                       "last_id=MAX(receipts.last_id, excluded.last_id), ts=excluded.ts",
                       (cid, viewer, row["id"], now()))
            db.commit()
    last_id = _receipt_last_id(cid, viewer)
    return {"ok": 1, "channel": cid, "last_read_id": last_id,
            "unread": _unread_count(cid, last_id)}

@api_route("/api/v1/react")
@wrap_json_api
def api_v1_react(viewer, data, link_id):
    """Toggle a reaction (one emoji per user per message). Emoji are unicode
    emoji characters (BMP symbols, supplementary plane, ZWJ composites) or
    legacy ASCII shortcodes ('+1', 'laugh', ...) for older rows."""
    msg_id = data.get("msg")
    emoji = (data.get("emoji") or "").strip().lower()
    if not _emoji_ok(emoji):
        return {"ok": 0, "error": "emoji must be an emoji or a shortcode (a-z0-9_+-)",
                "code": 400}
    with db_lock:
        row = db.execute("SELECT channel_id, deleted_at FROM messages WHERE msg_id=?",
                         (msg_id,)).fetchone()
        if not row:
            return {"ok": 0, "error": "unknown message", "code": 404}
    if not has_perm(viewer, row["channel_id"], PERMS["VIEW"]):
        return {"ok": 0, "error": "view permission required", "code": 403}
    with db_lock:
        existing = db.execute("SELECT 1 FROM reactions WHERE msg_id=? AND user_hash=? "
                              "AND emoji=?", (msg_id, viewer, emoji)).fetchone()
        if existing:
            db.execute("DELETE FROM reactions WHERE msg_id=? AND user_hash=? AND emoji=?",
                       (msg_id, viewer, emoji))
            active = False
        else:
            distinct = db.execute("SELECT COUNT(DISTINCT emoji) AS n FROM reactions "
                                  "WHERE msg_id=?", (msg_id,)).fetchone()["n"]
            if distinct >= MAX_EMOJI_PER_MSG:
                return {"ok": 0, "error": "too many reactions on this message", "code": 400}
            db.execute("INSERT INTO reactions (msg_id, user_hash, emoji, ts) "
                       "VALUES (?, ?, ?, ?)", (msg_id, viewer, emoji, now()))
            active = True
        counts = {r["emoji"]: r["n"] for r in db.execute(
            "SELECT emoji, COUNT(*) AS n FROM reactions WHERE msg_id=? "
            "GROUP BY emoji", (msg_id,)).fetchall()}
        db.commit()
    _emit_event(row["channel_id"], "reaction", {
        "msg_id": msg_id, "emoji": emoji, "user_hash": viewer,
        "username": get_username(viewer), "active": active, "counts": counts,
    })
    return {"ok": 1, "active": active, "counts": counts}

@api_route("/api/v1/search")
@wrap_json_api
def api_v1_search(viewer, data, link_id):
    """Substring search over the ledger. {q, channel?, limit?} — deleted
    messages are excluded; LIKE wildcards in the query are escaped."""
    q = (data.get("q") or "").strip()
    if len(q) < 2:
        return {"ok": 0, "error": "query too short (min 2 chars)", "code": 400}
    try:
        limit = min(max(int(data.get("limit", 20)), 1), 50)
    except (TypeError, ValueError):
        limit = 20
    sql = ("SELECT * FROM messages WHERE deleted_at IS NULL AND content LIKE ? "
           "ESCAPE '\\' ")
    args = ["%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"]
    cid = data.get("channel")
    if cid:
        with db_lock:
            if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
                return {"ok": 0, "error": "unknown channel", "code": 404}
        if not has_perm(viewer, cid, PERMS["VIEW"]):
            return {"ok": 0, "error": "view permission required", "code": 403}
        sql += "AND channel_id=? "
        args.append(cid)
    else:
        # Filter before LIMIT (tay fork tier-1): trimming the first N
        # matches afterwards can return an empty page when hidden-channel
        # posts are newer than a visible match — and the trim also leaks
        # the mere existence of results it discarded.
        with db_lock:
            channel_ids = [r["id"] for r in
                           db.execute("SELECT id FROM channels").fetchall()]
        visible = [ch for ch in channel_ids
                   if has_perm(viewer, ch, PERMS["VIEW"])]
        if not visible:
            return {"ok": 1, "query": q, "results": []}
        sql += "AND channel_id IN (%s) " % ",".join("?" for _ in visible)
        args.extend(visible)
    sql += "ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db_lock:
        rows = db.execute(sql, args).fetchall()
    return {"ok": 1, "query": q, "results": [msg_row_json(r) for r in rows]}

# ---- whoami + moderation + channel deletion (U3: context-menu backend) -------------

@api_route("/api/v1/whoami")
@wrap_json_api
def api_v1_whoami(viewer, data, link_id):
    """The requester's own identity as the guild sees it. The client uses
    this to show/hide moderator affordances (menus are a convenience —
    every action is still enforced server-side)."""
    with db_lock:
        rows = db.execute(
            "SELECT r.role_id, r.name, r.perms, r.color FROM user_roles ur "
            "JOIN roles r ON r.role_id = ur.role_id WHERE ur.user_hash=? "
            "ORDER BY r.position DESC", (viewer,)).fetchall()
    roles = [{"role_id": r["role_id"], "name": r["name"], "perms": r["perms"],
              "color": r["color"]} for r in rows]
    is_admin = any(r["perms"] & PERMS["ADMIN"] for r in roles)
    icon = _cfg_get("guild_icon", "") or None
    banner = _cfg_get("banner", "") or None
    return {"ok": 1, "user_hash": viewer, "username": get_username(viewer),
            "is_moderator": is_moderator(viewer) or is_admin,
            "is_admin": is_admin, "roles": roles,
            "guild": {"name": _cfg_get("guild_name", "") or GUILD_NAME,
                      "motd": _cfg_get("motd", "") or "",
                      "icon": icon, "banner": banner,
                      # P11 arrow 5: what this server runs + the toggle,
                      # so a client can offer to pull the release
                      "app_version": SERVER_VERSION,
                      "update_channel": _cfg_get(
                          "update_channel", "off")}}

def _register_mod_endpoint(path, verb, apply_fn):
    """Register one moderator-gated JSON endpoint. `apply_fn(viewer, target,
    reason)` performs the action; the gate, hash validation and audit log
    are shared."""
    @api_route(path)
    @wrap_json_api
    def handler(viewer, data, link_id):
        if not is_moderator(viewer):
            return {"ok": 0, "error": "moderator required", "code": 403}
        target = (data.get("user") or "").strip().lower()
        if not re.match(r"^[0-9a-f]{32}$", target):
            return {"ok": 0, "error": "user must be a 32-hex hash", "code": 400}
        if target == viewer:
            return {"ok": 0, "error": "cannot moderate yourself", "code": 400}
        reason = (data.get("reason") or "").strip()[:200]
        apply_fn(viewer, target, reason)
        RNS.log("moderation: %s %s %s" % (viewer[:12], verb, target[:12]),
                RNS.LOG_NOTICE)
        _audit(viewer, "mod.%s" % verb, target, reason)
        return {"ok": 1}

_register_mod_endpoint(
    "/api/v1/mod/ban", "banned",
    lambda viewer, target, reason: ban_user(
        target, reason or "banned by %s" % viewer[:12]))
_register_mod_endpoint(
    "/api/v1/mod/unban", "unbanned",
    lambda viewer, target, reason: unban_user(target))
_register_mod_endpoint(
    "/api/v1/mod/mute", "muted",
    lambda viewer, target, reason: mute_user(
        target, reason or "muted by %s" % viewer[:12], viewer))
_register_mod_endpoint(
    "/api/v1/mod/unmute", "unmuted",
    lambda viewer, target, reason: unmute_user(target))

def _audit(actor, action, target=None, detail=None):
    """Append one row to the audit log (U8). Every privileged mutation
    (ban/mute/delete/channel/role/config) lands here. Never raises — an
    audit failure must not break the action that was being audited."""
    try:
        with db_lock:
            db.execute("INSERT INTO audit_log (ts, actor, action, target, detail) "
                       "VALUES (?, ?, ?, ?, ?)",
                       (now(), actor or "-", str(action)[:64],
                        (target or "")[:128], (detail or "")[:400]))
            db.commit()
    except Exception as e:
        RNS.log("audit write failed: %s" % e, RNS.LOG_ERROR)

def _cfg_get(key, default=None):
    with db_lock:
        row = db.execute("SELECT value FROM server_config WHERE key=?",
                         (key,)).fetchone()
    return row["value"] if row else default

def _cfg_set(key, value):
    with db_lock:
        db.execute("INSERT INTO server_config (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))
        db.commit()

def _cfg_all():
    with db_lock:
        rows = db.execute("SELECT key, value FROM server_config").fetchall()
    return {r["key"]: r["value"] for r in rows}

# Server settings the admin panel may change (U8). Everything else in
# server_config is server-internal.
SERVER_CONFIG_KEYS = {
    "guild_name": "sHades",
    "registration": "open",      # open | code
    "slowmode": "0",             # seconds between posts, 0 = off
    "motd": "",
    "guild_icon": "",            # empty = no icon; else a stored blob_id
    "banner": "",                # empty = no banner; else a stored blob_id
    "attachments": "saved",      # saved | ephemeral (P2: RAM-only bytes)
    "update_channel": "off",     # P11: off | github | rns
    "update_check_minutes": "360",  # P11: soft check interval; 0 = boot only
    "update_channel_peer": "",   # P11d: the pinned peer's node destination
                                 # hash (64-hex); the software channel's
                                 # trust anchor for serve and pull
    "update_channel_shapin": "",  # P11e: the admin-pinned artifact sha256
                                  # (64-hex); the pull's authority for what
                                  # bytes may run. Sourced out-of-band (the
                                  # release body, another admin). When set,
                                  # the serving peer is a courier only.
    "update_trusted_hub": "",     # P11f: the DISTRIBUTED hub's node dest
                                  # hash (64-hex) - the sole trusted
                                  # initiating update source. Any other hub
                                  # may be compromised; a non-trusted pull
                                  # needs the artifact pin to run at all.
}

# ---------------------------------------------------------------------- P11 updater

def _update_boot_guard(tree=None):
    """P11f failback: a DIRECT boot (no update_run parent, marker still
    set) restores the backup members BEFORE serving. The pending-version
    marker means: a swap was interrupted mid-flight (power loss, a
    process manager kill) or a previous update never proved its boot.
    The supervised path (SHADES_UPDATE_RUN=1, from update_run.py) owns
    its own rollback loop; the guard stands out of its way and covers
    every other boot."""
    root = tree or os.path.dirname(os.path.abspath(__file__))
    marker = os.path.join(root, "update_staging", "pending_version")
    if not os.path.isfile(marker):
        return None
    if (os.environ.get("SHADES_UPDATE_RUN") == "1"
            and tree is None):
        return None
    bdir = os.path.join(root, "update_backup")
    n = 0
    try:
        names = sorted(os.listdir(bdir))
    except OSError:
        names = []
    for name in names:
        src = os.path.join(bdir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(root, name))
            n += 1
    try:
        os.remove(marker)
    except OSError:
        pass
    updater._note(
        "rolled-back",
        "the previous update never confirmed its boot; restored %d "
        "backup members" % n)
    return n

_UPDATE_LOCK = threading.Lock()
_UPDATE_LAST = [0.0]
_BOOT_ARGV = []


def _update_worker():
    with _UPDATE_LOCK:
        try:
            tree = os.path.dirname(os.path.abspath(__file__))
            res = updater.check_and_apply(
                tree, SERVER_VERSION, _cfg_get,
                argv=(["sHades.py"] + _BOOT_ARGV), exec_restart=True,
                rns_fetch=_rns_pull)
            RNS.log("updater: %s" % (res,), RNS.LOG_INFO)
        except Exception as e:
            RNS.log("updater: failed: %s" % e, RNS.LOG_ERROR)


def _update_maybe_tick(force=False):
    """Called by the reaper loop and at boot; honours the toggle + interval."""
    ch = (_cfg_get("update_channel", "off") or "off").strip().lower()
    if ch == "off":
        return
    now_t = now()
    try:
        iv = int(_cfg_get("update_check_minutes", "360") or 0)
    except (TypeError, ValueError):
        iv = 360
    iv_s = max(iv, 1) * 60
    if not force and _UPDATE_LAST[0] and (now_t - _UPDATE_LAST[0]) < iv_s:
        return
    _UPDATE_LAST[0] = now_t
    if not _UPDATE_LOCK.acquire(False):
        return  # a worker is already out; its result answers next tick
    threading.Thread(target=_update_worker, daemon=True).start()


@api_route("/api/v1/update_status")
def api_v1_update_status(viewer, data, request_id, link_id,
                         remote_identity, requested_at):
    """P11: the updater's last verdict, admin-only read (the panel
    consumes this in chain 5)."""
    denied = _require_admin(viewer)
    if denied:
        return denied
    out = updater.get_status()
    out["channel"] = _cfg_get("update_channel", "off")
    out["interval"] = _cfg_get("update_check_minutes", "360")
    out["peer"] = _cfg_get("update_channel_peer", "")
    out["shapin"] = _cfg_get("update_channel_shapin", "")
    out["trusted"] = _cfg_get("update_trusted_hub", "")
    return out


# ------------------------------------------------------------------------ P11d
# The RNS software channel: tay's contingency, rncp's PATTERN on raw RNS
# primitives (no rncp code, no HTTP anywhere). A pinned peer's node
# destination hash rides admin_config update_channel_peer; the serve
# routes gate on an admin OR that pin; the pull side establishes an
# outbound link, identifies this server's own identity on it, requests
# serve_info + chunked bytes, verify's the sha, then hands
# {version, sha256, bundle_bytes, bundle_name} to the same P11a core
# the github channel uses. Each updated server serves its bundle, so
# one GitHub seed propagates through the mesh peer to peer.
_SERVE_CACHE = {"version": None, "path": None, "sha": None}
_SERVE_EXCLUDE = {"update_staging", "update_backup", "__pycache__",
                  "scratch_probes", ".git", "dist", "build", "bin",
                  ".buildozer", ".github"}
_SERVE_SKIP_EXT = (".pyc", ".log", ".sha256", ".tar.gz")


def _serve_peer_ok(viewer, remote_identity):
    """P11d gate: an admin, or the pin's identity identified on the link."""
    if _require_admin(viewer) is None:
        return True
    pin = (_cfg_get("update_channel_peer", "") or "").strip().lower()
    if not pin or remote_identity is None:
        return False
    try:
        return remote_identity.hash.hex() == pin
    except Exception:
        return False


def _build_update_bundle():
    """Tar this running tree (the swap members + their data files),
    name it sHades-server-<V>.tar.gz (the release-name contract),
    memoize (version, path, sha)."""
    if (_SERVE_CACHE["version"] == SERVER_VERSION
            and _SERVE_CACHE["path"]
            and os.path.isfile(_SERVE_CACHE["path"])):
        return _SERVE_CACHE["path"], _SERVE_CACHE["sha"]
    import tarfile
    import tempfile
    import hashlib
    tree = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(tempfile.mkdtemp(prefix="sHades-serve-"),
                       "sHades-server-%s.tar.gz" % SERVER_VERSION)
    with tarfile.open(out, "w:gz") as tf:
        for root, dirs, files in os.walk(tree):
            dirs[:] = sorted(d for d in dirs if d not in _SERVE_EXCLUDE)
            for fn in sorted(files):
                if fn.endswith(_SERVE_SKIP_EXT):
                    continue
                p = os.path.join(root, fn)
                tf.add(p, arcname=os.path.relpath(p, tree))
    b = open(out, "rb").read()
    _SERVE_CACHE.update(version=SERVER_VERSION, path=out,
                        sha=hashlib.sha256(b).hexdigest())
    return out, _SERVE_CACHE["sha"]


@api_route("/api/v1/update/serve_info")
def api_v1_update_serve_info(viewer, data, request_id, link_id,
                             remote_identity, requested_at):
    """P11d: the software channel's fingerprint. Returns this running
    tree's bundle name + version + sha256; the version gate rides the
    puller, the bytes ride serve_chunk with the same gate."""
    if not _serve_peer_ok(viewer, remote_identity):
        return {"ok": 0, "error": "peer not allowed", "code": 403}
    tree = os.path.dirname(os.path.abspath(__file__))
    for m in updater.TREE_MEMBERS:
        if not os.path.isfile(os.path.join(tree, m)):
            return {"ok": 0, "error": "serving tree incomplete: missing %s"
                    % m, "code": 500}
    bpath, sha = _build_update_bundle()
    return {"ok": 1, "version": SERVER_VERSION,
            "bundle_name": os.path.basename(bpath),
            "bundle_sha256": sha}


@api_route("/api/v1/update/serve_chunk")
def api_v1_update_serve_chunk(viewer, data, request_id, link_id,
                              remote_identity, requested_at):
    """P11d: chunked bundle read, the same shape as /api/v1/blobs/get."""
    if not _serve_peer_ok(viewer, remote_identity):
        return {"ok": 0, "error": "peer not allowed", "code": 403}
    bpath, _ = _build_update_bundle()
    offset, length = _chunk_params(data or {})
    total, chunk = _read_slice(bpath, offset, length)
    if total is None:
        return {"ok": 0, "error": "bundle unreadable", "code": 500}
    return {"ok": 1, "total": total, "offset": offset,
            "eof": offset + len(chunk) >= total,
            "data_b64": base64.b64encode(chunk).decode("ascii")}


def _rns_pull():
    """P11d pull: rncp's shape on raw RNS primitives. Link to the
    pinned peer (the 64-hex update_channel_peer), identify with this
    server's own identity, request serve_info -> serve_chunk slices,
    verify the sha, return the fetch dict for updater.check_and_apply.
    None on any refusal; verdicts ride the updater's status note."""
    import RNS
    import msgpack
    pin = (_cfg_get("update_channel_peer", "") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", pin):
        updater._note("refused", "update_channel_peer is not a 64-hex pin")
        return None
    nid = RNS.Identity.recall(bytes.fromhex(pin))
    if nid is None:
        updater._note("no-bundle", "pinned peer's announce not heard yet")
        return None
    nd = RNS.Destination(nid, RNS.Destination.OUT, RNS.Destination.SINGLE,
                         "nomadnetwork", "node")
    link = RNS.Link(nd)
    for _ in range(200):
        if link.status == RNS.Link.ACTIVE:
            break
        time.sleep(0.1)
    if link.status != RNS.Link.ACTIVE:
        updater._note("no-bundle", "link to pinned peer never went ACTIVE")
        return None
    # identify so the serving gate sees this server as the trusted peer
    link.identify(node_destination.identity)

    def _req(path, data=None, timeout=30.0):
        receipt = link.request(path, data=data or {})
        deadline = time.time() + timeout
        while getattr(receipt, "response", None) is None \
                and time.time() < deadline:
            time.sleep(0.05)
        resp = getattr(receipt, "response", None)
        if resp is None:
            raise TimeoutError(path)
        return msgpack.unpackb(resp, raw=False)

    try:
        info = _req("/api/v1/update/serve_info")
        if not info.get("ok"):
            updater._note("no-bundle", "peer serve_info refused: %s"
                          % info.get("error"))
            return None
        cur = updater.parse_version(SERVER_VERSION)
        new = updater.parse_version(info.get("version") or "")
        if cur is None or new is None:
            updater._note("refused", "peer carried no parseable version")
            return None
        if new <= cur:
            updater._note("current",
                          "%s is the newest peer bundle" % info["version"])
            return None
        # P11e: the artifact pin outranks the wire. The admin pins the
        # sha out-of-band (the release body, another admin); a served
        # fingerprint that disagrees with it is refused BEFORE any
        # bytes move, and the assembled bytes are verified against the
        # PIN, never against the peer's own claim. Empty pin =
        # peer-only mode (integrity only; provenance rides the identity
        # pin, and the status line says so).
        shapin = (_cfg_get("update_channel_shapin", "") or
                  "").strip().lower()
        trusted = (_cfg_get("update_trusted_hub", "") or "").strip().lower()
        pin = (_cfg_get("update_channel_peer", "") or "").strip().lower()
        target_sha = shapin
        sha_source = "pin"
        if not shapin:
            target_sha = (info.get("bundle_sha256") or "").lower()
            # P11f tiers: the DISTRIBUTED hub (the sole trusted
            # initiating source) may act as the sha oracle by itself;
            # any other hub without the artifact pin stays peer-only.
            sha_source = ("trusted-hub" if trusted and pin == trusted
                          else "peer-only")
        elif (info.get("bundle_sha256") or "").lower() != shapin:
            updater._note("refused",
                          "peer bundle disagrees with the pinned "
                          "artifact sha")
            return None
        total = None
        parts = []
        offset = 0
        while True:
            res = _req("/api/v1/update/serve_chunk", {"offset": offset})
            if not res.get("ok"):
                updater._note("refused", "peer chunk refused: %s"
                              % res.get("error"))
                return None
            total = res["total"]
            parts.append(base64.b64decode(res["data_b64"]))
            offset += len(parts[-1])
            if res.get("eof") or offset >= total:
                break
        data = b"".join(parts)
        if len(data) != total:
            updater._note("refused", "bundle size disagree: %d of %d"
                          % (len(data), total))
            return None
        if not updater.verify_sha(data, target_sha):
            updater._note("refused",
                          "bundle bytes disagree with the %s sha"
                          % ("pinned artifact" if shapin else "peer's"))
            return None
        return {"version": info["version"],
                "sha256": target_sha,
                "sha_source": sha_source,
                "bundle_name": info["bundle_name"],
                "bundle_bytes": data}
    except Exception as e:
        updater._note("no-bundle", "peer fetch failed: %s" % e)
        return None

def _require_admin(viewer):
    """Admin gate for the panel endpoints (U8). ADMIN role only — the
    legacy moderator set is NOT enough here."""
    if not has_role_perm(viewer, PERMS["ADMIN"]):
        return {"ok": 0, "error": "admin required", "code": 403}
    return None

@api_route("/api/v1/admin/config")
@wrap_json_api
def api_v1_admin_config(viewer, data, link_id):
    """Read or change server settings. GET returns the current values;
    a POST with {key, value} changes one. Only SERVER_CONFIG_KEYS are
    writable — anything else is server-internal."""
    denied = _require_admin(viewer)
    if denied:
        return denied
    key = data.get("key")
    if key is None:
        cfg = dict(SERVER_CONFIG_KEYS)
        cfg.update(_cfg_all())
        cfg["guild_name"] = cfg.get("guild_name") or GUILD_NAME
        return {"ok": 1, "config": cfg}
    if key not in SERVER_CONFIG_KEYS:
        return {"ok": 0, "error": "unknown config key", "code": 400}
    if "value" not in data:
        return {"ok": 1, "config": {key: _cfg_get(key,
                                                  SERVER_CONFIG_KEYS[key])}}
    value = str(data.get("value") or "")[:2000]
    if key == "registration" and value not in ("open", "code"):
        return {"ok": 0, "error": "registration must be 'open' or 'code'",
                "code": 400}
    if key == "update_channel_peer" and value:
        # P11d: the pin is an RNS node destination hash: 32 bytes = 64 hex
        digest = value.strip().lower()
        if not re.match(r"^[0-9a-f]{64}$", digest):
            return {"ok": 0,
                    "error": "update_channel_peer must be a 64-hex node "
                             "destination hash", "code": 400}
        value = digest
    if key == "update_channel_shapin" and value:
        # P11e: the artifact pin is a sha256 hex digest: 64 hex. Empty
        # is allowed (and means the pull trusts the peer's own sha -
        # integrity only, provenance rides the identity pin).
        digest = value.strip().lower()
        if not re.match(r"^[0-9a-f]{64}$", digest):
            return {"ok": 0,
                    "error": "update_channel_shapin must be a 64-hex "
                             "sha256 digest", "code": 400}
        value = digest
    if key == "update_trusted_hub" and value:
        # P11f: the trusted hub is an RNS node destination hash
        digest = value.strip().lower()
        if not re.match(r"^[0-9a-f]{64}$", digest):
            return {"ok": 0,
                    "error": "update_trusted_hub must be a 64-hex node "
                             "destination hash", "code": 400}
        value = digest
    if key == "guild_icon" and value:
        # the icon rides the standard blob store: the value must resolve to
        # a committed blob (uploaded via blobs/put or the chunked flow)
        digest = value.strip().lower()
        if len(digest) > 32 or not re.match(r"^[0-9a-f]{32}$", digest) \
                or not _blob_fname(digest):
            return {"ok": 0, "error": "guild_icon must be a stored blob_id",
                    "code": 400}
        value = digest
    if key == "banner" and value:
        # P5: same shape as guild_icon -- the asset rides the standard
        # blob store; the config only references it
        digest = value.strip().lower()
        if len(digest) > 32 or not re.match(r"^[0-9a-f]{32}$", digest) \
                or not _blob_fname(digest):
            return {"ok": 0, "error": "banner must be a stored blob_id",
                    "code": 400}
        value = digest
    if key == "banner" and value:
        # P5: same shape as guild_icon -- the asset rides the standard
        # blob store; the config only references it
        digest = value.strip().lower()
        if len(digest) > 32 or not re.match(r"^[0-9a-f]{32}$", digest) \
                or not _blob_fname(digest):
            return {"ok": 0, "error": "banner must be a stored blob_id",
                    "code": 400}
        value = digest
    if key == "banner" and value:
        # P5: same shape as guild_icon -- the asset rides the standard
        # blob store; the config only references it
        digest = value.strip().lower()
        if len(digest) > 32 or not re.match(r"^[0-9a-f]{32}$", digest) \
                or not _blob_fname(digest):
            return {"ok": 0, "error": "banner must be a stored blob_id",
                    "code": 400}
        value = digest
    if key == "banner" and value:
        # P5: same shape as guild_icon -- the asset rides the standard
        # blob store; the config only references it
        digest = value.strip().lower()
        if len(digest) > 32 or not re.match(r"^[0-9a-f]{32}$", digest) \
                or not _blob_fname(digest):
            return {"ok": 0, "error": "banner must be a stored blob_id",
                    "code": 400}
        value = digest
    if key == "slowmode":
        try:
            secs = int(value)
        except ValueError:
            return {"ok": 0, "error": "slowmode must be an integer", "code": 400}
        if secs < 0 or secs > 3600:
            return {"ok": 0, "error": "slowmode must be 0-3600", "code": 400}
        value = str(secs)
    if key == "attachments" and value not in ("saved", "ephemeral"):
        return {"ok": 0, "error": "attachments must be 'saved' or "
                "'ephemeral'", "code": 400}
    old = _cfg_get(key, SERVER_CONFIG_KEYS[key])
    _cfg_set(key, value)
    _audit(viewer, "config.set", key, "%s -> %s" % (old, value))
    return {"ok": 1, "config": {key: value}}

@api_route("/api/v1/admin/audit")
@wrap_json_api
def api_v1_admin_audit(viewer, data, link_id):
    """Recent audit-log rows, newest first."""
    denied = _require_admin(viewer)
    if denied:
        return denied
    try:
        limit = int(data.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))
    with db_lock:
        rows = db.execute("SELECT ts, actor, action, target, detail FROM "
                          "audit_log ORDER BY id DESC LIMIT ?",
                          (limit,)).fetchall()
    return {"ok": 1, "entries": [
        {"ts": r["ts"], "actor": r["actor"], "action": r["action"],
         "target": r["target"], "detail": r["detail"]} for r in rows]}

@api_route("/api/v1/admin/mods")
@wrap_json_api
def api_v1_admin_mods(viewer, data, link_id):
    """Manage the whitelists. {op: list|add|remove|admin_add|admin_remove,
    user: <32-hex>}. moderators.txt and admin.txt stay the source of truth
    and are rewritten atomically; the live sets are updated to match.
    Adding a moderator does NOT make them an admin."""
    denied = _require_admin(viewer)
    if denied:
        return denied
    op = (data.get("op") or "list").lower()
    if op == "list":
        return {"ok": 1, "mods": sorted(MODERATORS),
                "admins": sorted(ADMIN_USERS)}
    target = (data.get("user") or "").strip().lower()
    if not re.match(r"^[0-9a-f]{32}$", target):
        return {"ok": 0, "error": "user must be a 32-hex hash", "code": 400}
    if op == "add":
        MODERATORS.add(target)
        _write_hash_file(os.path.join(STORAGE, "moderators.txt"),
                         sorted(MODERATORS))
        _audit(viewer, "mods.add", target)
    elif op == "remove":
        MODERATORS.discard(target)
        _write_hash_file(os.path.join(STORAGE, "moderators.txt"),
                         sorted(MODERATORS))
        _audit(viewer, "mods.remove", target)
    elif op == "admin_add":
        ADMIN_USERS.add(target)
        _write_hash_file(os.path.join(STORAGE, "admin.txt"),
                         sorted(ADMIN_USERS))
        _assign_role(target, _ensure_role("Admin", PERM_ALL, position=100,
                                          color="F8a8"))
        _audit(viewer, "admins.add", target)
    elif op == "admin_remove":
        ADMIN_USERS.discard(target)
        _write_hash_file(os.path.join(STORAGE, "admin.txt"),
                         sorted(ADMIN_USERS))
        _audit(viewer, "admins.remove", target)
    else:
        return {"ok": 0, "error": "op must be list|add|remove|admin_add|"
                                  "admin_remove", "code": 400}
    return {"ok": 1, "mods": sorted(MODERATORS), "admins": sorted(ADMIN_USERS)}

def _write_hash_file(path, hashes):
    """Atomically rewrite a one-hash-per-line whitelist file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for h in hashes:
            f.write(h + "\n")
    os.replace(tmp, path)

def _registration_policy():
    """'open' (default) or 'code' — whether a new member needs an invite
    code to register."""
    return (_cfg_get("registration", "open") or "open").strip().lower()

def _has_joined(user_hash):
    """True if this user is already in the guild for gate purposes: either
    they joined with a code, or they already registered a username (so
    turning the gate on never locks existing members out)."""
    if not user_hash:
        return False
    with db_lock:
        if db.execute("SELECT 1 FROM guild_joins WHERE user_hash=?",
                      (user_hash,)).fetchone():
            return True
        row = db.execute("SELECT username FROM users WHERE dest_hash=?",
                         (user_hash,)).fetchone()
    return bool(row and row["username"])

def _new_invite_code():
    """Short unambiguous code (Crockford-ish base32, no I/O/0/1)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = os.urandom(8)
    return "".join(alphabet[b % len(alphabet)] for b in raw)

@api_route("/api/v1/join")
@wrap_json_api
def api_v1_join(viewer, data, link_id):
    """Join gate (U6b). GET (no code) reports the state; POST {code} tries
    to join with an invite code. The policy is the `registration` server
    setting: 'open' (default — everybody may register) or 'code' (a valid
    invite code is required to register)."""
    policy = _registration_policy()
    if policy not in ("open", "code"):
        policy = "open"
    required = (policy == "code")
    joined = _has_joined(viewer)
    code = (data.get("code") or "").strip().upper()
    if not code:
        return {"ok": 1, "required": required, "joined": joined,
                "policy": policy}
    if not required:
        # open guild: joining is implicit, but still record it so the state
        # is consistent if the policy is tightened later. A channel-targeted
        # code still resolves its landing channel in an open guild.
        channel_id = None
        with db_lock:
            db.execute("INSERT OR IGNORE INTO guild_joins (user_hash, joined_at, "
                       "code) VALUES (?, ?, ?)", (viewer, now(), code))
            db.commit()
            row = db.execute("SELECT channel_id FROM invites WHERE code=?",
                             (code,)).fetchone()
            channel_id = (row["channel_id"] or None) if row else None
        return {"ok": 1, "required": False, "joined": True, "policy": policy,
                "channel_id": channel_id}
    if not re.match(r"^[A-Z0-9]{6,16}$", code):
        return {"ok": 0, "error": "invalid code", "code": 400}
    with db_lock:
        row = db.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
        if not row:
            return {"ok": 0, "error": "invalid code", "code": 403}
        if row["expires_at"] and now() > row["expires_at"]:
            return {"ok": 0, "error": "code expired", "code": 403}
        if row["max_uses"] and row["uses"] >= row["max_uses"]:
            return {"ok": 0, "error": "code exhausted", "code": 403}
        db.execute("UPDATE invites SET uses = uses + 1 WHERE code=?", (code,))
        db.execute("INSERT OR IGNORE INTO guild_joins (user_hash, joined_at, "
                   "code) VALUES (?, ?, ?)", (viewer, now(), code))
        db.commit()
    _audit(viewer, "join", None, "code=%s" % code)
    with db_lock:
        row = db.execute("SELECT channel_id FROM invites WHERE code=?",
                         (code,)).fetchone()
    return {"ok": 1, "required": True, "joined": True, "policy": policy,
            "channel_id": (row["channel_id"] or None) if row else None}

@api_route("/api/v1/invites")
@wrap_json_api
def api_v1_invites(viewer, data, link_id):
    """Invite codes (admin only). {op: list|create|revoke, code?, label?,
    max_uses?, expires_in?} — max_uses 0 means unlimited, expires_in 0
    means never expires."""
    denied = _require_admin(viewer)
    if denied:
        return denied
    op = (data.get("op") or "list").lower()
    if op == "list":
        with db_lock:
            rows = db.execute("SELECT * FROM invites ORDER BY created_at DESC"
                              ).fetchall()
        return {"ok": 1, "invites": [
            {"code": r["code"], "label": r["label"], "created_at": r["created_at"],
             "expires_at": r["expires_at"], "max_uses": r["max_uses"],
             "uses": r["uses"], "created_by": r["created_by"],
             "channel_id": r["channel_id"] or None} for r in rows]}
    if op == "create":
        try:
            max_uses = int(data.get("max_uses", 0))
            expires_in = int(data.get("expires_in", 0))
        except (TypeError, ValueError):
            return {"ok": 0, "error": "max_uses/expires_in must be integers",
                    "code": 400}
        if max_uses < 0 or max_uses > 10000:
            return {"ok": 0, "error": "max_uses must be 0-10000", "code": 400}
        if expires_in < 0 or expires_in > 60 * 60 * 24 * 30:
            return {"ok": 0, "error": "expires_in must be 0-2592000", "code": 400}
        label = str(data.get("label") or "")[:64]
        # optional channel target: the joiner is told which channel to open
        channel = data.get("channel")
        if channel:
            with db_lock:
                if not db.execute("SELECT 1 FROM channels WHERE id=?",
                                  (channel,)).fetchone():
                    return {"ok": 0, "error": "unknown channel", "code": 400}
        code = _new_invite_code()
        with db_lock:
            if db.execute("SELECT 1 FROM invites WHERE code=?", (code,)).fetchone():
                code = _new_invite_code()
            expires_at = (now() + expires_in) if expires_in else 0
            db.execute("INSERT INTO invites (code, label, created_at, "
                       "expires_at, max_uses, uses, created_by, channel_id) "
                       "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                       (code, label, now(), expires_at, max_uses, viewer,
                        channel or None))
            db.commit()
        _audit(viewer, "invite.create", code, label)
        return {"ok": 1, "code": code, "label": label, "max_uses": max_uses,
                "expires_at": expires_at, "channel_id": channel or None}
    if op == "revoke":
        code = (data.get("code") or "").strip().upper()
        if not re.match(r"^[A-Z0-9]{6,16}$", code):
            return {"ok": 0, "error": "invalid code", "code": 400}
        with db_lock:
            cur = db.execute("DELETE FROM invites WHERE code=?", (code,))
            db.commit()
        if not cur.rowcount:
            return {"ok": 0, "error": "unknown code", "code": 404}
        _audit(viewer, "invite.revoke", code)
        return {"ok": 1, "code": code}
    return {"ok": 0, "error": "op must be list|create|revoke", "code": 400}

def _delete_channel_txn(cid, viewer):
    """Shared channel deletion (API + slash command). Returns
    (error_or_None, name_or_None)."""
    if not has_perm(viewer, cid, PERMS["MANAGE_CHANNEL"]):
        return "manage channel permission required", None
    with db_lock:
        row = db.execute("SELECT id, name FROM channels WHERE id=?", (cid,)).fetchone()
        if not row:
            return "unknown channel", None
        n = db.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
        if n <= 1:
            return "cannot delete the last channel", None
        name = row["name"]
        msg_ids = "SELECT msg_id FROM messages WHERE channel_id=?"
        db.execute("DELETE FROM attachments WHERE msg_id IN (%s)" % msg_ids, (cid,))
        db.execute("DELETE FROM reactions WHERE msg_id IN (%s)" % msg_ids, (cid,))
        db.execute("DELETE FROM messages WHERE channel_id=?", (cid,))
        db.execute("DELETE FROM receipts WHERE channel_id=?", (cid,))
        db.execute("DELETE FROM members WHERE channel_id=?", (cid,))
        db.execute("DELETE FROM channel_state WHERE channel_id=?", (cid,))
        db.execute("DELETE FROM channels WHERE id=?", (cid,))
        db.commit()
    with TERM_SESSIONS_LOCK:
        for sid in [s for s, t in TERM_SESSIONS.items() if t["channel"] == cid]:
            TERM_SESSIONS.pop(sid, None)
    cdir = os.path.join(_site_root(), cid)
    if os.path.isdir(cdir):
        shutil.rmtree(cdir, ignore_errors=True)
        # NOTE: per-file /file/sites/<cid>/... request handlers stay registered
        # on the destination (RNS 1.1.3 has no deregister); they now 404.
    _emit_event(cid, "channel_deleted",
                {"channel": cid, "name": name}, guild_wide=True)
    _audit(viewer, "channel.delete", cid, name)
    return None, name

@api_route("/api/v1/channels/delete")
@wrap_json_api
def api_v1_channels_delete(viewer, data, link_id):
    """Hard-delete a channel (moderator-gated; tightened to ADMIN in U5):
    messages, attachments rows, reactions, receipts, members, state — and
    site resources on disk. The last remaining channel cannot be deleted.
    Subscribers get a guild-wide channel_deleted event."""
    err, name = _delete_channel_txn(data.get("channel"), viewer)
    if err:
        return {"ok": 0, "error": err,
                "code": 403 if "permission required" in err or "moderator" in err
                else (404 if "unknown" in err else 400)}
    return {"ok": 1, "channel": data.get("channel"), "name": name}

@api_route("/api/v1/command")
@wrap_json_api
def api_v1_command(viewer, data, link_id):
    """Slash-command endpoint: runs the shared console dispatcher and returns
    the output ephemerally to the requester — nothing is posted to a channel."""
    line = data.get("input")
    if not isinstance(line, str) or not line.strip():
        return {"ok": 0, "error": "input required", "code": 400}
    if len(line) > TERM_MAX_INPUT:
        return {"ok": 0, "error": "input too long", "code": 400}
    if not rate_ok(viewer, "_term", *TERM_IN_RATE):
        return {"ok": 0, "error": "rate limited", "code": 429}
    # same input sanitisation as the term console: strip ANSI sequences and
    # control characters before anything reaches the dispatcher or ledger
    res = _dispatch_command(viewer, _term_sanitize(line.lstrip("/")))
    return {"ok": 1, "output": _term_sanitize(res["out"])[:4000],
            "clear": res["clear"], "quit": res["quit"]}

# ---- roles (U5): role CRUD, assignment, channel overwrites -------------------------

def _require_manage_roles(viewer):
    if not has_role_perm(viewer, PERMS["MANAGE_ROLES"] | PERMS["ADMIN"]):
        return {"ok": 0, "error": "manage roles permission required", "code": 403}
    return None

def _role_row(r):
    with db_lock:
        members = db.execute("SELECT user_hash FROM user_roles WHERE role_id=? "
                             "ORDER BY user_hash LIMIT 64", (r["role_id"],)).fetchall()
    return {"role_id": r["role_id"], "name": r["name"], "color": r["color"],
            "position": r["position"], "perms": r["perms"],
            "members": [m["user_hash"] for m in members]}

@api_route("/api/v1/roles/list")
@wrap_json_api
def api_v1_roles_list(viewer, data, link_id):
    with db_lock:
        rows = db.execute("SELECT * FROM roles ORDER BY position DESC, name").fetchall()
    return {"ok": 1, "roles": [_role_row(r) for r in rows]}

@api_route("/api/v1/roles/create")
@wrap_json_api
def api_v1_roles_create(viewer, data, link_id):
    denied = _require_manage_roles(viewer)
    if denied:
        return denied
    name = (data.get("name") or "").strip()
    if not re.match(r"^[A-Za-z0-9_\- ]{1,32}$", name):
        return {"ok": 0, "error": "invalid role name", "code": 400}
    perms = data.get("perms", 0)
    if not isinstance(perms, int) or perms < 0 or perms > PERM_ALL:
        return {"ok": 0, "error": "perms must be a bitmask 0-63", "code": 400}
    color = (data.get("color") or "").strip().upper()
    if color and not re.match(r"^[0-9A-F]{3}$", color):
        return {"ok": 0, "error": "color must be 3-hex", "code": 400}
    position = data.get("position", 0)
    try:
        position = max(0, min(int(position), 1000))
    except (TypeError, ValueError):
        position = 0
    with db_lock:
        if db.execute("SELECT 1 FROM roles WHERE name=? COLLATE NOCASE",
                      (name,)).fetchone():
            return {"ok": 0, "error": "role exists", "code": 409}
        cur = db.execute("INSERT INTO roles (name, color, position, perms) "
                         "VALUES (?, ?, ?, ?)", (name, color or None, position, perms))
        db.commit()
        role_id = cur.lastrowid
    return {"ok": 1, "role": {"role_id": role_id, "name": name, "perms": perms,
                              "color": color or None, "position": position}}

@api_route("/api/v1/messages/edits")
@wrap_json_api
def api_v1_message_edits(viewer, data, link_id):
    """Prior versions are visible only to viewers of this channel."""
    msg_id = data.get("msg")
    with db_lock:
        row = db.execute("SELECT channel_id, deleted_at FROM messages WHERE msg_id=?",
                         (msg_id,)).fetchone()
        if row is None or row["deleted_at"]:
            return {"ok": 0, "error": "message unavailable", "code": 404}
        if not has_perm(viewer, row["channel_id"], PERMS["VIEW"]):
            return {"ok": 0, "error": "view permission required", "code": 403}
        edits = db.execute("SELECT edited_at, prior_content FROM message_edits "
                           "WHERE msg_id=? ORDER BY edited_at DESC LIMIT 50",
                           (msg_id,)).fetchall()
    return {"ok": 1, "edits": [dict(r) for r in edits]}


@api_route("/api/v1/account/export")
@wrap_json_api
def api_v1_account_export(viewer, data, link_id):
    """Paginate this identity's own posts, including edits and tombstones."""
    try:
        after = max(0, int(data.get("after", 0)))
        limit = max(1, min(100, int(data.get("limit", 100))))
    except (TypeError, ValueError):
        return {"ok": 0, "error": "invalid cursor", "code": 400}
    with db_lock:
        rows = db.execute("SELECT * FROM messages WHERE author_hash=? AND id>? "
                          "ORDER BY id LIMIT ?", (viewer, after, limit)).fetchall()
        messages = []
        for row in rows:
            msg = msg_row_json(row)
            msg["edits"] = [dict(e) for e in db.execute(
                "SELECT edited_at, prior_content FROM message_edits WHERE msg_id=? "
                "ORDER BY edited_at ASC", (row["msg_id"],)).fetchall()]
            messages.append(msg)
    return {"ok": 1, "messages": messages,
            "next_after": rows[-1]["id"] if len(rows) == limit else None,
            "scope": "own guild posts and attachment metadata; no attachment bytes"}


@api_route("/api/v1/account/delete-posts")
@wrap_json_api
def api_v1_account_delete_posts(viewer, data, link_id):
    """Erase this identity's ledger text and references; retain tombstones."""
    if data.get("confirm") != "DELETE MY POSTS":
        return {"ok": 0, "error": "confirmation required", "code": 400}
    with db_lock:
        rows = db.execute("SELECT msg_id, channel_id FROM messages WHERE "
                          "author_hash=? AND deleted_at IS NULL", (viewer,)).fetchall()
        all_ids = [r["msg_id"] for r in db.execute(
            "SELECT msg_id FROM messages WHERE author_hash=?", (viewer,)).fetchall()]
        paths = set()
        for msg_id in all_ids:
            paths.update(r["path"] for r in db.execute(
                "SELECT path FROM attachments WHERE msg_id=?", (msg_id,)).fetchall())
            db.execute("DELETE FROM attachments WHERE msg_id=?", (msg_id,))
            db.execute("DELETE FROM reactions WHERE msg_id=?", (msg_id,))
            db.execute("DELETE FROM message_edits WHERE msg_id=?", (msg_id,))
        deleted_at = now()
        db.execute("UPDATE messages SET content='', deleted_at=coalesce(deleted_at, ?) "
                   "WHERE author_hash=?", (deleted_at, viewer))
        channels = {r["channel_id"] for r in rows}
        for cid in channels:
            bump_seq(cid)
        db.commit()
        orphaned = [path for path in paths if not db.execute(
            "SELECT 1 FROM attachments WHERE path=? LIMIT 1", (path,)).fetchone()]
        orphaned_blobs = []
        for path in orphaned:
            if not re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{1,8}", path):
                continue
            digest = path.split(".", 1)[0]
            record = db.execute("SELECT path FROM blobs WHERE id=? AND by=?",
                                (digest, viewer)).fetchone()
            if record and not db.execute(
                    "SELECT 1 FROM attachments WHERE path LIKE ? LIMIT 1",
                    (digest + ".%",)).fetchone():
                orphaned_blobs.append(record["path"])
                db.execute("DELETE FROM blobs WHERE id=?", (digest,))
        db.commit()
        # Clear the live WAL and compact historical page versions where possible.
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
        except sqlite3.Error as exc:
            RNS.log("post compaction deferred: %s" % exc, RNS.LOG_WARNING)
    for path in orphaned:
        if re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{1,8}", path):
            for directory, name in (("attachments", path),
                                    ("previews", path + ".jpg")):
                try:
                    os.unlink(os.path.join(STORAGE, directory, name))
                except OSError:
                    pass
    for path in orphaned_blobs:
        if re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{1,8}", path):
            try:
                os.unlink(os.path.join(_blob_dir(), path))
            except OSError:
                pass
    for cid in channels:
        _emit_event(cid, "posts_purged", {"author_hash": viewer})
    _audit(viewer, "account.delete_posts", None, "posts=%d" % len(all_ids))
    return {"ok": 1, "deleted": len(all_ids), "deleted_at": deleted_at,
            "scope": "ledger content, edit history, reactions and attachment references; "
                     "audit metadata and external backups may remain"}


@api_route("/api/v1/account/leave")
@wrap_json_api
def api_v1_account_leave(viewer, data, link_id):
    """Release registration and roles without silently deleting messages."""
    if data.get("confirm") != "LEAVE GUILD":
        return {"ok": 0, "error": "confirmation required", "code": 400}
    with db_lock:
        avatar = db.execute("SELECT avatar_path FROM users WHERE dest_hash=?",
                            (viewer,)).fetchone()
        db.execute("DELETE FROM guild_joins WHERE user_hash=?", (viewer,))
        db.execute("DELETE FROM user_roles WHERE user_hash=?", (viewer,))
        db.execute("DELETE FROM receipts WHERE user_hash=?", (viewer,))
        db.execute("DELETE FROM members WHERE member_hash=?", (viewer,))
        db.execute("DELETE FROM users WHERE dest_hash=?", (viewer,))
        db.commit()
    with NODE_REGISTRY_LOCK:
        for node, record in list(NODE_REGISTRY.items()):
            if record.get("user_hash") == viewer:
                del NODE_REGISTRY[node]
    if viewer in MODERATORS:
        MODERATORS.discard(viewer)
        _write_hash_file(os.path.join(STORAGE, "moderators.txt"), sorted(MODERATORS))
    if viewer in ADMIN_USERS:
        ADMIN_USERS.discard(viewer)
        _write_hash_file(os.path.join(STORAGE, "admin.txt"), sorted(ADMIN_USERS))
    if avatar and avatar["avatar_path"] == viewer + ".img":
        try:
            os.unlink(os.path.join(STORAGE, "avatars", viewer + ".img"))
        except OSError:
            pass
    _audit(viewer, "account.leave")
    return {"ok": 1, "scope": "guild membership, roles and profile removed; posts remain "
            "unless deleted separately"}


@api_route("/api/v1/catchup")
@wrap_json_api
def api_v1_catchup(viewer, data, link_id):
    """Small per-channel summary after the member's own read watermark."""
    with db_lock:
        channels = db.execute("SELECT id, name FROM channels ORDER BY name").fetchall()
        result = []
        for channel in channels:
            cid = channel["id"]
            if not has_perm(viewer, cid, PERMS["VIEW"]):
                continue
            receipt = db.execute("SELECT last_id FROM receipts WHERE channel_id=? "
                                 "AND user_hash=?", (cid, viewer)).fetchone()
            if receipt is None:
                continue
            last_id = receipt["last_id"]
            count = db.execute("SELECT count(*) FROM messages WHERE channel_id=? "
                               "AND id>? AND author_hash!=? AND deleted_at IS NULL",
                               (cid, last_id, viewer)).fetchone()[0]
            if not count:
                continue
            samples = db.execute("SELECT author_hash, content, ts FROM messages "
                                 "WHERE channel_id=? AND id>? AND author_hash!=? "
                                 "AND deleted_at IS NULL ORDER BY id DESC LIMIT 3",
                                 (cid, last_id, viewer)).fetchall()
            result.append({"channel": cid, "name": channel["name"], "count": count,
                           "recent": [{"username": get_username(r["author_hash"]),
                                       "content": r["content"][:100], "ts": r["ts"]}
                                      for r in reversed(samples)]})
    return {"ok": 1, "channels": result[:100]}


@api_route("/api/v1/moderation/public")
@wrap_json_api
def api_v1_moderation_public(viewer, data, link_id):
    """Read-only member log of moderation, omitting reasons and private config."""
    if not _has_joined(viewer):
        return {"ok": 0, "error": "guild membership required", "code": 403}
    try:
        limit = max(1, min(int(data.get("limit", 50)), 100))
    except (TypeError, ValueError):
        limit = 50
    actions = ("mod.muted", "mod.unmuted", "mod.banned", "mod.unbanned",
               "mods.add", "mods.remove", "admins.add", "admins.remove",
               "channel.delete", "role.assign", "role.unassign")
    with db_lock:
        rows = db.execute(
            "SELECT ts, actor, action, target FROM audit_log WHERE action IN "
            "(%s) ORDER BY id DESC LIMIT ?" % ",".join("?" for _ in actions),
            (*actions, limit)).fetchall()
    return {"ok": 1, "entries": [
        {"ts": r["ts"], "actor": r["actor"], "action": r["action"],
         "target": r["target"]} for r in rows]}


@api_route("/api/v1/roles/delete")
@wrap_json_api
def api_v1_roles_delete(viewer, data, link_id):
    denied = _require_manage_roles(viewer)
    if denied:
        return denied
    role_id = data.get("role_id")
    with db_lock:
        row = db.execute("SELECT name FROM roles WHERE role_id=?", (role_id,)).fetchone()
        if not row:
            return {"ok": 0, "error": "unknown role", "code": 404}
        db.execute("DELETE FROM user_roles WHERE role_id=?", (role_id,))
        db.execute("DELETE FROM channel_overwrites WHERE role_id=?", (role_id,))
        db.execute("DELETE FROM roles WHERE role_id=?", (role_id,))
        db.commit()
    _audit(viewer, "role.delete", None, "role_id=%s" % role_id)
    return {"ok": 1, "role_id": role_id, "name": row["name"]}

@api_route("/api/v1/roles/assign")
@wrap_json_api
def api_v1_roles_assign(viewer, data, link_id):
    denied = _require_manage_roles(viewer)
    if denied:
        return denied
    role_id = data.get("role_id")
    target = _resolve_user(data.get("user"))
    if not target:
        return {"ok": 0, "error": "unknown user", "code": 404}
    with db_lock:
        if not db.execute("SELECT 1 FROM roles WHERE role_id=?", (role_id,)).fetchone():
            return {"ok": 0, "error": "unknown role", "code": 404}
        db.execute("INSERT OR IGNORE INTO user_roles (user_hash, role_id) VALUES (?, ?)",
                   (target, role_id))
        db.commit()
    _audit(viewer, "role.assign", target, "role_id=%s" % role_id)
    return {"ok": 1, "role_id": role_id, "user": target}

@api_route("/api/v1/roles/unassign")
@wrap_json_api
def api_v1_roles_unassign(viewer, data, link_id):
    denied = _require_manage_roles(viewer)
    if denied:
        return denied
    role_id = data.get("role_id")
    target = _resolve_user(data.get("user"))
    if not target:
        return {"ok": 0, "error": "unknown user", "code": 404}
    with db_lock:
        db.execute("DELETE FROM user_roles WHERE user_hash=? AND role_id=?",
                   (target, role_id))
        db.commit()
    _audit(viewer, "role.unassign", target, "role_id=%s" % role_id)
    return {"ok": 1, "role_id": role_id, "user": target}

@api_route("/api/v1/channels/patch")
@wrap_json_api
def api_v1_channels_patch(viewer, data, link_id):
    """Channel settings: topic and permission overwrites. Needs
    MANAGE_CHANNEL (guild-wide) for topic, and for overwrites the caller
    must also carry MANAGE_CHANNEL; overwrites are {role_id: perms} with
    perms null deleting the row (falling back to the default)."""
    cid = data.get("channel")
    with db_lock:
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return {"ok": 0, "error": "unknown channel", "code": 404}
    if not has_perm(viewer, cid, PERMS["MANAGE_CHANNEL"]):
        return {"ok": 0, "error": "manage channel permission required", "code": 403}
    topic = data.get("topic")
    if topic is not None:
        with db_lock:
            db.execute("UPDATE channels SET topic=? WHERE id=?",
                       (str(topic).strip()[:200] or None, cid))
            db.commit()
        _emit_event(cid, "channels_changed", {"channel": channel_name(cid)},
                    guild_wide=True)
    overwrites = data.get("overwrites")
    if overwrites is not None:
        if not isinstance(overwrites, dict):
            return {"ok": 0, "error": "overwrites must be an object", "code": 400}
        for rid, perms in overwrites.items():
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                return {"ok": 0, "error": "role_id must be an integer", "code": 400}
            with db_lock:
                if perms is None:
                    db.execute("DELETE FROM channel_overwrites "
                               "WHERE channel_id=? AND role_id=?", (cid, rid))
                else:
                    if not isinstance(perms, int) or perms < 0 or perms > PERM_ALL:
                        return {"ok": 0, "error": "perms must be a bitmask 0-127",
                                "code": 400}
                    db.execute("INSERT INTO channel_overwrites "
                               "(channel_id, role_id, perms) VALUES (?, ?, ?) "
                               "ON CONFLICT(channel_id, role_id) "
                               "DO UPDATE SET perms=excluded.perms", (cid, rid, perms))
        with db_lock:
            db.commit()
    return {"ok": 1, "channel": cid}

# ----------------------------------------------------------------------------------
# Boot (plan §9 skeleton — every call verified against the installed stack)
# ----------------------------------------------------------------------------------

def _announce_now():
    try:
        # LXMRouter.announce(destination_hash) announces the registered delivery
        # destination with the display-name app_data (LXMRouter.py:297-299)
        router.announce(guild_delivery.hash)
        router.announce_propagation_node()                 # delayed-thread announce
        node_destination.announce(app_data=GUILD_NAME.encode("utf-8"))  # Node.py:announce format
        RNS.log("Announced guild (delivery, propagation, node).", RNS.LOG_NOTICE)
    except Exception as e:
        RNS.log(f"announce error: {e}", RNS.LOG_ERROR)

def announce_all():
    time.sleep(ANNOUNCE_DELAY)
    _announce_now()

def run(argv=None):
    global db, router, guild_delivery, node_destination, guild_identity
    global STORAGE, GUILD_NAME, ANNOUNCE_DELAY

    parser = argparse.ArgumentParser(description="sHades guild server")
    parser.add_argument("--storage", default=DEFAULT_STORAGE)
    parser.add_argument("--name", default=GUILD_NAME)
    parser.add_argument("--announce-delay", type=float, default=LXMF.LXMRouter.NODE_ANNOUNCE_DELAY)
    parser.add_argument("--listen", default=None,
                        help="TCP listen address:port for direct client connections (e.g. 0.0.0.0:29221)")
    parser.add_argument("--rns-configdir", default=None,
                        help="Reticulum config dir (default: <storage>/rns)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose debug logging for attachments and ingest")
    args = parser.parse_args(argv)

    STORAGE = os.path.abspath(args.storage)
    GUILD_NAME = args.name
    ANNOUNCE_DELAY = args.announce_delay
    DEBUG = args.debug
    os.makedirs(STORAGE, exist_ok=True)
    os.makedirs(os.path.join(STORAGE, "attachments"), exist_ok=True)
    os.makedirs(os.path.join(STORAGE, "avatars"), exist_ok=True)
    os.makedirs(os.path.join(STORAGE, "blobs"), exist_ok=True)
    os.makedirs(os.path.join(STORAGE, "sites"), exist_ok=True)

    global _GUILD_BOOT
    _GUILD_BOOT = now()

    db = open_db(os.path.join(STORAGE, "sHades.db"))

    # Reticulum — RNS.initialize() does not exist in 1.1.3; construct the instance.
    # On first run, write our own config: standalone (share_instance = no, so the
    # guild never attaches to some other local instance's socket) + AutoInterface
    # for LAN discoverability. If --listen is given, also add a TCPServerInterface
    # so remote clients can connect directly by IP. If the config exists, it is
    # used untouched unless the listen setting changed.
    rns_configdir = args.rns_configdir or os.path.join(STORAGE, "rns")
    cfgfile = os.path.join(rns_configdir, "config")

    listen_host, listen_port = "0.0.0.0", 29221
    if args.listen:
        parts = args.listen.rsplit(":", 1)
        listen_host = parts[0] if len(parts) == 2 else "0.0.0.0"
        listen_port = int(parts[-1])

    def _config_matches():
        try:
            with open(cfgfile) as f:
                content = f.read()
            if args.listen:
                return ("listen_port = %d" % listen_port) in content and \
                       ("listen_ip = %s" % listen_host) in content
            return "AutoInterface" in content and "TCPServerInterface" not in content
        except Exception:
            return False

    if not os.path.exists(cfgfile) or not _config_matches():
        os.makedirs(rns_configdir, exist_ok=True)
        with open(cfgfile, "w") as f:
            f.write("[reticulum]\n"
                    "  enable_transport = no\n"
                    "  share_instance = no\n\n"
                    "[interfaces]\n")
            if args.listen:
                f.write("  [[TCPServerInterface]]\n"
                        "    type = TCPServerInterface\n"
                        "    enabled = yes\n"
                        "    listen_ip = %s\n"
                        "    listen_port = %d\n" % (listen_host, listen_port))
            else:
                f.write("  [[AutoInterface]]\n"
                        "    type = AutoInterface\n"
                        "    enabled = yes\n")
    reticulum = RNS.Reticulum(configdir=rns_configdir)

    ipath = os.path.join(STORAGE, "identity")
    if os.path.exists(ipath):
        guild_identity = RNS.Identity.from_file(ipath)
    else:
        guild_identity = RNS.Identity()
        guild_identity.to_file(ipath)

    # LXMF router: ingest + propagation (mesh reachability)
    router = LXMF.LXMRouter(identity=guild_identity, storagepath=STORAGE)
    guild_delivery = router.register_delivery_identity(guild_identity, display_name=GUILD_NAME)
    router.register_delivery_callback(on_message)
    router.enable_propagation()

    # The chatroom node destination — what makes the guild browsable
    node_destination = RNS.Destination(
        guild_identity, RNS.Destination.IN, RNS.Destination.SINGLE,
        "nomadnetwork", "node")

    for path, fn in PAGE_ROUTES.items():
        node_destination.register_request_handler(path, response_generator=fn,
                                                  allow=RNS.Destination.ALLOW_ALL)
    for path, fn in API_ROUTES.items():
        node_destination.register_request_handler(path, response_generator=fn,
                                                  allow=RNS.Destination.ALLOW_ALL)

    # re-register attachment files from previous runs
    with db_lock:
        paths = [r["path"] for r in
                 db.execute("SELECT DISTINCT path FROM attachments").fetchall()]
    for fname in paths:
        node_destination.register_request_handler(
            "/file/attachments/" + fname, serve_attachment_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)

    # re-register v1 blobs and site resources from previous runs (one handler
    # per file, the nomadnet Node.py register_files pattern). Also legacy
    # /file/attachments/ files: handlers live only in RAM from store time,
    # so a restart would otherwise strand every pre-restart attachment.
    # Directories (e.g. the chunked-upload staging dir) are not files and
    # get no handlers.
    bdir = os.path.join(STORAGE, "blobs")
    for fname in os.listdir(bdir):
        if os.path.isdir(os.path.join(bdir, fname)):
            continue
        node_destination.register_request_handler(
            "/file/blobs/" + fname, serve_blob_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
    adir = os.path.join(STORAGE, "attachments")
    if os.path.isdir(adir):
        for fname in os.listdir(adir):
            if not re.match(r"^[0-9a-f]{32}\.[a-z0-9]{1,8}$", fname):
                continue
            node_destination.register_request_handler(
                "/file/attachments/" + fname, serve_attachment_file,
                allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
    sroot = os.path.join(STORAGE, "sites")
    for cid in os.listdir(sroot):
        cdir = os.path.join(sroot, cid)
        if not os.path.isdir(cdir):
            continue
        for root, _, files in os.walk(cdir):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), cdir)
                rel = rel.replace(os.sep, "/")
                node_destination.register_request_handler(
                    f"/file/sites/{cid}/{rel}", serve_site_file,
                    allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)

    ensure_channel("general")

    # Seed the moderator set from moderators.txt (boot-time source of truth).
    # Without this, MODERATORS stays empty and ban/mod paths never authorize.
    load_moderators(os.path.join(STORAGE, "moderators.txt"))
    # Admin whitelist (same format) — also drives the Admin role.
    load_admins(os.path.join(STORAGE, "admin.txt"))
    # Role seeding (U5): admin.txt -> Admin role, moderators.txt -> Moderator.
    load_role_seeds(STORAGE)

    node_hash     = hexstr(node_destination.hash)
    delivery_hash = hexstr(guild_delivery.hash)
    prop_hash     = hexstr(router.propagation_destination.hash)
    with db_lock:
        db.execute("INSERT OR REPLACE INTO guilds (id, name, node_hash, delivery_hash, prop_hash) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (GUILD_ID, GUILD_NAME, node_hash, delivery_hash, prop_hash))
        db.commit()

    info = {"name": GUILD_NAME, "node_hash": node_hash,
            "delivery_hash": delivery_hash, "prop_hash": prop_hash,
            "storage": STORAGE}
    with open(os.path.join(STORAGE, "guild_info.json"), "w") as f:
        json.dump(info, f, indent=2)

    RNS.log(f"sHades guild '{GUILD_NAME}' active", RNS.LOG_NOTICE)
    RNS.log(f"  browse node : {node_hash}  (NomadNet: add as node)", RNS.LOG_NOTICE)
    RNS.log(f"  post via LXM: {delivery_hash}  (#channel first token)", RNS.LOG_NOTICE)
    RNS.log(f"  propagation : {prop_hash}", RNS.LOG_NOTICE)

    # Reap clients that miss two consecutive 4s announcements (8 s stale).
    threading.Thread(target=_node_reaper, daemon=True).start()
    global _BOOT_ARGV
    # tay's bare boot (2026-11-20): `python sHades.py` passes argv=None
    # here and list(None) crashed the hub right after the announce
    # banner on every bare boot since 0.36.21 (all CI boots passed
    # argv, so the bare path had no receipt). A bare boot restarts
    # bare instead of crashing.
    _BOOT_ARGV = list(argv) if argv is not None else []
    _update_maybe_tick(force=True)
    threading.Thread(target=announce_all, daemon=True).start()
    try:
        while True:
            time.sleep(10)
            _announce_now()   # re-announce frequently so clients can discover us
    except KeyboardInterrupt:
        RNS.log("Shutting down.", RNS.LOG_NOTICE)

if __name__ == "__main__":
    # tay's boot (0.36.24 fold-4): the flags live in sys.argv, not in
    # the argv parameter - list(None) crashed AFTER the banner on
    # every boot (bare or flagged) since P11 chain 2, and a guard
    # alone would restart a flagged hub flagless. The entry feeds the
    # real CLI here so _BOOT_ARGV carries the flags through a restart.
    try:
        _update_boot_guard()  # P11f: an interrupted swap never serves
    except Exception as e:
        RNS.log("boot guard: %s" % e, RNS.LOG_ERROR)
    run(sys.argv[1:])
