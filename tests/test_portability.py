"""Portability: edit history, paged own-post export, erasure, leave, catchup.

Adapted from the fork's tests/test_portability.py onto our no-mesh server
fixture (test_admin.py pattern: direct API_ROUTES dispatch against a temp
STORAGE + sqlite). Divergences documented inline:
  - /api/v1/att/preview assertions dropped: the preview endpoint rides the
    attachment-UI batch (previews are generated there, not in this slice).
  - blobs/get gating assertions dropped: our blobs getter is not visibility-
    gated yet (any identified link can read by digest); the fork's is. That
    divergence is a separate adjudication, not silent.
Search assertions dropped too: /api/v1/search predates this slice and its
behavioral contract is owned by the search tests.
"""
import json
import os
import sqlite3
import sys
import tempfile
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades as server  # noqa: E402
import RNS  # noqa: E402


def identity():
    key = RNS.Identity()
    hash_ = server.hexstr(RNS.Destination(key, RNS.Destination.OUT,
                                          RNS.Destination.SINGLE,
                                          "lxmf", "delivery").hash)
    return key, hash_


def call(route, user, data=None):
    response = server.API_ROUTES[route](route, json.dumps(data or {}).encode(),
                                        "req", "link", user, server.now())
    return json.loads(response)


def main():
    with tempfile.TemporaryDirectory() as folder:
        server.STORAGE = folder
        server.db = sqlite3.connect(os.path.join(folder, "guild.db"))
        server.db.row_factory = sqlite3.Row
        server.db.executescript(server.SCHEMA)
        server._migrate_schema(server.db)
        alice, ah = identity()
        bob, bh = identity()
        outside, _ = identity()
        channel = "c" * 64
        server.db.execute("INSERT INTO channels (id,name,created) VALUES (?,?,?)",
                          (channel, "general", server.now()))
        server.db.execute("INSERT INTO channel_state VALUES (?,0)", (channel,))
        for hash_, name in ((ah, "Alice"), (bh, "Bob")):
            server.db.execute("INSERT INTO users(dest_hash,username) VALUES (?,?)",
                              (hash_, name))
        for n in range(105):
            server.db.execute(
                "INSERT INTO messages(msg_id,channel_id,ts,author_hash,content) "
                "VALUES (?,?,?,?,?)", ("m%03d" % n, channel, n, ah, "hello world"))
        image = BytesIO()
        from PIL import Image
        Image.new("RGB", (300, 300), (33, 160, 108)).save(image, "PNG")
        stored = server.store_attachment("art.png", "image/png", image.getvalue())
        assert stored is not None
        blob = server._blob_commit(image.getvalue(), "art.png", "image/png", by=ah)
        assert blob and blob["ok"] == 1
        server.db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,?)",
                          ("m000", 0, "art.png", "image/png", len(image.getvalue()),
                           stored))
        attachment = os.path.join(folder, "attachments", stored)
        blob_file = os.path.join(folder, "blobs", blob["blob_id"] + ".png")
        server.db.execute("INSERT INTO receipts(channel_id,user_hash,last_id,ts) "
                          "VALUES (?,?,?,?)", (channel, bh, 102, server.now()))
        server.db.commit()

        summary = call("/api/v1/catchup", bob)
        assert summary["ok"] == 1 and summary["channels"][0]["count"] == 3, summary

        history = call("/api/v1/messages/edits", bob, {"msg": "m004"})
        assert history["ok"] == 1 and history["edits"] == []

        legacy = server.API_ROUTES["/api/edit"](
            "/api/edit", {"msg": "m001", "content": "updated"},
            "req", "link", alice, server.now())
        assert server.msgpack.unpackb(legacy)["ok"] == 1
        edits = call("/api/v1/messages/edits", bob, {"msg": "m001"})
        assert edits["ok"] == 1 and edits["edits"][0]["prior_content"] == "hello world"

        first = call("/api/v1/account/export", alice, {"limit": 100})
        second = call("/api/v1/account/export", alice,
                      {"after": first["next_after"]})
        assert len(first["messages"]) == 100 and len(second["messages"]) == 5
        assert call("/api/v1/account/export", outside)["messages"] == []
        assert first["messages"][0]["edits"] == [] or isinstance(
            first["messages"][0]["edits"], list)

        assert call("/api/v1/account/delete-posts", alice)["code"] == 400
        erased = call("/api/v1/account/delete-posts", alice,
                      {"confirm": "DELETE MY POSTS"})
        assert erased["ok"] == 1 and erased["deleted"] == 105, erased
        assert server.db.execute("SELECT count(*) FROM messages WHERE content!='' "
                                 "AND author_hash=?", (ah,)).fetchone()[0] == 0
        assert not os.path.exists(attachment)
        assert not os.path.exists(blob_file)
        assert server.db.execute("SELECT count(*) FROM blobs "
                                 "WHERE id=?", (blob["blob_id"],)).fetchone()[0] == 0
        assert call("/api/v1/messages/edits", bob, {"msg": "m001"})["code"] == 404
        assert call("/api/v1/account/export", alice)["messages"][0]["content"] == ""

        assert call("/api/v1/account/leave", alice)["code"] == 400
        server.MODERATORS.add(ah)
        server.ADMIN_USERS.add(ah)
        assert call("/api/v1/account/leave", alice,
                    {"confirm": "LEAVE GUILD"})["ok"] == 1
        assert ah not in server.MODERATORS and ah not in server.ADMIN_USERS
        assert not server.db.execute("SELECT 1 FROM users WHERE dest_hash=?",
                                     (ah,)).fetchone()
        server.db.close()
    print("PASS  edit history, paged export, erasure, leave and catchup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
