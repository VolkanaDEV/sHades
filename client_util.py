"""Shared pure helpers for the sHades Kivy client.

Moved verbatim out of kivy_client.py during the phase-2 client split
(they are the helpers the app mixins in app_*.py need). A shared home
avoids a circular import: the mixins cannot ``from kivy_client import
...`` because kivy_client.py imports the mixins, and it avoids a second
kivy_client copy on the ``python kivy_client.py`` script path (where
__main__ is not the kivy_client module, so a mixin importing it would
load a duplicate module with its own DEBUG flag and singletons).

kivy_client.py re-exports the names the app/tests still use
(ATT_EPHEMERAL, ATT_SERVER, dlog, hexstr, md_to_kivy, now,
rgba_to_hex) so ``kc.<name>`` keeps working. The rest
(parse_slash_help, _escape, hex_to_rgba, _MD) are imported from here
by the mixin modules directly.
"""

import re
import time

import RNS

DEBUG = False  # set via --debug CLI arg; see kivy_client.py __main__ block

# Attachment storage styles:
#   server   - file is stored on the guild; bytes re-fetched via /file/attachments
#              path (survives restart, shown with a loading placeholder until received).
#   ephemeral - file is sent inline as [name, mime, blob] (p2p); bytes kept locally
#              so they survive restart, but there is no server copy to re-fetch.
ATT_SERVER = "server"
ATT_EPHEMERAL = "ephemeral"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def hexstr(b):
    if isinstance(b, str):
        return b
    return RNS.hexrep(b, delimit=False)


def now():
    return time.time()


def parse_slash_help(text):
    """Parse the hub dispatcher's /help output into (cmd, usage) rows.

    Lines look like "  msg <channel> <text>   post <text> to..." —
    the first token after the indent is the command, the remainder
    (with run-length whitespace collapsed) is the usage string. The
    "commands:" header and empty lines are dropped. This must track
    sHades.py's help table by construction: the palette is the
    discovery surface for bot & script integration.
    """
    rows = []
    if not text:
        return rows
    for raw in str(text).splitlines():
        if not raw.startswith("  "):
            continue  # the dispatcher's table is indented exactly two
        line = raw.strip()
        if not line or line == "commands:":
            continue
        parts = line.split(None, 1)
        cmd = parts[0]
        usage = re.sub(r"\s+", " ", parts[1].strip()) if len(parts) > 1 else ""
        if cmd in ("help", "?"):
            usage = "this list"
        rows.append(("/" + cmd, usage))
    return rows


def rgba_to_hex(rgba):
    return "#%02x%02x%02x" % (
        int(max(0, min(1, rgba[0])) * 255),
        int(max(0, min(1, rgba[1])) * 255),
        int(max(0, min(1, rgba[2])) * 255),
    )


def hex_to_rgba(text):
    text = (text or "").lstrip("#").strip()
    if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError("bad hex color: %r" % text)
    return (
        int(text[0:2], 16) / 255.0,
        int(text[2:4], 16) / 255.0,
        int(text[4:6], 16) / 255.0,
        1.0,
    )


# ---------------------------------------------------------------------------
# Markdown -> Kivy markup (NomadNet subset, mirrored with the guild renderer)
# ---------------------------------------------------------------------------
# The guild translates this same message-markdown subset to Micron
# (sHades.md_to_micron_line); the client renders it to Kivy markup instead.
# Subset: **bold**, *italic*, __underline__, `code`, [label](target).
# Links are inert text — there is no HTTP on Reticulum, and micron link
# targets (node hashes, /page/... paths) are not browsable from the Kivy
# client — so no clickable hyperlinks are ever created.
_MD = re.compile(
    r"`(?P<code>[^`\n]{1,120})`"
    r"|\*\*(?P<bold>[^*\n]{1,200})\*\*"
    r"|__(?P<under>[^_\n]{1,200})__"
    r"|\[(?P<label>[^\]\n]{1,128})\]\((?P<target>[^)\s]{1,256})\)"
    r"|\*(?P<i>[^*\n]{1,200})\*"
)


def md_to_kivy(text):
    """Convert the NomadNet message-markdown subset to Kivy markup."""
    if not text:
        return ""
    out = []
    pos = 0
    for m in _MD.finditer(text):
        out.append(_escape(text[pos:m.start()]))
        if m.group("code") is not None:
            out.append("[color=#BFBFBF]" + _escape(m.group("code")) + "[/color]")
        elif m.group("bold") is not None:
            out.append("[b]" + _escape(m.group("bold")) + "[/b]")
        elif m.group("under") is not None:
            out.append("[u]" + _escape(m.group("under")) + "[/u]")
        elif m.group("label") is not None:
            out.append(_escape(m.group("label")) + " (" + _escape(m.group("target")) + ")")
        elif m.group("i") is not None:
            out.append("[i]" + _escape(m.group("i")) + "[/i]")
        pos = m.end()
    out.append(_escape(text[pos:]))
    return "".join(out)


def _escape(s):
    s = s.replace("&", "&" + "amp;")
    s = s.replace("[", "&bl;")
    s = s.replace("]", "&br;")
    return s


def dlog(msg):
    """Debug log — only prints when --debug is passed."""
    if DEBUG:
        print(f"[DEBUG] {msg}", flush=True)
