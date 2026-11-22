#!/usr/bin/env python3
"""P11 arrow 5: the CLIENT updater. The one file that talks HTTPS on the
client, and only toward the release host for its own APK.

Flow: the server's whoami payload carries guild.app_version (what the
server runs) and guild.update_channel (its toggle). The client decides
update_decision() -> if newer, the kv strip shows -> the user consents
-> download_with_dm() runs the system DownloadManager (the bytes land
in public Downloads, MediaStore-backed) -> the sha gate compares against
the release's .sha256 companion -> install_intent() hands the file to
the package installer. No update path touches the protocol surfaces.

Lanes: dm/installer/fetch arrive as arguments (shims), so every leg is
provable without jnius or a device.
"""
import hashlib
import re
import urllib.request

APP_APK_RE = re.compile(r"^shades-([0-9.]+)-arm64-v8a-debug\.apk$")
UA = "sHades-client-updater"
HTTP_TIMEOUT = 20
MAX_FETCH_BYTES = 8 * 1024 * 1024    # manifest/sha fetches: 8 MB ceiling
MAX_APK_BYTES = 300 * 1024 * 1024    # APK ceiling (the DM stream + RAM read)

# the release repo this APK came from (the client updates along it)
REPO = "eldeucebag/sHades"
LATEST_PATTERN = "https://api.github.com/repos/%s/releases/latest" % REPO


class HttpError(Exception):
    def __init__(self, msg):
        super(HttpError, self).__init__(msg)


def parse_version(text):
    """Mirror of the server core's compare: dotted ints, 'v' tolerated."""
    if not text:
        return None
    t = str(text).strip()
    if t[:1] in ("v", "V"):
        t = t[1:]
    parts = t.split(".")
    if not parts or any(p == "" or not p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def update_decision(my_version, server_version, channel):
    """Pure: None = nothing to do; (version) = offer server_version."""
    if (channel or "").strip().lower() != "github":
        return None
    mine, theirs = parse_version(my_version), parse_version(server_version)
    if mine is None or theirs is None or mine >= theirs:
        return None
    return theirs


def newest_client_asset(assets, since=None):
    """Pick the newest arm64 APK asset (optionally strictly-newer than
    `since`); its .sha256 companion must exist. Returns
    {version, apk_url, apk_name, sha_url} or None."""
    best = None
    for a in (assets or []):
        name = (a.get("name") or "")
        m = APP_APK_RE.match(name)
        if not m:
            continue
        v = parse_version(m.group(1))
        if v is None:
            continue
        if since is not None:
            base = parse_version(since)
            if base is None:
                raise HttpError("current version carries no parseable literal")
            if v <= base:
                continue
        if best is not None and v <= best[0]:
            continue
        best = (v, a.copy())
    if best is None:
        return None
    apk = best[1]
    sha = None
    for a in (assets or []):
        if a.get("name") == apk.get("name") + ".sha256":
            sha = a
            break
    if sha is None:
        return None
    return {"version": ".".join(str(x) for x in best[0]),
            "apk_name": apk.get("name"),
            "apk_url": apk.get("browser_download_url"),
            "sha_url": sha.get("browser_download_url")}


def fetch(url, timeout=HTTP_TIMEOUT, limit=MAX_FETCH_BYTES):
    """The one https/https-any-scheme GET the client is allowed. Reads are
    capped: the release manifest and the .sha256 companion are tiny, and an
    unbounded read into RAM is not an option on a phone."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(limit + 1)
            if len(body) > limit:
                raise HttpError("fetch exceeds %d bytes" % limit)
            return body
    except HttpError:
        raise
    except Exception as e:  # urllib raises too many shapes to count
        raise HttpError("fetch failed: %s" % e)


def parse_sha_file(body):
    first = (body or b"").decode("ascii", "replace").split("\n")[0].strip()
    parts = first.split()
    if len(parts) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
        return parts[0]
    if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
        return parts[0]
    raise HttpError("sha companion body carries no digest")


def download_with_dm(apk_url, apk_name, sha_hex,
                     dm=None, fetcher=fetch, sha_bytes=None):
    """The system DownloadManager: enqueue + wait + dig out the bytes.
    `dm` is the seam (a shim in lanes; the jnius DownloadManager in
    prod); `sha_bytes` lets a lane hand the bytes straight over. The
    sha gate refuses BEFORE anything install-shaped happens."""
    if sha_bytes is None:
        if dm is None:
            raise HttpError("no DownloadManager and no lane bytes")
        try:
            from jnius import autoclass
            env = autoclass("android.os.Environment")
            req = dm.Request(apk_url)
            req.setDestinationInExternalPublicDir(
                env.DIRECTORY_DOWNLOADS, apk_name)
            req.setTitle("sHades " + apk_name.split("-", 1)[-1])
            req.setMimeType("application/vnd.android.package-archive")
            req.setNotificationVisibility(req.VISIBILITY_VISIBLE_NOTIFY_DOWNLOADED)
            sid = dm.enqueue(req)
            q = autoclass("android.app.DownloadManager").Query()
            q.setFilterById(sid)
            import time
            for _ in range(120):
                cur = dm.query(q)
                if cur.moveToFirst():
                    st = cur.getInt(cur.getColumnIndex(
                        "status"))
                    if st == dm.STATUS_SUCCESSFUL:
                        break
                    if st == dm.STATUS_FAILED:
                        raise HttpError("DownloadManager failed the apk")
                time.sleep(1.0)
            else:
                raise HttpError("DownloadManager timed out")
            opened = dm.openDownloadedFile(sid)
            try:
                sz = opened.stat().getSize()
            except Exception:
                sz = -1  # stat unsupported: fall back to the read-time cap
            if sz > MAX_APK_BYTES:
                raise HttpError("downloaded apk exceeds %d bytes" % MAX_APK_BYTES)
            sha_bytes = opened.openInputStream().readBytes()
            if len(sha_bytes) > MAX_APK_BYTES:
                raise HttpError("downloaded apk exceeds %d bytes" % MAX_APK_BYTES)
        except HttpError:
            raise
        except Exception as e:
            raise HttpError("download leg failed: %s" % e)
    got = hashlib.sha256(sha_bytes).hexdigest()
    if got.lower() != (sha_hex or "").lower():
        raise HttpError("downloaded apk sha disagrees with the release "
                        ".sha256 (%s != %s)" % (got[:12], sha_hex[:12]))
    return sha_bytes


def install_intent(local_path, apk_name,
                   installer=None, activity=None):
    """Hand the downloaded apk to the package installer. The PROD leg
    writes the cached bytes into the app's own files dir (a file:// uri
    from app-private storage is what the debug installer accepts) and
    fires ACTION_VIEW with the apk mime. Lanes pass `installer` and
    assert the args."""
    if installer is not None:
        return installer(local_path, "application/vnd.android."
                                     "package-archive")
    try:
        from jnius import autoclass
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        act = activity
        if act is None:
            act = autoclass(
                "org.kivy.android.PythonActivity").mActivity
        intent = Intent(Intent.ACTION_VIEW)
        intent.setDataAndType(Uri.parse("file://" + str(local_path)),
                              "application/vnd.android.package-archive")
        intent.addFlags(0x10000000)  # FLAG_ACTIVITY_NEW_TASK
        act.startActivity(intent)
        return True
    except HttpError:
        raise
    except Exception as e:
        raise HttpError("install leg failed: %s" % e)


def save_candidate(bytes_, apk_name, dest_dir, writer=None):
    """Park the verified apk for the installer; the lane seams `writer`."""
    if writer is not None:
        return writer(bytes_, apk_name, dest_dir)
    import os
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, apk_name)
    with open(path, "wb") as f:
        f.write(bytes_)
    return path
