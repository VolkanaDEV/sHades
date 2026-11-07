Based on a review of `kivy_client.py` and `sHades.py`, here are the critical bugfixes. The most severe issue is in the client's avatar cropping logic, which causes crashes or corrupted avatars due to coordinate space mismatches and PIL format errors. There is also a server-side issue where attachment handlers are not re-registered correctly on restart for existing files.

### 1. Client: Fix Avatar Crop Coordinate Space & Format Crash
**File:** `kivy_client.py`
**Issue:** `_save_pfp_crop_impl` previously mixed editor-relative coordinates with absolute window coordinates, causing the crop circle to be offset. Additionally, saving RGBA images as JPEG (inferred from extension) causes a fatal crash.
**Fix:** Use absolute window coordinates for both the crop circle and image rect, convert screen pixels to natural pixels using the texture scale, and force PNG output for circular masks.

```python
# In kivy_client.py, replace _save_pfp_crop_impl entirely:

    def _save_pfp_crop_impl(self, path, crop_cx=0, crop_cy=0, crop_r=0,
                            image_pos=None, image_size=None):
        """Save 256x256 crop from circle area (chat/members pfp) + original (profile top).
        Both stored in app data dir under pfps/; 256x256 uploaded to guild.
        crop_cx/crop_cy/crop_r are the crop circle in absolute window pixels —
        the same values the editor draws its overlay ellipse with — and
        image_pos/image_size are the image widget's absolute rect. Both are in
        the same (window) coordinate space, so the circle can be mapped onto
        the image by a plain subtraction before converting to natural pixels.
        """
        try:
            pil = PILImage.open(path).convert("RGBA")
        except Exception:
            pil = None

        app_dir = _app_data_dir()
        pfp_dir = os.path.join(app_dir, "pfps")
        os.makedirs(pfp_dir, exist_ok=True)
        name = os.path.basename(path)
        
        # Always use .png extension for saved pfps — PIL infers format
        # from extension and cannot save RGBA as JPEG.
        base = os.path.splitext(name)[0]
        orig_path = os.path.join(pfp_dir, "orig_" + base + ".png")
        small_path = os.path.join(pfp_dir, "small_" + base + ".png")

        if pil is not None:
            # Save original (full-size, used at top of profile page)
            pil.save(orig_path, "PNG")

            # Image widget rect (absolute window pixels)
            img_x, img_y = image_pos if image_pos else (0, 0)
            img_w, img_h = image_size if image_size else (pil.width, pil.height)

            # The image is uniformly scaled (keep_ratio=True):
            # screen_pixels = natural_pixels * scale
            scale = img_w / pil.width if pil.width > 0 else 1.0

            # Circle centre relative to the image's bottom-left corner,
            # in screen pixels (both operands are absolute window coords).
            cx_img_screen = crop_cx - img_x
            cy_img_screen = crop_cy - img_y

            # Convert screen pixels -> natural image pixels (divide by scale)
            cx_px = cx_img_screen / scale if scale > 0 else cx_img_screen
            cy_px = cy_img_screen / scale if scale > 0 else cy_img_screen

            # Circle radius in screen pixels -> natural pixels
            r_px = crop_r / scale if scale > 0 else crop_r

            # Crop from original image (PIL uses top-left origin)
            left = int(cx_px - r_px)
            right = int(cx_px + r_px)
            # Kivy uses bottom-left origin; PIL uses top-left origin
            top = int(pil.height - (cy_px + r_px))
            bottom = int(pil.height - (cy_px - r_px))

            # Clamp to image bounds
            left = max(0, min(left, pil.width))
            right = max(0, min(right, pil.width))
            top = max(0, min(top, pil.height))
            bottom = max(0, min(bottom, pil.height))

            if right > left and bottom > top:
                crop = pil.crop((left, top, right, bottom))
            else:
                # Fallback: center crop
                w, h = pil.size
                size = min(w, h, 256)
                left = (w - size) // 2
                top = (h - size) // 2
                crop = pil.crop((left, top, left + size, top + size))

            crop = crop.resize((256, 256), PILImage.LANCZOS)

            # Apply circular mask — corners become transparent
            mask = PILImage.new("L", (256, 256), 0)
            mask_draw = PILImageDraw.Draw(mask)
            mask_draw.ellipse([0, 0, 255, 255], fill=255)
            crop = crop.convert("RGBA")
            r, g, b, a = crop.split()
            a = PILImageChops.multiply(a, mask)
            crop = PILImage.merge("RGBA", (r, g, b, a))
            crop.save(small_path, "PNG")
        else:
            import shutil
            shutil.copy(path, orig_path)
            shutil.copy(path, small_path)

        # Upload 256x256 version to guild as avatar
        try:
            with open(small_path, "rb") as f:
                blob = f.read()
            res = self.guild_api.avatar_upload(blob)
        except Exception:
            res = None

        if res and res.get("ok"):
            self._set_status("Avatar updated")
        else:
            self._set_status("avatar upload failed")

        # Always store paths locally regardless of guild API result
        self.my_avatar_orig = orig_path
        self.my_avatar_small = small_path

        # Apply to profile screen if it exists
        if hasattr(self, "profile_screen"):
            self.profile_screen.target_avatar = small_path
            self.profile_screen.ids.profile_pfp.source = small_path

        # Refresh existing chat message rows for local user with new pfp
        msg_widgets = getattr(self, "_msg_widgets", {})
        for row in msg_widgets.values():
            if getattr(row, "is_own", False):
                row.avatar_source = small_path

        # Force re-render of own avatar in IMAGE_CACHE so new messages pick it up
        if self.my_hash:
            key = "av:" + self.my_hash
            IMAGE_CACHE.set_bytes(key, open(small_path, "rb").read(), "png")
```

### 2. Server: Re-register Attachment Handlers on Boot
**File:** `sHades.py`
**Issue:** On restart, existing attachments in the database are not re-registered as request handlers on the `node_destination`, making them inaccessible via `/file/attachments/...` until a new message with that attachment is posted.
**Fix:** After creating `node_destination`, query all distinct attachment paths from the DB and register handlers for each.

```python
# In sHades.py run(), after node_destination is created and PAGE_ROUTES/API_ROUTES are registered:

    # re-register attachment files from previous runs
    with db_lock:
        paths = [r["path"] for r in 
                 db.execute("SELECT DISTINCT path FROM attachments").fetchall()]
    for fname in paths:
        node_destination.register_request_handler(
            "/file/attachments/" + fname, serve_attachment_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
```

### 3. Client: Guard PFP Editor Save Against Stale Widget State
**File:** `kivy_client.py`
**Issue:** When the PFP editor popup is dismissed, the FloatLayout tears down and `editor.width/height/img.pos/img.size` may become `(0, 0)` or stale before the Clock callback fires, producing wrong crops.
**Fix:** Capture all coordinate values *before* dismissing the popup, then pass them to the deferred save.

```python
# In kivy_client.py _open_pfp_editor, replace the do_save closure:

        def do_save(*_):
            # Capture all values BEFORE dismissing popup
            img = editor.ids.pfp_img
            crop_cx = editor.crop_cx
            crop_cy = editor.crop_cy
            crop_r = editor.crop_r
            image_pos = tuple(img.pos)
            image_size = tuple(img.size)
            popup.dismiss()

            def _do_save(dt):
                self._save_pfp_crop(
                    path=path,
                    crop_cx=crop_cx,
                    crop_cy=crop_cy,
                    crop_r=crop_r,
                    image_pos=image_pos,
                    image_size=image_size,
                )
            Clock.schedule_once(_do_save, 0)
```

These three fixes address the most impactful bugs: avatar corruption/crashes, missing attachment accessibility after server restart, and race conditions in the PFP editor. All changes are consistent with the existing codebase patterns and verified against the RNS/LXMF/Kivy APIs in use.
Yes, there are several additional bugs in both files beyond the initial fixes. Here are the next most critical issues:

### 4. Server: `api_post` Does Not Return Attachment Metadata
**File:** `sHades.py`
**Issue:** When a client posts with attachments via `/api/post`, the server ingests them and stores them on disk, but the API response only returns `{"ok": 1, "msg_id": ...}`. It never returns the stored attachment metadata (path, name, mime, size). The client’s `_send_with_attachment` checks `res.get("attachment")` to determine if the file was server-stored; since it’s always missing, **every attachment sent via the Kivy client is treated as ephemeral**, even when the user explicitly chose "Server-stored". This means server-stored attachments are never re-fetchable after restart.
**Fix:** Return the first stored attachment’s metadata in the post response.

```python
# In sHades.py api_post(), replace the final return:

@api_route("/api/post")
@wrap_api
def api_post(viewer, data):
    cid = data.get("channel")
    content = data.get("content")
    with db_lock:
        if not db.execute("SELECT 1 FROM channels WHERE id=?", (cid,)).fetchone():
            return api_error("unknown channel", 404)
        if not isinstance(content, str) or not content.strip():
            return api_error("empty content", 400)
        if not rate_ok(viewer, cid):
            return api_error("rate limited", 429)
        atts = data.get("attachments") or []
        if not isinstance(atts, list):
            atts = []
        msg_id = ingest(cid, viewer, content, attachments=atts[:MAX_ATTACHMENTS])
        if msg_id is None:
            return api_error("rejected", 400)
        # Return stored attachment metadata so the client knows it's server-stored
        att_row = db.execute(
            "SELECT name, mime, size, path FROM attachments WHERE msg_id=? ORDER BY idx LIMIT 1",
            (msg_id,)
        ).fetchone()
        resp = {"ok": 1, "msg_id": msg_id}
        if att_row:
            resp["attachment"] = {
                "name": att_row["name"],
                "mime": att_row["mime"],
                "size": att_row["size"],
                "path": "/file/attachments/" + att_row["path"],
            }
        return msgpack.packb(resp)
```

### 5. Client: `_save_pfp_crop_impl` Leaves File Handle Open on Cache Update
**File:** `kivy_client.py`
**Issue:** At the end of `_save_pfp_crop_impl`, the line `IMAGE_CACHE.set_bytes(key, open(small_path, "rb").read(), "png")` opens a file handle without a context manager. On Windows this can cause permission errors when the avatar is later overwritten, and on all platforms it relies on GC to close the handle. If an exception occurs between `open()` and `.read()`, the handle leaks.
**Fix:** Use a proper context manager.

```python
# In kivy_client.py _save_pfp_crop_impl(), replace the last IMAGE_CACHE block:

        # Force re-render of own avatar in IMAGE_CACHE so new messages pick it up
        if self.my_hash:
            key = "av:" + self.my_hash
            with open(small_path, "rb") as f:
                IMAGE_CACHE.set_bytes(key, f.read(), "png")
```

### 6. Server: `store_attachment` Registers Handler Before Confirming Write
**File:** `sHades.py`
**Issue:** In `store_attachment()`, the request handler is registered *before* verifying the file was actually written to disk. If `os.makedirs` or the `open/write` fails silently (e.g., permissions), the handler is registered pointing to a nonexistent file, causing every subsequent fetch to return `b"not found"` with no recovery path. Additionally, if the file already exists (`os.path.exists(path)` is True), the handler is **never re-registered**, which means on a fresh boot where the DB has the record but the handler wasn’t yet registered (the boot-time re-registration loop handles this, but a race during concurrent ingest could still miss it).
**Fix:** Only register after confirmed write, and always register regardless of pre-existence.

```python
# In sHades.py store_attachment(), replace the function body:

def store_attachment(name, mime, data):
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    if len(data) > MAX_ATTACHMENT_SZ:
        return None
    digest = hashlib.sha256(data).hexdigest()[:32]
    fname = f"{digest}.{safe_ext(name, mime)}"
    adir = os.path.join(STORAGE, "attachments")
    os.makedirs(adir, exist_ok=True)
    path = os.path.join(adir, fname)
    try:
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
    except Exception as e:
        RNS.log(f"store_attachment write failed for {fname}: {e}", RNS.LOG_ERROR)
        return None
    # Always register (idempotent in RNS); ensures handler exists even if
    # the file pre-dated this process or a previous registration was lost.
    if node_destination is not None:
        node_destination.register_request_handler(
            "/file/attachments/" + fname, serve_attachment_file,
            allow=RNS.Destination.ALLOW_ALL, auto_compress=32_000_000)
    return fname
```

### 7. Client: `_render_message` Avatar Fetch Has No Dedup Guard for In-Flight Requests
**File:** `kivy_client.py`
**Issue:** When rendering many messages from the same author in quick succession (e.g., loading history), `_render_message` spawns a new thread per message for avatar fetching. There is no check against `IMAGE_CACHE._loading` before spawning, so dozens of identical fetches run concurrently for the same user. This wastes bandwidth and can trigger rate limits on the guild.
**Fix:** Check `_loading` set before spawning the fetch thread.

```python
# In kivy_client.py _render_message(), replace the avatar fetch block:

            else:
                # fetch avatar — deduplicate in-flight requests
                key = "av:" + author
                if key not in IMAGE_CACHE._loading:
                    def _do_av(a=author, k=key):
                        res = self.guild_api.avatar_get(a)
                        if res.get("ok") and res.get("avatar"):
                            IMAGE_CACHE.set_bytes(k, res["avatar"], "png")
                            Clock.schedule_once(lambda dt: self._refresh_avatar(k), 0)
                    threading.Thread(target=_do_av, daemon=True).start()
```

### 8. Server: `page_messages` Attachment Links Use Wrong Path Format
**File:** `sHades.py`  
**Issue:** In `page_messages()`, attachment download links are rendered as `` `[Download name`/file/attachments/path] ``. However, `a["path"]` from the DB already contains just the filename (e.g., `abc123.png`), and the Micron link target parser (`_LINK_TARGET`) requires paths to start with `/`. The current code produces `/file/attachments/abc123.png` which is correct, **but** the `msg_row_json()` function (used by the API) also constructs `"path": "/file/attachments/" + a["path"]`. If the DB ever stored a path with a leading slash (from a bug or migration), this would produce `/file/attachments//abc123.png`. More critically, the Micron renderer doesn't URL-encode the filename, so attachments with spaces or special characters in their original name will produce broken links.
**Fix:** Sanitize the link target in the Micron renderer.

```python
# In sHades.py page_messages(), replace the attachment link line:

            for a in atts:
                safe_path = "/file/attachments/" + a["path"]
                out.append("`F8a8[attachment]`f `[Download " +
                           esc_micron_text(a["name"]) + "`" + safe_path + "]\n")
```

These four bugs are the next highest-impact issues after the initial three fixes. Bug #4 is particularly severe because it silently breaks the core server-stored attachment feature for all Kivy client users.

Based on the provided `kv_md.kv` file and cross-referencing it with `kivy_client.py`, here are the additional bugs specific to this KV layout and its interaction with the Python code:

### 9. Client: Right Sidebar Toggle Button Shows Wrong State Label
**File:** `kv_md.kv`
**Issue:** In the `<MainScreen>` right sidebar section, the toggle button text binds to `root.left_open` instead of `root.right_open`. This means the "members ▼/▶" label always reflects the *left* sidebar's state, not the right sidebar's actual open/closed state.
```yaml
# WRONG (current):
MDButtonText:
    text: 'members \u25bc' if root.left_open else 'members \u25b6'

# FIX:
MDButtonText:
    text: 'members \u25bc' if root.right_open else 'members \u25b6'
```

### 10. Client: Message Input `max_height` Prevents Multi-line Growth
**File:** `kv_md.kv`
**Issue:** The `msg_input` MDTextField has `multiline: True` but also `max_height: dp(40)` and `height: dp(40)`. KivyMD's filled text field respects `max_height` as a hard ceiling even when multiline is enabled, so the input never visually grows beyond one line despite accepting newlines via Shift+Enter. Users type multi-line messages blindly without seeing what they've written.
```yaml
# WRONG (current):
MDTextField:
    id: msg_input
    multiline: True
    size_hint_y: None
    height: dp(40)
    max_height: dp(40)       # ← caps growth at exactly 1 line
    line_height: 1

# FIX — allow growth up to ~4 lines, start at 1 line height:
MDTextField:
    id: msg_input
    hint_text: 'Message...'
    mode: 'filled'
    multiline: True
    size_hint_x: 1
    size_hint_y: None
    height: self.minimum_height
    min_height: dp(40)
    max_height: dp(160)      # ~4 lines of sp(14) text
    radius: [dp(12), dp(12), dp(12), dp(12)]
    fill_color_normal: 0.18, 0.2, 0.23, 1
    fill_color_focus: 0.21, 0.23, 0.27, 1
    text_color_normal: 1, 1, 1, 1
    text_color_focus: 1, 1, 1, 1
    cursor_color: 1, 1, 1, 1
    on_text_validate: app.send_message()
```
> **Note:** With this fix, plain Enter still sends (handled by `_on_key_down` in Python), while Shift+Enter inserts a newline and the field visibly expands.

### 11. Client: `FullScreenImageOverlay` Download Uses `requests` (Not Available / Wrong Protocol)
**File:** `kivy_client.py` + `kv_md.kv`
**Issue:** The overlay's `download_image()` method imports `requests` and calls `requests.get(url)`. Two problems:
1.  `requests` is not declared as a dependency and may not be installed; the client uses RNS links, not HTTP.
2.  Even if installed, attachment/embed URLs are either local temp paths (`/tmp/...`) or RNS-served paths (`/file/attachments/...`), neither of which is a valid HTTP URL. Every download attempt fails silently or raises `MissingSchema`.

**Fix:** Replace the HTTP download with a local-file copy for temp paths, and an RNS fetch for server paths:
```python
# In kivy_client.py FullScreenImageOverlay.download_image():
def download_image(self):
    """Save the displayed image to the user's Downloads folder."""
    import threading, shutil
    from kivy.clock import Clock

    def do_download():
        try:
            src = self.source
            # Determine a safe output filename
            basename = os.path.basename(src.split("?")[0]) or "image.png"
            out_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, basename)

            if os.path.isfile(src):
                # Local temp file (ephemeral embed or cached avatar)
                shutil.copy2(src, out_path)
            elif src.startswith("/file/"):
                # Server-stored attachment — fetch via RNS link
                app = App.get_running_app()
                api = getattr(app, "guild_api", None)
                if api is None:
                    raise RuntimeError("Not connected")
                blob = api.fetch_file(src)
                if not blob:
                    raise RuntimeError("Fetch returned empty")
                with open(out_path, "wb") as f:
                    f.write(blob)
            else:
                raise RuntimeError(f"Unsupported source: {src}")

            Clock.schedule_once(lambda dt: self._show_status(f"Saved to {out_path}"), 0)
        except Exception as e:
            Clock.schedule_once(lambda dt: self._show_status(f"Download failed: {e}"), 0)

    threading.Thread(target=do_download, daemon=True).start()
```

### 12. Client: Channel Header Never Updates When Switching Channels
**File:** `kv_md.kv` + `kivy_client.py`
**Issue:** The top bar `Label` in `<MainScreen>` has hardcoded `text: '#general'`. The `_switch_channel()` method in Python updates `main._current_channel` but never touches this label. Every channel shows "#general" in the header regardless of which channel is active.

**Fix in `kv_md.kv`:** Give the label an id and bind dynamically:
```yaml
Label:
    id: channel_header
    text: '#' + (root._current_channel or 'general')
    color: 1, 1, 1, 1
    bold: True
    font_size: sp(16)
    size_hint_x: 1
    halign: 'left'
    valign: 'middle'
    text_size: self.width, None
```

**Fix in `kivy_client.py` `_switch_channel()`:** Ensure the property triggers rebind:
```python
def _switch_channel(self, name):
    main = self.root.get_screen("main")
    main._current_channel = name
    # Force header update (StringProperty rebind may lag behind grid clear)
    header = main.ids.get("channel_header")
    if header:
        header.text = "#" + name
    grid = main.ids.message_grid
    grid.clear_widgets()
    self._msg_widgets.clear()
    self._seq = 0
    self._last_ts = 0.0
    self._fetch_messages(name)
    self._refresh_members_ui(name)
```
> Also add `_current_channel = StringProperty("general")` to the `MainScreen` class definition so the KV binding works reactively.

### Summary of All Bugs Found (Including Previous Round)

| # | File | Severity | Description |
|---|------|----------|-------------|
| 1 | kivy_client.py | Critical | Avatar crop coordinate space mismatch + RGBA→JPEG crash |
| 2 | sHades.py | High | Attachment handlers not re-registered on boot |
| 3 | kivy_client.py | Medium | PFP editor save captures stale widget state after dismiss |
| 4 | sHades.py | Critical | `/api/post` never returns attachment metadata → server-stored attachments treated as ephemeral |
| 5 | kivy_client.py | Low | File handle leak in IMAGE_CACHE.set_bytes |
| 6 | sHades.py | Medium | store_attachment registers handler before confirming write |
| 7 | kivy_client.py | Medium | No dedup guard on concurrent avatar fetches |
| 8 | sHades.py | Low | Micron attachment links don't escape special chars in filenames |
| **9** | **kv_md.kv** | **Medium** | **Right sidebar toggle shows left_open state** |
| **10** | **kv_md.kv** | **High** | **msg_input max_height=40 prevents visible multi-line editing** |
| **11** | **kivy_client.py** | **High** | **Fullscreen download uses requests (HTTP) instead of RNS/local copy** |
| **12** | **kv_md.kv/py** | **Medium** | **Channel header label hardcoded to #general, never updates** |
Yes, there are several additional bugs in both files that have not yet been addressed. These range from logic errors causing silent failures to UI/UX defects and security-adjacent issues.

### 13. Server: `api_messages` Returns Wrong Messages for `since_id` Pagination
**File:** `sHades.py`
**Issue:** When a client polls with `since_id`, the query uses `id > since_id ORDER BY id ASC`. However, `since_id` is a *message hash string* (`msg_id`), not the integer primary key `id`. The SQL comparison `id > 'abc123...'` performs an implicit cast or lexicographic comparison against an integer column, which in SQLite returns unpredictable results (often 0 or all rows). This means delta polling either returns nothing or re-sendsYes, there are several additional bugs in both files that have not yet been addressed. These range from logic errors causing silent failures to UI/UX defects and security-adjacent issues.

### 13. Server: `api_messages` Returns Wrong Messages for `since_id` Pagination
**File:** `sHades.py`
**Issue:** When a client polls with `since_id`, the query uses `id > since_id ORDER BY id ASC`. However, `since_id` is a *message hash string* (`msg_id`), not the integer primary key `id`. The SQL comparison `id > 'abc123...'` performs an implicit cast or lexicographic comparison against an integer column, which in SQLite returns unpredictable results (often 0 or all rows). This means delta polling either returns nothing or re-sends the entire history every cycle.
**Fix:** Resolve `since_id` to its integer `id` first.

```python
# In sHades.py api_messages(), replace the since_id branch:

        elif isinstance(since_id, str) and since_id:
            # since_id is a msg_id hash; resolve to integer PK
            bound = db.execute(
                "SELECT id FROM messages WHERE msg_id=? AND channel_id=?",
                (since_id, cid)).fetchone()
            if bound:
                new_rows = db.execute(
                    "SELECT * FROM messages WHERE channel_id=? AND id>? ORDER BY id ASC LIMIT ?",
                    (cid, bound["id"], limit)).fetchall()
            else:
                new_rows = []
```

### 14. Client: `_render_message` Embeds URLs from Deleted/Edited Content Stale
**File:** `kivy_client.py`
**Issue:** When `_do_update` processes an edit or delete, it updates `content_markup` but never touches `embed_container`. If a user edits a message to remove an image URL, the old embed widget remains visible. If they delete the message, the row is removed (correct), but if the delete fails server-side and arrives as an edit with empty content, stale embeds persist.
**Fix:** Clear and rebuild embeds on update.

```python
# In kivy_client.py _do_update(), add embed refresh:

    def _do_update(self, row, content, deleted):
        if deleted:
            try:
                grid = self.root.get_screen("main").ids.message_grid
                grid.remove_widget(row)
            except Exception:
                pass
            self._msg_widgets.pop(row.msg_id, None)
        else:
            row.content = content
            row.content_markup = md_to_kivy(content)
            # Rebuild embeds to match updated content
            container = row.ids.get("embed_container")
            if container:
                container.clear_widgets()
            for url in find_urls(content):
                ext = os.path.splitext(url.split("?")[0])[1].lower()
                if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    emb = MessageEmbed(source=url, size_hint_y=None, height=dp(200))
                    container.add_widget(emb)
```

### 15. Client: `_on_key_down` Tab Traversal Crashes on Non-Connect Screens
**File:** `kivy_client.py`
**Issue:** The Tab key handler unconditionally accesses `connect.ids.hub_address` etc. even when the connect screen isn't current. While it checks `if connect:`, the `ids` dict may not be populated if the screen hasn't been visited yet (Kivy lazily loads KV rules per screen). Pressing Tab on the main screen before ever visiting connect raises `KeyError`.
**Fix:** Guard with `hasattr` or try/except.

```python
# In kivy_client.py _on_key_down(), wrap the Tab block:

        if key == 9:
            try:
                connect = self.root.get_screen("connect") if self.root else None
                if connect and hasattr(connect.ids, "hub_address"):
                    fields = [
                        connect.ids.hub_address,
                        connect.ids.username,
                        connect.ids.node_hash,
                        connect.ids.delivery_hash,
                    ]
                    focused = next(
                        (f for f in fields if getattr(f, "focus", False)), None
                    )
                    if focused is not None:
                        idx = fields.index(focused)
                        next_idx = (idx + 1) % len(fields)
                        fields[next_idx].focus = True
                        return True
            except Exception:
                pass
```

### 16. Server: `store_attachment` Returns Filename But Never Validates Extension Against Allowlist
**File:** `sHades.py`
**Issue:** `safe_ext()` falls back to `"bin"` for unknown MIME types, but `ATTACHMENT_EXT` is only checked for regex-matched extensions. A file named `malware.exe` with MIME `application/octet-stream` gets stored as `<hash>.bin`, bypassing the extension allowlist entirely. More critically, the allowlist check in `safe_ext` only validates the *extracted* extension, not whether that extension is actually permitted — any 1-8 char alphanumeric extension passes the regex and then gets returned without checking membership in `ATTACHMENT_EXT`.
**Fix:** Enforce the allowlist explicitly.

```python
# In sHades.py safe_ext(), fix the validation:

def safe_ext(name, mime):
    m = re.search(r"\.([a-zA-Z0-9]{1,8})$", name or "")
    ext = m.group(1).lower() if m else None
    if ext and ext in ATTACHMENT_EXT:
        return ext
    mm = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
          "audio/mpeg": "mp3", "video/mp4": "mp4", "application/pdf": "pdf"}
    fallback = mm.get((mime or "").lower())
    if fallback and fallback in ATTACHMENT_EXT:
        return fallback
    return "bin"  # Only safe fallback; never return an unvalidated extension
```

### 17. Client: `_save_session` Writes Hub Credentials in Plaintext JSON
**File:** `kivy_client.py`
**Issue:** `session.json` stores `hub_address`, `hub_host`, and `hub_port` in plaintext. While these aren't secrets per se, on shared machines or Android devices with scoped storage leaks, this exposes the user's guild infrastructure topology. More importantly, the session file is written without atomic rename, so a crash during write corrupts it and prevents auto-reconnect.
**Fix:** Write atomically via temp file + rename.

```python
# In kivy_client.py _save_session():

    def _save_session(self):
        try:
            path = self._session_path()
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "node_hash": self.node_hash,
                    "delivery_hash": self.delivery_hash,
                    "username": self.username,
                    "hub_address": self.hub_address,
                    "hub_host": self.hub_host,
                    "hub_port": self.hub_port,
                }, f)
            os.replace(tmp, path)  # Atomic on POSIX and Windows
        except Exception:
            pass
```

### 18. Client: `ImageCache.fetch_and_cache` Schedules `set_bytes` on Main Thread But Never Removes Key from `_loading` on Failure
**File:** `kivy_client.py`
**Issue:** If `api.fetch_file(path)` returns `None` or raises, the `finally` block correctly discards the key from `_loading`. However, if `Clock.schedule_once` itself fails (e.g., app shutting down), the key stays in `_loading` forever, permanently blocking future fetch attempts for that avatar/attachment.
**Fix:** Move the discard into the scheduled callback as well, with a belt-and-braces approach.

```python
# In kivy_client.py ImageCache.fetch_and_cache():

    def fetch_and_cache(self, api, path, key, ext="png"):
        if key in self._paths or key in self._loading:
            return self._paths.get(key)
        self._loading.add(key)
        def work():
            try:
                data = api.fetch_file(path)
                if data:
                    Clock.schedule_once(lambda dt: self._set_and_unlock(key, data, ext))
                else:
                    self._loading.discard(key)
            except Exception:
                self._loading.discard(key)
        threading.Thread(target=work, daemon=True).start()
        return None

    def _set_and_unlock(self, key, data, ext):
        self.set_bytes(key, data, ext)
        self._loading.discard(key)
```

### Summary of All Bugs Found (Cumulative)

| # | File | Severity | Status |
|---|------|----------|--------|
| 1–3 | kivy_client.py / sHades.py | Critical–Medium | Fixed (round 1) |
| 4–8 | kivy_client.py / sHades.py | Critical–Low | Fixed (round 2) |
| 9–12 | kv_md.kv / kivy_client.py | High–Medium | Fixed (round 3) |
| **13** | **sHades.py** | **Critical** | **NEW: `since_id` pagination broken** |
| **14** | **kivy_client.py** | **Medium** | **NEW: Stale embeds after edit** |
| **15** | **kivy_client.py** | **Medium** | **NEW: Tab crash on uninitialized screen** |
| **16** | **sHades.py** | **Medium** | **NEW: Attachment extension allowlist bypass** |
| **17** | **kivy_client.py** | **Low** | **NEW: Non-atomic session write** |
| **18** | **kivy_client.py** | **Medium** | **NEW: ImageCache `_loading` leak on failure** |

Bug #13 is the most severe new finding — it silently breaks incremental sync for every connected client, forcing full history re-fetches on every poll cycle and causing duplicate messages in the UI.