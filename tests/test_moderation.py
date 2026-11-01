#!/usr/bin/env python3
"""Moderation unit tests for sHades — no RNS mesh needed.

Covers: load_moderators seeding, ban_user / unban_user, is_banned,
and can_delete authorisation. Exercises the code path that was
never invoked at boot (load_moderators was missing from run()).
"""
import os, sys, tempfile, sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

def main():
    base = tempfile.mkdtemp(prefix="shades_mod_test_")
    db_path = os.path.join(base, "sHades.db")
    mod_path = os.path.join(base, "moderators.txt")

    sHades.db = sqlite3.connect(db_path)
    sHades.db.row_factory = sqlite3.Row
    sHades.db.executescript(sHades.SCHEMA)
    sHades.db.commit()

    MOD_HASH = "a" * 32
    USER_HASH = "b" * 32
    BANNED_HASH = "c" * 32
    MUTED_HASH = "d" * 32

    # --- load_moderators seeding ---
    with open(mod_path, "w") as f:
        f.write("# comment line\n" + MOD_HASH + "\n" + USER_HASH + "\n")
    count = sHades.load_moderators(mod_path)
    check("load_moderators seeds two moderators", count == 2, count)
    check("MODERATORS contains mod", MOD_HASH in sHades.MODERATORS)
    check("MODERATORS contains user", USER_HASH in sHades.MODERATORS)

    # invalid lines ignored
    with open(mod_path, "w") as f:
        f.write("not-a-hash\n" + "z" * 31 + "\n" + MOD_HASH + "\n")
    sHades.load_moderators(mod_path)
    check("load_moderators ignores invalid lines", len(sHades.MODERATORS) == 1)

    # --- ban / unban / is_banned ---
    check("not banned initially", not sHades.is_banned(BANNED_HASH))
    sHades.ban_user(BANNED_HASH, "spam")
    check("is_banned after ban", sHades.is_banned(BANNED_HASH))
    sHades.unban_user(BANNED_HASH)
    check("not banned after unban", not sHades.is_banned(BANNED_HASH))

    # --- mute / unmute / is_muted ---
    check("not muted initially", not sHades.is_muted(MUTED_HASH))
    sHades.mute_user(MUTED_HASH, "spammy", MOD_HASH)
    check("is_muted after mute", sHades.is_muted(MUTED_HASH))
    sHades.unmute_user(MUTED_HASH)
    check("not muted after unmute", not sHades.is_muted(MUTED_HASH))

    # --- can_delete ---
    # create a message row owned by USER_HASH
    sHades.db.execute(
        "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
        "VALUES (?, ?, ?, ?, ?)",
        ("msg_owned", "chan_1", 1.0, USER_HASH, "hello"))
    sHades.db.commit()

    class Row(dict): pass
    row = sHades.db.execute(
        "SELECT * FROM messages WHERE msg_id=?", ("msg_owned",)).fetchone()

    check("author can delete own message",
          sHades.can_delete(USER_HASH, row))
    check("moderator can delete anyone's message",
          sHades.can_delete(MOD_HASH, row))
    check("random user cannot delete", not sHades.can_delete("d" * 32, row))
    check("unidentified cannot delete", not sHades.can_delete(None, row))

    # moderator set is live
    check("MODERATORS still has mod after tests", MOD_HASH in sHades.MODERATORS)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL); sys.exit(1)

if __name__ == "__main__":
    main()
