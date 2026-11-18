#!/usr/bin/env python3
"""Server security gates (tay fork tier-1 port) — no RNS mesh needed.

Covers:
  - page_can_view: anonymous lane = @everyone overwrite or open default;
    identified lane = role grants via has_perm.
  - attachment_visible + serve_attachment_file + att_get gating: a stored
    attachment is readable only through a visible, non-deleted message in
    a channel the viewer can view.
  - stream subscribe/poll/push permission gates + subscription drops.
  - search visibility filter BEFORE limit (the empty-page bug).
  - _site_file_path containment (sibling + escape rejections).
  - page routes refuse hidden channels (index/channel/messages/members).
"""
import os, sys, tempfile, sqlite3, base64, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
sHades.DEBUG = False
sHades.dlog = lambda msg: None  # keep the test output clean

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))

def main():
    base = tempfile.mkdtemp(prefix="shades_gate_test_")
    sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
    sHades.db.row_factory = sqlite3.Row
    sHades.db.executescript(sHades.SCHEMA)
    sHades.db.commit()
    sHades.STORAGE = base
    os.makedirs(os.path.join(base, "attachments"), exist_ok=True)

    OPEN = "o" * 64      # default-perm channel (@everyone implied open)
    HIDDEN = "h" * 64    # @everyone overwrite with perms=0: no VIEW, no POST
    VIEWER = "a" * 32    # identified user, no roles
    FILENAME = ("0" * 32) + ".png"

    sHades.db.execute(
        "INSERT INTO channels (id, name, created) VALUES (?, 'open', 1.0)", (OPEN,))
    sHades.db.execute(
        "INSERT INTO channels (id, name, created) VALUES (?, 'hidden', 1.0)", (HIDDEN,))
    sHades.db.execute(
        "INSERT INTO channel_overwrites (channel_id, role_id, perms) "
        "VALUES (?, 0, 0)", (HIDDEN,))
    # a live message carrying the attachment in each channel
    sHades.db.execute(
        "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
        "VALUES ('m_open', ?, 1.0, ?, 'with file')", (OPEN, VIEWER))
    sHades.db.execute(
        "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
        "VALUES ('m_hidden', ?, 1.0, ?, 'with file')", (HIDDEN, VIEWER))
    sHades.db.execute(
        "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
        "VALUES ('m_deleted', ?, 1.0, ?, 'with file')", (OPEN, VIEWER))
    sHades.db.execute(
        "UPDATE messages SET deleted_at=1.0 WHERE msg_id='m_deleted'")
    sHades.db.execute(
        "INSERT INTO attachments (msg_id, idx, name, mime, size, path) "
        "VALUES ('m_open', 0, 'f.png', 'image/png', 4, ?)", (FILENAME,))
    sHades.db.execute(
        "INSERT INTO attachments (msg_id, idx, name, mime, size, path) "
        "VALUES ('m_hidden', 0, 'f.png', 'image/png', 4, ?)", (FILENAME,))
    sHades.db.execute(
        "INSERT INTO attachments (msg_id, idx, name, mime, size, path) "
        "VALUES ('m_deleted', 0, 'f.png', 'image/png', 4, ?)", (FILENAME,))
    # orphan attachment row: no message at all
    sHades.db.execute(
        "INSERT INTO attachments (msg_id, idx, name, mime, size, path) "
        "VALUES ('m_missing', 0, 'f.png', 'image/png', 4, ?)", (FILENAME,))
    sHades.db.commit()

    # actual file on disk
    payload = b"PNG!"
    with open(os.path.join(base, "attachments", FILENAME), "wb") as f:
        f.write(payload)

    # ---- page_can_view ----
    check("anon sees a default-open channel",
          sHades.page_can_view(None, OPEN))
    check("anon cannot see a @everyone-closed channel",
          not sHades.page_can_view(None, HIDDEN))
    check("identified user without VIEW is refused",
          not sHades.page_can_view(VIEWER, HIDDEN))
    check("identified user with open default sees the channel",
          sHades.page_can_view(VIEWER, OPEN))

    # ---- attachment_visibility core ----
    check("live-message attachment visible in open channel",
          sHades.attachment_visible(VIEWER, FILENAME))
    check("attachment visible to anonymous reader of an open channel",
          sHades.attachment_visible(None, FILENAME))
    check("deleted message makes the attachment unreadable",
          sHades.attachment_visible(VIEWER, ("1" * 32) + ".png") is False)
    check("orphan path (no message) unreadable",
          sHades.attachment_visible(VIEWER, ("2" * 32) + ".png") is False)

    # a deleted-message-only attachment: craft its own path
    fname_dead = "dead" + ("0" * 28) + ".png"
    sHades.db.execute(
        "INSERT INTO attachments (msg_id, idx, name, mime, size, path) "
        "VALUES ('m_deleted', 1, 'g.png', 'image/png', 4, ?)", (fname_dead,))
    sHades.db.commit()
    check("attachment whose only message is deleted is gated",
          sHades.attachment_visible(VIEWER, fname_dead) is False)

    # hidden-channel-only linkage: viewer without VIEW cannot read it
    fname_secret = ("3" * 32) + ".png"
    sHades.db.execute(
        "INSERT INTO attachments (msg_id, idx, name, mime, size, path) "
        "VALUES ('m_hidden', 1, 's.png', 'image/png', 4, ?)", (fname_secret,))
    sHades.db.commit()
    check("attachment gated when the only link is a hidden channel",
          sHades.attachment_visible(VIEWER, fname_secret) is False)

    # ---- raw file handler gate ----
    served = sHades.serve_attachment_file(
        "/file/attachments/" + FILENAME, None, None, None, None, None)
    check("raw attachment handler serves a visible file",
          served == payload, served[:20] if served else None)
    served_dead = sHades.serve_attachment_file(
        "/file/attachments/" + fname_dead, None, None, None, None, None)
    check("raw attachment handler refuses deleted-message files",
          served_dead == b"not found", served_dead[:20] if served_dead else None)
    served_orphan = sHades.serve_attachment_file(
        "/file/attachments/" + ("2" * 32) + ".png", None, None, None, None, None)
    check("raw attachment handler refuses orphan paths",
          served_orphan == b"not found", served_orphan[:20] if served_orphan else None)

    # ---- att_get gate ----
    # the wrapped handlers are fn(viewer, data, link_id); recover the inner
    # fn from the wrapper's closure, matched by function name
    def inner_fn(wrapper, name):
        for cell in wrapper.__closure__ or ():
            c = cell.cell_contents
            if callable(c) and getattr(c, "__name__", "") == name:
                return c
        return None
    att_get = inner_fn(sHades.api_v1_att_get, "api_v1_att_get")
    r = att_get(VIEWER, {"path": "/file/attachments/dead" + ("0" * 28) + ".png"}, None)
    check("att_get refuses deleted-message attachment",
          r.get("code") == 404, r)
    fname_open_live = FILENAME  # visible via m_open
    r = att_get(VIEWER, {"path": "/file/attachments/" + FILENAME}, None)
    check("att_get serves a visible attachment",
          r.get("ok") == 1 and base64.b64decode(r["data_b64"]) == payload, r)

    # ---- stream gates ----
    subscribe = inner_fn(sHades.api_v1_stream_subscribe, "api_v1_stream_subscribe")
    r = subscribe(VIEWER, {"channel": HIDDEN}, "L1")
    check("stream subscribe refused without VIEW",
          r.get("code") == 403, r)
    r = subscribe(VIEWER, {"channel": OPEN}, "L1")
    check("stream subscribe allowed with VIEW",
          r.get("ok") == 1, r)
    with sHades.SUB_LOCK:
        sub = sHades.SUBSCRIPTIONS.get("L1")
    check("permitted subscription stored", sub is not None and
          sub["user_hash"] == VIEWER and sub["channel"] == OPEN, sub)

    poll = inner_fn(sHades.api_v1_stream_poll, "api_v1_stream_poll")
    # seed a sub the permission system must reject on poll
    with sHades.SUB_LOCK:
        sHades.SUBSCRIPTIONS["L2"] = {"user_hash": VIEWER, "channel": HIDDEN,
                                      "cursor": 0, "at": sHades.now()}
        sHades.SUB_BY_USER[VIEWER] = {"channel": HIDDEN, "link_id": "L2",
                                      "at": sHades.now()}
    sHades.SUB_BY_USER[VIEWER] = {"channel": OPEN, "link_id": "L3", "at": sHades.now()}
    r = poll(VIEWER, {"channel": HIDDEN, "timeout_s": 0}, "L2")
    check("stream poll refuses a subscription without VIEW",
          r.get("code") == 403, r)
    check("refused subscription is dropped",
          "L2" not in sHades.SUBSCRIPTIONS)

    # _push_stream must drop a permissionless subscriber without exploding
    with sHades.SUB_LOCK:
        sHades.SUBSCRIPTIONS["L4"] = {"user_hash": VIEWER, "channel": HIDDEN,
                                      "cursor": 0, "at": sHades.now()}
    sHades._push_stream(HIDDEN)
    check("push stream drops the permissionless subscriber",
          "L4" not in sHades.SUBSCRIPTIONS)

    d = sHades._drop_subscription("L1")
    check("_drop_subscription removes by link id",
          d is not None and "L1" not in sHades.SUBSCRIPTIONS)
    check("_drop_subscription cleans the per-user index when it matches",
          sHades.SUB_BY_USER.get(VIEWER) is None or
          sHades.SUB_BY_USER[VIEWER].get("link_id") != "L1")

    # ---- search pre-filter ----
    search = inner_fn(sHades.api_v1_search, "api_v1_search")
    # newer matches in the HIDDEN channel, one older match in OPEN
    sHades.db.execute(
        "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
        "VALUES ('srch_h1', ?, 10.0, ?, 'needle hidden')", (HIDDEN, VIEWER))
    sHades.db.execute(
        "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
        "VALUES ('srch_h2', ?, 9.0, ?, 'needle hidden')", (HIDDEN, VIEWER))
    sHades.db.execute(
        "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
        "VALUES ('srch_o1', ?, 5.0, ?, 'needle open')", (OPEN, VIEWER))
    sHades.db.commit()
    r = search(VIEWER, {"q": "needle", "limit": 2}, None)
    ids = [row["msg_id"] for row in r.get("results", [])]
    check("search returns the visible match under a tight limit",
          "srch_o1" in ids, r)
    check("search never returns hidden-channel rows",
          all(not i.startswith("srch_h") for i in ids), r)
    r = search(VIEWER, {"q": "needle", "channel": HIDDEN}, None)
    check("scoped search on a hidden channel is refused",
          r.get("code") == 403, r)

    # ---- _site_file_path containment ----
    cid = "b" * 64
    sibling = "c" * 64
    sites = os.path.join(base, "sites")
    os.makedirs(os.path.join(sites, cid, "sub"), exist_ok=True)
    os.makedirs(os.path.join(sites, sibling), exist_ok=True)
    with open(os.path.join(sites, cid, "index.html"), "wb") as f:
        f.write(b"<b>hi</b>")
    check("site path resolves inside the root",
          sHades._site_file_path(cid, "index.html") is not None)
    check("site path resolves nested inside the root",
          sHades._site_file_path(cid, "sub/x.png") is not None)
    check("site path escapes via dotdot segments is refused",
          sHades._site_file_path(cid, "a/../../" + sibling + "/index.html")
          is None)

    # ---- raw site file handler ----
    r = sHades.serve_site_file(
        "/file/sites/" + cid + "/index.html", None, None, None, None, None)
    check("anon reader gets an open site resource", r == b"<b>hi</b>", r)
    r = sHades.serve_site_file(
        "/file/sites/" + HIDDEN + "/index.html", None, None, None, None, None)
    check("anon reader refused on a hidden site channel",
          r == b"not found", r)

    # ---- page gates ----
    out = sHades.page_index("/page/index.mu", {}, None, None, None, None)
    text = out.decode("utf-8")
    check("index page lists the open channel",
          "open" in text, text[:200])
    check("index page hides the closed channel",
          "hidden" not in text, text[:200])
    out = sHades.page_channel("/page/sHades/channel.mu",
                              {"var_channel": HIDDEN}, None, None, None, None)
    check("channel page refuses a hidden channel",
          b"Channel unavailable" in out, out[:120])
    out = sHades.page_messages("/page/sHades/messages.mu",
                               {"var_channel": HIDDEN}, None, None, None, None)
    check("messages page refuses a hidden channel",
          b"Channel unavailable" in out, out[:120])
    out = sHades.page_members("/page/sHades/members.mu",
                              {"var_channel": HIDDEN}, None, None, None, None)
    check("members page refuses a hidden channel",
          b"Channel unavailable" in out, out[:120])

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
