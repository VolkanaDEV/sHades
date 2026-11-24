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
Media link targets under /file/... with a media extension render as inline
embeds in Kivy and stay ordinary links in NomadNet.
"""
import re

MICRON_MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp",
                     ".mp3", ".ogg", ".wav", ".flac", ".mp4", ".webm")
_MICRON_HEX3 = re.compile(r"^[0-9a-fA-F]{3}$")


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
    """
    blocks = []
    literal = None
    for raw in (text or "").split("\n"):
        if not raw and literal is None:
            continue    # blank line: spacing comes from the layout, not a block
        if literal is not None:
            if raw.strip() == "`=":
                blocks.append({"type": "code", "text": "\n".join(literal)})
                literal = None
            else:
                literal.append(raw)
            continue
        if raw.strip() == "`=":
            literal = []
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
        blocks.append({"type": "code", "text": "\n".join(literal)})
    return blocks


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
