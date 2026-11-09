#!/usr/bin/env python3
"""Standalone DM peer for the integ test (U7).

Runs as its OWN process with its OWN RNS instance, so it is a genuine
second node on the mesh (two LXMF routers in one process share a single
RNS transport and therefore can never learn a path to each other — the
path has to come over the wire).

Usage:
  dm_peer.py <rns-configdir> <target_delivery_hash_hex> <message>

It announces its own lxmf.delivery destination, waits for a transport path
to the target's delivery destination, then sends one DIRECT LXMF message.
Prints "SENT" on success, "ERR:<reason>" on failure.
"""
import os
import sys
import time

sys.path.insert(0, "/usr/lib/python3.14/site-packages")
import RNS    # noqa: E402
import LXMF   # noqa: E402


def main():
    if len(sys.argv) < 4:
        print("ERR:usage")
        return 2
    configdir = sys.argv[1]
    target_hash = bytes.fromhex(sys.argv[2])
    message = sys.argv[3]

    os.makedirs(configdir, exist_ok=True)
    RNS.Reticulum(configdir=configdir)   # noqa: F841 (kept alive by the module)
    time.sleep(1.0)
    identity = RNS.Identity()
    router = LXMF.LXMRouter(identity=identity, storagepath=configdir)
    delivery = router.register_delivery_identity(identity,
                                                 display_name="DMPeer")
    router.announce(delivery.hash)
    time.sleep(1.5)

    # wait for a transport path to the target's delivery destination
    for _ in range(60):
        if RNS.Transport.has_path(target_hash):
            break
        RNS.Transport.request_path(target_hash)
        time.sleep(0.5)
    if not RNS.Transport.has_path(target_hash):
        print("ERR:no_path")
        return 3

    nid = RNS.Identity.recall(target_hash)
    if nid is None:
        print("ERR:no_identity")
        return 4

    dest = RNS.Destination(nid, RNS.Destination.OUT, RNS.Destination.SINGLE,
                           "lxmf", "delivery")
    lxm = LXMF.LXMessage(dest, delivery, message, title="sHades dm",
                         desired_method=LXMF.LXMessage.DIRECT)
    router.handle_outbound(lxm)

    # give the message time to be transmitted before the process exits
    for _ in range(40):
        if lxm.state == LXMF.LXMessage.DELIVERED:
            print("SENT")
            return 0
        time.sleep(0.5)
    print("SENT" if lxm.state != LXMF.LXMessage.FAILED else "ERR:failed")
    return 0 if lxm.state != LXMF.LXMessage.FAILED else 5


if __name__ == "__main__":
    sys.exit(main())
