
"""Voice room roster tests (tay build order item 2). No RNS mesh needed.

Covers, per the p2p plan (hub = directory, never the relay):
  - a voice channel can exist (store.CHANNEL_KINDS carries "voice");
  - join populates the room, drops the joiner from any other room, and
    emits a voice_state roster event on the room's channel;
  - leave empties it and the room is gone;
  - join on a non-voice channel is refused;
  - join without a dest is refused;
  - rooms listing carries the remaining roster;
  - a member without a live link is swept from the room.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail)


base = tempfile.mkdtemp(prefix="shades_voice_test_")
sHades.STORAGE = base


def mkid():
    i = RNS.Identity()
    return i, sHades.hexstr(RNS.Destination(
        i, RNS.Destination.OUT, RNS.Destination.SINGLE,
        "lxmf", "delivery").hash)


a_ident, ALICE = mkid()
b_ident, BOB = mkid()

sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
for uh, name in ((ALICE, "alice"), (BOB, "bob")):
    sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                      "VALUES (?, ?, ?)", (uh, name, sHades.now()))
sHades.db.commit()
sHades.load_role_seeds(base)
everyone_role = sHades._ensure_role("everyone", sHades.PERMS["VIEW"])
sHades._assign_role(ALICE, everyone_role)
sHades._assign_role(BOB, everyone_role)
sHades.DEBUG = False


def call(path, data=None, ident=None):
    ident = ident or a_ident
    payload = sHades.json.dumps(
        data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1",
                                  ident, sHades.now())
    return sHades.json.loads(raw.decode("utf-8"))


def voice_cid(name="Lounge"):
    return sHades.store.ensure_channel(
        sHades.db, sHades.db_lock, "testguild", name, kind="voice")


def last_event(cid):
    ring = sHades.EVENT_LOG.get(cid)
    if not ring:
        return None
    return ring[-1][1]


def main():
    check("voice is a channel kind", "voice" in sHades.store.CHANNEL_KINDS)

    cid = voice_cid()
    chat_cid = sHades.store.ensure_channel(
        sHades.db, sHades.db_lock, "testguild", "General", kind="chat")
    check("voice channel row carries the kind",
          sHades.store.channel_row(sHades.db, sHades.db_lock,
                                   cid)["kind"] == "voice")

    # join gates
    r = call("/api/v1/voice/join", {"channel": "nope",
                                    "dest": "aabbccdd11223344"}, ident=a_ident)
    check("join on unknown channel refused", r.get("code") == 404, r)
    r = call("/api/v1/voice/join", {"channel": chat_cid,
                                    "dest": "aabbccdd11223344"}, ident=a_ident)
    check("join on a chat channel refused", r.get("code") == 400, r)
    r = call("/api/v1/voice/join", {"channel": cid, "dest": ""},
             ident=a_ident)
    check("join without a dest refused", r.get("code") == 400, r)
    stranger_ident, STRANGER = mkid()
    sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                      "VALUES (?, ?, ?)", (STRANGER, "stranger", sHades.now()))
    sHades.db.commit()
    # a voice channel whose @everyone overwrite denies VIEW; the open
    # default grants VIEW to every registered user, so the refusal must
    # come from the overwrite
    cid_locked = voice_cid("Locked Booth")
    sHades.store.set_channel_overwrite(sHades.db, sHades.db_lock,
                                       cid_locked, 0, 0)
    sHades.db.commit()
    r = call("/api/v1/voice/join",
             {"channel": cid_locked, "dest": "00ff00ff00ff00ff"},
             ident=stranger_ident)
    check("join without view permission refused", r.get("code") == 403, r)

    # alice joins
    r = call("/api/v1/voice/join",
             {"channel": cid, "dest": "aa11bb22cc33dd44", "muted": False},
             ident=a_ident)
    check("alice joins", r.get("ok") == 1, r)
    check("roster on join answers the one member",
          [m["viewer"] for m in r.get("members", [])] == [ALICE], r)
    ev = last_event(cid)
    check("join emitted a voice_state roster event",
          ev and ev.get("kind") == "voice_state", dict(ev or {}))
    check("roster event carries the dest",
          ev and ev["data"]["members"][0]["dest"] == "aa11bb22cc33dd44",
          ev and ev["data"])

    # bob joins the same room; rooms listing shows both
    call("/api/v1/voice/join",
         {"channel": cid, "dest": "5566778800aabbcc"}, ident=b_ident)
    r = call("/api/v1/voice/rooms", ident=a_ident)
    room = r.get("rooms", {}).get(cid)
    check("rooms listing carries two members",
          room and len(room["members"]) == 2, r)

    # switching rooms empties the old one
    cid2 = voice_cid("Backstage")
    call("/api/v1/voice/join", {"channel": cid2, "dest": "5566778800aabbcc"},
         ident=b_ident)
    old = call("/api/v1/voice/rooms", ident=a_ident).get("rooms", {})
    check("bob appears in the new room, single roster per viewer",
          set(old.keys()) == {cid, cid2}
          and {m["viewer"] for m in old[cid]["members"]} == {ALICE}
          and {m["viewer"] for m in old[cid2]["members"]} == {BOB}, old)

    # leave + sweep
    r = call("/api/v1/voice/leave", ident=BOB and b_ident)
    check("leave answers the touched rooms", cid2 in (r.get("left") or []), r)
    r = call("/api/v1/voice/rooms", ident=a_ident)
    check("room listing after leave holds alice only",
          set(r.get("rooms", {}).keys()) == {cid}, r)

    dead = sHades.VOICE_ROOMS.get(cid)
    check("room survives with alice", cid in (sHades.VOICE_ROOMS or {}),
          str(dead))

    # staleness sweep: timestamps forced past the cutoff, next join sweeps
    _sweep_check()
    check("staleness sweep emptied the room",
          not sHades.VOICE_ROOMS, str(sHades.VOICE_ROOMS))

    print("RESULT: %d pass, %d fail" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


def _sweep_check():
    for cid, members in list(sHades.VOICE_ROOMS.items()):
        for m in members.values():
            m["at"] = 0
    r = call("/api/v1/voice/rooms", ident=a_ident)
    return r.get("ok") == 1


if __name__ == "__main__":
    main()
