#!/usr/bin/env python3
"""EXAMPLE bot for the sHades Bot API — not a supported runtime.

This is a minimal illustration of the bot data path, nothing more:
it subscribes to one channel, prints events as they arrive, and posts
one message. It is deliberately small and has no reconnect logic, no
retry logic, no logging, and no error handling beyond bailing out —
a real bot needs all of those. Read BOTS.md first; the doc is the
contract, this file is just one way to drive it.

Transport (mirrors the real client, GuildAPI._request_json in
guild_api.py): JSON bytes over an RNS link — `link.request(path,
data=json.dumps(data).encode())`, wait for `receipt.response`, then
`json.loads` it. There is no HTTP anywhere. The bot's link identity
is its own throwaway RNS identity: bot auth is `bot_id` + `api_key`
in the request data, never the link identity, so an ephemeral
identity is fine here (the server derives the owner from the bot
row, not from the link).

Setup: create the bot in the client (Profile -> Bots), copy the
one-time API key, and export:

    export SHADES_BOT_ID=<bot_id>            # 32 hex chars
    export SHADES_BOT_API_KEY=sk-...         # shown once at create
    export SHADES_GUILD_NODE_HASH=<node_hash>  # from the gateway block
    export SHADES_CHANNEL=<channel id>       # or a channel name, see below
    export SHADES_POST_TEXT="hello from the example bot"  # optional

Run:  python3 examples/bot_example.py

The bot needs the MSG bit (1) to see messages and the POST bit (64)
to post; without them the server answers 403. The channel id is the
hex id from /api/v1/bots/channel_info (READ_CHANNELS bit, 256) — a
name is not accepted, so look it up first if you only know the name.
"""
import json
import os
import sys
import time

# A bot needs MSG (1) to receive messages and POST (64) to post.
# It subscribes with whatever perms it was created with; the server
# enforces the filter, so asking for more than it has just yields
# fewer events, never an error at subscribe time.
POLL_TIMEOUT_S = 4.0      # server long-poll wait per poll (max 10)
LINK_WAIT_S = 20.0        # how long to wait for the link to go ACTIVE


def request_json(link, path, data, timeout=10.0):
    """One JSON request over the RNS link (GuildAPI._request_json_once
    in miniature: same payload shape, same receipt wait, same parse).
    Returns the decoded response dict."""
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    receipt = link.request(path, data=payload)
    if isinstance(receipt, bool):
        if not receipt:
            return {"ok": 0, "error": "request failed"}
        receipt = link.request(path, data=payload)
    deadline = time.time() + timeout
    while (hasattr(receipt, "response") and receipt.response is None
           and time.time() < deadline):
        time.sleep(0.05)
    if not hasattr(receipt, "response") or receipt.response is None:
        return {"ok": 0, "error": "timeout"}
    return json.loads(receipt.response.decode("utf-8"))


def connect(node_hash_hex):
    """Open an RNS link to the guild node (same shape as the client's
    link setup in kivy_client.py: recall the node's identity, build a
    SINGLE "nomadnetwork"/"node" destination, link, wait ACTIVE)."""
    import RNS
    RNS.Reticulum()
    nid = RNS.Identity.recall(bytes.fromhex(node_hash_hex))
    if nid is None:
        raise RuntimeError(
            "RNS does not know the identity for node %s — the guild's "
            "announce has not been heard yet; wait and retry."
            % node_hash_hex)
    dest = RNS.Destination(nid, RNS.Destination.OUT,
                           RNS.Destination.SINGLE, "nomadnetwork", "node")
    link = RNS.Link(dest)
    deadline = time.time() + LINK_WAIT_S
    while link.status != RNS.Link.ACTIVE and time.time() < deadline:
        time.sleep(0.1)
    if link.status != RNS.Link.ACTIVE:
        raise RuntimeError("link to guild node never went ACTIVE")
    # Identify with our own throwaway identity; the server needs a
    # link identity, but bot auth does not come from it.
    link.identify(RNS.Identity())
    return link


def main():
    bot_id = os.environ.get("SHADES_BOT_ID", "")
    api_key = os.environ.get("SHADES_BOT_API_KEY", "")
    node_hash = os.environ.get("SHADES_GUILD_NODE_HASH", "")
    channel = os.environ.get("SHADES_CHANNEL", "")
    post_text = os.environ.get("SHADES_POST_TEXT", "")
    missing = [n for n, v in (("SHADES_BOT_ID", bot_id),
                              ("SHADES_BOT_API_KEY", api_key),
                              ("SHADES_GUILD_NODE_HASH", node_hash),
                              ("SHADES_CHANNEL", channel)) if not v]
    if missing:
        print("missing env: %s" % ", ".join(missing))
        print("see the header of this file for setup.")
        return 2

    link = connect(node_hash)
    creds = {"bot_id": bot_id, "api_key": api_key}

    # 1. Subscribe. cursor 0 would replay the whole ring window;
    # omitting it starts at the head (only new events arrive).
    sub = request_json(link, "/api/v1/stream/subscribe",
                       dict(creds, channel=channel))
    if not sub.get("ok"):
        print("subscribe failed: %s" % sub)
        return 1
    print("subscribed to %s (seq %s)" % (channel, sub.get("seq")))
    cursor = sub.get("cursor", sub.get("seq", 0))

    # 2. Poll loop: long-poll, print each event, advance the cursor.
    # reset: true means the ring rotated past our cursor — resync by
    # subscribing again from the head instead of polling onward.
    posted = False
    try:
        while True:
            res = request_json(link, "/api/v1/stream/poll",
                               {"cursor": cursor,
                                "timeout_s": POLL_TIMEOUT_S},
                               timeout=POLL_TIMEOUT_S + 5)
            if not res.get("ok"):
                print("poll failed: %s" % res)
                return 1
            if res.get("reset"):
                print("cursor reset by server; resubscribing")
                sub = request_json(link, "/api/v1/stream/subscribe",
                                   dict(creds, channel=channel))
                if not sub.get("ok"):
                    print("resubscribe failed: %s" % sub)
                    return 1
                cursor = sub.get("cursor", sub.get("seq", 0))
                continue
            for ev in res.get("events") or []:
                print("event seq=%s kind=%s data=%s"
                      % (ev.get("seq"), ev.get("kind"),
                         json.dumps(ev.get("data"))))
            cursor = res.get("cursor", cursor)
            if not posted:
                # 3. Post exactly once, after the first poll round.
                # The message is attributed to the bot's owner; the
                # bot_id lands in the audit trail, not the message.
                post = request_json(
                    link, "/api/v1/bots/post",
                    dict(creds, channel=channel,
                         content=post_text or "hello from the example bot"))
                print("post: %s" % post)
                posted = True
    except KeyboardInterrupt:
        print("stopping.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
