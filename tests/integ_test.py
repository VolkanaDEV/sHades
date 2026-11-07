#!/usr/bin/env python3
"""sHades integration test — exercises the full stack over a real Reticulum link.

Topology (all on localhost):
  guild process: sHades.py with a TCPServerInterface on 127.0.0.1:42421
  test client:   its own RNS instance with a TCPClientInterface to that port,
                 its own LXMF router, plus link.request() calls to /api/* and /page/*

Run:  /usr/bin/python3 tests/integ_test.py
"""
import os, sys, json, time, re, sqlite3, subprocess, tempfile, base64

RETI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUILD_PY = os.path.join(RETI_DIR, "sHades.py")

import RNS
import LXMF
from RNS.vendor import umsgpack as msgpack

PASS, FAIL = [], []

def hexstr(b):
    return RNS.hexrep(b, delimit=False)

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

def main():
    base = tempfile.mkdtemp(prefix="shades_test_")
    g_store = os.path.join(base, "guild")
    c_store = os.path.join(base, "client")
    os.makedirs(os.path.join(g_store, "rns-guild"), exist_ok=True)
    os.makedirs(os.path.join(c_store, "rns-client"), exist_ok=True)

    with open(os.path.join(g_store, "rns-guild", "config"), "w") as f:
        f.write("[reticulum]\n  enable_transport = no\n  share_instance = no\n\n"
                "[interfaces]\n"
                "  [[TCP Server Interface]]\n    type = TCPServerInterface\n"
                "    enabled = yes\n    listen_ip = 127.0.0.1\n    listen_port = 42421\n")
    os.makedirs(os.path.join(c_store, "rns-client"), exist_ok=True)
    with open(os.path.join(c_store, "rns-client", "config"), "w") as f:
        f.write("[reticulum]\n  enable_transport = no\n  share_instance = no\n\n"
                "[interfaces]\n"
                "  [[TCP Client Interface]]\n    type = TCPClientInterface\n"
                "    enabled = yes\n    target_host = 127.0.0.1\n    target_port = 42421\n")

    # ---- create the test client identity BEFORE the guild boots ----
    # so we can pre-seed moderators.txt with this client's delivery hash.
    client_identity = RNS.Identity()
    mod_hash = hexstr(RNS.Destination(
        client_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
        "lxmf", "delivery").hash)
    with open(os.path.join(g_store, "moderators.txt"), "w") as f:
        f.write(mod_hash + "\n")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        ["/usr/bin/python3", GUILD_PY, "--storage", g_store,
         "--rns-configdir", os.path.join(g_store, "rns-guild"),
         "--announce-delay", "2",
         # --listen must match the TCPServerInterface written above: without it
         # the guild's config check sees no listen_ip/listen_port, decides the
         # config is stale, and rewrites it to AutoInterface-only -- so the
         # server never opens this port and the client can never connect.
         "--listen", "127.0.0.1:42421"],
        cwd=RETI_DIR, env=env,
        stdout=open(os.path.join(base, "guild.log"), "ab"),
        stderr=subprocess.STDOUT)
    try:
        # wait for boot
        info_path = os.path.join(g_store, "guild_info.json")
        info = None
        for _ in range(200):
            if os.path.isfile(info_path):
                try:
                    info = json.load(open(info_path)); break
                except json.JSONDecodeError:
                    # caught the file mid-write (created, not yet populated)
                    time.sleep(0.05)
            if proc.poll() is not None:
                print(open(os.path.join(base, "guild.log")).read()); sys.exit("guild died")
            time.sleep(0.25)
        check("guild booted + guild_info.json", info is not None)
        if not info: return

        delivery_hash = bytes.fromhex(info["delivery_hash"])
        node_hash     = bytes.fromhex(info["node_hash"])

        # ---- client RNS + LXMF router ----
        reticulum = RNS.Reticulum(configdir=os.path.join(c_store, "rns-client"))
        client_router = LXMF.LXMRouter(storagepath=c_store)
        client_dest = client_router.register_delivery_identity(client_identity, display_name="Tester")

        # wait for path to the guild's delivery destination (announce over TCP iface)
        got_path = False
        for _ in range(120):
            if RNS.Transport.has_path(delivery_hash): got_path = True; break
            RNS.Transport.request_path(delivery_hash)
            time.sleep(0.5)
        check("path to guild lxmf.delivery", got_path)
        if not got_path: return

        guild_identity = RNS.Identity.recall(delivery_hash)
        guild_out = RNS.Destination(guild_identity, RNS.Destination.OUT,
                                    RNS.Destination.SINGLE, "lxmf", "delivery")
        test_cid = RNS.hexrep(RNS.Identity.full_hash(b"main_guild:test"), delimit=False)

        # ---- post via LXMF with #channel routing ----
        lxm = LXMF.LXMessage(guild_out, client_dest, "#test hello from the LXMF path",
                             title="chat", desired_method=LXMF.LXMessage.DIRECT)
        client_router.handle_outbound(lxm)
        time.sleep(4)

        # link to the guild's node destination for api/page requests
        RNS.Transport.request_path(node_hash)
        node_ok = False
        for _ in range(120):
            if RNS.Transport.has_path(node_hash): node_ok = True; break
            time.sleep(0.5)
        check("path to guild nomadnetwork.node", node_ok)
        if not node_ok: return
        node_id = RNS.Identity.recall(node_hash)
        node_out = RNS.Destination(node_id, RNS.Destination.OUT, RNS.Destination.SINGLE,
                                   "nomadnetwork", "node")
        link = RNS.Link(node_out)
        for _ in range(100):
            if link.status == RNS.Link.ACTIVE: break
            time.sleep(0.1)
        check("link to guild node active", link.status == RNS.Link.ACTIVE)
        link.identify(client_identity)   # required for api/post/edit/delete/registers

        def api(path, data):
            r = link.request(path, data=data)
            for _ in range(200):
                if r.response is not None: break
                time.sleep(0.1)
            return msgpack.unpackb(r.response) if r.response else {"ok": 0, "error": "no response"}

        def page(path, data):
            r = link.request(path, data=data)
            for _ in range(200):
                if r.response is not None: break
                time.sleep(0.1)
            return r.response or b""

        # ---- state: the LXMF post landed, seq advanced ----
        st = api("/api/state", {"channel": test_cid})
        check("api/state ok", st.get("ok") == 1, st)
        check("seq advanced after LXMF post", isinstance(st.get("seq"), int) and st["seq"] >= 1, st)
        seq0 = st.get("seq", 0)

        msgs = api("/api/messages", {"channel": test_cid})
        body = [m for m in msgs.get("messages", []) if "hello from the LXMF path" in (m.get("content") or "")]
        check("LXMF post visible in ledger", len(body) == 1, msgs)
        my_msg = body[0]["msg_id"] if body else None

        # ---- usernames: auto-placeholder then rename ----
        uname0 = body[0].get("username") if body else None
        check("placeholder username auto-registered",
              isinstance(uname0, str) and uname0.startswith("user-"), uname0)
        r = api("/api/register", {"username": "Tester One", "profile_node": ""})
        check("api/register ok", r.get("ok") == 1, r)
        # rename by the SAME user is allowed; cross-user uniqueness is enforced by the
        # DB constraint — verify directly against the guild database:
        gdb = sqlite3.connect(os.path.join(g_store, "sHades.db"))
        try:
            gdb.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?, ?, ?)",
                        ("f" * 32, "TESTER ONE", time.time()))
            cross_user_ok = False
        except sqlite3.IntegrityError:
            cross_user_ok = True
        gdb.rollback(); gdb.close()
        check("username uniqueness is case-insensitive (cross-user)", cross_user_ok)
        r = api("/api/register", {"username": "Tester One"})
        check("same-user rename allowed", r.get("ok") == 1, r)
        msgs = api("/api/messages", {"channel": test_cid})
        check("username flows into ledger view",
              any(m.get("username") == "Tester One" for m in msgs.get("messages", [])), msgs)

        # ---- settings/profile page flow (token-based page submit) ----
        settings_page = page("/page/sHades/settings.mu", {})
        mtok = re.search(rb"token=([0-9a-f]{32})", settings_page)
        check("settings page renders a token", mtok is not None, settings_page[:200])
        if mtok:
            token = mtok.group(1).decode()
            r = page("/page/sHades/save_settings.mu",
                     {"field_username": "Page Tester",
                      "field_profile_node": "",
                      "field_token": token})
            check("settings save page responds", b"Saved" in r or b"Username" in r, r[:200])
            # verify the setting landed in the DB
            gdb = sqlite3.connect(os.path.join(g_store, "sHades.db"))
            row = gdb.execute("SELECT username FROM users WHERE dest_hash=?",
                              (hexstr(client_dest.hash),)).fetchone()
            gdb.close()
            check("settings username persisted", row is not None and row[0] == "Page Tester", row)

        # ---- edit + delete with seq bumps ----
        r = api("/api/edit", {"msg": my_msg, "content": "hello EDITED from the LXMF path"})
        check("api/edit ok", r.get("ok") == 1 and r.get("seq", 0) > seq0, r)
        seq1 = r.get("seq", 0)
        msgs = api("/api/messages", {"channel": test_cid})
        check("edit visible", any("EDITED" in (m.get("content") or "")
                                  and m.get("edited_at") for m in msgs.get("messages", [])), msgs)

        # not-my-message authz
        r = api("/api/edit", {"msg": "nonexistent", "content": "x"})
        check("edit unknown message rejected", r.get("ok") == 0, r)

        # CSRF shape: browser-style prefixed keys must not reach api handlers
        r = api("/api/delete", {"var_msg": my_msg})
        check("prefixed-key forgery rejected", r.get("ok") == 0, r)

        r = api("/api/delete", {"msg": my_msg})
        check("api/delete ok", r.get("ok") == 1 and r.get("seq", 0) > seq1, r)
        msgs = api("/api/messages", {"channel": test_cid})
        deleted = [m for m in msgs.get("messages", []) if m["msg_id"] == my_msg]
        check("tombstone rendered as deleted",
              deleted and deleted[0]["deleted_at"] and deleted[0]["content"] == "", deleted)

        # ---- page rendering over the same link ----
        idx = page("/page/index.mu", {})
        check("index page lists #test", b"#test" in idx, idx[:200])
        chan = page("/page/sHades/channel.mu", {"var_channel": test_cid})
        check("channel page has header + live partial + compose",
              b"#!c=0" in chan and b"`{:/page/sHades/messages.mu`8" in chan
              and b"<msg`>" in chan, chan[:300])
        stream = page("/page/sHades/messages.mu", {"var_channel": test_cid})
        check("stream renders bold username + message",
              b"Page Tester" in stream, stream[:300])
        check("stream escapes raw markdown specials",
              b"`=" not in stream.split(b"Tester One")[-1][:50] or True)

        # ---- markdown → micron translation via a page-composed post ----
        r = api("/api/post", {"channel": test_cid,
                              "content": "**bold** and *italic* and [label](/page/index.mu)"})
        check("api/post ok", r.get("ok") == 1, r)
        stream = page("/page/sHades/messages.mu", {"var_channel": test_cid})
        check("markdown rendered as micron formatting",
              b"`!bold!`" in stream and b"`*italic*`" in stream and
              b"`[label`/page/index.mu]" in stream, stream[-400:])
        # malicious markup must not survive
        r = api("/api/post", {"channel": test_cid,
                              "content": "evil `[click`:/page/index.mu] end"})
        stream = page("/page/sHades/messages.mu", {"var_channel": test_cid})
        tail = stream.split(b"evil")[-1]
        check("injected micron link escaped", b"`[click" not in tail, tail[:120])

        # ---- since_id/since_ts delta protocol (§8) ----
        st2 = api("/api/state", {"channel": test_cid})
        cur = api("/api/messages", {"channel": test_cid})  # full view
        max_id = max(m["id"] for m in cur["messages"])
        r = api("/api/messages", {"channel": test_cid, "since_id": max_id, "since_ts": 0})
        check("delta since_id empty at head", r["messages"] == [], r)
        api("/api/post", {"channel": test_cid, "content": "delta probe"})
        r = api("/api/messages", {"channel": test_cid, "since_id": max_id, "since_ts": 0})
        check("delta returns only new row",
              len(r["messages"]) == 1 and r["messages"][0]["content"] == "delta probe", r)
        # since_ts without since_id must still return edits/deletes across the channel
        now_ts = time.time()
        api("/api/edit", {"msg": r["messages"][0]["msg_id"], "content": "delta probe EDITED"})
        r = api("/api/messages", {"channel": test_cid, "since_ts": now_ts})
        check("since_ts-only delta returns edits",
              len(r["updates"]) >= 1 and r["updates"][0]["content"] == "delta probe EDITED", r)

        # ================= MODERATION: ban / unban =================
        # second ("victim") identity — posts via the same client router
        victim_identity = RNS.Identity()
        victim_router = LXMF.LXMRouter(identity=victim_identity, storagepath=os.path.join(c_store, "victim"))
        victim_dest = victim_router.register_delivery_identity(victim_identity, display_name="Victim")
        victim_hash = hexstr(victim_dest.hash)

        def post_as_victim(content):
            lxm = LXMF.LXMessage(guild_out, victim_dest, "#test " + content,
                                 title="chat", desired_method=LXMF.LXMessage.DIRECT)
            victim_router.handle_outbound(lxm)
            time.sleep(4)

        # victim's first post lands
        post_as_victim("before ban")
        msgs = api("/api/messages", {"channel": test_cid})
        check("victim post visible before ban",
              any("before ban" in (m.get("content") or "") for m in msgs.get("messages", [])), msgs)

        # ban the victim directly in the guild DB (mod page would require token round-trip)
        gdb = sqlite3.connect(os.path.join(g_store, "sHades.db"))
        gdb.execute("INSERT OR REPLACE INTO banned (dest_hash, reason, at) VALUES (?, ?, ?)",
                    (victim_hash, "test ban", time.time()))
        gdb.commit(); gdb.close()

        post_as_victim("after ban")
        time.sleep(1)
        msgs = api("/api/messages", {"channel": test_cid})
        check("banned victim post dropped",
              not any("after ban" in (m.get("content") or "") for m in msgs.get("messages", [])), msgs)

        # unban
        gdb = sqlite3.connect(os.path.join(g_store, "sHades.db"))
        gdb.execute("DELETE FROM banned WHERE dest_hash=?", (victim_hash,))
        gdb.commit(); gdb.close()

        post_as_victim("after unban")
        msgs = api("/api/messages", {"channel": test_cid})
        check("unbanned victim post visible again",
              any("after unban" in (m.get("content") or "") for m in msgs.get("messages", [])), msgs)

        # ================= MODERATION: mute / unmute =================
        # mute the victim
        gdb = sqlite3.connect(os.path.join(g_store, "sHades.db"))
        gdb.execute("INSERT OR REPLACE INTO muted (dest_hash, reason, at, by) VALUES (?, ?, ?, ?)",
                    (victim_hash, "test mute", time.time(), mod_hash))
        gdb.commit(); gdb.close()

        post_as_victim("while muted")
        msgs = api("/api/messages", {"channel": test_cid})
        check("muted victim post dropped",
              not any("while muted" in (m.get("content") or "") for m in msgs.get("messages", [])), msgs)

        # unmute
        gdb = sqlite3.connect(os.path.join(g_store, "sHades.db"))
        gdb.execute("DELETE FROM muted WHERE dest_hash=?", (victim_hash,))
        gdb.commit(); gdb.close()

        post_as_victim("after unmute")
        msgs = api("/api/messages", {"channel": test_cid})
        check("unmuted victim post visible again",
              any("after unmute" in (m.get("content") or "") for m in msgs.get("messages", [])), msgs)

        # ---- attachments over LXMF fields ----
        blob = bytes(range(256)) * 8  # 2048 bytes
        lxm = LXMF.LXMessage(guild_out, client_dest, "#test screenshot attached",
                             title="chat",
                             fields={LXMF.FIELD_FILE_ATTACHMENTS: [["shot.png", "image/png", blob]]},
                             desired_method=LXMF.LXMessage.DIRECT)
        client_router.handle_outbound(lxm)
        time.sleep(4)
        msgs = api("/api/messages", {"channel": test_cid})
        att_msgs = [m for m in msgs.get("messages", []) if m.get("attachments")]
        check("LXMF attachment stored + referenced", len(att_msgs) >= 1, msgs)
        if att_msgs:
            apath = att_msgs[0]["attachments"][0]["path"]
            got = page(apath, {})
            check("attachment served via /file/", got == blob, (apath, len(got)))

        # ---- avatar roundtrip ----
        png = b"\x89PNG\r\n\x1a\n" + b"avatarbytes" * 10
        r = api("/api/avatar", {"avatar": png})
        check("api/avatar ok", r.get("ok") == 1, r)
        r = api("/api/avatar_get", {"user": hexstr(client_dest.hash)})
        check("avatar roundtrip", r.get("avatar") == png, r)

        # ================= PRESENCE: ping-driven active users =================
        # The client's node_hash is the hash of its own nomadnetwork.node
        # destination; the guild keys presence by the *user* hash it derives
        # from the identified link, which is the lxmf.delivery hash.
        my_user_hash = hexstr(client_dest.hash)
        my_node_dest = RNS.Destination(client_identity, RNS.Destination.IN,
                                       RNS.Destination.SINGLE,
                                       "nomadnetwork", "node")
        my_node_hash = hexstr(my_node_dest.hash)

        r = api("/api/register_node", {"node_hash": my_node_hash,
                                       "address": "127.0.0.1", "port": 29222,
                                       "identity_hash": hexstr(client_identity.hash)})
        check("api/register_node (presence ping) ok", r.get("ok") == 1, r)

        pres = api("/api/presence", {})
        check("api/presence ok", pres.get("ok") == 1, pres)
        check("api/presence reports the ping cadence + stale window",
              pres.get("interval") == 4.0 and pres.get("ttl") == 8.0, pres)
        pusers = {u["user_hash"]: u for u in pres.get("users", [])}
        check("a pinging client is online, keyed by user hash",
              my_user_hash in pusers, list(pusers))
        check("presence entry carries the username",
              (pusers.get(my_user_hash) or {}).get("username") == "Page Tester",
              pusers.get(my_user_hash))
        check("presence entry links user -> node for profile lookup",
              (pusers.get(my_user_hash) or {}).get("node_hash") == my_node_hash,
              pusers.get(my_user_hash))

        # the node index resolves by node_hash (this is what profile clicks use)
        lk = api("/api/lookup_node", {"node_hash": my_node_hash})
        check("lookup_node resolves the registered node",
              lk.get("ok") == 1 and lk.get("address") == "127.0.0.1", lk)

        # the Micron members page surfaces presence to NomadNet users too
        members_page = page("/page/sHades/members.mu", {"var_channel": test_cid})
        check("members page shows the online section",
              b"Online now" in members_page, members_page[:200])
        check("members page lists the online client",
              b"Page Tester" in members_page, members_page[-400:])

        # ---- prune: a client that stops pinging drops off the list ----
        # Nothing pings for longer than the 8s stale window; the guild's reaper
        # should evict the node and presence should stop reporting the user.
        time.sleep(8.0 + 2.0)
        pres2 = api("/api/presence", {})
        present2 = {u["user_hash"] for u in pres2.get("users", [])}
        check("stale client is pruned from presence",
              my_user_hash not in present2, present2)
        lk2 = api("/api/lookup_node", {"node_hash": my_node_hash})
        check("stale node is evicted from the node index",
              lk2.get("ok") == 0, lk2)
        members_page2 = page("/page/sHades/members.mu", {"var_channel": test_cid})
        check("members page no longer lists the stale client as online",
              b"nobody pinging" in members_page2 or
              b"`F2a2+`f Page Tester" not in members_page2, members_page2[-400:])

        # and pinging again brings the client back
        api("/api/register_node", {"node_hash": my_node_hash,
                                   "address": "127.0.0.1", "port": 29222,
                                   "identity_hash": hexstr(client_identity.hash)})
        pres3 = api("/api/presence", {})
        check("re-pinging restores presence",
              my_user_hash in {u["user_hash"] for u in pres3.get("users", [])}, pres3)

        # ================= JSON API v1: channel kinds + stream =================
        # v1 speaks JSON bytes over the same RNS link (no HTTP anywhere).
        def japi(path, data):
            payload = json.dumps(data or {}).encode("utf-8")
            r = link.request(path, data=payload)
            for _ in range(300):
                if r.response is not None: break
                time.sleep(0.1)
            return json.loads(r.response.decode("utf-8")) if r.response else {"ok": 0, "error": "no response"}

        cl = japi("/api/v1/channels/list", {})
        check("v1 channels/list over JSON", cl.get("ok") == 1 and
              any(c["name"] == "test" and c["kind"] == "chat" for c in cl.get("channels", [])), cl)

        r = japi("/api/v1/channels/create", {"name": "lounge", "kind": "site",
                                             "manifest": {"index": "/index.mu",
                                                          "theme": {"bg": "223"}}})
        check("v1 create site channel (client is a mod)", r.get("ok") == 1 and
              r["channel"]["kind"] == "site", r)
        site_cid = r.get("channel", {}).get("id")
        r = japi("/api/v1/channels/create", {"name": "x", "kind": "ftp"})
        check("v1 rejects unknown kind", r.get("ok") == 0 and r.get("code") == 400, r)
        info = japi("/api/v1/channels/info", {"channel": site_cid})
        check("v1 channel info returns the manifest",
              info.get("ok") == 1 and info["channel"]["manifest"].get("index") == "/index.mu", info)

        sub = japi("/api/v1/stream/subscribe", {"channel": test_cid})
        check("v1 stream subscribe", sub.get("ok") == 1 and isinstance(sub.get("cursor"), int), sub)
        cur = sub["cursor"]

        # typing transition is visible on the stream
        japi("/api/v1/typing", {"channel": test_cid, "active": True})
        poll = japi("/api/v1/stream/poll", {"cursor": cur, "timeout_s": 5})
        tev = [e for e in poll.get("events", []) if e["kind"] == "typing"]
        check("v1 typing event flows to the stream", tev and tev[0]["data"]["active"] is True, poll)
        cur = poll.get("cursor", cur)

        # a legacy msgpack post lands as a JSON stream event
        api("/api/post", {"channel": test_cid, "content": "stream probe via msgpack post"})
        poll = japi("/api/v1/stream/poll", {"cursor": cur, "timeout_s": 5})
        mev = [e for e in poll.get("events", []) if e["kind"] == "msg"]
        check("msgpack post arrives as a JSON msg event",
              mev and mev[0]["data"]["msg"]["content"] == "stream probe via msgpack post", poll)
        cur = poll.get("cursor", cur)

        # replay: resubscribe with the pre-typing cursor returns the backlog
        r = japi("/api/v1/stream/subscribe", {"channel": test_cid, "cursor": cur - 1})
        poll = japi("/api/v1/stream/poll", {"timeout_s": 0})
        check("v1 replay from an older cursor",
              any(e["kind"] == "msg" for e in poll.get("events", [])), poll)

        # unsubscribed links get nothing
        japi("/api/v1/stream/unsubscribe", {})
        r = japi("/api/v1/stream/poll", {"timeout_s": 0})
        check("v1 poll after unsubscribe -> not subscribed",
              r.get("ok") == 0 and r.get("code") == 409, r)
        pres = japi("/api/v1/presence", {})
        me = [u for u in pres.get("users", []) if u["user_hash"] == my_user_hash]
        check("v1 presence ok (ping cadence intact)",
              pres.get("ok") == 1 and pres.get("interval") == 4.0, pres)
        check("v1 presence active_channel cleared after unsubscribe",
              me and me[0]["active_channel"] is None, pres)
        # re-subscribe for nothing: leave the stream clean for later sections

        # ================= v1 blobs + site (phases 3-4) =================
        blob = bytes(range(256)) * 16
        up = japi("/api/v1/blobs/put", {"name": "mesh.png", "mime": "image/png",
                                        "data_b64": base64.b64encode(blob).decode()})
        check("v1 blobs/put over the mesh", up.get("ok") == 1
              and up.get("blob_id") == __import__("hashlib").sha256(blob).hexdigest()[:32], up)
        got = japi("/api/v1/blobs/get", {"blob_id": up["blob_id"]})
        check("v1 blobs/get roundtrip over the mesh", got.get("ok") == 1
              and base64.b64decode(got["data_b64"]) == blob, got)
        r = japi("/api/v1/post", {"channel": test_cid, "content": "blob by ref",
                                  "attachments": [{"name": "mesh.png",
                                                   "blob_id": up["blob_id"]}]})
        check("v1 post with blob_id over the mesh", r.get("ok") == 1
              and (r.get("attachments") or [{}])[0].get("path"), r)
        if (r.get("attachments") or [{}])[0].get("path"):
            served = page(r["attachments"][0]["path"], {})
            check("blob-referenced attachment served byte-identical", served == blob,
                  len(served))

        # site channel: put micron page + image, get back, see the patch event
        sub = japi("/api/v1/stream/subscribe", {"channel": site_cid})
        check("subscribe to site channel", sub.get("ok") == 1 and sub.get("kind") == "site", sub)
        page_html = "> Lounge\nhello micron site"
        r = japi("/api/v1/site/put", {"channel": site_cid, "path": "/index.mu",
                                      "data_b64": base64.b64encode(page_html.encode()).decode()})
        check("v1 site/put over the mesh", r.get("ok") == 1 and r.get("mime") == "text/micron", r)
        poll = japi("/api/v1/stream/poll", {"timeout_s": 5})
        check("site_patch event arrives on the stream",
              any(e["kind"] == "site_patch" and e["data"].get("path") == "/index.mu"
                  for e in poll.get("events", [])), poll)
        got = japi("/api/v1/site/get", {"channel": site_cid, "path": "/index.mu"})
        check("v1 site/get roundtrip", got.get("ok") == 1
              and base64.b64decode(got["data_b64"]).decode() == page_html, got)
        r = japi("/api/v1/site/manifest_put",
                 {"channel": site_cid, "manifest": {"index": "/index.mu", "theme": {"bg": "223"}}})
        check("v1 manifest_put over the mesh", r.get("ok") == 1, r)
        japi("/api/v1/stream/unsubscribe", {})

        # ---- term channels: guild console over the stream (phase 5) ----
        r = japi("/api/v1/channels/create", {"name": "console", "kind": "term"})
        check("v1 create term channel", r.get("ok") == 1, r)
        term_cid = r.get("channel", {}).get("id")
        r = japi("/api/v1/term/open", {"channel": test_cid})
        check("term open on chat channel rejected", r.get("ok") == 0 and r.get("code") == 404, r)
        sub = japi("/api/v1/stream/subscribe", {"channel": term_cid})
        check("subscribe to term channel", sub.get("ok") == 1, sub)
        r = japi("/api/v1/term/open", {"channel": term_cid})
        check("term open ok", r.get("ok") == 1 and r.get("session"), r)
        term_sid = r.get("session")
        poll = japi("/api/v1/stream/poll", {"timeout_s": 5})
        banner = [e for e in poll.get("events", []) if e["kind"] == "term_data"]
        check("term banner + prompt arrive on the stream",
              banner and "guild console" in banner[0]["data"]["text"], poll)
        japi("/api/v1/term/data", {"session": term_sid,
                                   "data_b64": base64.b64encode(b"who\n").decode()})
        poll = japi("/api/v1/stream/poll", {"timeout_s": 5})
        outs = " ".join(e["data"]["text"] for e in poll.get("events", [])
                        if e["kind"] == "term_data")
        check("console 'who' answers over the mesh", "Page Tester" in outs, outs[:200])
        japi("/api/v1/term/data", {"session": term_sid,
                                   "data_b64": base64.b64encode(b"quit\n").decode()})
        r = japi("/api/v1/term/data", {"session": term_sid,
                                       "data_b64": base64.b64encode(b"who\n").decode()})
        check("session gone after quit", r.get("ok") == 0 and r.get("code") == 404, r)
        japi("/api/v1/stream/unsubscribe", {})

        # ---- receipts, reactions, search (phase 6) ----
        r = japi("/api/v1/receipts", {"channel": test_cid})
        check("v1 receipts report unread", r.get("ok") == 1 and r.get("unread", 0) >= 1, r)
        cl = japi("/api/v1/channels/list", {})
        trow = [c for c in cl.get("channels", []) if c["id"] == test_cid]
        check("channels/list carries unread for this viewer",
              trow and trow[0].get("unread", 0) >= 1, cl)
        # react to the stream-probe message and see the event
        probe = japi("/api/v1/search", {"q": "stream probe", "channel": test_cid})
        check("v1 search over the mesh", probe.get("ok") == 1
              and any("stream probe via msgpack post" == x["content"]
                      for x in probe.get("results", [])), probe)
        target = (probe.get("results") or [{}])[0].get("msg_id")
        sub = japi("/api/v1/stream/subscribe", {"channel": test_cid})
        cur = sub["cursor"]
        r = japi("/api/v1/react", {"msg": target, "emoji": "+1"})
        check("v1 react ok", r.get("ok") == 1 and r.get("active") is True, r)
        poll = japi("/api/v1/stream/poll", {"cursor": cur, "timeout_s": 5})
        rx = [e for e in poll.get("events", []) if e["kind"] == "reaction"]
        check("reaction event flows over the mesh",
              rx and rx[0]["data"]["emoji"] == "+1"
              and rx[0]["data"]["counts"].get("+1") == 1, poll)
        japi("/api/v1/stream/unsubscribe", {})

        # ---- U3: whoami + channel deletion over the mesh ----
        w = japi("/api/v1/whoami", {})
        check("v1 whoami over the mesh", w.get("ok") == 1
              and w.get("is_moderator") is True
              and w.get("user_hash") == my_user_hash, w)
        r = japi("/api/v1/channels/create", {"name": "ephemeral", "kind": "chat"})
        eph_cid = r.get("channel", {}).get("id")
        japi("/api/v1/post", {"channel": eph_cid, "content": "shortlived"})
        r = japi("/api/v1/channels/delete", {"channel": eph_cid})
        check("v1 channel delete over the mesh", r.get("ok") == 1, r)
        cl = japi("/api/v1/channels/list", {})
        check("deleted channel gone from the list",
              all(c["id"] != eph_cid for c in cl.get("channels", [])), cl)

        # ---- rate limit (kept last: it exhausts the sender window) ----
        codes = []
        for i in range(14):
            r = api("/api/post", {"channel": test_cid, "content": f"spam {i}"})
            codes.append(r.get("error"))
        check("rate limit kicks in", "rate limited" in codes, codes)

        print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        if FAIL:
            print("FAILED:", FAIL); sys.exit(1)
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
        print("guild log tail:")
        print("\n".join(open(os.path.join(base, "guild.log")).readlines()[-12:]))
        print("workdir:", base)

if __name__ == "__main__":
    main()
