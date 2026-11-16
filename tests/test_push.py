#!/usr/bin/env python3
"""v0.28 push delivery proof -- real RNS link, real guild process.

Topology is the integ_test one (guild hub on 127.0.0.1:42421). The probe
client establishes the link, registers the packet callback exactly as the
shipping client does (link.set_packet_callback), subscribes via
/api/v1/stream/subscribe, then posts a message over the same link. PASS =
the callback received a pushed stream frame carrying the message event,
without any poll call in between.
"""
import os
import sys
import json
import time
import tempfile
import subprocess

RETI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RETI_DIR)
GUILD_PY = os.path.join(RETI_DIR, "sHades.py")

import RNS  # noqa: E402
from RNS.vendor import umsgpack as msgpack  # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))

def main():
    base = tempfile.mkdtemp(prefix="shades_push_")
    g_store = os.path.join(base, "guild")
    os.makedirs(os.path.join(g_store, "rns-guild"), exist_ok=True)
    with open(os.path.join(g_store, "rns-guild", "config"), "w") as f:
        f.write("[reticulum]\n  enable_transport = yes\n  share_instance = no\n\n"
                "[interfaces]\n"
                "  [[TCP Server Interface]]\n    type = TCPServerInterface\n"
                "    enabled = yes\n    listen_ip = 127.0.0.1\n    listen_port = 42421\n")
    CLIENT_CFG = ("[reticulum]\n  enable_transport = no\n  share_instance = no\n\n"
                  "[interfaces]\n"
                  "  [[TCP Client Interface]]\n    type = TCPClientInterface\n"
                  "    enabled = yes\n    target_host = 127.0.0.1\n    target_port = 42421\n")
    for sub in ("sub", "rns2"):
        d = os.path.join(base, "client", sub)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config"), "w") as f:
            f.write(CLIENT_CFG)

    # Seed the subscriber as Admin so its post to general has POST rights
    # (same seeding integrations use; delivery hash = lxmf.delivery of the
    # identity the link identifies with).
    subscriber = RNS.Identity()
    sub_delivery = RNS.hexrep(RNS.Destination(
        subscriber, RNS.Destination.OUT, RNS.Destination.SINGLE,
        "lxmf", "delivery").hash, delimit=False)
    with open(os.path.join(g_store, "admin.txt"), "w") as f:
        f.write(sub_delivery + "\n")
    with open(os.path.join(g_store, "moderators.txt"), "w") as f:
        f.write(sub_delivery + "\n")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, GUILD_PY, "--storage", g_store,
         "--rns-configdir", os.path.join(g_store, "rns-guild"),
         "--announce-delay", "1",
         "--listen", "127.0.0.1:42421"],
        cwd=RETI_DIR, env=env,
        stdout=open(os.path.join(base, "guild.log"), "ab"),
        stderr=subprocess.STDOUT)
    try:
        info_path = os.path.join(g_store, "guild_info.json")
        info = None
        for _ in range(200):
            if os.path.isfile(info_path):
                try:
                    info = json.load(open(info_path))
                    break
                except json.JSONDecodeError:
                    time.sleep(0.05)
            if proc.poll() is not None:
                print(open(os.path.join(base, "guild.log")).read())
                sys.exit("guild died")
            time.sleep(0.25)
        check("guild booted", info is not None)
        if not info:
            return
        node_hash = bytes.fromhex(info["node_hash"])

        # -- subscriber client: RNS + link + packet callback (the v0.28
        #    client-side mechanics verbatim) --
        RNS.Reticulum(configdir=os.path.join(base, "client", "sub"))
        # path discovery exactly as the integ harness does: announce
        # arrival needs an explicit request on a fresh client instance
        got_path = False
        for _ in range(120):
            if RNS.Transport.has_path(node_hash):
                got_path = True
                break
            RNS.Transport.request_path(node_hash)
            time.sleep(0.5)
        check("path to nomadnetwork.node", got_path)
        if not got_path:
            return
        nid = RNS.Identity.recall(node_hash)
        out_dest = RNS.Destination(nid, RNS.Destination.OUT,
                                   RNS.Destination.SINGLE,
                                   "nomadnetwork", "node")
        link = RNS.Link(out_dest)
        for _ in range(200):
            if link.status == RNS.Link.ACTIVE:
                break
            time.sleep(0.1)
        check("link ACTIVE", link.status == RNS.Link.ACTIVE)
        link.identify(subscriber)

        push_frames = []

        def _on_push(plaintext, packet=None):
            try:
                frame = msgpack.unpackb(plaintext, raw=False)
                if isinstance(frame, dict) and frame.get("k") == "stream":
                    push_frames.append(frame)
            except Exception:
                pass
        link.set_packet_callback(_on_push)
        check("packet callback registered", link.callbacks.packet is not None)

        # -- subscribe (json bytes on /api/v1/stream/subscribe) --
        def link_request_json(path, payload):
            receipt = link.request(path,
                                   data=json.dumps(payload).encode("utf-8"))
            deadline = time.time() + 10
            while (hasattr(receipt, "response") and receipt.response is None
                   and time.time() < deadline):
                time.sleep(0.05)
            resp = getattr(receipt, "response", None)
            if resp:
                return json.loads(resp)
            return None

        chans = link_request_json("/api/v1/channels/list", {})
        if not (chans and chans.get("ok")):
            check("channels list ok", False, chans)
            return
        gen = next((c for c in chans.get("channels") or []
                    if c.get("name") == "general"), None)
        check("general channel listed", gen is not None, chans)
        if not gen:
            return
        res = link_request_json("/api/v1/stream/subscribe",
                                {"channel": gen["id"]})
        check("subscribe ok", bool(res) and res.get("ok") == 1, res)
        if not res or not res.get("ok"):
            return
        # -- post over the SAME link (RNS is a per-process singleton, so
        #    no second instance: a second link to the node would carry the
        #    same identity anyway). Subscribing is link-1's state; posting
        #    via the JSON v1 API emits server-side and the guild PUSHES the
        #    event back down the link. No poll in between. --
        posted = link_request_json("/api/v1/post",
                                   {"channel": gen["id"],
                                    "content": "push-arrival-marker"})
        check("post accepted", bool(posted) and posted.get("ok") == 1, posted)

        # -- assert the PUSH (no stream/poll call anywhere) --
        deadline = time.time() + 60
        while time.time() < deadline and not push_frames:
            time.sleep(0.2)
        check("push frame arrived (no poll)", bool(push_frames),
              "frames=%d" % len(push_frames))
        got_msg = False
        for fr in push_frames:
            for ev in fr.get("events") or []:
                msg = ev.get("data", {}).get("msg") or {}
                if "push-arrival-marker" in (msg.get("content") or ""):
                    got_msg = True
        check("push carries the posted message", got_msg)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
