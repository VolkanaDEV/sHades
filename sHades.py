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
import json
import time
import base64
import argparse
import sqlite3
import hashlib
import secrets
import threading
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
MAX_ATTACHMENT_SZ = 32 * 1024 * 1024   # 32 MB per attachment
MAX_AVATAR_SZ     = 64 * 1024
RATE_MAX_POSTS    = 10            # per sender per channel
RATE_WINDOW_S     = 60
PAGE_SIZE         = 50
TOKEN_TTL_S       = 900
ATTACHMENT_EXT    = {"png", "jpg", "jpeg", "gif", "webp", "mp3", "ogg", "wav",
                     "flac", "mp4", "webm", "pdf", "txt"}

# Channel kinds: chat, a NomadNet micron "site", or a terminal stream.
CHANNEL_KINDS     = ("chat", "site", "term")

# Blobs + site resources (JSON API v1 phases 3-4): content-addressed media,
# uploaded once and referenced by id. Chunked get keeps memory bounded.
BLOB_CHUNK_DEFAULT = 256 * 1024   # /api/v1/blobs/get default slice
BLOB_CHUNK_MAX     = 1024 * 1024  # ... and hard cap per request
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
NODE_REGISTRY = {}                # node_hash -> {address, port, last_seen}
NODE_REGISTRY_LOCK = threading.Lock()
NODE_ANNOUNCE_INTERVAL = 4.0      # clients re-register (ping) this often
NODE_MISS_LIMIT = 2               # miss this many consecutive announcements → drop
NODE_STALE_S = NODE_ANNOUNCE_INTERVAL * NODE_MISS_LIMIT   # 8.0 s

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

# ----------------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    id TEXT PRIMARY KEY, name TEXT, node_hash TEXT, delivery_hash TEXT, prop_hash TEXT);
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, created REAL,
    kind TEXT NOT NULL DEFAULT 'chat', manifest TEXT NOT NULL DEFAULT '{}');
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
CREATE TABLE IF NOT EXISTS attachments (
    msg_id TEXT, idx INTEGER, name TEXT, mime TEXT, size INTEGER, path TEXT,
    PRIMARY KEY (msg_id, idx));
CREATE TABLE IF NOT EXISTS banned (
    dest_hash TEXT PRIMARY KEY, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS muted (
    dest_hash TEXT PRIMARY KEY, reason TEXT, at REAL, by TEXT);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
"""

def _migrate_schema(conn):
    """Additive migrations for databases written by earlier versions.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so a column added
    to SCHEMA after a guild has already booted has to be added explicitly."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
    for col, decl in (("kind", "TEXT NOT NULL DEFAULT 'chat'"), ("manifest", "TEXT")):
        if col not in have:
            conn.execute("ALTER TABLE channels ADD COLUMN %s %s" % (col, decl))

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

def rate_ok(sender, channel_id):
    key = (sender, channel_id)
    with _rate_lock:
        w = _rate_windows[key]
        cutoff = now() - RATE_WINDOW_S
        while w and w[0] < cutoff:
            w.popleft()
        if len(w) >= RATE_MAX_POSTS:
            return False
        w.append(now())
        return True

def is_banned(sender):
    with db_lock:
        return db.execute("SELECT 1 FROM banned WHERE dest_hash=?", (sender,)).fetchone() is not None

def is_moderator(user):
    return user is not None and user in MODERATORS

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

def _reap_stale_nodes():
    """Drop NODE_REGISTRY entries whose last_seen is older than NODE_STALE_S,
    i.e. the client missed NODE_MISS_LIMIT consecutive 4s announcements.

    The presence event fires when a user loses their LAST node — comparing
    online-set snapshots around the deletion can't see the transition: by the
    time the reaper runs, the entries it drops are already too stale to be
    'online' from the snapshot's point of view."""
    cutoff = now() - NODE_STALE_S
    stale = []
    gone_users = set()
    with NODE_REGISTRY_LOCK:
        stale = [h for h, e in list(NODE_REGISTRY.items())
                 if e["last_seen"] < cutoff]
        if stale:
            gone_users = {NODE_REGISTRY[h].get("user_hash") for h in stale}
            for h in stale:
                del NODE_REGISTRY[h]
                RNS.log("dropped stale node %s (missed %d consecutive announcements)"
                        % (h[:12], NODE_MISS_LIMIT), RNS.LOG_NOTICE)
            gone_users -= {e.get("user_hash") for e in NODE_REGISTRY.values()}
    if gone_users:
        # users just went offline — guild-wide event for every streamer
        _emit_event(None, "presence", {"users": _presence_payload()}, guild_wide=True)
    return stale

def _node_reaper():
    while True:
        time.sleep(1.0)
        try:
            _reap_stale_nodes()
        except Exception as e:
            RNS.log("node reaper error: %s" % e, RNS.LOG_ERROR)

def online_users():
    """The active user list, derived from the client ping registry.

    Presence is *read out of* NODE_REGISTRY rather than kept in a second table:
    every client re-registers every NODE_ANNOUNCE_INTERVAL seconds (its IRC-style
    ping), and _node_reaper drops an entry that misses NODE_MISS_LIMIT consecutive
    pings. Deriving presence from that single structure means it prunes for free
    and can never disagree with the reaper.

    Keyed by canonical user hash (the lxmf.delivery destination hash) so the
    result joins directly against messages.author_hash / members.member_hash.
    A user with several registered nodes keeps only their most recent entry.

    Returns {user_hash: {user_hash, username, node_hash, last_seen}}.
    """
    cutoff = now() - NODE_STALE_S
    with NODE_REGISTRY_LOCK:
        entries = [(e.get("user_hash"), h, e.get("last_seen", 0.0))
                   for h, e in NODE_REGISTRY.items()
                   if e.get("last_seen", 0.0) >= cutoff and e.get("user_hash")]
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

def store_attachment(name, mime, data):
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    if len(data) > MAX_ATTACHMENT_SZ:
        return None
    digest = hashlib.sha256(data).hexdigest()[:32]
    fname = f"{digest}.{safe_ext(name, mime)}"
    adir = os.path.join(STORAGE, "attachments")
    os.makedirs(adir, exist_ok=True)
    path = os.path.join(adir, fname)
    try:
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
    except Exception as e:
        RNS.log(f"store_attachment write failed for {fname}: {e}", RNS.LOG_ERROR)
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

def ingest(channel_id, author_hash, content, ts=None, attachments=None):
    dlog(f"ingest: channel={channel_id[:12]} author={author_hash[:12]} content_len={len(content or '')} attachments={len(attachments or [])}")
    if is_banned(author_hash):
        return None
    if is_muted(author_hash):
        return None  # muted: post silently dropped
    content = (content or "").strip()
    if not content and not attachments:
        dlog(f"ingest: empty content and no attachments, rejecting")
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
            fname = store_attachment(str(name)[:128], str(mime)[:64], blob)
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
        "attachments": att_meta,
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
    return {
        "id": row["id"], "msg_id": row["msg_id"], "channel_id": row["channel_id"],
        "ts": row["ts"], "author_hash": row["author_hash"],
        "username": get_username(row["author_hash"]),
        "content": "" if row["deleted_at"] else row["content"],
        "edited_at": row["edited_at"], "deleted_at": row["deleted_at"],
        "attachments": [{"name": a["name"], "mime": a["mime"], "size": a["size"],
                         "path": "/file/attachments/" + a["path"]} for a in atts],
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
    out = [page_header()]
    out.append("> #" + esc_micron_text(chan["name"]) + "\n")
    out.append("-\n")
    # live subframe: auto-refresh partial (verified partial grammar + parse_url)
    out.append("`{:/page/sHades/messages.mu`8`channel=" + chan["id"] + "}\n")
    out.append("-\n")
    viewer = viewer_of(remote_identity)
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
        db.execute("UPDATE messages SET content=?, edited_at=? WHERE msg_id=?",
                   (content.strip()[:MAX_CONTENT_LEN], now(), msg_id))
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
    if not row or not can_delete(viewer, row):
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
    online = online_users()
    out = [page_header()]
    out.append("> Members of #" + esc_micron_text(chan["name"]) + "\n-\n")
    # Presence, straight from the client ping registry: everyone still pinging.
    # Stale clients are already gone — the reaper drops a node that misses
    # NODE_MISS_LIMIT consecutive pings, so this list prunes itself.
    out.append("`!Online now`!  `F555" + str(len(online)) +
               " pinging (guild-wide)`f\n")
    if online:
        for u in sorted(online.values(), key=lambda u: u["username"].lower()):
            # "+" and not "*"/"_": those are Micron formatting controls, so
            # inside a formatting run they toggle style instead of printing.
            out.append("`F2a2+`f " + username_fragment(u["user_hash"]))
            if u["user_hash"] == viewer:
                out.append(" `F555(you)`f")
            out.append("\n")
    else:
        out.append("`F555(nobody pinging)`f\n")
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
            db.execute("UPDATE messages SET content=?, edited_at=? WHERE msg_id=?",
                       (new_content, edited_at, msg_id))
            event = {"msg_id": msg_id, "content": new_content,
                     "edited_at": edited_at, "deleted_at": None}
        else:
            deleted_at = now()
            db.execute("UPDATE messages SET deleted_at=? WHERE msg_id=?", (deleted_at, msg_id))
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
    return msgpack.packb({"ok": 1})

@api_route("/api/register_node")
@wrap_api
def api_register_node(viewer, data):
    """Client registers its node address with the guild index.

    This call is also the presence ping: each client repeats it every
    NODE_ANNOUNCE_INTERVAL seconds and the guild treats the ping as "still
    here". data: {node_hash, address, port, identity_hash?}

    The user identity is taken from the *identified link* the request arrived
    on (viewer = the lxmf.delivery hash derived from the requester's identity),
    NOT from anything in the payload. A client-supplied identity could be
    forged — one client could report a victim as online, or claim a name that
    isn't theirs — whereas the link identity is bound by the RNS handshake.
    """
    node_hash = (data.get("node_hash") or "").strip().lower()
    address = (data.get("address") or "").strip()
    port = data.get("port")
    if not node_hash or not re.match(r"^[0-9a-f]{32}$", node_hash):
        return api_error("invalid node_hash", 400)
    if not address:
        return api_error("address required", 400)
    try:
        port = int(port)
    except (TypeError, ValueError):
        return api_error("invalid port", 400)
    before = set(online_users())
    with NODE_REGISTRY_LOCK:
        NODE_REGISTRY[node_hash] = {
            "address": address,
            "port": port,
            "last_seen": now(),
            "user_hash": viewer,
        }
    # A first ping (or the last ping before a reap) changes the online set —
    # streamers get a guild-wide presence event so member lists update live.
    _emit_presence_if_changed(before)
    return msgpack.packb({"ok": 1})

@api_route("/api/lookup_node")
@wrap_api
def api_lookup_node(viewer, data):
    """Look up a node's address by node_hash.
    data: {node_hash}
    Returns: {ok, address, port} or {ok: 0, error: "not found"}
    """
    node_hash = (data.get("node_hash") or "").strip().lower()
    if not node_hash:
        return api_error("node_hash required", 400)
    with NODE_REGISTRY_LOCK:
        entry = NODE_REGISTRY.get(node_hash)
    if not entry:
        return msgpack.packb({"ok": 0, "error": "node not found"})
    return msgpack.packb({"ok": 1, "address": entry["address"], "port": entry["port"]})

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
    """The active user list (who is currently pinging), for member presence.

    Strictly ping-driven: the requesting client is NOT added just for asking —
    being able to make a request is not the same as being present. A client
    whose ping loop has died drops off this list even if it is still polling,
    which is the point of ping-based presence. Its first ping lands at connect,
    so a healthy client always sees itself.

    Stale users are *absent*, not flagged: the reaper has already dropped them
    from the ping registry, so a client that stops pinging disappears from this
    list within at most ttl seconds.

    Returns {ok, users: [{user_hash, username, node_hash, last_seen}],
             interval, ttl, now}.
    """
    users = online_users()
    return msgpack.packb({
        "ok": 1,
        "users": sorted(users.values(), key=lambda u: u["username"].lower()),
        "interval": NODE_ANNOUNCE_INTERVAL,
        "ttl": NODE_STALE_S,
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
        return  # nobody is streaming; presence history is served on demand
    with EVENT_COND:
        EVENT_SEQ += 1
        ev = {"seq": EVENT_SEQ, "kind": kind, "ts": now(), "data": data}
        for cid in targets:
            ring = EVENT_LOG.setdefault(cid, deque(maxlen=EVENT_LOG_MAX))
            ring.append((EVENT_SEQ, ev))
        EVENT_COND.notify_all()

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
            s = SUBSCRIPTIONS.pop(lid)
            cur = SUB_BY_USER.get(s["user_hash"])
            if cur and cur.get("link_id") == lid:
                del SUB_BY_USER[s["user_hash"]]

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
        rows = db.execute("SELECT id, name, kind, created FROM channels "
                          "ORDER BY name ASC").fetchall()
        chans = [{"id": r["id"], "name": r["name"], "kind": r["kind"],
                  "created": r["created"], "seq": get_seq(r["id"])} for r in rows]
    return {"ok": 1, "channels": chans}

@api_route("/api/v1/channels/create")
@wrap_json_api
def api_v1_channels_create(viewer, data, link_id):
    name = (data.get("name") or "").strip()
    kind = (data.get("kind") or "chat").strip().lower()
    if not re.match(r"^[a-zA-Z0-9_\-]{1,32}$", name):
        return {"ok": 0, "error": "invalid channel name", "code": 400}
    if kind not in CHANNEL_KINDS:
        return {"ok": 0, "error": "kind must be one of: " + ", ".join(CHANNEL_KINDS),
                "code": 400}
    if kind in ("site", "term") and not is_moderator(viewer):
        return {"ok": 0, "error": "moderator required to create site/term channels",
                "code": 403}
    manifest = data.get("manifest")
    if manifest is not None and not isinstance(manifest, dict):
        return {"ok": 0, "error": "manifest must be an object", "code": 400}
    manifest_json = json.dumps(manifest)[:4096] if manifest else "{}"
    cid = hexstr(RNS.Identity.full_hash(f"{GUILD_ID}:{name}".encode()))
    with db_lock:
        if db.execute("SELECT 1 FROM channels WHERE id=? OR (name=? COLLATE NOCASE)",
                      (cid, name)).fetchone():
            return {"ok": 0, "error": "channel exists", "code": 409}
        db.execute("INSERT INTO channels (id, name, created, kind, manifest) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (cid, name, now(), kind, manifest_json))
        db.execute("INSERT OR IGNORE INTO channel_state (channel_id, seq) VALUES (?, 0)", (cid,))
        db.commit()
    return {"ok": 1, "channel": {"id": cid, "name": name, "kind": kind,
                                 "manifest": manifest or {}, "seq": 0}}

@api_route("/api/v1/channels/info")
@wrap_json_api
def api_v1_channels_info(viewer, data, link_id):
    cid = data.get("channel")
    with db_lock:
        r = db.execute("SELECT id, name, kind, manifest, created FROM channels "
                       "WHERE id=?", (cid,)).fetchone()
        if not r:
            return {"ok": 0, "error": "unknown channel", "code": 404}
        seq = get_seq(cid)
        try:
            manifest = json.loads(r["manifest"] or "{}")
        except Exception:
            manifest = {}
    return {"ok": 1, "channel": {"id": r["id"], "name": r["name"], "kind": r["kind"],
                                 "manifest": manifest, "created": r["created"], "seq": seq}}

# ---- stream -----------------------------------------------------------------------

@api_route("/api/v1/stream/subscribe")
@wrap_json_api
def api_v1_stream_subscribe(viewer, data, link_id):
    cid = data.get("channel")
    with db_lock:
        chan = db.execute("SELECT id, kind FROM channels WHERE id=?", (cid,)).fetchone()
    if not chan:
        return {"ok": 0, "error": "unknown channel", "code": 404}
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
    """Ping-driven active users (same discipline as /api/presence) plus the
    channel each user is currently streaming, and the ping cadence."""
    return {"ok": 1, "users": _presence_payload(),
            "interval": NODE_ANNOUNCE_INTERVAL, "ttl": NODE_STALE_S, "now": now()}

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
    return None

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
    digest = hashlib.sha256(blob).hexdigest()[:32]
    ext = safe_ext(name, mime)
    fname = f"{digest}.{ext}"
    bdir = _blob_dir()
    os.makedirs(bdir, exist_ok=True)
    fpath = os.path.join(bdir, fname)
    if not os.path.exists(fpath):
        try:
            with open(fpath, "wb") as f:
                f.write(blob)
        except OSError as e:
            RNS.log(f"blob store failed for {fname}: {e}", RNS.LOG_ERROR)
            return {"ok": 0, "error": "storage failed", "code": 500}
    if node_destination is not None:
        node_destination.register_request_handler(
            "/file/blobs/" + fname, serve_blob_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
    return {"ok": 1, "blob_id": digest, "size": len(blob),
            "mime": EXT_MIME.get(ext, mime or "application/octet-stream"),
            "name": name or fname, "path": "/file/blobs/" + fname}

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
    total, chunk = _read_slice(os.path.join(_blob_dir(), fname), offset, length)
    if total is None:
        return {"ok": 0, "error": "blob unreadable", "code": 500}
    return {"ok": 1, "blob_id": digest, "total": total, "offset": offset,
            "eof": offset + len(chunk) >= total,
            "data_b64": base64.b64encode(chunk).decode("ascii")}

def serve_blob_file(path, data, request_id, link_id, remote_identity, requested_at):
    """Raw /file/blobs/<fname> handler (one per stored blob, attachments
    pattern). Path is the registration key; validate before touching disk."""
    fname = path.rsplit("/", 1)[-1]
    if not re.match(r"^[0-9a-f]{32}\.[a-z0-9]{1,8}$", fname):
        return b"not found"
    fpath = os.path.join(_blob_dir(), fname)
    if os.path.isfile(fpath):
        with open(fpath, "rb") as f:
            return f.read()
    return b"not found"

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
            return None, "attachment too large (max 32 MB)"
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

def _site_channel(cid):
    with db_lock:
        r = db.execute("SELECT id, kind FROM channels WHERE id=?", (cid,)).fetchone()
    return r if (r and r["kind"] == "site") else None

@api_route("/api/v1/site/put")
@wrap_json_api
def api_v1_site_put(viewer, data, link_id):
    """Store one site resource (micron page, image, audio, video).
    {channel, path, data_b64, mime?} -> registers /file/sites/<cid>/<path> and
    emits a site_patch event on the channel stream. Moderator-only."""
    if not is_moderator(viewer):
        return {"ok": 0, "error": "moderator required", "code": 403}
    cid = data.get("channel")
    if not _site_channel(cid):
        return {"ok": 0, "error": "unknown or non-site channel", "code": 404}
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
    cdir = os.path.join(_site_root(), cid)
    fpath = os.path.join(cdir, rel)
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
    rel = _site_safe_rel(data.get("path"))
    if not rel:
        return {"ok": 0, "error": "invalid resource path", "code": 400}
    fpath = os.path.join(_site_root(), cid, rel)
    # traversal guard: the resolved file must stay inside this site's dir
    if os.path.realpath(fpath) != os.path.join(os.path.realpath(_site_root()), cid, rel):
        return {"ok": 0, "error": "invalid resource path", "code": 400}
    offset, length = _chunk_params(data)
    total, chunk = _read_slice(fpath, offset, length)
    if total is None:
        return {"ok": 0, "error": "resource not found", "code": 404}
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    return {"ok": 1, "channel": cid, "path": "/" + rel, "total": total,
            "offset": offset, "eof": offset + len(chunk) >= total,
            "mime": EXT_MIME.get(ext, "application/octet-stream"),
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
    """Update a site manifest (mod-only): {channel, manifest} — emits a
    site_patch event carrying the manifest so open clients re-theme."""
    if not is_moderator(viewer):
        return {"ok": 0, "error": "moderator required", "code": 403}
    cid = data.get("channel")
    if not _site_channel(cid):
        return {"ok": 0, "error": "unknown or non-site channel", "code": 404}
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
    fpath = os.path.join(_site_root(), cid, rel)
    root = os.path.realpath(os.path.join(_site_root(), cid))
    if os.path.realpath(fpath).startswith(root) and os.path.isfile(fpath):
        with open(fpath, "rb") as f:
            return f.read()
    return b"not found"

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
    # per file, the nomadnet Node.py register_files pattern)
    bdir = os.path.join(STORAGE, "blobs")
    for fname in os.listdir(bdir):
        node_destination.register_request_handler(
            "/file/blobs/" + fname, serve_blob_file,
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
    threading.Thread(target=announce_all, daemon=True).start()
    try:
        while True:
            time.sleep(10)
            _announce_now()   # re-announce frequently so clients can discover us
    except KeyboardInterrupt:
        RNS.log("Shutting down.", RNS.LOG_NOTICE)

if __name__ == "__main__":
    run()
