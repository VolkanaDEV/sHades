"""Small, Kivy-free presentation rules for mesh state and chat history."""

import re


REPLY_RE = re.compile(r"^↳ @([^\n·]{1,48}) · ([^\n]{1,160})\n\n", re.UNICODE)


def link_state(active, hops=None):
    """Describe only what the client can actually observe about its path.

    A one-hop active link is direct. An active multi-hop link is relayed; the
    app cannot know whether a propagation node was used from hop count alone.
    """
    if not active:
        return "offline", "OFFLINE", 0
    if isinstance(hops, int) and hops >= 0:
        if hops <= 1:
            return "direct", "DIRECT LINK", 1
        return "relay", "%d HOPS · MESH" % hops, min(hops, 4)
    return "relay", "MESH LINK", 2


def grouped_with(previous, current):
    """Collapse adjacent posts from one author within five minutes."""
    if not previous or not current:
        return False
    if not previous.get("author_hash") or previous.get("author_hash") != current.get("author_hash"):
        return False
    try:
        delta = float(current.get("ts", 0)) - float(previous.get("ts", 0))
    except (TypeError, ValueError):
        return False
    return 0 <= delta <= 300


def reply_wire(username, excerpt, body):
    """Readable on older clients; new clients display the prefix as a card."""
    name = re.sub(r"[\n\r·]", " ", username or "user").strip()[:48]
    snippet = re.sub(r"\s+", " ", excerpt or "").strip()[:160]
    return "↳ @%s · %s\n\n%s" % (name, snippet, body)


def split_reply(content):
    text = content or ""
    match = REPLY_RE.match(text)
    if match:
        return match.group(1), match.group(2), text[match.end():]
    return "", "", text
