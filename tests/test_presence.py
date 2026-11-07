#!/usr/bin/env python3
"""Presence unit tests for sHades — no RNS mesh needed.

Presence is the IRC-style ping list: every client re-registers (pings) the guild
every NODE_ANNOUNCE_INTERVAL seconds, and a client that misses NODE_MISS_LIMIT
consecutive pings is dropped. What these tests pin down:

  1. presence is *derived* from the ping registry, so stale users are absent
     rather than flagged — the member list that mirrors it prunes for free;
  2. the reaper actually deletes stale entries;
  3. one user with several registered nodes still counts as one presence entry;
  4. the user identity comes from the identified RNS link, NOT the request
     payload — a client cannot report someone else as online, or claim a
     victim's identity by pinging on their behalf.
"""
import os, sys, tempfile, sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402
from RNS.vendor import umsgpack as msgpack  # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

USER_A = "a" * 32
USER_B = "b" * 32
NODE_A = "1" * 32
NODE_B = "2" * 32
NODE_A2 = "3" * 32


def ping(node_hash, user_hash, age=0.0, address="10.0.0.1", port=29222):
    """Record a ping directly, as api_register_node would (age in seconds)."""
    with sHades.NODE_REGISTRY_LOCK:
        sHades.NODE_REGISTRY[node_hash] = {
            "address": address, "port": port,
            "last_seen": sHades.now() - age,
            "user_hash": user_hash,
        }


def main():
    base = tempfile.mkdtemp(prefix="shades_presence_test_")
    sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
    sHades.db.row_factory = sqlite3.Row
    sHades.db.executescript(sHades.SCHEMA)
    sHades.db.commit()
    sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                      (USER_A, "alice", sHades.now()))
    sHades.db.commit()
    sHades.DEBUG = False

    # --- stated requirements: 4s ping cadence, drop after 2 misses ---
    check("ping interval is 4s", sHades.NODE_ANNOUNCE_INTERVAL == 4.0,
          sHades.NODE_ANNOUNCE_INTERVAL)
    check("stale threshold is 2 missed pings = 8s", sHades.NODE_STALE_S == 8.0,
          sHades.NODE_STALE_S)

    # --- empty presence ---
    sHades.NODE_REGISTRY.clear()
    check("no pings -> nobody online", sHades.online_users() == {})

    # --- a fresh ping is present, keyed by user hash ---
    ping(NODE_A, USER_A)
    users = sHades.online_users()
    check("fresh ping is online", USER_A in users, users)
    check("presence keyed by user hash, not node hash",
          USER_A in users and NODE_A not in users, users)
    check("presence carries the node hash for profile lookup",
          users[USER_A]["node_hash"] == NODE_A, users.get(USER_A))
    check("username resolved from the users table",
          users[USER_A]["username"] == "alice", users.get(USER_A))

    # --- a stale ping is absent BEFORE the reaper even runs ---
    ping(NODE_B, USER_B, age=sHades.NODE_STALE_S + 1)
    users = sHades.online_users()
    check("stale ping is not online", USER_B not in users, users)
    check("stale entry still in the raw registry until reaped",
          NODE_B in sHades.NODE_REGISTRY)

    # --- the reaper deletes it ---
    reaped = sHades._reap_stale_nodes()
    check("reaper reports the stale node", NODE_B in reaped, reaped)
    check("reaper deleted the stale entry", NODE_B not in sHades.NODE_REGISTRY)
    check("reaper left the fresh entry alone", NODE_A in sHades.NODE_REGISTRY)

    # --- boundary: just inside the window is still online ---
    sHades.NODE_REGISTRY.clear()
    ping(NODE_B, USER_B, age=sHades.NODE_STALE_S - 0.5)
    check("ping just inside the stale window is online",
          USER_B in sHades.online_users())

    # --- one user, several nodes -> one presence entry, newest wins ---
    sHades.NODE_REGISTRY.clear()
    ping(NODE_A, USER_A, age=3.0, port=1111)
    ping(NODE_A2, USER_A, age=0.0, port=2222)
    users = sHades.online_users()
    check("multi-node user counts once", len(users) == 1, users)
    check("newest ping wins for the node hash",
          users[USER_A]["node_hash"] == NODE_A2, users.get(USER_A))

    # --- an entry with no user hash is ignored (defensive) ---
    sHades.NODE_REGISTRY.clear()
    ping(NODE_A, None)
    check("entry without a user hash is excluded", sHades.online_users() == {})

    # --- the /api/presence wire shape ---
    sHades.NODE_REGISTRY.clear()
    ping(NODE_A, USER_A)
    ping(NODE_B, USER_B, age=sHades.NODE_STALE_S + 5)
    caller = RNS.Identity()
    body = sHades.api_presence(None, {}, "req", "link", caller, sHades.now())
    res = msgpack.unpackb(body, raw=False)
    check("api/presence ok", res.get("ok") == 1, res)
    check("api/presence reports the ping cadence",
          res.get("interval") == sHades.NODE_ANNOUNCE_INTERVAL and
          res.get("ttl") == sHades.NODE_STALE_S, res)
    present = {u["user_hash"] for u in res.get("users", [])}
    check("api/presence lists the online user", USER_A in present, present)
    check("api/presence omits the stale user", USER_B not in present, present)
    # Strictly ping-driven: asking is not being present. The caller here has
    # never pinged, so it must not appear just for having made the request.
    check("api/presence does not invent presence for the requester",
          sHades.viewer_of(caller) not in present, present)
    check("api/presence user entries carry name + node",
          all(k in u for u in res.get("users", [])
              for k in ("user_hash", "username", "node_hash")), res.get("users"))

    # --- spoof resistance: identity comes from the link, not the payload ---
    sHades.NODE_REGISTRY.clear()
    attacker = RNS.Identity()
    attacker_viewer = sHades.viewer_of(attacker)
    victim = "c" * 32
    check("attacker's link identity is not the victim's",
          attacker_viewer != victim, attacker_viewer)
    sHades.api_register_node(None, {
        "node_hash": NODE_A, "address": "10.0.0.9", "port": 29222,
        "user_hash": victim,        # forged
        "delivery_hash": victim,    # forged (legacy field)
    }, "req", "link", attacker, sHades.now())
    entry = sHades.NODE_REGISTRY.get(NODE_A, {})
    check("registered entry attributes the link identity",
          entry.get("user_hash") == attacker_viewer, entry)
    check("payload-supplied identity is ignored, not stored",
          victim not in (entry.get("user_hash"), entry.get("delivery_hash")), entry)
    users = sHades.online_users()
    check("victim is NOT reported online by a forged ping", victim not in users, users)
    check("attacker is reported online under their own identity",
          attacker_viewer in users, users)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL); sys.exit(1)

if __name__ == "__main__":
    main()
