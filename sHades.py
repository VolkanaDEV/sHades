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

MAX_CONTENT_LEN   = 2000          # per-message character cap (Discord-style)
MAX_ATTACHMENTS   = 4             # files per message
MAX_ATTACHMENT_SZ = 8 * 1024 * 1024
MAX_AVATAR_SZ     = 64 * 1024
RATE_MAX_POSTS    = 10            # per sender per channel
RATE_WINDOW_S     = 60
PAGE_SIZE         = 50
TOKEN_TTL_S       = 900
ATTACHMENT_EXT    = {"png", "jpg", "jpeg", "gif", "webp", "mp3", "ogg", "wav",
                     "flac", "mp4", "webm", "pdf", "txt"}

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

FIELD_ATTACHMENTS = LXMF.FIELD_FILE_ATTACHMENTS  # 0x05 — envelope ours (plan §14)

# ----------------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    id TEXT PRIMARY KEY, name TEXT, node_hash TEXT, delivery_hash TEXT, prop_hash TEXT);
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, created REAL);
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

def open_db(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def ensure_channel(name):
    """Create a channel + its revision counter if needed. Returns channel id."""
    cid = hexstr(RNS.Identity.full_hash(f"{GUILD_ID}:{name}".encode()))
    with db_lock:
        row = db.execute("SELECT id FROM channels WHERE id=?", (cid,)).fetchone()
        if not row:
            db.execute("INSERT INTO channels (id, name, created) VALUES (?, ?, ?)",
                       (cid, name, time.time()))
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
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    if node_destination is not None:
        node_destination.register_request_handler(
            "/file/attachments/" + fname, serve_attachment_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
    return fname

def serve_attachment_file(path, data, request_id, link_id, remote_identity, requested_at):
    fname = path.rsplit("/", 1)[-1]
    if not re.match(r"^[0-9a-f]{32}\.[a-z0-9]{1,8}$", fname):
        return b"not found"
    fpath = os.path.join(STORAGE, "attachments", fname)
    if os.path.isfile(fpath):
        with open(fpath, "rb") as f:
            return f.read()
    return b"not found"

# ----------------------------------------------------------------------------------
# Ingest (plan §4): both LXMF delivery and page compose converge here.
# One transaction: user placeholder, message row, attachments, members, seq bump.
# ----------------------------------------------------------------------------------

def ingest(channel_id, author_hash, content, ts=None, attachments=None):
    if is_banned(author_hash):
        return None
    if is_muted(author_hash):
        return None  # muted: post silently dropped
    content = (content or "").strip()
    if not content:
        return None
    if len(content) > MAX_CONTENT_LEN:
        content = content[:MAX_CONTENT_LEN]
    ts = ts if isinstance(ts, (int, float)) and ts > 0 else now()
    msg_id = hexstr(RNS.Identity.full_hash(
        f"{channel_id}|{author_hash}|{ts}|{content}".encode()))
    with db_lock:
        if db.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone():
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
                continue
            fname = store_attachment(str(name)[:128], str(mime)[:64], blob)
            if fname:
                db.execute("INSERT OR REPLACE INTO attachments "
                           "(msg_id, idx, name, mime, size, path) VALUES (?, ?, ?, ?, ?, ?)",
                           (msg_id, idx, str(name)[:128], str(mime)[:64], len(blob), fname))
        db.execute("INSERT INTO members (channel_id, member_hash, last_post) VALUES (?, ?, ?) "
                   "ON CONFLICT(channel_id, member_hash) DO UPDATE SET last_post=excluded.last_post",
                   (channel_id, author_hash, ts))
        bump_seq(channel_id)
        db.commit()
    RNS.log(f"ingest #{channel_name(channel_id)} msg {msg_id[:12]} from {author_hash[:12]}",
            RNS.LOG_DEBUG)
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
    out = [page_header()]
    out.append("> Members of #" + esc_micron_text(chan["name"]) + "\n-\n")
    for r in rows:
        out.append(username_fragment(r["member_hash"]) +
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
    with db_lock:
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return api_error("unknown channel", 404)
    if not isinstance(content, str) or not content.strip():
        return api_error("empty content", 400)
    if not rate_ok(viewer, cid):
        return api_error("rate limited", 429)
    atts = data.get("attachments") or []
    if not isinstance(atts, list):
        atts = []
    msg_id = ingest(cid, viewer, content, attachments=atts[:MAX_ATTACHMENTS])
    if msg_id is None:
        return api_error("rejected", 400)
    return msgpack.packb({"ok": 1, "msg_id": msg_id})

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
            db.execute("UPDATE messages SET content=?, edited_at=? WHERE msg_id=?",
                       (content.strip()[:MAX_CONTENT_LEN], now(), msg_id))
        else:
            db.execute("UPDATE messages SET deleted_at=? WHERE msg_id=?", (now(), msg_id))
        bump_seq(row["channel_id"])
        db.commit()
        seq = get_seq(row["channel_id"])
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
    args = parser.parse_args(argv)

    STORAGE = os.path.abspath(args.storage)
    GUILD_NAME = args.name
    ANNOUNCE_DELAY = args.announce_delay
    os.makedirs(STORAGE, exist_ok=True)
    os.makedirs(os.path.join(STORAGE, "attachments"), exist_ok=True)
    os.makedirs(os.path.join(STORAGE, "avatars"), exist_ok=True)

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

    threading.Thread(target=announce_all, daemon=True).start()
    try:
        while True:
            time.sleep(10)
            _announce_now()   # re-announce frequently so clients can discover us
    except KeyboardInterrupt:
        RNS.log("Shutting down.", RNS.LOG_NOTICE)

if __name__ == "__main__":
    run()
