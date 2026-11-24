#!/usr/bin/env python3
"""Regression test for fixes.md #13 (Critical): /api/messages since_id
pagination.

The client polls with since_id set to the last seen message's msg_id
HASH string, not the integer PK. The pre-fix query compared the integer
`id` column directly against that string (`id > '<hash>'`); in SQLite an
INTEGER is always less than TEXT, so every delta poll returned zero rows
and incremental sync silently broke. store.fetch_messages must resolve a
string since_id to the integer PK first.

Run:  python tests/test_fix13_since_id.py
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import store  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


def main():
    base = tempfile.mkdtemp(prefix="shades_fix13_test_")
    conn = store.open_db(os.path.join(base, "sHades.db"))
    lock = threading.Lock()
    conn.execute("INSERT INTO channels (id, name) VALUES ('general', 'general')")
    hashes = ["hash-%04d" % i for i in range(1, 6)]
    for i, h in enumerate(hashes, start=1):
        conn.execute(
            "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content)"
            " VALUES (?, 'general', ?, 'author', ?)",
            (h, float(i), "message %d" % i))
    conn.commit()

    def rows(since_id, since_ts=0):
        seq, new_rows, upd_rows = store.fetch_messages(
            conn, lock, "general", since_id, since_ts, None, 200)
        return [r["msg_id"] for r in new_rows]

    check("string since_id (hash of msg 2) returns rows 3,4,5",
          rows(hashes[1]) == hashes[2:])
    check("int since_id 2 returns rows 3,4,5",
          rows(2) == hashes[2:])
    check("string since_id (hash of msg 5) returns no rows",
          rows(hashes[4]) == [])
    check("unknown hash since_id returns no rows, not an error",
          rows("no-such-hash") == [])
    check("no since_id returns full history in order",
          rows(None) == hashes)
    check("string and int since_id agree",
          rows(hashes[1]) == rows(2))

    # The bug mechanism, documented: SQLite orders INTEGER < TEXT, so the
    # pre-fix comparison could never match a row.
    pre_fix = conn.execute(
        "SELECT msg_id FROM messages WHERE channel_id='general'"
        " AND id > ? ORDER BY id ASC", (hashes[1],)).fetchall()
    check("pre-fix query (id > hash string) returns nothing",
          pre_fix == [])

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
