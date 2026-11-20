#!/usr/bin/env python3
"""Blobs + site-channel unit tests (JSON API v1 phases 3-4) — no RNS mesh.

Pins down:
  1. blobs/put|get: content-addressed storage, strict base64, chunked reads,
     digest-only lookup, size cap, rate limit;
  2. /api/v1/post with inline (data_b64) and referenced (blob_id) attachments;
  3. site channels: resource put/get (nested paths, traversal rejection),
     manifest get/put, site_patch stream events, moderator gating;
  4. the raw /file/blobs and /file/sites handlers (traversal guards included).
"""
import os, sys, json, sqlite3, tempfile, hashlib, base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(extra)) if extra and not cond else ""))

base = tempfile.mkdtemp(prefix="shades_blobs_test_")
sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.STORAGE = base
sHades.DEBUG = False

identity = RNS.Identity()
USER = sHades.hexstr(RNS.Destination(identity, RNS.Destination.OUT,
                                     RNS.Destination.SINGLE, "lxmf", "delivery").hash)
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (USER, "uploader", sHades.now()))
sHades.db.commit()
sHades.MODERATORS.add(USER)

# a second, non-mod identity for gating checks
other = RNS.Identity()
OTHER = sHades.hexstr(RNS.Destination(other, RNS.Destination.OUT,
                                      RNS.Destination.SINGLE, "lxmf", "delivery").hash)
sHades.db.execute("INSERT OR IGNORE INTO users (dest_hash, username, registered_at) VALUES (?,?,?)",
                  (OTHER, "pleb", sHades.now()))
sHades.db.commit()

def call(path, data=None, ident=identity, link_id="L1"):
    payload = json.dumps(data if data is not None else {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", link_id, ident, sHades.now())
    return json.loads(raw.decode("utf-8"))

def b64(b):
    return base64.b64encode(b).decode("ascii")

def main():
    cid_chat = call("/api/v1/channels/create", {"name": "general", "kind": "chat"})["channel"]["id"]
    cid_site = call("/api/v1/channels/create", {"name": "lounge", "kind": "site"})["channel"]["id"]

    # ---------- blobs: put + get ----------
    blob = bytes(range(256)) * 64          # 16 KB, content known
    digest = hashlib.sha256(blob).hexdigest()[:32]
    up = call("/api/v1/blobs/put", {"name": "shot.png", "mime": "image/png",
                                    "data_b64": b64(blob)})
    check("blob put ok, id is sha256[:32]", up.get("ok") == 1 and up.get("blob_id") == digest, up)
    check("blob put returns mime + path", up.get("mime") == "image/png"
          and up.get("path") == f"/file/blobs/{digest}.png", up)
    check("blob stored content-addressed",
          os.path.isfile(os.path.join(base, "blobs", f"{digest}.png")))

    r = call("/api/v1/blobs/put", {"name": "x", "data_b64": "!!!not-b64!!!"})
    check("invalid base64 rejected", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/blobs/put", {"name": "x", "data_b64": ""})
    check("empty blob rejected", r.get("ok") == 0 and r.get("code") == 400, r)

    got = call("/api/v1/blobs/get", {"blob_id": digest, "len": 1024 * 1024})
    check("blob get roundtrip", got.get("ok") == 1
          and base64.b64decode(got["data_b64"]) == blob, got.get("total"))
    check("blob get reports total + eof", got.get("total") == len(blob)
          and got.get("eof") is True, got)

    c1 = call("/api/v1/blobs/get", {"blob_id": digest, "offset": 0, "len": 1000})
    c2 = call("/api/v1/blobs/get", {"blob_id": digest, "offset": 1000, "len": 1000})
    check("chunked get slices correctly",
          base64.b64decode(c1["data_b64"]) == blob[:1000]
          and base64.b64decode(c2["data_b64"]) == blob[1000:2000]
          and c1["eof"] is False and c2["eof"] is False, (c1["eof"], c2["eof"]))
    tail = call("/api/v1/blobs/get", {"blob_id": digest,
                                      "offset": len(blob) - 10, "len": 1000})
    check("final chunk flagged eof", tail["eof"] is True
          and base64.b64decode(tail["data_b64"]) == blob[-10:], tail)
    beyond = call("/api/v1/blobs/get", {"blob_id": digest, "offset": len(blob) + 10})
    check("offset past end -> empty chunk, eof", beyond["ok"] == 1
          and beyond["data_b64"] == "" and beyond["eof"] is True, beyond)
    r = call("/api/v1/blobs/get", {"blob_id": "f" * 32})
    check("unknown blob_id -> 404", r.get("ok") == 0 and r.get("code") == 404, r)

    # raw /file/blobs handler
    out = sHades.serve_blob_file(f"/file/blobs/{digest}.png", None, "r", "L",
                                 identity, sHades.now())
    check("serve_blob_file serves stored bytes", out == blob, len(out))
    out = sHades.serve_blob_file("/file/blobs/../../etc/passwd", None, "r", "L", identity, sHades.now())
    check("serve_blob_file rejects traversal-ish names", out == b"not found", out)

    # ---------- chunked attachment reads (api_v1_att_get) ----------
    # receive-path chunking: one slice per request instead of the whole
    # file on a single-shot receipt (v0.31)
    data = bytes(range(256)) * 700          # ~175 KB, content known
    afname = sHades.store_attachment("big_note.txt", "text/plain", data)
    check("store_attachment returns a registered fname", bool(afname), afname)
    # tier-1 gate (tay fork port): a stored attachment is readable only
    # through a visible, non-deleted message. Mirror the shape ingest
    # persists so this pin exercises the receive path, not the bare
    # store primitive.
    sHades.db.execute(
        "INSERT INTO messages (msg_id, channel_id, ts, author_hash, content) "
        "VALUES ('m_att', ?, 1.0, ?, 'with big note')",
        (cid_chat, sHades.viewer_of(identity)))
    sHades.db.execute(
        "INSERT INTO attachments (msg_id, idx, name, mime, size, path) "
        "VALUES ('m_att', 0, 'big_note.txt', 'text/plain', ?, ?)",
        (len(data), afname))
    sHades.db.commit()
    r0 = call("/api/v1/att/get", {"path": "/file/attachments/" + afname,
                                  "offset": 0, "len": 100000})
    r1 = call("/api/v1/att/get", {"path": "/file/attachments/" + afname,
                                  "offset": 100000})
    check("att get roundtrips and slices",
          r0.get("ok") == 1 and r1.get("ok") == 1
          and base64.b64decode(r0["data_b64"]) == data[:100000]
          and base64.b64decode(r1["data_b64"]) == data[100000:]
          and r0.get("total") == len(data) and r1.get("eof") is True,
          (r0.get("ok"), r1.get("ok"), r0.get("total")))
    r = call("/api/v1/att/get", {"path": "/file/attachments/../../notes"})
    check("att get rejects non-blob-ish names", r.get("ok") == 0, r)

    # ---------- chunked uploads: init -> offset slices -> finish ----------
    # tay (v0.29-c): "The payload should be maximum of 10mb & automatically
    # chunked, not timer based." The ack carries next_offset; the sender is
    # data-driven, the server never waits on a timer.
    upb = bytes(range(256)) * 600          # 153,600 bytes
    updigest = hashlib.sha256(upb).hexdigest()[:32]
    st = call("/api/v1/blobs/put/init", {"name": "up.bin", "mime": "application/octet-stream",
                                         "size": len(upb)})
    check("upload init issues session + chunk size",
          st.get("ok") == 1 and len(st.get("upload_id", "")) == 32
          and st.get("chunk_size") == sHades.BLOB_UPLOAD_CHUNK, st)
    uid = st.get("upload_id", "")
    STEP = st["chunk_size"]
    acks = []
    for off in range(0, len(upb), STEP):
        piece = upb[off:off + STEP]
        r = call("/api/v1/blobs/put/chunk", {"upload_id": uid, "offset": off,
                                             "data_b64": b64(piece)})
        acks.append(r)
    check("every chunk acked with correct next_offset",
          all(r["ok"] == 1 and r["next_offset"] == min(off + STEP, len(upb))
              for r, off in zip(acks, range(0, len(upb), STEP))), acks[-1])
    final_r = acks[-1]
    check("last chunk flagged eof", final_r.get("eof") is True, final_r)
    fin = call("/api/v1/blobs/put/finish", {"upload_id": uid})
    check("finish returns the content digest", fin.get("ok") == 1
          and fin.get("blob_id") == updigest, fin)
    leg = call("/api/v1/blobs/put", {"name": "up.bin", "mime": "application/octet-stream",
                                     "data_b64": b64(upb)})
    check("chunked and single-shot digests agree",
          leg.get("ok") == 1 and leg.get("blob_id") == updigest, leg)
    back = call("/api/v1/blobs/get", {"blob_id": updigest})
    check("uploaded blob round-trips byte-identical",
          base64.b64decode(back["data_b64"]) == upb, len(back.get("data_b64", "")))
    # storage law (tay): native file on disk, DB carries path metadata only
    rows = sHades.db.execute("SELECT id, path FROM blobs").fetchall()
    row = next((r for r in rows if r["id"] == updigest), None)
    # DB stores the bare filename; the response path is the URI built on it
    check("blobs table holds path metadata for the upload",
          row is not None
          and row["path"] == fin.get("path", "").rsplit("/", 1)[-1]
          and fin.get("path", "").endswith(row["path"]),
          {"row": dict(row) if row else None})

    # retry-safety: rewriting the same offset cannot corrupt
    st2 = call("/api/v1/blobs/put/init", {"name": "retry.bin", "size": 8,
                                          "mime": "application/octet-stream"})
    uid2 = st2["upload_id"]
    call("/api/v1/blobs/put/chunk", {"upload_id": uid2, "offset": 0,
                                     "data_b64": b64(b"XXXXXXXX")})
    call("/api/v1/blobs/put/chunk", {"upload_id": uid2, "offset": 0,
                                     "data_b64": b64(b"AAAA")})
    call("/api/v1/blobs/put/chunk", {"upload_id": uid2, "offset": 4,
                                     "data_b64": b64(b"BBBB")})
    fin2 = call("/api/v1/blobs/put/finish", {"upload_id": uid2})
    check("retried + out-of-order offsets finish ok",
          fin2.get("ok") == 1 and fin2.get("size") == 8, fin2)
    stored = call("/api/v1/blobs/get", {"blob_id": fin2.get("blob_id", ""), "offset": 0, "len": 8})
    check("staged file content is AAAABBBB",
          base64.b64decode(stored.get("data_b64", "")) == b"AAAABBBB", stored)

    # session ownership: another identity cannot finish someone else's upload
    st3 = call("/api/v1/blobs/put/init", {"name": "owned.bin", "size": 4,
                                          "mime": "application/octet-stream"})
    r = call("/api/v1/blobs/put/finish", {"upload_id": st3["upload_id"]}, ident=other)
    check("foreign finish rejected", r.get("ok") == 0 and r.get("code") == 403, r)
    big = call("/api/v1/blobs/put/init", {"name": "big.bin", "mime": "application/octet-stream",
                                          "size": sHades.MAX_BLOB_SZ + 1})
    check("over-cap payload rejected at init",
          big.get("ok") == 0 and big.get("code") == 413
          and "10 MB" in big.get("error", ""), big)
    bad = call("/api/v1/blobs/put/chunk", {"upload_id": st3["upload_id"], "offset": 2,
                                           "data_b64": b64(b"XXXX")})
    check("chunk overrunning declared size rejected",
          bad.get("ok") == 0 and bad.get("code") == 400, bad)
    missing = call("/api/v1/blobs/put/finish", {"upload_id": "z" * 32})
    check("finish on unknown upload_id -> 404",
          missing.get("ok") == 0 and missing.get("code") == 404, missing)

    # ---------- v1 post: inline + blob-referenced attachments ----------
    sHades.reset_for_tests() if hasattr(sHades, "reset_for_tests") else None
    call("/api/v1/stream/subscribe", {"channel": cid_chat})
    png = b"\x89PNG\r\n\x1a\n" + b"imgdata" * 16
    r = call("/api/v1/post", {"channel": cid_chat, "content": "inline pic",
                              "attachments": [{"name": "p.png", "mime": "image/png",
                                               "data_b64": b64(png)}]})
    check("v1 post with inline attachment", r.get("ok") == 1 and r.get("msg_id"), r)
    atts = r.get("attachments") or []
    check("v1 post returns attachment path", atts and atts[0]["path"].startswith("/file/attachments/"), r)
    if atts:
        stored = os.path.join(base, "attachments", atts[0]["path"].rsplit("/", 1)[-1])
        check("inline attachment stored content-addressed",
              os.path.isfile(stored) and open(stored, "rb").read() == png)
    # referenced upload: blob first, then post with blob_id
    up2 = call("/api/v1/blobs/put", {"name": "ref.bin", "mime": "application/octet-stream",
                                     "data_b64": b64(b"referenced-bytes")})
    r = call("/api/v1/post", {"channel": cid_chat, "content": "by reference",
                              "attachments": [{"blob_id": up2["blob_id"]}]})
    check("v1 post with blob_id reference", r.get("ok") == 1, r)
    check("blob_id mime derived from stored ext",
          (r.get("attachments") or [{}])[0].get("mime") == "application/octet-stream", r)
    r = call("/api/v1/post", {"channel": cid_chat, "content": "missing blob",
                              "attachments": [{"blob_id": "e" * 32}]})
    check("unknown blob_id in post -> 400", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/post", {"channel": cid_chat, "content": "bad b64",
                              "attachments": [{"name": "x", "data_b64": "@@"}]})
    check("bad base64 in post -> 400", r.get("ok") == 0 and r.get("code") == 400, r)
    r = call("/api/v1/post", {"channel": cid_chat, "content": "",
                              "attachments": [{"name": "only.bin",
                                               "mime": "application/octet-stream",
                                               "data_b64": b64(b"only")}]})
    check("attachment-only post ok", r.get("ok") == 1, r)
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    msg_events = [e for e in poll["events"] if e["kind"] == "msg"]
    check("post events carry attachment metadata",
          len(msg_events) >= 3 and all("attachments" in e["data"]["msg"] for e in msg_events),
          [e["data"]["msg"].get("attachments") for e in msg_events])

    # ---------- site channels ----------
    def reset_stream():
        with sHades.EVENT_COND:
            sHades.EVENT_LOG.clear()
            sHades.EVENT_SEQ = 0
        with sHades.SUB_LOCK:
            sHades.SUBSCRIPTIONS.clear()
            sHades.SUB_BY_USER.clear()
    reset_stream()
    call("/api/v1/stream/subscribe", {"channel": cid_site})

    page = "> Welcome\n`[pic`:/file/sites/%s/img/pic.png]\n" % cid_site
    r = call("/api/v1/site/put", {"channel": cid_site, "path": "/index.mu",
                                  "data_b64": b64(page.encode())})
    check("site put index page", r.get("ok") == 1 and r["mime"] == "text/micron", r)
    pic = b"\x89PNG" + b"fakepng" * 8
    r = call("/api/v1/site/put", {"channel": cid_site, "path": "/img/pic.png",
                                  "data_b64": b64(pic)})
    check("site put nested resource", r.get("ok") == 1 and r["mime"] == "image/png", r)
    check("site resource on disk under sites/<cid>",
          os.path.isfile(os.path.join(base, "sites", cid_site, "img", "pic.png")))

    got = call("/api/v1/site/get", {"channel": cid_site, "path": "/index.mu"})
    check("site get roundtrip", got.get("ok") == 1
          and base64.b64decode(got["data_b64"]).decode() == page, got)
    got = call("/api/v1/site/get", {"channel": cid_site, "path": "/img/pic.png",
                                    "len": 16})
    check("site get chunked", base64.b64decode(got["data_b64"]) == pic[:16]
          and got["eof"] is False, got)
    r = call("/api/v1/site/get", {"channel": cid_site, "path": "/nope.mu"})
    check("site get missing resource -> 404", r.get("ok") == 0 and r.get("code") == 404, r)
    # tay's device receipt (0.36.24): the read verdict rides the
    # not-found response so the client's empty state can be the door.
    check("site get 404 carries the edit verdict (moderator)",
          r.get("can_edit") is True, r)
    r = call("/api/v1/site/get", {"channel": cid_site, "path": "/nope.mu"},
             ident=other)
    check("site get 404 verdict False without the bits",
          r.get("ok") == 0 and r.get("can_edit") is False, r)
    r = call("/api/v1/site/get", {"channel": cid_site, "path": "/a\\b.mu"})
    check("site get 400 carries no verdict",
          r.get("ok") == 0 and "can_edit" not in r, r)

    # tay's device receipt (0.36.24): a fresh site channel seeds its
    # index, and the client's empty-state starter template mirrors the
    # server's seed text exactly (one edit, or they drift apart).
    cid_seed = call("/api/v1/channels/create",
                    {"name": "seeded", "kind": "site"})["channel"]["id"]
    check("new site channel seeds /index.mu",
          os.path.isfile(os.path.join(base, "sites", cid_seed, "index.mu")),
          cid_seed)
    seed_txt = open(os.path.join(base, "sites", cid_seed, "index.mu"),
                    encoding="utf-8").read()
    check("seeded text == DEFAULT_SITE_INDEX_MU",
          seed_txt == sHades.DEFAULT_SITE_INDEX_MU, repr(seed_txt[:40]))
    import ast
    _kc_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "kivy_client.py")
    _kc_tree = ast.parse(open(_kc_path, encoding="utf-8").read())
    starter = None
    for node in ast.walk(_kc_tree):
        tgt = node.targets[0] if getattr(node, "targets", None) else None
        if (isinstance(node, ast.Assign) and tgt is not None
                and ((isinstance(tgt, ast.Attribute)
                      and tgt.attr == "SITE_STARTER_INDEX_MU")
                     or (isinstance(tgt, ast.Name)
                         and tgt.id == "SITE_STARTER_INDEX_MU"))):
            starter = ast.literal_eval(node.value)
    check("client starter mirrors the server seed text",
          starter == sHades.DEFAULT_SITE_INDEX_MU, repr((starter or "")[:40]))

    for bad in ("/../evil.mu", "no-slash.mu", "/a//b.mu", "/a/../b.mu", "/a\\b.mu"):
        r = call("/api/v1/site/put", {"channel": cid_site, "path": bad,
                                      "data_b64": b64(b"x")})
        check(f"site put rejects path {bad!r}", r.get("ok") == 0 and r.get("code") == 400, r)
        r = call("/api/v1/site/get", {"channel": cid_site, "path": bad})
        check(f"site get rejects path {bad!r}", r.get("ok") == 0, r)

    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    patches = [e for e in poll["events"] if e["kind"] == "site_patch"]
    check("site_patch events flow to subscribers",
          [p["data"]["path"] for p in patches] == ["/index.mu", "/img/pic.png"], patches)

    # manifest
    r = call("/api/v1/site/manifest", {"channel": cid_site})
    check("site manifest get (empty default)", r.get("ok") == 1 and r["manifest"] == {}, r)
    r = call("/api/v1/site/manifest_put", {"channel": cid_site,
                                           "manifest": {"index": "/index.mu",
                                                        "theme": {"bg": "223", "accent": "F8a8"}}})
    check("site manifest put ok", r.get("ok") == 1, r)
    r = call("/api/v1/site/manifest", {"channel": cid_site})
    check("site manifest persisted", r["manifest"]["theme"]["accent"] == "F8a8", r)
    poll = call("/api/v1/stream/poll", {"timeout_s": 0})
    mp = [e for e in poll["events"] if e["kind"] == "site_patch" and "manifest" in e["data"]]
    check("manifest update emits a site_patch event", mp and
          mp[0]["data"]["manifest"]["index"] == "/index.mu", poll)
    r = call("/api/v1/site/manifest", {"channel": cid_chat})
    check("manifest on non-site channel -> 404", r.get("ok") == 0 and r.get("code") == 404, r)

    # gating: non-mod
    r = call("/api/v1/site/put", {"channel": cid_site, "path": "/x.mu", "data_b64": b64(b"x")},
             ident=other)
    check("non-mod site put -> 403", r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/site/manifest_put", {"channel": cid_site, "manifest": {}}, ident=other)
    check("non-mod manifest put -> 403", r.get("ok") == 0 and r.get("code") == 403, r)
    r = call("/api/v1/site/put", {"channel": cid_chat, "path": "/x.mu", "data_b64": b64(b"x")})
    check("site put on chat channel -> 404", r.get("ok") == 0 and r.get("code") == 404, r)

    # raw /file/sites handler + traversal guard
    out = sHades.serve_site_file(f"/file/sites/{cid_site}/index.mu", None, "r", "L",
                                 identity, sHades.now())
    check("serve_site_file serves stored page", out == page.encode(), len(out))
    out = sHades.serve_site_file(f"/file/sites/{cid_site}/../../../etc/passwd", None,
                                 "r", "L", identity, sHades.now())
    check("serve_site_file rejects traversal", out == b"not found", out)

    # oversized resource (patch the cap small, then restore)
    old_cap = sHades.MAX_ATTACHMENT_SZ
    sHades.MAX_ATTACHMENT_SZ = 16
    r = call("/api/v1/site/put", {"channel": cid_site, "path": "/big.mu",
                                  "data_b64": b64(b"x" * 64)})
    check("oversized site resource -> 413", r.get("ok") == 0 and r.get("code") == 413, r)
    r = call("/api/v1/blobs/put", {"name": "big.bin", "data_b64": b64(b"x" * 64)})
    check("oversized blob -> 413", r.get("ok") == 0 and r.get("code") == 413, r)
    sHades.MAX_ATTACHMENT_SZ = old_cap

    # blobs rate limit (10/60 s) — keep last; earlier puts consumed the window
    codes = []
    for i in range(11):
        r = call("/api/v1/blobs/put", {"name": f"r{i}.bin", "data_b64": b64(bytes([i]))})
        codes.append(r.get("code") if r.get("ok") == 0 else None)
    check("blob uploads rate limited", 429 in codes, codes)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)

if __name__ == "__main__":
    main()
