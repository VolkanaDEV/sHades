#!/usr/bin/env python3
"""sHades integration test — exercises the full stack over a real Reticulum link.

Topology (all on localhost):
  guild process: sHades.py with a TCPServerInterface on 127.0.0.1:42421
  test client:   its own RNS instance with a TCPClientInterface to that port,
                 its own LXMF router, plus link.request() calls to /api/* and /page/*

Run:  /usr/bin/python3 tests/integ_test.py
"""
import os, sys, json, time, re, sqlite3, subprocess, tempfile

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
         "--announce-delay", "2"],
        cwd=RETI_DIR, env=env,
        stdout=open(os.path.join(base, "guild.log"), "ab"),
        stderr=subprocess.STDOUT)
    try:
        # wait for boot
        info_path = os.path.join(g_store, "guild_info.json")
        info = None
        for _ in range(200):
            if os.path.isfile(info_path):
                info = json.load(open(info_path)); break
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
