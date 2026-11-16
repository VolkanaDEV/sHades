#!/usr/bin/env python3
"""Presence unit tests for sHades — no RNS mesh needed (v0.29).

Presence is LINK LIVENESS: RNS.Link keepalives and teardowns already monitor
connection health (tay: "those are literally pings baked in"), so the guild
derives the online set from RNS.Transport.active_links and binds every
identified request's link_id -> user_hash. register_node is a ONE-SHOT
directory write of the node destination hash -- it is not a heartbeat. What
these tests pin down:

  1. online == live link bound + directory entry; there is no ping cadence,
     no stale window, no ttl on presence anymore;
  2. the keeper sweep prunes a dead link's binding AND its directory entry
     (the node is unreachable) in one pass;
  3. one user with several registered nodes still counts as one presence
     entry, newest directory entry wins;
  4. the user identity comes from the RNS link handshake, NOT the request
     payload — a client cannot report someone else as online, or claim a
     name that isn't theirs; address/port fields are ignored outright.
"""
import os, sys, sqlite3, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402
from RNS.vendor import umsgpack as msgpack  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


USER_A = "a" * 32
USER_B = "b" * 32
NODE_A = "1" * 32
NODE_B = "2" * 32
NODE_A2 = "3" * 32


def ping(node_hash, user_hash, age=0.0):
    """Write a directory entry directly, as api_register_node's success
    path does (age in seconds)."""
    with sHades.NODE_REGISTRY_LOCK:
        sHades.NODE_REGISTRY[node_hash] = {
            "last_seen": sHades.now() - age,
            "user_hash": user_hash,
        }


class _LiveLink:
    """Stand-in for an active_links member (test_streams doctrine)."""

    def __init__(self, lid, status=RNS.Link.ACTIVE):
        self.link_id = lid
        self.status = status


def bind(lid, user_hash, status=RNS.Link.ACTIVE):
    """Do what wrap_api does on an identified request: bind the link."""
    RNS.Transport.active_links.append(_LiveLink(lid, status))
    with sHades.ONLINE_LINKS_LOCK:
        sHades.ONLINE_LINKS[lid] = user_hash


def main():
    base = tempfile.mkdtemp(prefix="shades_presence_test_")
    sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
    sHades.db.row_factory = sqlite3.Row
    sHades.db.executescript(sHades.SCHEMA)
    sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at)"
                      " VALUES (?,?,?)", (USER_A, "alice", sHades.now()))
    sHades.db.commit()
    sHades.DEBUG = False

    saved_links = list(RNS.Transport.active_links)
    saved_bindings = dict(sHades.ONLINE_LINKS)
    saved_subs = dict(sHades.SUBSCRIPTIONS)
    try:
        # --- empty presence ---
        RNS.Transport.active_links.clear()
        with sHades.NODE_REGISTRY_LOCK:
            sHades.NODE_REGISTRY.clear()
        check("no live links -> nobody online", sHades.online_users() == {})

        # --- a live link with a registered node is online ---
        bind("L1", USER_A)
        ping(NODE_A, USER_A)
        users = sHades.online_users()
        check("live link + directory entry is online", USER_A in users, users)
        check("presence keyed by user hash, not node hash",
              USER_A in users and NODE_A not in users, users)
        check("presence carries the node hash for profile lookup",
              users[USER_A]["node_hash"] == NODE_A, users.get(USER_A))
        check("username resolved from the users table",
              users[USER_A]["username"] == "alice", users.get(USER_A))

        # --- link death prunes binding AND directory entry in one sweep ---
        RNS.Transport.active_links[-1].status = RNS.Link.CLOSED
        users = sHades.online_users()
        # The dead link can still be listed until the keeper sweep runs --
        # the documented drop window is "within at most the keeper's 1s
        # sweep" (the real server sweeps every second).
        check("dead link still listed until the sweep", USER_A in users, users)
        with sHades.NODE_REGISTRY_LOCK:
            still_listed = NODE_A in sHades.NODE_REGISTRY
        check("directory entry survives until the sweep", still_listed)
        reaped = sHades._reap_stale_nodes()
        check("keeper reports the unreachable node", NODE_A in reaped, reaped)
        with sHades.NODE_REGISTRY_LOCK:
            pruned = NODE_A not in sHades.NODE_REGISTRY
        check("keeper deleted the unreachable node's entry", pruned)
        with sHades.ONLINE_LINKS_LOCK:
            unbound = "L1" not in sHades.ONLINE_LINKS
        check("keeper dropped the dead link's binding", unbound)

        # --- a directory entry with no live link is invisible (and reaped) ---
        ping(NODE_B, None)
        check("directory entry without a live link is excluded",
              sHades.online_users() == {})

        # --- one user, several nodes -> one entry, newest wins ---
        bind("L1", USER_A)
        bind("L2", USER_A)
        ping(NODE_A, USER_A, age=3.0)
        ping(NODE_A2, USER_A)
        users = sHades.online_users()
        check("multi-node user counts once", len(users) == 1, users)
        check("newest directory entry wins for the node hash",
              users[USER_A]["node_hash"] == NODE_A2, users.get(USER_A))

        # --- the /api/presence wire shape (no cadence fields anymore) ---
        ping(NODE_A, USER_A)
        caller = RNS.Identity()
        body = sHades.api_presence(None, {}, "req", "link", caller, sHades.now())
        res = msgpack.unpackb(body, raw=False)
        check("api/presence ok", res.get("ok") == 1, res)
        check("api/presence reports link-derived mode",
              res.get("mode") == "link", res)
        check("presence carries no ping cadence fields",
              "interval" not in res and "ttl" not in res, list(res.keys()))
        present = {u["user_hash"] for u in res.get("users", [])}
        check("api/presence lists the online user", USER_A in present, present)
        # Strictly link-derived: asking is not being present. The caller here
        # has no live link, so it must not appear just for having made a
        # request.
        check("api/presence does not invent presence for the requester",
              sHades.viewer_of(caller) not in present, present)
        check("api/presence user entries carry name + node",
              all(k in u for u in res.get("users", [])
                  for k in ("user_hash", "username", "node_hash")),
              res.get("users"))

        # --- spoof resistance: identity comes from the link, not the payload ---
        with sHades.ONLINE_LINKS_LOCK:
            sHades.ONLINE_LINKS.clear()
        with sHades.NODE_REGISTRY_LOCK:
            sHades.NODE_REGISTRY.clear()
        attacker = RNS.Identity()
        attacker_viewer = sHades.viewer_of(attacker)
        victim = "c" * 32
        check("attacker's link identity is not the victim's",
              attacker_viewer != victim, attacker_viewer)
        bind("LA", attacker_viewer)
        sHades.api_register_node(attacker_viewer, {
            "node_hash": NODE_B, "address": "10.0.0.9", "port": 29222,
            "user_hash": victim,     # forged
            "delivery_hash": victim  # forged (legacy field)
        }, "req", "link", attacker, sHades.now())
        entry = sHades.NODE_REGISTRY.get(NODE_B, {})
        check("registered entry attributes the link identity",
              entry.get("user_hash") == attacker_viewer, entry)
        check("payload-supplied identity is ignored, not stored",
              victim not in (entry.get("user_hash"), entry.get("delivery_hash")),
              entry)
        check("older clients' address/port are ignored, not stored",
              "address" not in entry and "port" not in entry, entry)
        users = sHades.online_users()
        check("victim is NOT reported online by a forged registration",
              victim not in users, users)
        check("attacker is reported online under their own identity",
              attacker_viewer in users, users)
    finally:
        RNS.Transport.active_links[:] = saved_links
        with sHades.ONLINE_LINKS_LOCK:
            sHades.ONLINE_LINKS.clear()
            sHades.ONLINE_LINKS.update(saved_bindings)
        with sHades.SUB_LOCK:
            sHades.SUBSCRIPTIONS.clear()
            sHades.SUBSCRIPTIONS.update(saved_subs)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
