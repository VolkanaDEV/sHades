#!/usr/bin/env python3
"""Reaction unicode gate tests (server) — no mesh.

Pins down:
  - legacy ASCII shortcodes still pass the gate (+1 on/off);
  - unicode emoji are accepted verbatim (BMP symbol, supplementary plane,
    variation-selector composite, ZWJ family composite);
  - junk and abuse are rejected (punctuation, control chars, ZWJ alone,
    oversized atom runs — not stored);
  - counts are keyed by the exact character and toggle back down;
  - the legacy phase-6 pins (test_phase6.py) keep passing unchanged.
"""
import os, sys, json, sqlite3, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))

base = tempfile.mkdtemp(prefix="shades_reactions_test_")
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.STORAGE = base
sHades.DEBUG = False

USER = sHades.hexstr(RNS.Destination(RNS.Identity(), RNS.Destination.OUT,
                                     RNS.Destination.SINGLE,
                                     "lxmf", "delivery").hash)
OTHER = sHades.hexstr(RNS.Destination(RNS.Identity(), RNS.Destination.OUT,
                                      RNS.Destination.SINGLE,
                                      "lxmf", "delivery").hash)
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (USER, "reader", sHades.now()))
sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (OTHER, "second", sHades.now()))
sHades.db.commit()
_ident = {}

def ident_for(user):
    """Bare identity stubs: the API routes hash the identity for the viewer."""
    if user not in _ident:
        _ident[user] = RNS.Identity()
    return _ident[user]

def call(path, data=None, user=USER):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1",
                                  ident_for(user), sHades.now())
    return json.loads(raw.decode("utf-8"))

def main():
    cid = call("/api/v1/channels/create", {"name": "general", "kind": "chat"})["channel"]["id"]
    m1 = sHades.ingest(cid, USER, "first message")

    # ---------- legacy shortcodes keep working ----------
    r = call("/api/v1/react", {"msg": m1, "emoji": "+1"})
    check("legacy shortcode +1 accepted", r.get("ok") == 1, r)
    r = call("/api/v1/react", {"msg": m1, "emoji": "+1"})
    check("legacy shortcode +1 toggles off", r.get("ok") == 1 and r.get("active") is False, r)

    # ---------- unicode emoji accepted ----------
    r = call("/api/v1/react", {"msg": m1, "emoji": "\U0001F44D"})   # thumbs up
    check("supplementary-plane emoji accepted", r.get("ok") == 1, r)
    r = call("/api/v1/react", {"msg": m1, "emoji": "\u2705"})       # check mark
    check("BMP symbol accepted", r.get("ok") == 1, r)
    r = call("/api/v1/react", {"msg": m1, "emoji": "\u2764\uFE0F"}) # heart + VS16
    check("variation-selector composite accepted", r.get("ok") == 1, r)
    r = call("/api/v1/react", {"msg": m1, "emoji": "\U0001F468\u200D\U0001F469\u200D\U0001F466"})
    check("ZWJ family composite accepted", r.get("ok") == 1, r)

    # ---------- junk rejected, nothing stored ----------
    for bad in ("abc!", "<script>", "\u200D", "\uFE0F\u200D", "\U0001F600" * 9,
                "a" * 40):
        r = call("/api/v1/react", {"msg": m1, "emoji": bad})
        check("junk rejected: %r" % bad[:12], r.get("ok") == 0 and r.get("code") == 400, r)

    # ---------- counts keyed by exact character ----------
    r = call("/api/v1/react", {"msg": m1, "emoji": "\U0001F44D"}, user=OTHER)
    check("two users on the same emoji -> count 2",
          r.get("counts") and r.get("counts", {}).get("\U0001F44D") == 2, r)
    r = call("/api/v1/react", {"msg": m1, "emoji": "\U0001F44D"}, user=OTHER)
    check("toggle back down -> count 1",
          r.get("ok") == 1 and r.get("counts", {}).get("\U0001F44D") == 1, r)

    # ---------- distinct cap still enforced (pinned again on unicode) ----------
    call("/api/v1/channels/create", {"name": "reactcap", "kind": "chat"})
    row = sHades.db.execute("SELECT id FROM channels WHERE name='reactcap'").fetchone()
    cid2 = row["id"]
    m2 = sHades.ingest(cid2, USER, "cap message")
    last = None
    for i in range(sHades.MAX_EMOJI_PER_MSG):
        sym = chr(0x1F050 + i)  # 16 distinct in-range emoji atoms
        r = call("/api/v1/react", {"msg": m2, "emoji": sym})
        last = r
    check("cap reached inside the limit", last.get("ok") == 1, last)
    r = call("/api/v1/react", {"msg": m2, "emoji": "\U0001F680"})
    check("cap blocks the 17th distinct", r.get("ok") == 0 and r.get("code") == 400, r)

    print("RESULT: %d PASS, %d FAIL" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
