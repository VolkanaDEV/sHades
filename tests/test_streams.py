#!/usr/bin/env python3
"""JSON API v1 unit tests — no RNS mesh needed.

Pins down the server side of the v1 surface:

  1. the JSON envelope: identified-link auth, JSON payloads, {ok, error, code};
  2. channel kinds (chat / site / term) incl. moderator gating and manifests;
  3. the stream: one subscription per link ("only the active channel flows"
     as a server-side rule), cursor replay, gap → reset, long-poll shapes;
  4. typing transitions (event only on change, TTL snapshot);
  5. presence events on the stream and active_channel in the payload.

Handlers are exercised through API_ROUTES (the full wrap_json_api path) with a
real RNS.Identity so viewer derivation runs exactly as it does on the wire.
"""
import os, sys, json, time, sqlite3, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

# ---- harness -----------------------------------------------------------------------

base = tempfile.mkdtemp(prefix="shades_streams_test_")
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.STORAGE = base
sHades.DEBUG = False

identity = RNS.Identity()
USER = sHades.hexstr(RNS.Destination(identity, RNS.Destination.OUT,
                                     RNS.Destination.SINGLE, "lxmf", "delivery").hash)
MOD_USER = "m" * 32
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (USER, "streamer", sHades.now()))
sHades.db.commit()

def call(path, data=None, link_id="L1", ident=identity):
    """Invoke a v1 handler through its registered wrapper — raw JSON bytes in,
    parsed dict out, exactly as the wire would deliver it."""
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", link_id, ident, sHades.now())
    return json.loads(raw.decode("utf-8"))

def reset_stream_state():
    with sHades.EVENT_COND:
        sHades.EVENT_LOG.clear()
        sHades.EVENT_SEQ = 0
    with sHades.SUB_LOCK:
        sHades.SUBSCRIPTIONS.clear()
        sHades.SUB_BY_USER.clear()
    with sHades.TYPING_LOCK:
        sHades.TYPING.clear()

def main():
    # ---------- envelope + auth ----------
    r = call("/api/v1/channels/list", ident=None)
    check("no identified link -> 401 shape", r.get("ok") == 0 and r.get("code") == 401, r)
    raw = sHades.API_ROUTES["/api/v1/channels/list"]("/api/v1/x", b"{not json", "r", "L1",
                                                     identity, sHades.now())
    r = json.loads(raw.decode("utf-8"))
    check("invalid JSON payload -> 400", r.get("ok") == 0 and r.get("code") == 400, r)
    raw = sHades.API_ROUTES["/api/v1/channels/list"]("/api/v1/x", 12345, "r", "L1",
                                                     identity, sHades.now())
    r = json.loads(raw.decode("utf-8"))
    check("non-object payload -> 400", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/channels/list", {})
    check("JSON envelope round-trips", r.get("ok") == 1 and "channels" in r, r)

    # ---------- channel kinds ----------
    cid_chat = call("/api/v1/channels/create", {"name": "general", "kind": "chat"})["channel"]["id"]
    r = call("/api/v1/channels/create", {"name": "lounge", "kind": "site"})
    check("non-mod cannot create site channel", r.get("ok") == 0 and r.get("code") == 403, r)
    sHades.MODERATORS.add(USER)
    r = call("/api/v1/channels/create", {"name": "lounge", "kind": "site",
                                         "manifest": {"index": "/index.mu",
                                                      "theme": {"bg": "223", "accent": "F8a8"}}})
    check("mod creates site channel", r.get("ok") == 1 and r["channel"]["kind"] == "site", r)
    cid_site = r["channel"]["id"]
    r = call("/api/v1/channels/create", {"name": "console", "kind": "term"})
    check("mod creates term channel", r.get("ok") == 1 and r["channel"]["kind"] == "term", r)
    r = call("/api/v1/channels/create", {"name": "weird", "kind": "ftp"})
    check("unknown kind rejected", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/channels/create", {"name": "bad name!", "kind": "chat"})
    check("invalid channel name rejected", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/channels/create", {"name": "GENERAL", "kind": "chat"})
    check("duplicate (case-insensitive) name rejected", r.get("ok") == 0 and r.get("code") == 409, r)

    chans = {c["name"]: c for c in call("/api/v1/channels/list")["channels"]}
    check("list reports kinds", chans["general"]["kind"] == "chat"
          and chans["lounge"]["kind"] == "site" and chans["console"]["kind"] == "term", chans)
    check("list carries seq counters", all(isinstance(c.get("seq"), int) for c in chans.values()), chans)
    info = call("/api/v1/channels/info", {"channel": cid_site})["channel"]
    check("info returns the manifest", info["manifest"].get("index") == "/index.mu"
          and info["manifest"]["theme"]["accent"] == "F8a8", info)
    r = call("/api/v1/channels/info", {"channel": "f" * 32})
    check("info on unknown channel -> 404", r.get("ok") == 0 and r.get("code") == 404, r)

    # ---------- stream: subscribe / single-active / replay ----------
    reset_stream_state()
    r = call("/api/v1/stream/subscribe", {"channel": "f" * 32})
    check("subscribe to unknown channel -> 404", r.get("ok") == 0 and r.get("code") == 404, r)
    r = call("/api/v1/stream/subscribe", {"channel": cid_chat})
    check("subscribe ok, returns head + typing snapshot",
          r.get("ok") == 1 and r.get("kind") == "chat" and r.get("typing") == []
          and isinstance(r.get("cursor"), int), r)
    sub_head = r["cursor"]

    # events posted to the subscribed channel arrive on poll
    sHades.ingest(cid_chat, USER, "hello stream")
    sHades.ingest(cid_chat, USER, "second message")
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    kinds = [e["kind"] for e in poll["events"]]
    check("msg events flow to the subscriber", kinds == ["msg", "msg"], poll)
    check("msg event carries the full row",
          poll["events"][0]["data"]["msg"]["content"] == "hello stream"
          and poll["events"][0]["data"]["msg"]["username"] == "streamer", poll)
    check("cursor advances to the last delivered event",
          poll["cursor"] == poll["events"][-1]["seq"], poll)
    poll2 = call("/api/v1/stream/poll", {"timeout_s": 0})
    check("drained poll returns no events", poll2["events"] == [], poll2)

    # resubscribe with the old cursor replays from the ring
    r = call("/api/v1/stream/subscribe", {"channel": cid_chat, "cursor": sub_head})
    check("subscribe honours an explicit cursor", r.get("cursor") == sub_head, r)
    poll3 = call("/api/v1/stream/poll", {"timeout_s": 0})
    check("replay from cursor returns the same events",
          [e["data"]["msg"]["content"] for e in poll3["events"]] ==
          ["hello stream", "second message"], poll3)

    # ONE subscription per link: switching channels stops the old stream
    sHades.ingest(cid_site, USER, "site update")
    r = call("/api/v1/stream/subscribe", {"channel": cid_site})
    check("resubscribe replaces the link's subscription", r.get("ok") == 1, r)
    sHades.ingest(cid_chat, USER, "posted while watching site")
    poll4 = call("/api/v1/stream/poll", {"timeout_s": 0})
    check("old channel's events no longer flow after switching",
          poll4["channel"] == cid_site and
          not any(e["kind"] == "msg" and
                  e["data"].get("msg", {}).get("channel_id") == cid_chat
                  for e in poll4["events"]), poll4)

    # another link can stream a different channel independently
    call("/api/v1/stream/subscribe", {"channel": cid_chat}, link_id="L2")
    sHades.ingest(cid_chat, USER, "for the second link")
    poll5 = call("/api/v1/stream/poll", {"timeout_s": 0}, link_id="L2")
    check("second link streams its own channel",
          poll5["channel"] == cid_chat and
          any(e["kind"] == "msg" and e["data"]["msg"]["content"] == "for the second link"
              for e in poll5["events"]), poll5)

    # gap -> reset: shrink the ring, fall behind, expect reset=True
    reset_stream_state()
    sHades.EVENT_LOG.clear()
    old_max = sHades.EVENT_LOG_MAX
    sHades.EVENT_LOG_MAX = 2   # applies to rings created after this point
    call("/api/v1/stream/subscribe", {"channel": cid_chat, "cursor": 0})
    for i in range(5):
        sHades.ingest(cid_chat, USER, f"overflow {i}")
    poll6 = call("/api/v1/stream/poll", {"timeout_s": 0, "cursor": 0})
    sHades.EVENT_LOG_MAX = old_max
    check("cursor behind the ring -> reset flag", poll6.get("reset") is True, poll6)
    check("reset carries the head to resync to", isinstance(poll6.get("seq"), int), poll6)

    # ---------- edit / del events ----------
    reset_stream_state()
    call("/api/v1/stream/subscribe", {"channel": cid_chat})
    mid = sHades.ingest(cid_chat, USER, "to be edited")
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    seq_after_msg = poll["cursor"]
    raw = sHades.API_ROUTES["/api/edit"]("/api/edit", {"msg": mid, "content": "edited body"},
                                         "r", "L9", identity, sHades.now())
    check("legacy msgpack edit still works",
          sHades.msgpack.unpackb(raw)["ok"] == 1, raw[:40])
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    ev = [e for e in poll["events"] if e["kind"] == "edit"]
    check("edit event on the stream",
          ev and ev[0]["data"]["content"] == "edited body"
          and ev[0]["data"]["edited_at"], poll)
    raw = sHades.API_ROUTES["/api/delete"]("/api/delete", {"msg": mid},
                                           "r", "L9", identity, sHades.now())
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    ev = [e for e in poll["events"] if e["kind"] == "delete"]
    check("delete event on the stream",
          ev and ev[0]["data"]["deleted_at"] and ev[0]["data"]["content"] == "", poll)
    _ = seq_after_msg

    # ---------- typing ----------
    reset_stream_state()
    call("/api/v1/stream/subscribe", {"channel": cid_chat})
    call("/api/v1/typing", {"channel": "f" * 32, "active": True})
    check("typing on unknown channel rejected",
          call("/api/v1/typing", {"channel": "f" * 32, "active": True}).get("code") == 404)
    call("/api/v1/typing", {"channel": cid_chat, "active": True})
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    tev = [e for e in poll["events"] if e["kind"] == "typing"]
    check("typing start emits an event",
          tev and tev[0]["data"]["active"] is True
          and tev[0]["data"]["username"] == "streamer", poll)
    call("/api/v1/typing", {"channel": cid_chat, "active": True})  # refresh, no event
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    check("typing refresh emits no event",
          not [e for e in poll["events"] if e["kind"] == "typing"], poll)
    check("typing snapshot lists the typist",
          [t["user_hash"] for t in poll["typing"]] == [USER], poll)
    call("/api/v1/typing", {"channel": cid_chat, "active": False})
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    tev = [e for e in poll["events"] if e["kind"] == "typing"]
    check("typing stop emits active=False", tev and tev[0]["data"]["active"] is False, poll)
    sub = call("/api/v1/stream/subscribe", {"channel": cid_chat})
    check("subscribe returns a fresh typing snapshot", sub.get("typing") == [], sub)

    # ---------- presence ----------
    reset_stream_state()
    call("/api/v1/stream/subscribe", {"channel": cid_chat})
    r = sHades.API_ROUTES["/api/register_node"](
        "/api/register_node",
        {"node_hash": "1" * 32, "address": "10.0.0.1", "port": 29222},
        "r", "L9", identity, sHades.now())
    check("legacy ping still ok", sHades.msgpack.unpackb(r)["ok"] == 1)
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    pev = [e for e in poll["events"] if e["kind"] == "presence"]
    check("first ping emits a guild-wide presence event",
          pev and any(u["user_hash"] == USER for u in pev[0]["data"]["users"]), poll)
    pres = call("/api/v1/presence", {})
    me = [u for u in pres["users"] if u["user_hash"] == USER]
    check("v1 presence reports ping cadence",
          pres.get("interval") == 4.0 and pres.get("ttl") == 8.0, pres)
    check("v1 presence carries active_channel from the stream",
          me and me[0]["active_channel"] == cid_chat, pres)
    # a user NOT streaming has active_channel None
    other = RNS.Identity()
    other_hash = sHades.hexstr(RNS.Destination(other, RNS.Destination.OUT,
                                               RNS.Destination.SINGLE, "lxmf", "delivery").hash)
    sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) "
                      "VALUES (?,?,?)", (other_hash, "quiet", sHades.now()))
    sHades.db.commit()
    sHades.API_ROUTES["/api/register_node"](
        "/api/register_node", {"node_hash": "2" * 32, "address": "10.0.0.2", "port": 29222},
        "r", "L9", other, sHades.now())
    pres = call("/api/v1/presence", {})
    quiet = [u for u in pres["users"] if u["user_hash"] == other_hash]
    check("non-streaming user has null active_channel",
          quiet and quiet[0]["active_channel"] is None, pres)

    # reap drops the stale pings and emits a presence event for users that
    # lost their last node
    with sHades.NODE_REGISTRY_LOCK:
        for h in sHades.NODE_REGISTRY:
            sHades.NODE_REGISTRY[h]["last_seen"] = sHades.now() - 30.0
    sHades._reap_stale_nodes()
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    pev = [e for e in poll["events"] if e["kind"] == "presence"]
    check("reap emits a presence event", bool(pev), poll)
    check("reaped users are gone from the event",
          pev and not pev[-1]["data"]["users"], pev)

    # ---------- unsubscribe ----------
    # L1 holds the live subscription (the long-poll poller re-subscribed it);
    # unsubscribing that link must clear the user's active_channel.
    call("/api/v1/stream/unsubscribe", {}, link_id="L1")
    r = call("/api/v1/stream/poll", {"timeout_s": 0}, link_id="L1")
    check("poll after unsubscribe -> not subscribed", r.get("ok") == 0 and r.get("code") == 409, r)
    # the reap emptied the registry; come back online, then check the flag
    sHades.API_ROUTES["/api/register_node"](
        "/api/register_node", {"node_hash": "1" * 32, "address": "10.0.0.1", "port": 29222},
        "r", "L9", identity, sHades.now())
    pres = call("/api/v1/presence", {})
    me = [u for u in pres["users"] if u["user_hash"] == USER]
    check("unsubscribed user's active_channel clears", me and me[0]["active_channel"] is None, pres)

    # ---------- long-poll wake-up ----------
    reset_stream_state()
    call("/api/v1/stream/subscribe", {"channel": cid_chat})
    result = {}
    def poller():
        t0 = time.time()
        result["resp"] = call("/api/v1/stream/poll", {"timeout_s": 5})
        result["dt"] = time.time() - t0
    import threading
    th = threading.Thread(target=poller, daemon=True)
    th.start()
    time.sleep(0.5)
    sHades.ingest(cid_chat, USER, "wake up")
    th.join(timeout=8)
    check("long-poll wakes on event (well before timeout)",
          result.get("resp", {}).get("events") and result.get("dt", 99) < 3.0, result)
    check("woken poll carries the event",
          result["resp"]["events"][0]["data"]["msg"]["content"] == "wake up", result)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
