"""Server attachment persistence toggle (P2, plan_admin v1.1).

The "attachments" admin config switches where blob bytes land:
  - "saved" (default): today's law — content-addressed files on disk,
    DB carries metadata only;
  - "ephemeral": bytes live in the ATT_RAM registry (digest-keyed,
    TTL-swept by the 1 s reaper, hard-capped with LRU eviction);
    serve paths resolve RAM-first, then disk. Flipping back to
    "saved" restores disk commits for NEW uploads; RAM blobs keep
    serving until the TTL drops them.

Direct module-level tests against api_v1_* routes, the ingest walk,
the raw /file handlers, and the chunked flow — no mesh needed.
"""
import os
import sys
import json
import sqlite3
import tempfile
import time
import hashlib
import base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sHades  # noqa: E402
import RNS     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("" if cond else ("  | " + str(extra)))
    print(mark + name + tail)


base = tempfile.mkdtemp(prefix="shades_att_toggle_")
sHades.STORAGE = base

ident = RNS.Identity()
h = sHades.hexstr(RNS.Destination(ident, RNS.Destination.OUT,
                                  RNS.Destination.SINGLE,
                                  "lxmf", "delivery").hash)

sHades.db = sqlite3.connect(os.path.join(base, "sHades.db"))
sHades.db.row_factory = sqlite3.Row
sHades.db.executescript(sHades.SCHEMA)
sHades._migrate_schema(sHades.db)
sHades.db.commit()
sHades.db.execute("INSERT INTO users (dest_hash, username, registered_at) "
                  "VALUES (?, ?, ?)", (h, "tester", sHades.now()))
sHades.db.commit()
sHades.load_role_seeds(base)
role = sHades._ensure_role("Admin", sHades.PERM_ALL, position=100)
sHades._assign_role(h, role)
sHades.DEBUG = False


def call(path, data=None):
    payload = json.dumps(data or {}).encode("utf-8")
    raw = sHades.API_ROUTES[path]("/api/v1/x", payload, "req-1", "L1",
                                  ident, sHades.now())
    return json.loads(raw.decode("utf-8"))


def serve_file(fname, handler="blob"):
    path = ("/file/blobs/" if handler == "blob" else "/file/attachments/"
            ) + fname
    fn = (sHades.serve_blob_file if handler == "blob"
          else sHades.serve_attachment_file)
    return fn(path, None, "req-1", "L1", ident, sHades.now())


def blob_file(digest):
    fname = sHades._blob_fname(digest)
    return os.path.isfile(os.path.join(sHades._blob_dir(), fname)) \
        if fname else False


def main():
    # ---------- config surface ----------
    check("default mode is saved", sHades._attachment_mode() == "saved")
    cfg = call("/api/v1/admin/config")["config"]
    check("config carries attachments: saved", cfg.get("attachments")
          == "saved", cfg)
    r = call("/api/v1/admin/config", {"key": "attachments", "value": "weird"})
    check("bogus value rejected", r.get("code") == 400, r)
    r = call("/api/v1/admin/config", {"key": "attachments",
                                      "value": "ephemeral"})
    check("flip to ephemeral accepted", r.get("ok") == 1, r)
    check("mode binds live", sHades._attachment_mode() == "ephemeral")
    r = call("/api/v1/admin/audit")
    row = [e for e in r.get("entries", [])
           if e["action"] == "config.set" and e["target"] == "attachments"]
    check("flip audited saved -> ephemeral", row and "saved -> ephemeral"
          in row[0]["detail"], row)
    audit_ok = True

    # ---------- single-shot blobs/put in ephemeral ----------
    blob = b"ephemeral-bytes-%d" % time.time_ns()
    digest = sHades.hexstr(hashlib.sha256(blob).digest())[:32]
    r = call("/api/v1/blobs/put", {"data_b64": base64.b64encode(blob)
                                   .decode("ascii"), "name": "note.txt",
                                   "mime": "text/plain"})
    check("ephemeral put ok", r.get("ok") == 1, r)
    check("ephemeral put returns the digest", r.get("blob_id") == digest, r)
    check("no disk file in ephemeral mode", not blob_file(digest))
    entry = sHades._att_ram_get(digest)
    check("bytes live in RAM", entry is not None and bytes(entry["bytes"])
          == blob, entry if entry else None)
    check("metadata row still carries the blob",
          sHades._blob_fname(digest) == "%s.txt" % digest,
          sHades._blob_fname(digest))
    out = serve_file(digest + ".txt")
    check("/file/blobs serves from RAM", out == blob, len(out))
    g = call("/api/v1/blobs/get", {"blob_id": digest})
    check("blobs/get RAM-first whole blob", bytes(base64.b64decode(
        g["data_b64"])) == blob, g)
    g0 = call("/api/v1/blobs/get", {"blob_id": digest, "offset": 0,
                                    "len": 5})
    g1 = call("/api/v1/blobs/get", {"blob_id": digest, "offset": 5})
    check("blobs/get slices RAM",
          base64.b64decode(g0["data_b64"]) == blob[:5]
          and base64.b64decode(g1["data_b64"]) == blob[5:]
          and g1.get("eof") in (1, True), (g0, g1))

    # ---------- flip back: disk restores for NEW uploads only ----------
    call("/api/v1/admin/config", {"key": "attachments", "value": "saved"})
    check("flip back binds live", sHades._attachment_mode() == "saved")
    r2 = call("/api/v1/admin/audit")
    row2 = [e for e in r2.get("entries", [])
            if e["action"] == "config.set" and e["target"] == "attachments"
            and "ephemeral -> saved" in e["detail"]]
    if not row2:
        audit_ok = False
    check("old RAM blob still serves after flip", serve_file(digest
          + ".txt") == blob)
    blob2 = b"saved-after-flip-%d" % time.time_ns()
    digest2 = sHades.hexstr(hashlib.sha256(blob2)
                            .digest())[:32]
    r = call("/api/v1/blobs/put", {"data_b64": base64.b64encode(blob2)
                                   .decode("ascii"), "name": "saved.txt"})
    check("new put in saved mode ok", r.get("ok") == 1, r)
    check("new put lands on disk", blob_file(digest2))
    sHades.ATT_RAM.get(digest, {})["ts"] = sHades.now() - 10**9
    sHades._att_ram_sweep()
    check("TTL sweep drops expired RAM rows",
          sHades._att_ram_get(digest) is None)
    check("expired RAM blob falls back to disk rules",
          serve_file(digest + ".txt") == b"not found")
    check("every flip audited", audit_ok)

    # ---------- ingest path (inline attachment) ----------
    call("/api/v1/admin/config", {"key": "attachments", "value": "ephemeral"})
    cid = call("/api/v1/channels/create", {"name": "attchan"})["channel"]["id"]
    att = ("report.txt", "text/plain", b"inline-attachment-bytes")
    msg_id = sHades.ingest(cid, h, "with attachment", attachments=[att])
    check("ingest with attachment persists the post", bool(msg_id))
    row = sHades.db.execute("SELECT path FROM attachments WHERE msg_id=?",
                            (msg_id,)).fetchone()
    fname = row["path"].rsplit("/", 1)[-1] if row else ""
    check("attachment metadata row present", bool(fname), row)
    check("ingest skipped the attachments dir",
          not os.path.isfile(os.path.join(sHades.STORAGE, "attachments",
                                          fname)))
    g = call("/api/v1/att/get", {"path": "/file/attachments/" + fname})
    check("att/get serves ingest bytes from RAM",
          base64.b64decode(g["data_b64"]) == att[2], g)
    out = serve_file(fname, handler="attachment")
    check("/file/attachments handler serves RAM", out == att[2], len(out))
    # flip back: the SAME digest re-posted (or a new one) hits disk
    call("/api/v1/admin/config", {"key": "attachments", "value": "saved"})
    att2 = ("file.txt", "text/plain", b"persisted-attachment-bytes")
    msg_id2 = sHades.ingest(cid, h, "again", attachments=[att2])
    row2 = sHades.db.execute("SELECT path FROM attachments WHERE msg_id=?",
                             (msg_id2,)).fetchone()
    fname2 = row2["path"].rsplit("/", 1)[-1] if row2 else ""
    check("saved-mode ingest lands on disk",
          os.path.isfile(os.path.join(sHades.STORAGE, "attachments", fname2)),
          fname2)

    # ---------- chunked flow in ephemeral ----------
    call("/api/v1/admin/config", {"key": "attachments", "value":
                                  "ephemeral"})
    pay = b"chunked-rampayload-%d" % time.time_ns()
    i = call("/api/v1/blobs/put/init", {"name": "c.bin", "mime":
                                        "application/octet-stream",
                                        "size": len(pay)})
    check("ephemeral init ok", i.get("ok") == 1, i)
    up_id = i["upload_id"]
    staging = os.path.join(sHades._blob_staging_dir(), up_id + ".part")
    check("init staged no .part on disk", not os.path.isfile(staging),
          staging)
    c = call("/api/v1/blobs/put/chunk", {"upload_id": up_id, "offset": 0,
                                         "data_b64": base64.b64encode(
                                             pay).decode("ascii")})
    check("ephemeral chunk ok + eof", c.get("ok") == 1 and c.get("eof") in
          (1, True), c)
    f = call("/api/v1/blobs/put/finish", {"upload_id": up_id})
    cdigest = f.get("blob_id")
    check("ephemeral finish ok, digest matches single-shot",
          f.get("ok") == 1 and cdigest == sHades.hexstr(__import__(
              "hashlib").sha256(pay).digest())[:32], f)
    check("ephemeral finish left no disk file", not blob_file(cdigest))
    check("ephemeral finish left no staging file", not os.path.isfile(
        staging))
    check("ephemeral chunked blob serves from RAM", serve_file(cdigest
          + ".bin") == pay)
    # same bytes, saved mode: disk artifact (flip binds at finish)
    call("/api/v1/admin/config", {"key": "attachments", "value": "saved"})
    i2 = call("/api/v1/blobs/put/init", {"name": "d.bin", "size": len(pay)})
    call("/api/v1/blobs/put/chunk", {"upload_id": i2["upload_id"],
                                     "offset": 0,
                                     "data_b64": base64.b64encode(
                                         pay).decode("ascii")})
    f2 = call("/api/v1/blobs/put/finish", {"upload_id": i2["upload_id"]})
    check("saved-mode finish lands on disk", blob_file(f2.get("blob_id")))

    # ---------- blob_id post reference resolves RAM-first ----------
    call("/api/v1/admin/config", {"key": "attachments", "value": "ephemeral"})
    bid = cdigest
    triples, err = sHades._resolve_v1_attachments([{"blob_id": bid}])
    check("blob_id reference resolves from RAM",
          triples == [["c.bin", "application/octet-stream", pay], ] and
          err is None, (triples, err))

    # ---------- RAM cap LRU eviction ----------
    old_cap = sHades.ATT_RAM_MAX_BYTES
    try:
        sHades.ATT_RAM_MAX_BYTES = 64
        sHades._att_ram_put("a" * 32, b"x" * 50, "a.txt", "text/plain",
                            None)
        time.sleep(0.02)
        sHades._att_ram_put("b" * 32, b"y" * 50, "b.txt", "text/plain",
                            None)
        check("cap evicts the LRU row", sHades._att_ram_get("a" * 32)
              is None and sHades._att_ram_get("b" * 32) is not None)
    finally:
        sHades.ATT_RAM_MAX_BYTES = old_cap

    # ---------- reaper drains the registry without dying ----------
    rd = hashlib.sha256(b"reaper-row-%d" % time.time_ns()).hexdigest()[:32]
    sHades._att_ram_put(rd, b"reaper-bytes", "r.txt", "text/plain", None)
    sHades.ATT_RAM[rd]["ts"] = sHades.now() - 10**9
    n = sHades._att_ram_sweep()
    check("reaper sweep counts and clears", n >= 1
          and sHades._att_ram_get(rd) is None, n)

    print("PASS attachment toggle suite: %d checks, %d failed"
          % (len(PASS) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
