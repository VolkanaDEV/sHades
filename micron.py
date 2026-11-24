#!/usr/bin/env python3
"""Micron page parser for sHades site channels — pure stdlib, NO Kivy.

Site pages are NOMADNET MICRON, never HTML: the same grammar the guild's own
pages emit (verified against nomadnet MicronParser + the guild's
md_to_micron output). kivy_client imports this module for rendering; keeping
the parser Kivy-free makes it testable headless.

Supported subset (everything else renders literally):
  #!c=0 / #!bg=223        page directives (cache defeat / background)
  > text                  heading          -            separator (lone dash)
  c text                  centered line    \\`  \\\\       escapes
  `!bold!` `*italic*` `_under_`          formatting runs
  `F8a8colored`f `B333inverse`b      3-hex fg/bg runs
  `[label`target`vars]    link (vars ignored; target drives navigation)
  `{:/page/x`8`vars}      auto-refresh partial -> rendered as a plain link
  `= ... =                literal block (monospace, unformatted)
  `=sanguis ... `=        script block (Sanguis source, run on render;
                          unterminated -> code block, never executed)
  `=sanguis markup ... `= script block whose printed lines are
                          re-parsed as micron markup on a successful
                          run (nested script blocks neutralized, never
                          executed); `=sanguis anim markup is not a
                          script block at all, and renders literally
  `=sanguis interact ... `= script block that re-runs when the viewer
                          taps its result: the first run carries
                          event="load", event_x/event_y/state 0.0, and
                          each tap re-runs it with event="tap", the tap
                          position in the block's local coordinates,
                          and the stored numeric state (the previous
                          run's numeric result, or 0.0). Any opener
                          carrying two suffixes (anim+markup,
                          anim+interact, markup+interact, either order)
                          is not a script block at all.
Media link targets under /file/... with a media extension render as inline
embeds in Kivy and stay ordinary links in NomadNet. A script block may
also read a page asset by blob id (see micron_page_media): the host
fetches each /file/attachments/<digest>.<ext> media link on the page
once per render and injects it as a `media_<digest>` binding the
script's media_blob() builtin parses.
"""
import re

MICRON_MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp",
                     ".mp3", ".ogg", ".wav", ".flac", ".mp4", ".webm")
_MICRON_HEX3 = re.compile(r"^[0-9a-fA-F]{3}$")
# A page asset a script may read by blob id: at most this many
# /file/attachments/<32-hex digest>.<ext> media links per page
# (see micron_page_media); the host fetches each once per render.
MICRON_PAGE_MEDIA_MAX = 8
_MICRON_ATTACHMENT_FNAME = re.compile(r"^([0-9a-f]{32})\.[a-z0-9]{1,8}$")


def micron_hex3(h):
    '''"8a8" -> "#88aa88" (Kivy color); None when not 3-hex.'''
    if not _MICRON_HEX3.match(h or ""):
        return None
    return "#" + "".join(c + c for c in h.lower())


def micron_media_target(target):
    """Media extension (with dot) when a link target points at fetchable
    media, else None. Only /file/... targets qualify."""
    t = (target or "").split("`")[0].strip()
    if not t.startswith("/file/"):
        return None
    low = t.lower()
    for ext in MICRON_MEDIA_EXTS:
        if low.endswith(ext):
            return ext
    return None


def _micron_runs(s):
    """Parse one line's inline formatting into runs. A run is a dict with
    'text' plus only the keys that deviate from plain:
    bold / italic / under / fg / bg / link. Every construct is
    self-delimiting in the grammar the guild emits (`!x!`, `F8a8x`f, ...)."""
    runs = []
    cur = {"text": ""}

    def flush():
        if cur["text"]:
            runs.append(dict(cur))
        cur["text"] = ""

    def styled(text, **kw):
        flush()
        run = {"text": text}
        run.update({k: v for k, v in kw.items() if v})
        runs.append(run)

    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:          # escaped char -> literal
            cur["text"] += s[i + 1]
            i += 2
            continue
        if ch != "`":
            cur["text"] += ch
            i += 1
            continue
        nxt = s[i + 1:i + 2]
        if nxt == "!":
            end = s.find("!`", i + 2)
            if end == -1:
                cur["text"] += "`"
                i += 1
                continue
            styled(s[i + 2:end], bold=True)
            i = end + 2
        elif nxt == "*":
            end = s.find("*`", i + 2)
            if end == -1:
                cur["text"] += "`"
                i += 1
                continue
            styled(s[i + 2:end], italic=True)
            i = end + 2
        elif nxt == "_":
            end = s.find("_`", i + 2)
            if end == -1:
                cur["text"] += "`"
                i += 1
                continue
            styled(s[i + 2:end], under=True)
            i = end + 2
        elif nxt == "F" and _MICRON_HEX3.match(s[i + 2:i + 5]):
            end = s.find("`f", i + 5)
            if end == -1:
                cur["text"] += "`"
                i += 1
                continue
            styled(s[i + 5:end], fg=micron_hex3(s[i + 2:i + 5]))
            i = end + 2
        elif nxt == "B" and _MICRON_HEX3.match(s[i + 2:i + 5]):
            end = s.find("`b", i + 5)
            if end == -1:
                cur["text"] += "`"
                i += 1
                continue
            styled(s[i + 5:end], bg=micron_hex3(s[i + 2:i + 5]))
            i = end + 2
        elif nxt == "[":
            end = s.find("]", i + 2)
            if end == -1:
                cur["text"] += "`"
                i += 1
                continue
            parts = s[i + 2:end].split("`")
            # Micron same-node targets are colon-prefixed (":/page/x");
            # node-hash targets have no colon. Strip it either way.
            styled(parts[0], link=(parts[1] if len(parts) > 1 else "").lstrip(":"))
            i = end + 1
        elif nxt == "{":
            end = s.find("}", i + 2)
            if end == -1:
                cur["text"] += "`"
                i += 1
                continue
            fields = s[i + 2:end].split("`")
            target = fields[0].lstrip(":") if fields else ""
            styled(target or "live", link=target)
            i = end + 1
        else:
            # unknown control (stray closers/backticks) -> literal
            cur["text"] += "`"
            i += 1
    flush()
    return [r for r in runs if r.get("text") or r.get("link")]


def micron_parse(text):
    """Parse a micron page into block dicts:
      {"type": "directive", "key", "value"}
      {"type": "heading", "runs": [...]}
      {"type": "sep"}
      {"type": "para", "runs": [...], "center": bool}
      {"type": "code", "text": "..."}
      {"type": "script", "lang": "sanguis", "text": "..."}
      {"type": "script", "lang": "sanguis", "text": "...", "anim": True}
      {"type": "script", "lang": "sanguis", "text": "...", "markup": True}
      {"type": "script", "lang": "sanguis", "text": "...", "interact": True}
    A script block opens on a line that is exactly "`=sanguis" and
    closes on the next line that is exactly "`=". An opener of
    exactly "`=sanguis anim" marks the block animated: the renderer
    re-runs it once per frame with a `frame` binding and swaps the
    drained image in place (see app_site.py). An opener of exactly
    "`=sanguis markup" marks the block markup-emitting: on a
    successful run the renderer re-parses the printed lines as
    micron source (see micron_parse_emitted) instead of showing
    them as code. An opener of exactly "`=sanguis interact" marks
    the block interactive: tapping its result re-runs it with
    event="tap" and the stored state (see app_site.py). An opener
    carrying two suffixes ("`=sanguis anim markup", "`=sanguis
    markup anim", "`=sanguis anim interact", "`=sanguis interact
    anim", "`=sanguis markup interact", "`=sanguis interact
    markup") is not a script block at all -- it renders as a
    literal paragraph, like any unknown suffix. An unterminated
    script block (EOF before the closer) is emitted as a "code"
    block instead: fail-closed, so partial source is never executed.
    """
    blocks = []
    literal = None
    literal_type = "code"
    literal_anim = False
    literal_markup = False
    literal_interact = False
    for raw in (text or "").split("\n"):
        if not raw and literal is None:
            continue    # blank line: spacing comes from the layout, not a block
        if literal is not None:
            if raw.strip() == "`=":
                if literal_type == "script":
                    block = {"type": "script", "lang": "sanguis",
                             "text": "\n".join(literal)}
                    if literal_anim:
                        block["anim"] = True
                    if literal_markup:
                        block["markup"] = True
                    if literal_interact:
                        block["interact"] = True
                    blocks.append(block)
                else:
                    blocks.append({"type": "code", "text": "\n".join(literal)})
                literal = None
                literal_anim = False
                literal_markup = False
                literal_interact = False
            else:
                literal.append(raw)
            continue
        if raw.strip() in ("`=sanguis", "`=sanguis anim",
                           "`=sanguis markup", "`=sanguis interact"):
            literal = []
            literal_type = "script"
            stripped = raw.strip()
            literal_anim = stripped.endswith(" anim")
            literal_markup = stripped.endswith(" markup")
            literal_interact = stripped.endswith(" interact")
            continue
        if raw.strip() == "`=":
            literal = []
            literal_type = "code"
            continue
        if raw.startswith("#!"):
            key, _, value = raw[2:].partition("=")
            blocks.append({"type": "directive", "key": key.strip(),
                           "value": value.strip()})
            continue
        if raw.strip() == "-":
            blocks.append({"type": "sep"})
            continue
        if raw.startswith("\\"):               # line-start escape -> literal para
            blocks.append({"type": "para", "runs": _micron_runs(raw[1:]),
                           "center": False})
            continue
        if raw.startswith(">"):
            blocks.append({"type": "heading", "runs": _micron_runs(raw[1:].strip())})
            continue
        if raw.startswith("c") and not raw.startswith("c`"):
            blocks.append({"type": "para", "runs": _micron_runs(raw[1:]),
                           "center": True})
            continue
        blocks.append({"type": "para", "runs": _micron_runs(raw), "center": False})
    if literal is not None:
        # Unterminated literal: always a code block. A script block
        # without its closer is partial source and must never execute.
        blocks.append({"type": "code", "text": "\n".join(literal)})
    return blocks


def micron_parse_emitted(text):
    """Parse micron source printed by a page script, with script
    blocks neutralized: any block the parser would execute is
    turned into a code block showing the fenced source verbatim
    (opener with its anim/markup/interact suffix, body, closer), so emitted
    markup can never run a nested script -- no `=sanguis, anim,
    markup, or interact block inside emitted output. Headings, paragraphs,
    links, media, separators and directives pass through exactly
    as authored markup does. The parser is total, so this never
    raises; callers still treat a failure as fatal and fall back
    to plain code."""
    blocks = micron_parse(text)
    out = []
    for b in blocks:
        if b.get("type") != "script":
            out.append(b)
            continue
        opener = "`=sanguis"
        if b.get("anim"):
            opener += " anim"
        if b.get("markup"):
            opener += " markup"
        if b.get("interact"):
            opener += " interact"
        out.append({"type": "code",
                    "text": opener + "\n" + b.get("text", "") + "\n`="})
    return out


def micron_page_media(blocks):
    """(digest, target) pairs for the page assets a script may read.

    Scans every block's runs, in page order, for a link where
    micron_media_target(link) is not None AND the target starts
    with /file/attachments/ AND the filename matches
    ^([0-9a-f]{32})\\.[a-z0-9]{1,8}$ (the digest is the 32-hex
    prefix -- the attachment digest algorithm, sha256 hexdigest
    [:32]). Deduped by digest (first occurrence wins), at most
    MICRON_PAGE_MEDIA_MAX entries.

    /file/sites/... targets are path-addressed site files, not
    blobs: they are excluded by design -- a script reads them
    only through the page's own media embeds, never by blob id.
    """
    seen = set()
    out = []
    for b in blocks or []:
        for run in b.get("runs") or []:
            if len(out) >= MICRON_PAGE_MEDIA_MAX:
                return out
            link = (run.get("link") or "").split("`")[0].strip()
            if not link.startswith("/file/attachments/"):
                continue
            if micron_media_target(link) is None:
                continue
            m = _MICRON_ATTACHMENT_FNAME.match(
                link.rsplit("/", 1)[-1])
            if m is None:
                continue
            digest = m.group(1)
            if digest in seen:
                continue
            seen.add(digest)
            out.append((digest, link))
    return out


def micron_page_bg(blocks, theme=None):
    """Page background: `#!bg=` directive wins over manifest theme bg."""
    theme = theme or {}
    bg = theme.get("bg")
    for b in blocks:
        if b["type"] == "directive" and b["key"] == "bg":
            bg = b["value"]
    return micron_hex3(bg) if bg else None


def runs_to_markup(runs, escape):
    """Runs -> one Kivy markup string; links become [ref=target] spans.
    `escape` is the host module's markup escaper (adds &/&bl;/&br;)."""
    out = []
    for r in runs:
        piece = escape(r.get("text", ""))
        if r.get("bold"):
            piece = "[b]" + piece + "[/b]"
        if r.get("italic"):
            piece = "[i]" + piece + "[/i]"
        if r.get("under"):
            piece = "[u]" + piece + "[/u]"
        if r.get("fg"):
            piece = "[color=" + r["fg"] + "]" + piece + "[/color]"
        if r.get("bg"):
            piece = "[color=" + r["bg"] + "]" + piece + "[/color]"
        if r.get("link"):
            piece = "[ref=" + escape(r["link"]) + "]" + piece + "[/ref]"
        out.append(piece)
    return "".join(out)
