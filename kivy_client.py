# sHades Kivy client — Discord-like UI over Reticulum/LXMF
#
# Connects to an sHades guild (sHades.py) over RNS: establishes a link to the
# guild's nomadnetwork.node destination, identifies, and polls the guild's
# /api/* endpoints (msgpack) for messages, state, avatars and attachments.
#
# UI (modeled after Discord):
#   - left  : guild channel list (collapsable)
#   - center: active channel messages + input bar
#   - right : channel member list (collapsable)
#
# Targets: Linux, Windows, Android. See .github/workflows/build.yml.

import os
import sys
import re
import json
import time
import hashlib
import threading
import random
from collections import OrderedDict
import mimetypes
from PIL import Image as PILImage
from PIL import ImageDraw as PILImageDraw
from PIL import ImageChops as PILImageChops

# ---------------------------------------------------------------------------
# Kivy (guarded so the module can be imported for linting without Kivy present)
# ---------------------------------------------------------------------------
try:
    from kivy.app import App
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.floatlayout import FloatLayout
    from kivy.uix.label import Label
    from kivy.uix.image import Image, AsyncImage
    from kivy.uix.screenmanager import ScreenManager, Screen
    from kivy.core.window import Window
    from kivy.clock import Clock
    from kivy.properties import (
        StringProperty, ObjectProperty, BooleanProperty, NumericProperty,
    )
    from kivy.uix.behaviors import ButtonBehavior
    from kivy.uix.filechooser import FileChooserListView
    from kivy.uix.popup import Popup
    from kivy.uix.button import Button
    from kivy.uix.togglebutton import ToggleButton
    from kivy.metrics import dp, sp
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e

# KivyMD imports (only available if Kivy is present)
if KIVY:
    try:
        from kivymd.app import MDApp
        KIVYMD = True
    except Exception as _e:
        KIVYMD = False
        _KIVYMD_IMPORT_ERROR = _e
else:
    KIVYMD = False

import RNS
from RNS.vendor import umsgpack as msgpack

# ---------------------------------------------------------------------------
# Theme (Discord-like dark palette)
# ---------------------------------------------------------------------------
BG        = "#23272A"   # app background
SIDEBAR   = "#2F3136"   # channel / member sidebar
SIDEBAR_D = "#292B2F"
PANEL     = "#36393F"   # chat area
INPUT_BG  = "#40444B"   # input bar
TEXT      = "#DCDDDE"   # primary text
TEXT_MUTED= "#8E9297"   # secondary text
LINK      = "#00A2E8"   # links / embeds
ACCENT    = "#5865F2"   # accent / unread divider
AVATAR_BG = "#7289DA"

# Attachment storage styles:
#   server   - file is stored on the guild; bytes re-fetched via /file/attachments
#              path (survives restart, shown with a loading placeholder until received).
#   ephemeral - file is sent inline as [name, mime, blob] (p2p); bytes kept locally
#              so they survive restart, but there is no server copy to re-fetch.
ATT_SERVER = "server"
ATT_EPHEMERAL = "ephemeral"
ATT_PLACEHOLDER_TEXT = "[loading image…]"

DEBUG = False  # set via --debug CLI arg; enables verbose attachment/ingest logging


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def hexstr(b):
    if isinstance(b, str):
        return b
    return RNS.hexrep(b, delimit=False)


def now():
    return time.time()


def cache_dir():
    d = os.path.join(_app_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def cached_path(key):
    return os.path.join(cache_dir(), hashlib.sha256(key.encode()).hexdigest()[:32])


def _app_data_dir():
    """Cross-platform writable app data directory.

    On Android (p4a) uses the app's internal storage directory so scoped
    storage (Android 10+) doesn't block writes. On desktop falls back to
    ~/.sHades_client.
    """
    try:
        from android.storage import app_storage_path  # p4a only, no-op on desktop
        return app_storage_path()
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".sHades_client")


# ---------------------------------------------------------------------------
# Markdown -> Kivy markup
# ---------------------------------------------------------------------------
_URL = re.compile(r"(https?://[^\s<>\"]+)")
_MD = re.compile(
    r"\*\*(?P<b>[^*\n]{1,200})\*\*"
    r"|\*(?P<i>[^*\n]{1,200})\*"
    r"`(?P<code>[^`\n]{1,120})`"
)


def md_to_kivy(text):
    """Convert a small markdown subset to Kivy markup."""
    if not text:
        return ""
    out = []
    pos = 0
    for m in _MD.finditer(text):
        out.append(_escape(text[pos:m.start()]))
        g = m.lastgroup
        if g == "b":
            out.append("[b]" + _escape(m.group("b")) + "[/b]")
        elif g == "i":
            out.append("[i]" + _escape(m.group("i")) + "[/i]")
        elif g == "code":
            out.append("[color=#BFBFBF]" + _escape(m.group("code")) + "[/color]")
        pos = m.end()
    out.append(_escape(text[pos:]))
    return "".join(out)


def _escape(s):
    s = s.replace("&", "&amp;")
    s = s.replace("[", "&bl;")
    s = s.replace("]", "&br;")
    return s


def find_urls(text):
    return _URL.findall(text or "")


def dlog(msg):
    """Debug log — only prints when --debug is passed."""
    if DEBUG:
        print(f"[DEBUG] {msg}", flush=True)


# ---------------------------------------------------------------------------
# API client over RNS link
# ---------------------------------------------------------------------------
class GuildAPI:
    """Thin wrapper around an RNS Link to the sHades guild node."""

    def __init__(self, reticulum, link, on_error=None):
        self.reticulum = reticulum
        self.link = link
        self.on_error = on_error or (lambda *a: None)

    def _request(self, path, data, timeout=10):
        if not isinstance(data, dict):
            data = {}
        receipt = self.link.request(path, data=data)
        # link.request() may return a Receipt object (with .response) or
        # True/False (bool) depending on RNS version. Handle both.
        if isinstance(receipt, bool):
            if not receipt:
                return {"ok": 0, "error": "request failed"}
            # RNS queued the request but gave no receipt — poll for response
            # via a fresh receipt. Create one by calling request again without data.
            receipt = self.link.request(path, data={})
        deadline = time.time() + timeout
        while (hasattr(receipt, "response")
               and receipt.response is None
               and time.time() < deadline):
            time.sleep(0.05)
        if not hasattr(receipt, "response") or receipt.response is None:
            return {"ok": 0, "error": "timeout"}
        try:
            return msgpack.unpackb(receipt.response, raw=False)
        except Exception as e:
            return {"ok": 0, "error": "bad response: %s" % e}

    def state(self, channel):
        return self._request("/api/state", {"channel": channel})

    def messages(self, channel, since_id=None, since_ts=None, before=None, limit=50):
        data = {"channel": channel, "limit": limit}
        if since_id is not None:
            data["since_id"] = since_id
        if since_ts is not None:
            data["since_ts"] = since_ts
        if before is not None:
            data["before"] = before
        return self._request("/api/messages", data)

    def post(self, channel, content, attachments=None):
        data = {"channel": channel, "content": content}
        if attachments:
            data["attachments"] = attachments
        return self._request("/api/post", data)

    def edit(self, msg_id, content):
        return self._request("/api/edit", {"msg": msg_id, "content": content})

    def delete(self, msg_id):
        return self._request("/api/delete", {"msg": msg_id})

    def register_node(self, node_hash, address, port, identity_hash):
        return self._request("/api/register_node", {
            "node_hash": node_hash,
            "address": address,
            "port": port,
            "identity_hash": identity_hash,
        })

    def lookup_node(self, node_hash):
        return self._request("/api/lookup_node", {"node_hash": node_hash})

    def register(self, username, profile_node=""):
        return self._request("/api/register",
                             {"username": username, "profile_node": profile_node})

    def avatar_upload(self, blob):
        return self._request("/api/avatar", {"avatar": blob})

    def avatar_get(self, user_hash):
        return self._request("/api/avatar_get", {"user": user_hash})

    def fetch_file(self, path):
        """Fetch raw bytes from a /file/... request handler on the guild."""
        dlog(f"fetch_file: requesting {path}")
        receipt = self.link.request(path, data={})
        # link.request() may return a Receipt object (with .response) or
        # True/False (bool) depending on RNS version. Handle both.
        if isinstance(receipt, bool):
            if not receipt:
                dlog(f"fetch_file: link.request returned False for {path}")
                return None
            receipt = self.link.request(path, data={})
        deadline = time.time() + 15
        while receipt.response is None and time.time() < deadline:
            time.sleep(0.05)
        if receipt.response is None:
            dlog(f"fetch_file: timeout for {path}")
            return None
        dlog(f"fetch_file: received {len(receipt.response)} bytes for {path}")
        return receipt.response


# ---------------------------------------------------------------------------
# Image cache (pfps + attachments)
# ---------------------------------------------------------------------------
class ImageCache:
    def __init__(self):
        self._paths = {}   # key -> cached file path
        self._loading = set()

    def get(self, key):
        return self._paths.get(key)

    def set_bytes(self, key, data, ext="png"):
        try:
            fpath = os.path.join(cache_dir(), key + "." + ext)
            with open(fpath, "wb") as f:
                f.write(data)
            self._paths[key] = fpath
            return fpath
        except Exception as e:
            print("image cache error:", e)
            return None

    def fetch_and_cache(self, api, path, key, ext="png"):
        if key in self._paths or key in self._loading:
            return self._paths.get(key)
        self._loading.add(key)

        def work():
            try:
                data = api.fetch_file(path)
                if data:
                    Clock.schedule_once(lambda dt: self.set_bytes(key, data, ext))
            finally:
                self._loading.discard(key)

        threading.Thread(target=work, daemon=True).start()
        return None


IMAGE_CACHE = ImageCache()


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------
if KIVY:
    class PfpImage(Image):
        """Circular-ish avatar placeholder with fallback color."""
        pass

    class PassThroughImage(Image):
        """Image that passes touch events through to parent widget."""
        def on_touch_down(self, touch):
            return False

    class PFPEditorWidget(FloatLayout):
        """Round-mask avatar cropper — image at natural size (never stretched),
        circle overlay is draggable and resizable."""
        source = StringProperty("")
        on_save = ObjectProperty(None, allownone=True)
        circle_x = NumericProperty(0.5)   # relative center x (0-1)
        circle_y = NumericProperty(0.5)   # relative center y (0-1)
        circle_radius = NumericProperty(0.25)  # relative radius (0-1)
        # Computed pixel values for KV canvas drawing
        crop_cx = NumericProperty(0)
        crop_cy = NumericProperty(0)
        crop_r = NumericProperty(0)

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._drag_mode = None   # 'move' or 'resize'
            self._last_pos = None
            self._img = None
            self.bind(
                circle_x=self._update_crop,
                circle_y=self._update_crop,
                circle_radius=self._update_crop,
                width=self._update_crop,
                height=self._update_crop,
                pos=self._update_crop,
            )
            self._update_crop()

        def _update_crop(self, *args):
            self.crop_cx = self.x + self.circle_x * self.width
            self.crop_cy = self.y + self.circle_y * self.height
            self.crop_r = self.circle_radius * min(self.width, self.height)
            self._size_image()

        def on_source(self, instance, value):
            """Re-size image when source changes or texture loads."""
            img = self.ids.get("pfp_img")
            if img:
                img.bind(texture_size=lambda *args: self._size_image())
            Clock.schedule_once(lambda dt: self._size_image(), 0)

        def _size_image(self):
            """Size the child image at natural size, centered, never stretched."""
            img = self.ids.get("pfp_img")
            if not img or not img.texture:
                return
            tw, th = img.texture_size
            if tw <= 0 or th <= 0:
                return
            if self.width <= 0 or self.height <= 0:
                return
            # Cap to fit within editor while preserving aspect ratio
            max_w = self.width * 0.95
            max_h = self.height * 0.95
            scale = min(max_w / tw, max_h / th, 1.0)
            img.size = (tw * scale, th * scale)
            img.pos = (
                self.center_x - img.size[0] / 2,
                self.center_y - img.size[1] / 2,
            )

        def _circle_hit(self, touch):
            dx = touch.x - self.crop_cx
            dy = touch.y - self.crop_cy
            return (dx * dx + dy * dy) ** 0.5

        def on_touch_down(self, touch):
            if not self.collide_point(*touch.pos):
                return super().on_touch_down(touch)
            r = self._circle_hit(touch)
            if r <= self.crop_r:
                self._drag_mode = 'move'
            else:
                self._drag_mode = 'resize'
            self._last_pos = touch.pos
            touch.grab(self)
            return True

        def on_touch_move(self, touch):
            if touch.grab_current is not self:
                return super().on_touch_move(touch)
            if self._last_pos is None:
                return True
            dx = touch.x - self._last_pos[0]
            dy = touch.y - self._last_pos[1]
            if self._drag_mode == 'move':
                new_cx = self.crop_cx + dx
                new_cy = self.crop_cy + dy
                r = self.crop_r
                new_cx = max(r, min(self.right - r, new_cx))
                new_cy = max(r, min(self.top - r, new_cy))
                self.circle_x = (new_cx - self.x) / self.width if self.width > 0 else 0.5
                self.circle_y = (new_cy - self.y) / self.height if self.height > 0 else 0.5
            elif self._drag_mode == 'resize':
                cx, cy = self.crop_cx, self.crop_cy
                new_dx = touch.x - cx
                new_dy = touch.y - cy
                new_r = (new_dx * new_dx + new_dy * new_dy) ** 0.5
                rel_r = max(0.05, min(new_r / min(self.width, self.height), 0.5))
                self.circle_radius = rel_r
                print(f"[PFP DEBUG] drag resize: circle_radius={self.circle_radius:.3f}")
            self._last_pos = touch.pos
            return True

        def on_touch_up(self, touch):
            if touch.grab_current is self:
                touch.ungrab(self)
                self._drag_mode = None
                self._last_pos = None
                return True
            return super().on_touch_up(touch)

    class MessageEmbed(AsyncImage, ButtonBehavior):
        """Clickable image embed - click to expand to full screen, click again to minimize.

        Renders at a fixed uniform height (STANDARD_HEIGHT) and scales to fit
        without stretching (allow_stretch=False, keep_ratio=True).
        """
        STANDARD_HEIGHT = dp(200)
        source_url = StringProperty("")
        is_expanded = BooleanProperty(False)

        def on_release(self):
            app = App.get_running_app()
            if not app:
                return True
            if self.is_expanded:
                app.dismiss_fullscreen_image(self)
            else:
                app.show_fullscreen_image(self.source, embed=self)
                self.is_expanded = True
            return True


    class FullScreenImageOverlay(FloatLayout):
        """Full-screen image viewer with dark overlay and download/close buttons."""
        source = StringProperty("")
        embed = ObjectProperty(None, allownone=True)

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.size_hint = (1, 1)

        def download_image(self):
            """Save the displayed image to the user's Downloads folder.

            Sources are either local temp files (ephemeral embeds / cached
            avatars) or /file/attachments/... paths served by the guild over
            the RNS link. requests/HTTP is not used: it is not a declared
            dependency and none of these sources are HTTP URLs.
            """
            import threading
            import shutil
            from kivy.clock import Clock
            from kivy.app import App

            def do_download():
                try:
                    src = self.source
                    basename = os.path.basename(src.split("?")[0]) or "image.png"
                    out_dir = os.path.join(os.path.expanduser("~"), "Downloads")
                    os.makedirs(out_dir, exist_ok=True)
                    out_path = os.path.join(out_dir, basename)

                    if os.path.isfile(src):
                        # Local temp file (ephemeral embed or cached avatar).
                        shutil.copy2(src, out_path)
                    elif src.startswith("/file/"):
                        # Server-stored attachment — fetch via RNS link.
                        app = App.get_running_app()
                        api = getattr(app, "guild_api", None)
                        if api is None:
                            raise RuntimeError("Not connected to guild")
                        blob = api.fetch_file(src)
                        if not blob:
                            raise RuntimeError("Fetch returned empty")
                        with open(out_path, "wb") as f:
                            f.write(blob)
                    else:
                        raise RuntimeError("Unsupported source: %s" % src)

                    Clock.schedule_once(
                        lambda dt: self._show_status("Saved to %s" % out_path), 0)
                except Exception as e:
                    Clock.schedule_once(
                        lambda dt: self._show_status("Download failed: %s" % e), 0)

            threading.Thread(target=do_download, daemon=True).start()

        def _show_status(self, text):
            app = App.get_running_app()
            if app:
                app._set_status(text)

        def dismiss(self):
            """Close the full-screen overlay and tell the embed to minimize."""
            app = App.get_running_app()
            if app and self.embed is not None:
                app.dismiss_fullscreen_image(self.embed)
            elif self.parent:
                self.parent.remove_widget(self)


    class MessageRow(BoxLayout):
        """One chat message."""
        author_hash = StringProperty("")
        username = StringProperty("user")
        ts = StringProperty("")
        content = StringProperty("")
        content_markup = StringProperty("")
        avatar_source = StringProperty("")
        msg_id = StringProperty("")
        is_own = BooleanProperty(False)

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.size_hint_y = None
            self.height = dp(56)
            self.padding = [dp(8), dp(4)]
            self.spacing = dp(8)
            # Ensure embed_container exists even if KV isn't loaded
            if not hasattr(self.ids, "embed_container"):
                from kivy.uix.boxlayout import BoxLayout
                container = BoxLayout(
                    orientation="vertical",
                    size_hint_y=None,
                    height=0,
                    spacing=dp(4),
                )
                container.bind(minimum_height=container.setter("height"))
                self.ids["embed_container"] = container
                self.add_widget(container)

        def on_content_markup(self, instance, value):
            # Rebuild content line after markup set
            pass


class ClickableLabel(ButtonBehavior, Label):
    """Username label that opens a user profile when tapped."""
    target_hash = StringProperty("")


class ChannelButton(ButtonBehavior, BoxLayout):
    channel_name = StringProperty("")


# ---------------------------------------------------------------------------
# KV UI
# ---------------------------------------------------------------------------
KV = r"""
#:import dp kivy.metrics.dp

<PFPEditorWidget>:
    canvas.after:
        Color:
            rgba: 0, 0, 0, 0.4
        Ellipse:
            pos: root.crop_cx - root.crop_r, root.crop_cy - root.crop_r
            size: root.crop_r * 2, root.crop_r * 2
        Color:
            rgba: 1, 1, 1, 1
        Line:
            circle: root.crop_cx, root.crop_cy, root.crop_r
            width: 2
    PassThroughImage:
        id: pfp_img
        source: root.source
        allow_stretch: False
        keep_ratio: True
        size_hint: None, None
        # Re-size when the texture arrives. on_source() cannot be relied on
        # for this: `source` is set in the constructor, i.e. before the KV rule
        # creates pfp_img, so root.ids is still empty when it fires.
        on_texture_size: root._size_image()

<MessageRow>:
    orientation: 'horizontal'
    size_hint_y: None
    height: self.minimum_height
    padding: [dp(8), dp(6)]
    spacing: dp(8)
    canvas.before:
        Color:
            rgba: (0.2, 0.22, 0.25, 1) if root.is_own else (0, 0, 0, 0)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8), dp(8), dp(8), dp(8)]

    PfpImage:
        id: pfp
        size_hint: None, None
        size: dp(32), dp(32)
        source: root.avatar_source or ""
        allow_stretch: False
        keep_ratio: True

    BoxLayout:
        orientation: 'vertical'
        size_hint_x: 1
        spacing: dp(2)
        BoxLayout:
            orientation: 'horizontal'
            size_hint_y: None
            height: dp(18)
            ClickableLabel:
                text: root.username
                target_hash: root.author_hash
                color: (1, 1, 1, 1) if root.is_own else (0.9, 0.9, 0.9, 1)
                bold: True
                font_size: sp(13)
                text_size: self.width, None
                size_hint_x: 1
                halign: 'left'
                valign: 'middle'
                shorten: True
                on_press: app.open_profile(root.author_hash, root.username)
            Label:
                text: root.ts
                color: (0.6, 0.63, 0.67, 1)
                font_size: sp(11)
                text_size: self.width, None
                size_hint_x: None
                width: dp(50)
                halign: 'right'
                valign: 'middle'
        Label:
            id: content_lbl
            text: root.content_markup
            markup: True
            color: (1, 1, 1, 1)
            font_size: sp(14)
            text_size: self.width, None
            size_hint_y: None
            height: self.texture_size[1]
            halign: 'left'
            valign: 'top'

    Widget:
        size_hint_x: None
        width: dp(4)

<ChannelButton>:
    orientation: 'vertical'
    size_hint_y: None
    height: dp(44)
    padding: [dp(10), 0]
    spacing: dp(4)
    Label:
        text: root.channel_name
        color: (1, 1, 1, 0.85)
        font_size: sp(13)
        halign: 'left'
        valign: 'middle'
        text_size: self.width, None

<MemberRow>:
    orientation: 'horizontal'
    size_hint_y: None
    height: dp(36)
    padding: [dp(8), 0]
    spacing: dp(8)
    PfpImage:
        id: pfpx
        size_hint: None, None
        size: dp(26), dp(26)
        source: root.avatar_source or ""
        allow_stretch: False
        keep_ratio: True
    ClickableLabel:
        text: root.username
        target_hash: root.author_hash
        color: (1, 1, 1, 0.9)
        font_size: sp(13)
        halign: 'left'
        valign: 'middle'
        text_size: self.width, None
        size_hint_x: 1
        on_press: app.open_profile(root.author_hash, root.username)

<ConnectScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(40)
        spacing: dp(20)
        canvas.before:
            Color:
                rgba: 0.14, 0.16, 0.19, 1
            Rectangle:
                pos: self.pos
                size: self.size
        Image:
            id: banner_config
            source: app.banner_config_path or ''
            size_hint_y: None
            height: dp(200)
            size_hint_x: None
            width: dp(400)
            allow_stretch: False
            keep_ratio: True
        Label:
            text: 'sHades'
            font_size: sp(40)
            bold: True
            color: 1, 1, 1, 1
            size_hint_y: None
            height: dp(60)
        Label:
            text: 'Connect to a guild'
            color: 0.7, 0.73, 0.77, 1
            font_size: sp(16)
            size_hint_y: None
            height: dp(30)
        BoxLayout:
            orientation: 'vertical'
            spacing: dp(10)
            size_hint_y: None
            height: dp(340)
            TextInput:
                id: hub_address
                hint_text: 'Hub address (ip:port, e.g. 192.168.1.50:29221)'
                multiline: False
                size_hint_y: None
                height: dp(44)
                background_color: 0.22, 0.24, 0.28, 1
                foreground_color: 1, 1, 1, 1
                cursor_color: 1, 1, 1, 1
            TextInput:
                id: username
                hint_text: 'Your username'
                multiline: False
                size_hint_y: None
                height: dp(44)
                background_color: 0.18, 0.2, 0.23, 1
                foreground_color: 1, 1, 1, 1
                cursor_color: 1, 1, 1, 1
            Label:
                text: 'Guild hashes (leave blank to auto-discover from hub announce)'
                color: 0.7, 0.73, 0.77, 1
                font_size: sp(12)
                size_hint_y: None
                height: dp(20)
                halign: 'left'
                valign: 'middle'
                text_size: self.width, None
            TextInput:
                id: node_hash
                hint_text: 'Guild node_hash (32 hex, optional)'
                multiline: False
                size_hint_y: None
                height: dp(40)
                background_color: 0.18, 0.2, 0.23, 1
                foreground_color: 1, 1, 1, 1
                cursor_color: 1, 1, 1, 1
            TextInput:
                id: delivery_hash
                hint_text: 'Guild delivery_hash (32 hex, optional)'
                multiline: False
                size_hint_y: None
                height: dp(40)
                background_color: 0.18, 0.2, 0.23, 1
                foreground_color: 1, 1, 1, 1
                cursor_color: 1, 1, 1, 1
        Button:
            text: 'Connect'
            size_hint_y: None
            height: dp(50)
            background_color: 0.36, 0.41, 0.59, 1
            color: 1, 1, 1, 1
            on_press:app.connect_to_guild(hub_address.text,username.text,node_hash.text,delivery_hash.text)
        Label:
            id: status
            text: ''
            color: 1, 0.4, 0.4, 1
            font_size: sp(13)
            size_hint_y: None
            height: dp(30)

<ProfileScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(20)
        spacing: dp(15)
        canvas.before:
            Color:
                rgba: 0.14, 0.16, 0.19, 1
            Rectangle:
                pos: self.pos
                size: self.size
        Label:
            text: 'Profile'
            font_size: sp(32)
            bold: True
            color: 1, 1, 1, 1
            size_hint_y: None
            height: dp(50)
        BoxLayout:
            orientation: 'horizontal'
            size_hint_y: None
            height: dp(200)
            spacing: dp(10)
            Widget:
                size_hint_x: 1
            PfpImage:
                id: profile_pfp
                size_hint: None, None
                size: dp(150), dp(150)
                source: root.target_avatar or ''
                allow_stretch: False
                keep_ratio: True
                canvas.after:
                    Color:
                        rgba: 0, 0, 0, 0.5
                    Ellipse:
                        pos: self.center_x - dp(75), self.center_y - dp(75)
                        size: dp(150), dp(150)
                    Color:
                        rgba: 1, 1, 1, 1
                    Line:
                        circle: self.center_x, self.center_y, 75
                        width: 2
            Widget:
                size_hint_x: 1
        Label:
            text: root.target_username
            font_size: sp(20)
            bold: True
            color: 1, 1, 1, 1
            size_hint_y: None
            height: dp(30)
            halign: 'center'
            text_size: self.width, None
        TextInput:
            id: profile_username_input
            text: root.target_username
            multiline: False
            size_hint_y: None
            height: dp(40)
            background_color: 0.22, 0.24, 0.28, 1
            foreground_color: 1, 1, 1, 1
            cursor_color: 1, 1, 1, 1
        Label:
            text: 'Node hash: ' + root.target_hash
            font_size: sp(11)
            color: 0.7, 0.73, 0.77, 1
            size_hint_y: None
            height: dp(20)
            halign: 'center'
            text_size: self.width, None
            shorten: True
        Label:
            text: 'Delivery: ' + root.target_username
            font_size: sp(11)
            color: 0.7, 0.73, 0.77, 1
            size_hint_y: None
            height: dp(20)
            halign: 'center'
            text_size: self.width, None
            shorten: True
        BoxLayout:
            size_hint_y: None
            height: dp(50)
            spacing: dp(10)
            Button:
                text: 'Change Avatar'
                size_hint_x: None
                width: dp(140)
                background_color: 0.36, 0.41, 0.59, 1
                color: 1, 1, 1, 1
                on_press: app.open_file_chooser(avatar=True)
            Button:
                text: 'Save Username'
                size_hint_x: None
                width: dp(140)
                background_color: 0.36, 0.41, 0.59, 1
                color: 1, 1, 1, 1
                on_press: app.save_profile()
            Button:
                text: 'Back'
                size_hint_x: None
                width: dp(100)
                background_color: 0.5, 0.53, 0.59, 1
                color: 1, 1, 1, 1
                on_press: app.root.current = 'main'

<MainScreen>:
    BoxLayout:
        orientation: 'vertical'
        canvas.before:
            Color:
                rgba: 0.14, 0.16, 0.19, 1
            Rectangle:
                pos: self.pos
                size: self.size
        BoxLayout:
            orientation: 'horizontal'
            size_hint_y: 1

            # ---------------- left: channels (collapsable) ----------------
            BoxLayout:
                id: left_sidebar
                size_hint_x: None
                width: dp(220) if root.left_open else dp(0)
                orientation: 'vertical'
                canvas.before:
                    Color:
                        rgba: 0.18, 0.2, 0.23, 1
                    Rectangle:
                        pos: self.pos
                        size: self.size
                Image:
                    id: banner_channel
                    source: app.banner_channel_path or ''
                    size_hint_y: None
                    height: dp(160)
                    size_hint_x: None
                    width: dp(200)
                    allow_stretch: False
                    keep_ratio: True
                    pos_hint: {'center_x': 0.5}
                Button:
                    text: 'channels \u25bc' if root.left_open else 'channels \u25b6'
                    size_hint_y: None
                    height: dp(34)
                    background_color: 0.24, 0.26, 0.3, 1
                    color: 1, 1, 1, 0.9
                    on_press: root.toggle_left()
                ScrollView:
                    do_scroll_x: False
                    GridLayout:
                        id: channel_grid
                        cols: 1
                        size_hint_y: None
                        height: self.minimum_height
                        spacing: dp(2)
                        padding: [dp(6), dp(6)]

            # ---------------- center: chat ----------------
            BoxLayout:
                orientation: 'vertical'
                size_hint_x: 1
                canvas.before:
                    Color:
                        rgba: 0.21, 0.23, 0.27, 1
                    Rectangle:
                        pos: self.pos
                        size: self.size
                BoxLayout:
                    orientation: 'vertical'
                    size_hint_y: None
                    height: dp(40)
                    padding: [dp(10), 0]
                    Label:
                        id: channel_header
                        text: '#general'
                        color: 1, 1, 1, 1
                        bold: True
                        font_size: sp(16)
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.width, None
                ScrollView:
                    id: scroll
                    do_scroll_x: False
                    bar_width: dp(6)
                    size_hint_y: 1
                    GridLayout:
                        id: message_grid
                        cols: 1
                        size_hint_y: None
                        height: self.minimum_height
                        spacing: dp(2)
                        padding: [dp(6), dp(6)]
                BoxLayout:
                    size_hint_y: None
                    height: dp(56)
                    padding: [dp(8), dp(6)]
                    spacing: dp(8)
                    canvas.before:
                        Color:
                            rgba: 0.25, 0.27, 0.31, 1
                        Rectangle:
                            pos: self.pos
                            size: self.size
                    TextInput:
                        id: msg_input
                        hint_text: 'Message...'
                        multiline: False
                        background_color: 0.18, 0.2, 0.23, 1
                        foreground_color: 1, 1, 1, 1
                        cursor_color: 1, 1, 1, 1
                        on_text_validate: app.send_message()
                    Button:
                        id: send_btn
                        text: 'Send'
                        size_hint_x: None
                        width: dp(70)
                        background_color: 0.36, 0.41, 0.59, 1
                        color: 1, 1, 1, 1
                        on_press: app.send_message()
                    Button:
                        id: attach_btn
                        text: 'Attach'
                        size_hint_x: None
                        width: dp(70)
                        background_color: 0.45, 0.5, 0.59, 1
                        color: 1, 1, 1, 1
                        on_press: app.open_file_chooser(avatar=False)

            # ---------------- right: members (collapsable) ----------------
            BoxLayout:
                id: right_sidebar
                size_hint_x: None
                width: dp(200) if root.right_open else dp(0)
                orientation: 'vertical'
                canvas.before:
                    Color:
                        rgba: 0.18, 0.2, 0.23, 1
                    Rectangle:
                        pos: self.pos
                        size: self.size
                BoxLayout:
                    orientation: 'vertical'
                    size_hint_y: None
                    height: dp(40)
                    padding: [dp(8), 0]
                    Label:
                        text: 'Members'
                        color: 1, 1, 1, 1
                        bold: True
                        font_size: sp(14)
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.width, None
                Button:
                    text: 'members \u25bc' if root.right_open else 'members \u25b6'
                    size_hint_y: None
                    height: dp(34)
                    background_color: 0.24, 0.26, 0.3, 1
                    color: 1, 1, 1, 0.9
                    on_press: root.toggle_right()
                ScrollView:
                    do_scroll_x: False
                    GridLayout:
                        id: member_grid
                        cols: 1
                        size_hint_y: None
                        height: self.minimum_height
                        spacing: dp(2)
                        padding: [dp(6), dp(6)]
"""



# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
if KIVY:
    class ConnectScreen(Screen):
        pass

    class ProfileScreen(Screen):
        """Profile view: avatar, username, identity hashes, edit controls."""
        target_hash = StringProperty("")
        target_username = StringProperty("")
        target_avatar = StringProperty("")

    class MemberRow(BoxLayout):
        username = StringProperty("")
        avatar_source = StringProperty("")
        author_hash = StringProperty("")

    class MainScreen(Screen):
        left_open = BooleanProperty(True)
        right_open = BooleanProperty(True)
        _current_channel = StringProperty("general")

        def toggle_left(self):
            self.left_open = not self.left_open

        def toggle_right(self):
            self.right_open = not self.right_open


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
class sHadesClientApp(MDApp):
    title = "sHades"

    guild_api = ObjectProperty(None, allownone=True)
    reticulum = ObjectProperty(None, allownone=True)
    link = ObjectProperty(None, allownone=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Theme config - KivyMD 2.0.0 Material You color system
        # Use valid palette names: "Blue", "Purple", "Red", "Green", etc.
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Blue"
        self.theme_cls.accent_palette = "Orange"
        self.node_hash = None
        self.delivery_hash = None
        self.client_identity = None
        self.client_dest = None
        self.username = "user"
        self.my_hash = None
        self._poll_thread = None
        self._poll_stop = threading.Event()
        self._seq = 0
        self._last_ts = 0.0
        self._channels = OrderedDict()   # cid -> name
        self._members = {}               # member_hash -> username
        self._msg_widgets = {}           # msg_id -> MessageRow
        self._popup = None
        self._poll_error_shown = False
        self._ui_populated = False

        # Banner images - random selection from media folder on startup
        self.banner_config_path = None
        self.banner_channel_path = None
        self._select_random_banners()

        # Avatar pfp paths (set by _save_pfp_crop)
        self.my_avatar_orig = None
        self.my_avatar_small = None

    def _select_random_banners(self):
        """Pick random banner images from media/ folder on startup.
        One for the connect screen, one for the channel header."""
        media_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "media"
        )
        if not os.path.isdir(media_dir):
            return
        images = [
            f for f in os.listdir(media_dir)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            and f != "1793645765.png"   # client icon/logo — not a banner
        ]
        if not images:
            return
        if len(images) == 1:
            self.banner_config_path = os.path.join(media_dir, images[0])
            self.banner_channel_path = os.path.join(media_dir, images[0])
        else:
            chosen = random.sample(images, 2)
            self.banner_config_path = os.path.join(media_dir, chosen[0])
            self.banner_channel_path = os.path.join(media_dir, chosen[1])
            print("Banner images: config=%s channel=%s" % (
                os.path.basename(self.banner_config_path),
                os.path.basename(self.banner_channel_path),
            ))

    def _channel_cid(self, name):
        """Convert a channel name to its RNS destination hash ID,
        matching the guild's ensure_channel() formula."""
        return hexstr(RNS.Identity.full_hash(b"main_guild:" + name.encode()))

    # -- lifecycle -------------------------------------------------------
    def build(self):
        if Window:
            self.theme_cls.set_colors()
            Window.clearcolor = self.theme_cls.backgroundColor
        # Import KivyMD widgets so they're registered in Factory before KV loads
        from kivymd.uix.button import MDButton, MDButtonText, MDIconButton  # noqa: F401
        from kivymd.uix.textfield import MDTextField  # noqa: F401
        from kivymd.uix.appbar import MDTopAppBar  # noqa: F401
        from kivy.lang import Builder
        Builder.load_file("kv_md.kv")
        sm = ScreenManager()
        sm.add_widget(ConnectScreen(name="connect"))
        sm.add_widget(MainScreen(name="main"))
        sm.add_widget(ProfileScreen(name="profile"))
        # Plain Enter sends the message; Shift+Enter inserts a newline
        # (multiline input). Handled via _on_key_down below.
        Window.bind(on_key_down=self._on_key_down)
        return sm

    def on_start(self):
        # Try to resume a saved session
        sess = self._load_session()
        if sess:
            self.node_hash = sess.get("node_hash")
            self.delivery_hash = sess.get("delivery_hash")
            self.username = sess.get("username", "user")
            self.hub_address = sess.get("hub_address")
            self.hub_host = sess.get("hub_host")
            self.hub_port = sess.get("hub_port")
            self._open_main()
            Clock.schedule_once(lambda dt: self._init_reticulum(), 0.5)
            # Re-render persisted history (embeds survive close/re-open) once
            # the guild API is up; server-stored attachments re-fetch with a
            # loading placeholder until their bytes arrive.
            Clock.schedule_once(self._load_history_ui, 2.0)

    def _open_main(self):
        """Switch to the main chat screen."""
        try:
            self.root.current = "main"
            self._refresh_channels_ui()
        except Exception as e:
            print("open main error:", repr(e))

    def show_fullscreen_image(self, source, embed=None):
        """Show an image in full-screen overlay with dark background and download/close buttons.

        Tracks the overlay so it can be dismissed (minimized) later, and
        records which embed is expanded so it can be reset on minimize.
        """
        # Dismiss any existing overlay first
        if getattr(self, "_fullscreen_overlay", None) is not None:
            self.dismiss_fullscreen_image(None)
        overlay = FullScreenImageOverlay(source=source, embed=embed)
        self._fullscreen_overlay = overlay
        # Parent to Window so the overlay covers the entire display regardless
        # of which screen is current. Parenting to a Screen can leave the
        # overlay undersized (Screen may not propagate size to late-added
        # children), which made its buttons appear floating in the corner.
        Window.add_widget(overlay)

    def dismiss_fullscreen_image(self, embed):
        """Remove the full-screen overlay and reset the embed's expanded flag."""
        overlay = getattr(self, "_fullscreen_overlay", None)
        if overlay is not None:
            try:
                if overlay.parent:
                    overlay.parent.remove_widget(overlay)
            except Exception:
                pass
            self._fullscreen_overlay = None
        if embed is not None:
            embed.is_expanded = False

    # -- session ---------------------------------------------------------
    def _session_path(self):
        d = _app_data_dir()
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "session.json")

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
            os.replace(tmp, path)  # atomic on POSIX and Windows
        except Exception:
            pass

    def _load_session(self):
        try:
            with open(self._session_path()) as f:
                return json.load(f)
        except Exception:
            return None

    # -- message history persistence (so embeds survive close/re-open) -----
    def _history_path(self):
        return os.path.join(_app_data_dir(), "history.json")

    def _attachments_dir(self):
        d = os.path.join(_app_data_dir(), "attachments")
        os.makedirs(d, exist_ok=True)
        return d

    def _save_attachment_blob(self, blob, name):
        """Persist an ephemeral attachment blob to disk; return its local path."""
        d = self._attachments_dir()
        safe = "".join(c for c in name if c.isalnum() or c in "._-") or "attachment"
        fname = "%s_%s" % (hashlib.sha256(blob).hexdigest()[:12], safe)
        fpath = os.path.join(d, fname)
        try:
            with open(fpath, "wb") as f:
                f.write(blob)
        except Exception:
            return None
        return fpath

    def _add_to_history(self, m):
        if not hasattr(self, "_history"):
            self._history = []
        self._history = [x for x in self._history if x.get("msg_id") != m.get("msg_id")]
        if m.get("msg_id"):
            self._history.append(m)
        if len(self._history) > 500:
            self._history = self._history[-500:]

    def _save_history(self):
        try:
            hist = []
            for m in getattr(self, "_history", []):
                entry = dict(m)
                kept = []
                for att in entry.get("attachments") or []:
                    if isinstance(att, dict) and att.get("kind") == ATT_EPHEMERAL:
                        # blob already on disk; keep the local_path reference
                        lp = att.get("local_path")
                        if lp and os.path.exists(lp):
                            kept.append(att)
                    else:
                        kept.append(att)
                entry["attachments"] = kept
                hist.append(entry)
            with open(self._history_path(), "w") as f:
                json.dump(hist, f)
        except Exception as e:
            print("save history error:", e)

    def _load_history(self):
        try:
            with open(self._history_path()) as f:
                return json.load(f) or []
        except Exception:
            return []

    def _load_history_ui(self, dt=0):
        """Re-render persisted messages once the guild API is available."""
        if not getattr(self, "guild_api", None):
            Clock.schedule_once(self._load_history_ui, 1.0)
            return
        try:
            main = self.root.get_screen("main")
            grid = main.ids.message_grid
        except Exception:
            grid = None
        if grid is None:
            Clock.schedule_once(self._load_history_ui, 0.5)
            return
        for m in self._load_history():
            self._render_message(grid, m)
        Clock.schedule_once(lambda dt: self._scroll_to_bottom(), 0.1)

    # -- connect ----------------------------------------------------------
    def connect_to_guild(self, hub_address, username, node_hash="", delivery_hash=""):
        hub_address = (hub_address or "").strip()
        username = (username or "user").strip() or "user"
        node_hash = (node_hash or "").strip().lower()
        delivery_hash = (delivery_hash or "").strip().lower()

        if not hub_address:
            self._set_status("Hub address required (ip:port)")
            return

        # Parse ip:port
        if ":" not in hub_address:
            self._set_status("Hub address must be ip:port (e.g. 192.168.1.50:29221)")
            return
        host, port_str = hub_address.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            self._set_status("Invalid port number")
            return

        # Validate hashes if provided
        if node_hash and not re.fullmatch(r"[0-9a-f]{32}", node_hash):
            self._set_status("Invalid node_hash (need 32 hex)")
            return
        if delivery_hash and not re.fullmatch(r"[0-9a-f]{32}", delivery_hash):
            self._set_status("Invalid delivery_hash (need 32 hex)")
            return

        self.hub_address = hub_address
        self.hub_host = host
        self.hub_port = port
        self.username = username
        self.node_hash = node_hash or None
        self.delivery_hash = delivery_hash or None
        self._set_status("Connecting...")
        self._save_session()
        # RNS.Reticulum() installs signal handlers and must run on the main
        # thread. Schedule it there, then do blocking link work in a bg thread.
        # If RNS is already initialized (retry), skip initialization.
        if self.reticulum is not None:
            Clock.schedule_once(lambda dt: self._connect_after_rns(), 0)
        else:
            Clock.schedule_once(lambda dt: self._init_reticulum(), 0)

    def _init_reticulum(self):
        """Main thread only: create RNS instance (signal handlers), then
        spawn a background thread for the blocking link establishment."""
        try:
            storage = os.path.join(_app_data_dir(), "rns")
            os.makedirs(storage, exist_ok=True)

            # Write an RNS config that routes to the guild. If a hub address was
            # provided, use a TCPClientInterface to reach it directly;
            # otherwise use AutoInterface for LAN multicast discovery.
            cfgfile = os.path.join(storage, "config")
            if not os.path.exists(cfgfile) or self._config_needs_update(cfgfile):
                with open(cfgfile, "w") as f:
                    f.write("[reticulum]\n"
                            "  enable_transport = no\n"
                            "  share_instance = no\n\n"
                            "[interfaces]\n")
                    if self.hub_address:
                        f.write("  [[TCPClientInterface]]\n"
                                "    type = TCPClientInterface\n"
                                "    enabled = yes\n"
                                "    target_host = %s\n"
                                "    target_port = %d\n" % (self.hub_host, self.hub_port))
                    else:
                        f.write("  [[AutoInterface]]\n"
                                "    type = AutoInterface\n"
                                "    enabled = yes\n")

                    f.write("  [[TCPServerInterface]]\\n"
                            "    type = TCPServerInterface\\n"
                            "    enabled = yes\\n"
                            "    listen_ip = 0.0.0.0\\n"
                            "    listen_port = %d\\n" % 29222)

            reticulum = RNS.Reticulum(configdir=storage)
            self.reticulum = reticulum
            self.client_port = 29222
        except Exception as e:
            print("init reticulum error:", repr(e))
            self._set_status("connect failed: %s" % e)
            return
        threading.Thread(target=self._connect_after_rns, daemon=True).start()

    def _config_needs_update(self, cfgfile):
        """Check if the stored RNS config matches the current hub address."""
        try:
            with open(cfgfile) as f:
                content = f.read()
            if self.hub_address:
                return ("target_host = %s" % self.hub_host) not in content or \
                       ("target_port = %d" % self.hub_port) not in content
            return "AutoInterface" not in content
        except Exception:
            return True

    def _discover_node_hash(self, reticulum):
        """Listen for RNS announces from the hub's nomadnetwork.node destination.

        When a hub announces, its announce packet carries the destination hash
        and public key. Our announce handler captures the hash; RNS populates
        known_destinations from the announce body, so Identity.recall() then
        returns the peer identity.
        Returns the node_hash as hex string, or None on timeout.
        """
        discovered = {}

        class _NodeAnnounceHandler:
            aspect_filter = "nomadnetwork.node"

            def received_announce(self, destination_hash, announced_identity, app_data):
                if isinstance(destination_hash, bytes):
                    discovered["hash"] = destination_hash.hex()
                else:
                    discovered["hash"] = str(destination_hash)

        handler = _NodeAnnounceHandler()
        RNS.Transport.register_announce_handler(handler)

        try:
            # Let the TCPClientInterface establish its connection to the
            # community hub before listening for announces.
            time.sleep(2.0)
            # Guild re-announces every 10 seconds; wait up to 60 seconds
            # to hear one after connecting.
            for _ in range(600):  # up to 60 seconds
                if "hash" in discovered:
                    return discovered["hash"]
                time.sleep(0.1)
        finally:
            # Handler stays registered; harmless for subsequent announces.
            pass

        return None

    def _connect_after_rns(self):
        """Background thread: identity, destinations, link to guild node."""
        try:
            reticulum = self.reticulum

            # identity
            storage = os.path.join(_app_data_dir(), "rns")
            ipath = os.path.join(storage, "identity")
            if os.path.exists(ipath):
                self.client_identity = RNS.Identity.from_file(ipath)
                if self.client_identity is None:
                    # Corrupted or empty identity file — create a fresh one
                    self.client_identity = RNS.Identity()
                    self.client_identity.to_file(ipath)
            else:
                self.client_identity = RNS.Identity()
                self.client_identity.to_file(ipath)

            # client delivery destination (for identification + LXMF)
            self.client_dest = RNS.Destination(
                self.client_identity, RNS.Destination.OUT,
                RNS.Destination.SINGLE, "lxmf", "delivery")
            self.my_hash = hexstr(self.client_dest.hash)

            # Discover guild node_hash via RNS announce if not provided.
            # The hub announces nomadnetwork.node over the TCPClientInterface
            # we just connected through; we capture the hash from the announce.
            if not self.node_hash:
                self.node_hash = self._discover_node_hash(reticulum)
                if not self.node_hash:
                    raise RuntimeError(
                        "Could not discover guild node_hash from hub announces. "
                        "Ask the hub admin for its node_hash and try again.")
                print(f"Discovered guild node_hash via announce: {self.node_hash}")

            # Validate that RNS knows this identity (from hearing its announce)
            nh = bytes.fromhex(self.node_hash)
            nid = RNS.Identity.recall(nh)
            if nid is None:
                raise RuntimeError(
                    "RNS does not know identity for node_hash %s — "
                    "the guild's announce was not heard yet. Try again." % self.node_hash)
            print(f"RNS recalled identity for node_hash {self.node_hash}")

            # link to guild node — retry up to 3 times
            link = None
            for attempt in range(3):
                nd = RNS.Destination(nid, RNS.Destination.OUT,
                                     RNS.Destination.SINGLE, "nomadnetwork", "node")
                link = RNS.Link(nd)
                for _ in range(200):
                    if link.status == RNS.Link.ACTIVE:
                        break
                    time.sleep(0.1)
                if link.status == RNS.Link.ACTIVE:
                    break
                print(f"Link attempt {attempt + 1} not ACTIVE, retrying...")
                time.sleep(1.0)

            if link.status != RNS.Link.ACTIVE:
                raise RuntimeError(
                    "Could not establish RNS link to guild node — "
                    "link never went ACTIVE after 3 attempts.")
            link.identify(self.client_identity)
            self.link = link

            self.guild_api = GuildAPI(reticulum, link)
            self._save_session()

            # Create this client's own nomadnetwork.node destination
            # for serving profile pages to other clients.
            self.node_destination = RNS.Destination(
                self.client_identity, RNS.Destination.IN,
                RNS.Destination.SINGLE, "nomadnetwork", "node")
            self.node_destination.register_request_handler(
                "/page/profile", self.page_profile_handler,
                allow=RNS.Destination.ALLOW_ALL)
            self.node_destination.register_request_handler(
                "/file", self._file_handler,
                allow=RNS.Destination.ALLOW_ALL)
            self.node_hash = hexstr(self.node_destination.hash)
            print(f"My node_hash: {self.node_hash}")

            # Register with guild so others can find me via ip:port
            try:
                import socket
                my_host = socket.gethostbyname(socket.gethostname())
                self.guild_api.register_node(
                    self.node_hash, my_host, self.client_port,
                    hexstr(self.client_identity.hash))
                print(f"Registered with guild: {my_host}:{self.client_port}")
            except Exception as e:
                print(f"Node registration failed: {e}")

            Clock.schedule_once(lambda dt: self._switch_main(), 0)
            Clock.schedule_once(lambda dt: self._start_poll(), 0)
        except Exception as e:
            print("connect guild error:", repr(e))
            err_msg = "connect failed: %s" % e
            Clock.schedule_once(
                lambda dt: self._set_status(err_msg), 0)

    def _switch_main(self):
        self.root.current = "main"
        # load channels (placeholder: general + anything discovered)
        self._refresh_channels_ui()

    def _start_poll(self):
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            try:
                main = self.root.get_screen("main")
                cid = getattr(main, "_current_channel", "general")
                st = self.guild_api.state(self._channel_cid(cid))
                if st.get("ok"):
                    self._poll_error_shown = False
                    seq = st.get("seq", 0)
                    last_ts = st.get("last_ts", 0.0)
                    msg_count = len(st.get("messages", []))
                    print("poll state ok: seq=%s last_ts=%s msgs=%s" % (seq, last_ts, msg_count))
                    if seq != self._seq or last_ts != self._last_ts:
                        self._seq = seq
                        self._last_ts = last_ts
                        self._fetch_messages(cid)
                        self._refresh_members_ui(self._channel_cid(cid))
                    else:
                        # no change, but make sure UI is populated on first poll
                        if not getattr(self, "_ui_populated", False):
                            self._ui_populated = True
                            self._fetch_messages(cid)
                            self._refresh_members_ui(self._channel_cid(cid))
                else:
                    err = st.get("error", "unknown")
                    self._poll_error_shown = True
                    Clock.schedule_once(
                        lambda dt: self._set_status("poll failed: %s" % err), 0)
                    print("poll state failed: %s" % err)
                Clock.schedule_once(lambda dt: self._ensure_general_channel(), 0)
            except Exception as exc:
                print("poll exception: %s" % repr(exc))
                self._poll_error_shown = True
                err_msg = "poll exception: %s" % exc
                Clock.schedule_once(
                    lambda dt: self._set_status(err_msg), 0)
            self._poll_stop.wait(3.0)

    def _fetch_messages(self, channel, since_id=None, since_ts=None):
        # Network call must not block the main thread — run it in background,
        # schedule UI updates via Clock.schedule_once.
        def work():
            cid = self._channel_cid(channel)
            res = self.guild_api.messages(cid, since_id=since_id, since_ts=since_ts, limit=100)
            if not res.get("ok"):
                err = res.get("error", "unknown")
                Clock.schedule_once(
                    lambda dt: self._set_status("fetch failed: %s" % err), 0)
                return
            msgs = res.get("messages", []) or []
            updates = res.get("updates", []) or []

            def apply(dt):
                try:
                    main = self.root.get_screen("main")
                    grid = main.ids.message_grid
                    for m in msgs:
                        self._render_message(grid, m)
                    Clock.schedule_once(lambda dt: self._scroll_to_bottom(), 0.1)
                except Exception as e:
                    print("render messages error:", e)
            Clock.schedule_once(apply, 0)

            for u in updates:
                self._apply_update(u)
        threading.Thread(target=work, daemon=True).start()

    def _render_message(self, grid, m):
        msg_id = m.get("msg_id")
        if msg_id in self._msg_widgets:
            return
        author = m.get("author_hash", "")
        uname = m.get("username") or ("user-" + author[:6])
        self._record_member(author, uname)
        content = m.get("content") or ""
        ts = time.strftime("%H:%M", time.localtime(m.get("ts", 0)))
        is_own = (author == self.my_hash)

        # avatar
        avatar_source = ""
        if is_own and getattr(self, "my_avatar_small", None):
            # Use our own saved circular pfp immediately
            avatar_source = self.my_avatar_small
        else:
            key = "av:" + author
            path = IMAGE_CACHE.get(key)
            if path:
                avatar_source = path
            elif key not in IMAGE_CACHE._loading:
                # fetch avatar — deduplicate in-flight requests
                def _do_av(a=author, k=key):
                    res = self.guild_api.avatar_get(a)
                    if res.get("ok") and res.get("avatar"):
                        IMAGE_CACHE.set_bytes(k, res["avatar"], "png")
                        Clock.schedule_once(lambda dt: self._refresh_avatar(k), 0)
                threading.Thread(target=_do_av, daemon=True).start()

        row = MessageRow(
            author_hash=author,
            username=uname,
            ts=ts,
            content=content,
            content_markup=md_to_kivy(content),
            avatar_source=avatar_source,
            msg_id=msg_id,
            is_own=is_own,
        )
        self._msg_widgets[msg_id] = row
        grid.add_widget(row)

        # embeds for URLs in content
        for url in find_urls(content):
            ext = os.path.splitext(url.split("?")[0])[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                emb = MessageEmbed(source=url, size_hint_y=None, height=dp(200))
                row.ids.embed_container.add_widget(emb)
            elif ext in (".mp3", ".ogg", ".wav", ".flac"):
                row.ids.embed_container.add_widget(
                    Label(
                        text="[audio] " + url,
                        size_hint_y=None,
                        height=dp(20),
                        color=(0.7, 0.73, 0.77, 1),
                        font_size=sp(12),
                    )
                )
            elif ext in (".mp4", ".webm"):
                row.ids.embed_container.add_widget(
                    Label(
                        text="[video] " + url,
                        size_hint_y=None,
                        height=dp(20),
                        color=(0.7, 0.73, 0.77, 1),
                        font_size=sp(12),
                    )
                )

        # attachment embeds (files uploaded with the message)
        for att in m.get("attachments") or []:
            if isinstance(att, dict):
                kind = att.get("kind")
                if kind == ATT_SERVER:
                    # Server-stored: re-fetch bytes on demand (loading
                    # placeholder until received) — this is what survives a
                    # restart for server-hosted files.
                    self._fetch_attachment(row, m.get("msg_id"), att)
                    continue
                if kind == ATT_EPHEMERAL:
                    # Ephemeral (P2P): bytes are kept in a local file; read
                    # them back and render immediately.
                    local_path = att.get("local_path") or ""
                    if not local_path:
                        continue
                    try:
                        with open(local_path, "rb") as f:
                            att_blob = f.read()
                    except Exception:
                        continue
                    self._add_attachment_embed(
                        row, att.get("name") or "attachment", att_blob)
                    continue
                # Legacy server dict without a kind field: has a path to fetch.
                if "path" in att:
                    self._fetch_attachment(row, m.get("msg_id"), att)
                    continue
            try:
                att_name = att[0]
                att_blob = att[2]
            except Exception:
                continue
            if isinstance(att_blob, str):
                import base64
                att_blob = base64.b64decode(att_blob)
            self._add_attachment_embed(row, att_name, att_blob)

    def _add_attachment_embed(self, row, name, blob):
        """Render one attachment: images inline, everything else as a label."""
        print(f"[TRACE] _add_attachment_embed: name={name} blob_size={len(blob)} ext={os.path.splitext(name)[1].lower()}", flush=True)
        dlog(f"_add_attachment_embed: name={name} blob_size={len(blob)} ext={os.path.splitext(name)[1].lower()}")
        ext = os.path.splitext(name)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
            tmp.write(blob)
            tmp.close()
            emb = MessageEmbed(source=tmp.name, size_hint_y=None, height=dp(200))
            row.ids.embed_container.add_widget(emb)
        elif ext in (".mp3", ".ogg", ".wav", ".flac"):
            row.ids.embed_container.add_widget(
                Label(
                    text="[audio] " + name,
                    size_hint_y=None,
                    height=dp(20),
                    color=(0.7, 0.73, 0.77, 1),
                    font_size=sp(12),
                )
            )
        elif ext in (".mp4", ".webm"):
            row.ids.embed_container.add_widget(
                Label(
                    text="[video] " + name,
                    size_hint_y=None,
                    height=dp(20),
                    color=(0.7, 0.73, 0.77, 1),
                    font_size=sp(12),
                )
            )
        else:
            row.ids.embed_container.add_widget(
                Label(
                    text="[file] " + name,
                    size_hint_y=None,
                    height=dp(20),
                    color=(0.7, 0.73, 0.77, 1),
                    font_size=sp(12),
                )
            )

    def _fetch_attachment(self, row, msg_id, att):
        """Download an attachment blob from the guild and render it.

        The guild only returns metadata; the bytes are served by its
        /file/attachments/... handler. Download off the main thread and add
        the embed back on the UI thread. A loading placeholder is shown until
        the image has been received.
        """
        print(f"[TRACE] _fetch_attachment: msg_id={msg_id} name={att.get('name')!r} path={att.get('path')!r}", flush=True)
        dlog(f"_fetch_attachment: msg_id={msg_id} name={att.get('name')!r} path={att.get('path')!r}")
        name = att.get("name") or ""
        path = att.get("path") or ""
        if not path:
            print(f"[TRACE] _fetch_attachment: no path, skipping", flush=True)
            dlog(f"_fetch_attachment: no path, skipping")
            return

        self._add_loading_placeholder(row)

        def work():
            print(f"[TRACE] _fetch_attachment.work: fetching {path}", flush=True)
            dlog(f"_fetch_attachment.work: fetching {path}")
            try:
                blob = self.guild_api.fetch_file(path)
            except Exception:
                blob = None
            if not blob:
                print(f"[TRACE] _fetch_attachment.work: fetch returned empty for {path}", flush=True)
                dlog(f"_fetch_attachment.work: fetch returned empty for {path}")
                Clock.schedule_once(
                    lambda dt: self._remove_placeholder(row), 0)
                return
            print(f"[TRACE] _fetch_attachment.work: received {len(blob)} bytes for {path}", flush=True)
            dlog(f"_fetch_attachment.work: received {len(blob)} bytes for {path}")

            def apply(dt):
                try:
                    self._remove_placeholder(row)
                    main = self.root.get_screen("main")
                    # channel may have switched while we were downloading
                    if main.ids.message_grid is not row.parent:
                        return
                    if msg_id and msg_id not in self._msg_widgets:
                        return
                    print(f"[TRACE] _fetch_attachment.apply: adding embed for {name}", flush=True)
                    dlog(f"_fetch_attachment.apply: adding embed for {name}")
                    self._add_attachment_embed(row, name, blob)
                except Exception as e:
                    print("attachment render error:", e)

            Clock.schedule_once(apply, 0)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_avatar(self, key):
        # re-render rows that use this avatar
        path = IMAGE_CACHE.get(key)
        if not path:
            return
        author = key[3:]  # strip "av:" prefix
        for row in self._msg_widgets.values():
            if row.author_hash == author:
                row.avatar_source = ""   # force Kivy to re-bind source
                row.avatar_source = path

    def _apply_update(self, u):
        # edit / delete delta
        msg_id = u.get("msg_id")
        content = u.get("content", "")
        deleted = bool(u.get("deleted_at"))
        # find widget
        row = self._msg_widgets.get(msg_id)
        if not row:
            return
        Clock.schedule_once(lambda dt: self._do_update(row, content, deleted), 0)

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
            # Rebuild embeds to match the updated content (an edit may remove
            # an image URL that a previous embed widget is still showing).
            container = row.ids.get("embed_container")
            if container:
                container.clear_widgets()
            for url in find_urls(content):
                ext = os.path.splitext(url.split("?")[0])[1].lower()
                if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    emb = MessageEmbed(source=url, size_hint_y=None, height=dp(200))
                    container.add_widget(emb)

    def _scroll_to_bottom(self):
        try:
            sv = self.root.get_screen("main").ids.scroll
            sv.scroll_y = 0
        except Exception:
            pass

    # -- channels / members ---------------------------------------------
    def _ensure_general_channel(self):
        # only add once
        if not hasattr(self, "_channels_added"):
            self._channels_added = True
            for name in ("general", "announcements", "random"):
                self._add_channel(name)

    def _add_channel(self, name):
        main = self.root.get_screen("main")
        grid = main.ids.channel_grid
        btn = ChannelButton(channel_name=name)
        btn.bind(on_press=lambda inst: self._switch_channel(name))
        grid.add_widget(btn)

    def _refresh_channels_ui(self):
        self._ensure_general_channel()

    def _switch_channel(self, name):
        main = self.root.get_screen("main")
        main._current_channel = name
        # Keep the channel header in sync with the active channel.
        try:
            main.ids.channel_header.text = "#" + name
        except Exception:
            pass
        grid = main.ids.message_grid
        grid.clear_widgets()
        self._msg_widgets.clear()
        self._seq = 0
        self._last_ts = 0.0
        self._fetch_messages(name)
        self._refresh_members_ui(name)

    def _refresh_members_ui(self, channel):
        # Widget access must happen on the main thread.
        Clock.schedule_once(lambda dt: self._refresh_members_ui_main(channel), 0)

    def _refresh_members_ui_main(self, channel):
        try:
            main = self.root.get_screen("main")
            grid = main.ids.member_grid
            # clear & repopulate from current members set
            grid.clear_widgets()
            # show self + any seen authors
            for h, uname in list(self._members.items())[:50]:
                row = MemberRow(username=uname, avatar_source="")
                grid.add_widget(row)
        except Exception as e:
            print("members ui error:", e)

    def _record_member(self, author_hash, username):
        if author_hash and username:
            self._members[author_hash] = username

    # -- input -----------------------------------------------------------
    def send_message(self):
        main = self.root.get_screen("main")
        inp = main.ids.msg_input
        text = (inp.text or "").strip()
        if not self.guild_api:
            return
        # Allow attachment-only messages (no text) when a file is pending.
        if not text and not getattr(self, "_pending_attachment", None):
            return
        cid = getattr(main, "_current_channel", "general")
        cid = self._channel_cid(cid)
        inp.text = ""

        def work():
            res = self.guild_api.post(cid, text,
                                      attachments=[self._pending_attachment]
                                      if getattr(self, "_pending_attachment",
                                                 None) else None)
            if res.get("ok"):
                # optimistically show
                msg_id = res.get("msg_id")
                m = {"msg_id": msg_id, "author_hash": self.my_hash,
                     "username": self.username, "content": text,
                     "ts": now(), "channel_id": cid,
                     # one attachment = [name, mime, blob]; the list holds
                     # those triples, matching the guild's ingest format
                     "attachments": [self._pending_attachment]
                     if getattr(self, "_pending_attachment", None) else None}
                Clock.schedule_once(lambda dt: self._render_message(
                    main.ids.message_grid, m), 0)
                self._pending_attachment = None
            else:
                err = res.get("error", "unknown")
                Clock.schedule_once(
                    lambda dt: self._set_status("send failed: %s" % err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _on_key_down(self, window, key, scancode, codepoint, modifier):
        """Keyboard hook for the message input and connect screen.

        Plain Enter sends the message; Shift+Enter inserts a newline
        (the multiline input handles the newline itself). Tab moves focus
        to the next input field on the connect screen. Returns True to
        claim the key when we handled it.
        """
        # Tab focus traversal on the connect screen
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

        main = self.root.get_screen("main") if self.root else None
        inp = main.ids.msg_input if main else None
        if inp and getattr(inp, "focus", False) and key in (13, 327):
            if "shift" not in (modifier or []):
                self.send_message()
                return True
        return None

    # -- attachments: server-stored vs ephemeral (P2P) -------------------
    def _add_loading_placeholder(self, row):
        """Show a loading placeholder in the row's embed container."""
        print(f"[TRACE] _add_loading_placeholder: row={row.msg_id[:12] if row.msg_id else '?'}", flush=True)
        dlog(f"_add_loading_placeholder: row={row.msg_id[:12] if row.msg_id else '?'}")
        lbl = Label(
            text=ATT_PLACEHOLDER_TEXT,
            size_hint_y=None,
            height=dp(200),
            color=(0.7, 0.73, 0.77, 1),
            font_size=sp(12),
        )
        lbl._is_loading_placeholder = True
        row.ids.embed_container.add_widget(lbl)
        row._loading_placeholder = lbl

    def _remove_placeholder(self, row):
        """Remove the loading placeholder from the row's embed container."""
        lbl = getattr(row, "_loading_placeholder", None)
        if lbl is not None:
            print(f"[TRACE] _remove_placeholder: row={row.msg_id[:12] if row.msg_id else '?'}", flush=True)
            dlog(f"_remove_placeholder: row={row.msg_id[:12] if row.msg_id else '?'}")
            try:
                row.ids.embed_container.remove_widget(lbl)
            except Exception:
                pass
            row._loading_placeholder = None

    def open_attach_dialog(self):
        """Open a window to pick a file and its storage style; OK auto-sends.

        Storage styles:
          * server-stored  - guild keeps the file; client re-fetches bytes on
                             restart (loading placeholder until received).
          * ephemeral (P2P) - bytes sent inline; kept locally so they survive
                             restart, but no server copy exists.
        """
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.button import Button
        from kivy.uix.popup import Popup

        main = self.root.get_screen("main")
        text = (main.ids.msg_input.text or "").strip()

        chooser = FileChooserListView(
            filters=["*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp",
                     "*.mp4", "*.mp3", ".pdf", ".txt"]
        )
        style_box = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        style_box.add_widget(
            Label(text="Store:", size_hint_x=None, width=dp(50),
                  color=(1, 1, 1, 1), font_size=sp(12),
                  halign="left", valign="middle"))
        btn_server = Button(text="Server-stored", size_hint_x=None, width=dp(130))
        btn_ephem = Button(text="Ephemeral (P2P)", size_hint_x=None, width=dp(130))
        selected = {"style": ATT_SERVER}

        def pick(s):
            selected["style"] = s
            btn_server.background_color = (0.42, 0.48, 1.0, 1) if s == ATT_SERVER else (0.25, 0.27, 0.31, 1)
            btn_ephem.background_color = (0.42, 0.48, 1.0, 1) if s == ATT_EPHEMERAL else (0.25, 0.27, 0.31, 1)

        btn_server.bind(on_press=lambda *_: pick(ATT_SERVER))
        btn_ephem.bind(on_press=lambda *_: pick(ATT_EPHEMERAL))
        pick(ATT_SERVER)
        style_box.add_widget(btn_server)
        style_box.add_widget(btn_ephem)

        btn_ok = Button(text="OK — Send", size_hint_y=None, height=dp(44),
                        background_color=(0.42, 0.48, 1.0, 1), color=(1, 1, 1, 1))
        btn_cancel = Button(text="Cancel", size_hint_y=None, height=dp(44),
                            background_color=(0.25, 0.27, 0.31, 1), color=(1, 1, 1, 1))
        btn_box = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        btn_box.add_widget(btn_ok)
        btn_box.add_widget(btn_cancel)

        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        content.add_widget(Label(text="Attach a file — choose how it is stored:",
                                 size_hint_y=None, height=dp(24),
                                 color=(1, 1, 1, 1), font_size=sp(12)))
        content.add_widget(chooser)
        content.add_widget(style_box)
        content.add_widget(btn_box)

        popup = Popup(title="Attach file", content=content, size_hint=(0.9, 0.9),
                      auto_dismiss=False)

        def do_ok(*_):
            sel = chooser.selection
            if not sel:
                self._set_status("No file selected")
                return
            path = sel[0]
            popup.dismiss()
            self._send_attached_file(text, path, selected["style"])

        btn_ok.bind(on_press=do_ok)
        btn_cancel.bind(on_press=lambda *_: popup.dismiss())
        popup.open()

    def _send_attached_file(self, text, path, style):
        """Read the chosen file and send it as an attachment of the given style."""
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except Exception as e:
            self._set_status("attach failed: %s" % e)
            return
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        name = path.split("/")[-1]
        # Client-side size check so the user gets a clear message instead of
        # a silent server rejection. Matches server MAX_ATTACHMENT_SZ (32 MB).
        if len(blob) > 32 * 1024 * 1024:
            self._set_status("attach failed: file too large (%d MB, max 32 MB)" % (len(blob) / 1024 / 1024))
            return
        self._send_with_attachment(text, name, mime, blob, style)

    def _send_with_attachment(self, text, name, mime, blob, style):
        """Post a message with one attachment and optimistically render it.

        The wire format is always a [name, mime, blob] triple; the chosen
        style only affects how the client persists/re-fetches the attachment.
        """
        print(f"[TRACE] _send_with_attachment: name={name} mime={mime} size={len(blob)} style={style} text_len={len(text or '')}", flush=True)
        dlog(f"_send_with_attachment: name={name} mime={mime} size={len(blob)} style={style} text_len={len(text or '')}")
        if not self.guild_api:
            self._set_status("Not connected to guild")
            return
        main = self.root.get_screen("main")
        cid = getattr(main, "_current_channel", "general")
        cid = self._channel_cid(cid)

        def work():
            print(f"[TRACE] _send_with_attachment.work: posting to channel={cid}", flush=True)
            dlog(f"_send_with_attachment.work: posting to channel={cid}")
            res = self.guild_api.post(cid, text, attachments=[[name, mime, blob]])
            print(f"[TRACE] _send_with_attachment.work: post response ok={res.get('ok')} msg_id={res.get('msg_id')} attachment={res.get('attachment')}", flush=True)
            dlog(f"_send_with_attachment.work: post response ok={res.get('ok')} msg_id={res.get('msg_id')} attachment={res.get('attachment')}")
            if not res.get("ok"):
                Clock.schedule_once(
                    lambda dt: self._set_status("send failed: %s" % res.get("error", "unknown")), 0)
                return
            msg_id = res.get("msg_id")
            # The guild may ack with stored metadata (path) => server-stored.
            srv_att = res.get("attachment") or {}
            srv_path = srv_att.get("path")
            if style == ATT_SERVER and srv_path:
                dlog(f"_send_with_attachment.work: server-stored path={srv_path}")
                att = {"name": name, "mime": mime, "size": len(blob),
                       "kind": ATT_SERVER, "path": srv_path}
            else:
                # Ephemeral (or server did not ack a path): keep blob locally.
                dlog(f"_send_with_attachment.work: ephemeral style={style} srv_path={srv_path}")
                local_path = self._save_attachment_blob(blob, name)
                att = {"name": name, "mime": mime, "kind": ATT_EPHEMERAL,
                       "local_path": local_path}
            m = {"msg_id": msg_id, "author_hash": self.my_hash,
                 "username": self.username, "content": text, "ts": now(),
                 "channel_id": cid, "attachments": [att]}
            Clock.schedule_once(lambda dt: self._render_message(
                main.ids.message_grid, m), 0)
            Clock.schedule_once(
                lambda dt: setattr(main.ids.msg_input, "text", ""), 0)
            self._add_to_history(m)
            self._save_history()

        threading.Thread(target=work, daemon=True).start()

    # -- profile ----------------------------------------------------------
    def open_profile(self, target_hash, username):
        """Open the profile screen for a user (self or another member).
        Connects to the remote client's node, fetches their profile page
        via RNS link, and renders it in a popup."""
        if not self.guild_api:
            return

        # If no target_hash, show own profile locally
        if not target_hash or target_hash == self.my_hash:
            self._show_own_profile()
            return

        # Look up node's address via guild
        try:
            res = self.guild_api.lookup_node(target_hash)
            if not res.get("ok"):
                self._set_status("Node not found: %s" % res.get("error", "unknown"))
                return
            address = res.get("address")
            port = res.get("port")
            identity_hash = res.get("identity_hash")
        except Exception as e:
            self._set_status("Lookup failed: %s" % e)
            return

        if not address or not port:
            self._set_status("No address for node: %s" % target_hash)
            return

        # Connect to remote node via RNS.Link and request profile page
        try:
            nh = bytes.fromhex(target_hash)
            nid = RNS.Identity.recall(nh)
            if nid is None:
                # Try with identity_hash from lookup
                nid = RNS.Identity(bytes.fromhex(identity_hash)) if identity_hash else None
            if nid is None:
                self._set_status("RNS does not know identity for %s" % target_hash)
                return
            nd = RNS.Destination(nid, RNS.Destination.OUT,
                                 RNS.Destination.SINGLE, "nomadnetwork", "node")
            link = RNS.Link(nd)
            for _ in range(200):
                if link.status == RNS.Link.ACTIVE:
                    break
                time.sleep(0.1)
            if link.status != RNS.Link.ACTIVE:
                self._set_status("Link to %s never active" % target_hash[:12])
                return

            # Request profile page
            req_path = "/page/profile/%s" % target_hash
            receipt = link.request(req_path, data={})
            if isinstance(receipt, bool):
                if not receipt:
                    self._set_status("Profile request failed")
                    return
                receipt = link.request(req_path, data={})
            deadline = time.time() + 10
            while (hasattr(receipt, "response")
                   and receipt.response is None
                   and time.time() < deadline):
                time.sleep(0.1)
            if not hasattr(receipt, "response") or receipt.response is None:
                self._set_status("Profile request timed out")
                return

            profile_text = receipt.response.decode("utf-8", errors="replace")

            # Fetch any images referenced in the profile page (![alt](/file/name))
            image_paths = {}  # filename -> local temp path
            import re as _re
            for m in _re.finditer(r"!\[([^\]]*)\]\(/file/([^)]+)\)", profile_text):
                filename = m.group(2)
                try:
                    file_req_path = "/file/%s" % filename
                    file_receipt = link.request(file_req_path, data={})
                    if isinstance(file_receipt, bool):
                        if not file_receipt:
                            continue
                        file_receipt = link.request(file_req_path, data={})
                    deadline = time.time() + 10
                    while (hasattr(file_receipt, "response")
                           and file_receipt.response is None
                           and time.time() < deadline):
                        time.sleep(0.1)
                    if hasattr(file_receipt, "response") and file_receipt.response is not None:
                        # Save to temp file
                        import tempfile as _tempfile
                        tmp = _tempfile.NamedTemporaryFile(
                            suffix=".png", delete=False, dir=_app_data_dir())
                        tmp.write(file_receipt.response)
                        tmp.close()
                        image_paths[filename] = tmp.name
                except Exception:
                    pass

            self._show_profile_popup(target_hash, username, profile_text, image_paths)

        except Exception as e:
            print("Profile fetch error:", repr(e))
            self._set_status("Profile fetch failed: %s" % e)

    def _show_own_profile(self):
        """Show own profile locally."""
        try:
            self.profile_screen = self.root.get_screen("profile")
        except Exception:
            self._set_status("Profile screen not available")
            return
        self.profile_screen.target_hash = self.my_hash or ""
        self.profile_screen.target_username = self.username or "user"
        # Show saved circular pfp if we have one, otherwise blank
        self.profile_screen.target_avatar = getattr(
            self, "my_avatar_small", ""
        ) or ""
        try:
            self.root.current = "profile"
        except Exception:
            self._set_status("Could not switch to profile screen")

    def _show_profile_popup(self, target_hash, username, profile_text,
                            image_paths=None):
        """Show a remote user's profile page in a popup.
        Displays any images referenced in the profile page
        (![alt](/file/name)) fetched from the remote node.
        """
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.image import Image as KivyImage

        # Remove image references from markdown (images shown separately)
        import re as _re
        clean_text = _re.sub(
            r"!\[([^\]]*)\]\(/file/[^)]+\)", r"\1", profile_text)

        # Convert micron/markdown to Kivy markup
        markup_text = md_to_kivy(clean_text)

        content = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(10))

        # Show fetched images at the top
        if image_paths:
            for filename, local_path in image_paths.items():
                img = KivyImage(
                    source=local_path,
                    allow_stretch=False,
                    keep_ratio=True,
                    size_hint_y=None,
                    height=dp(200),
                )
                content.add_widget(img)

        scroll = ScrollView()
        label = Label(text=markup_text, markup=True, size_hint_y=None,
                      halign="left", valign="top", text_size=(None, None))
        label.bind(texture_size=lambda *args: label.set_size(label.texture_size))
        scroll.add_widget(label)
        content.add_widget(scroll)

        close_btn = Button(text="Close", size_hint_y=None, height=dp(40),
                           background_color=(0.5, 0.53, 0.59, 1),
                           color=(1, 1, 1, 1))
        content.add_widget(close_btn)

        popup = Popup(title="Profile: %s" % (username or target_hash[:12]),
                      content=content, size_hint=(0.8, 0.8),
                      auto_dismiss=False)
        close_btn.bind(on_press=popup.dismiss)
        popup.open()

    def page_profile_handler(self, path, data, request_id, link_id,
                             remote_identity, requested_at):
        """Serve profile page for this client's node.
        Called by remote clients via RNS.Link.request().
        Returns micron/markdown profile page."""
        # Extract target hash from path like /page/profile/<node_hash>
        parts = path.split("/")
        target_hash = parts[-1] if len(parts) > 2 else ""

        if target_hash != self.node_hash:
            return b"Profile not found"

        # Build micron/markdown profile page
        lines = [
            "# %s" % (self.username or "user"),
            "",
            "Node: %s" % self.node_hash,
            "Delivery: %s" % (self.delivery_hash or ""),
            "",
        ]
        # Reference the original pfp image served from this node's /file handler
        orig_name = getattr(self, "my_avatar_orig", None)
        if orig_name:
            lines.append("![avatar](/file/%s)" % os.path.basename(orig_name))
            lines.append("")
        lines.append("Reticulum-native community hub client")
        lines.append("")
        return ("\n".join(lines)).encode("utf-8")

    def _file_handler(self, path, data, request_id, link_id,
                      remote_identity, requested_at):
        """Serve image files from this client's pfps directory.
        Called by remote clients via RNS.Link.request("/file/<filename>").
        Returns raw image bytes.
        """
        parts = path.split("/")
        filename = parts[-1] if len(parts) > 1 else ""
        if not filename:
            return b"Not found"

        app_dir = _app_data_dir()
        pfp_dir = os.path.join(app_dir, "pfps")
        file_path = os.path.join(pfp_dir, filename)

        # Only serve files from the pfps directory (path traversal guard)
        if not os.path.isfile(file_path):
            return b"Not found"

        with open(file_path, "rb") as f:
            return f.read()

    def open_file_chooser(self, avatar=False):
        """Open a file chooser popup to attach a file or select an avatar."""
        self._avatar_mode = avatar
        chooser = FileChooserListView(filters=["*.png", "*.jpg", "*.jpeg",
                                               "*.gif", "*.webp", "*.mp4",
                                               "*.mp3", "*.pdf", "*.txt"])
        popup = Popup(title="Select file", content=chooser,
                      size_hint=(0.9, 0.9))
        chooser.bind(on_submit=lambda inst, selection, *args:
                     self._on_file_selected(selection, popup))
        popup.open()

    def _on_file_selected(self, selection, popup):
        popup.dismiss()
        if not selection:
            return
        path = selection[0]
        if getattr(self, "_avatar_mode", False):
            self._open_pfp_editor(path)
        else:
            self.attach_file(path)

    def _open_pfp_editor(self, path):
        """Open round-mask PFP picker popup — drag circle to position, drag outside to resize.

        Save produces: 256x256 PNG from circle area (chat/members) + original (profile top).

        """
        from kivy.uix.popup import Popup
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button

        editor = PFPEditorWidget(source=path)

        btn_box = BoxLayout(
            size_hint_y=None,
            height=dp(44),
            spacing=dp(10),
        )
        save_btn = Button(text="Save PFP", background_color=(0.36, 0.41, 0.59, 1),
                          color=(1, 1, 1, 1))
        cancel_btn = Button(text="Cancel", background_color=(0.5, 0.53, 0.59, 1),
                            color=(1, 1, 1, 1))
        btn_box.add_widget(save_btn)
        btn_box.add_widget(cancel_btn)

        content = BoxLayout(orientation='vertical', spacing=dp(10), padding=dp(10))
        content.add_widget(editor)
        content.add_widget(btn_box)

        popup = Popup(
            title="Select PFP area — drag circle to move, drag edge to resize",
            content=content,
            size_hint=(0.85, 0.85),
            auto_dismiss=False,
        )

        def do_save(*_):
            # Capture all values BEFORE dismissing popup — once dismissed,
            # the FloatLayout tears down and editor.width/height/img.pos/
            # img.size may become (0, 0) or stale, producing wrong crops.
            #
            # crop_cx/crop_cy/crop_r are the circle in ABSOLUTE window pixels
            # (the exact values the overlay ellipse is drawn with) and
            # img.pos/img.size are the image widget's ABSOLUTE rect — the two
            # live in the same coordinate space, so the crop maths below can
            # map one onto the other directly.
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

        def do_cancel(*_):
            popup.dismiss()

        save_btn.bind(on_press=do_save)
        cancel_btn.bind(on_press=do_cancel)
        popup.open()

    def _save_pfp_crop(self, path, crop_cx=0, crop_cy=0, crop_r=0,
                       image_pos=None, image_size=None):
        """Save the cropped avatar without ever letting it kill the client.

        Any failure in the image pipeline (unreadable file, unwritable
        directory, unsupported format) is caught, logged with a traceback and
        surfaced in the status line. An unhandled exception here used to
        propagate out of the Clock callback, tear down the event loop and
        terminate the whole client with "cannot write mode RGBA as JPEG"
        style errors.
        """
        try:
            self._save_pfp_crop_impl(path, crop_cx, crop_cy, crop_r,
                                     image_pos, image_size)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._set_status("avatar save failed: %s" % e)

    def _save_pfp_crop_impl(self, path, crop_cx=0, crop_cy=0, crop_r=0,
                            image_pos=None, image_size=None):
        """Save 256x256 crop from circle area (chat/members pfp) + original (profile top).

        Both stored in app data dir under pfps/; 256x256 uploaded to guild.

        crop_cx/crop_cy/crop_r are the crop circle in absolute window pixels —
        the same values the editor draws its overlay ellipse with — and
        image_pos/image_size are the image widget's absolute rect. Both are in
        the same (window) coordinate space, so the circle can be mapped onto
        the image by a plain subtraction before converting to natural pixels.

        The old code mixed spaces: it took the circle centre as
        circle_x * editor_w (editor-relative) and subtracted the image's
        absolute pos, which shifted every crop by the editor's position inside
        the popup — always towards the lower-left of the source image.
        """
        try:
            pil = PILImage.open(path).convert("RGBA")
        except Exception:
            # PIL not available — use original directly
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

        # Always store paths locally regardless of guild API result —
        # the files are saved to disk even if guild upload fails.
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
            with open(small_path, "rb") as f:
                IMAGE_CACHE.set_bytes(key, f.read(), "png")

    def attach_file(self, path):
        """Attach a file to the current message input as an upload."""
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except Exception as e:
            self._set_status("attach failed: %s" % e)
            return
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        name = path.split("/")[-1]
        self._pending_attachment = [name, mime, blob]
        self._set_status("Attached: %s (%d bytes)" % (name, len(blob)))

    def upload_avatar(self, path):
        """Upload an avatar image to the guild."""
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except Exception as e:
            self._set_status("avatar upload failed: %s" % e)
            return
        try:
            res = self.guild_api.avatar_upload(blob)
        except Exception:
            res = None
        if res and res.get("ok"):
            self._set_status("Avatar updated")
            self.profile_screen.target_avatar = path
            if hasattr(self, "profile_screen"):
                self.profile_screen.ids.profile_pfp.source = path
        else:
            self._set_status("avatar upload failed")

    def save_profile(self):
        """Save username to the guild."""
        try:
            inp = self.root.get_screen("profile").ids.profile_username_input
            new_name = (inp.text or "").strip() or "user"
        except Exception:
            return
        try:
            res = self.guild_api.register(new_name, "")
            if res.get("ok"):
                self.username = new_name
                self.profile_screen.target_username = new_name
                self._set_status("Profile saved")
            else:
                self._set_status("save failed: %s" % res.get("error", "unknown"))
        except Exception as e:
            self._set_status("save failed: %s" % e)

    # -- ui helpers ------------------------------------------------------
    def _set_status(self, text):
        try:
            self.root.get_screen("connect").ids.status.text = text
        except Exception:
            pass

    def on_stop(self):
        self._poll_stop.set()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not KIVY:
        print("Kivy is required to run the client: pip install kivy[base]", file=sys.stderr)
        print(_KIVY_IMPORT_ERROR)
        sys.exit(1)

    # --debug enables verbose attachment/ingest logging on the client.
    import argparse as _argparse
    _dbg_parser = _argparse.ArgumentParser(add_help=False)
    _dbg_parser.add_argument("--debug", action="store_true")
    _dbg_args, _ = _dbg_parser.parse_known_args()
    DEBUG = _dbg_args.debug

    # Graceful GL backend fallback: Wayland without a GPU, headless runners,
    # and VMs often fail with "No matching FB config found" from SDL2/OpenGL.
    # Try software GL first, then the mock backend as last resort.
    _attempt = int(os.environ.get("_SHades_GL_ATTEMPT", "1"))
    try:
        sHadesClientApp().run()
    except Exception as _e:
        _msg = str(_e)
        if _attempt < 3 and (
            "FB config" in _msg
            or "No matching" in _msg
            or "couldn't connect to X server" in _msg
        ):
            os.environ["_SHades_GL_ATTEMPT"] = str(_attempt + 1)
            if _attempt == 1:
                os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
            else:
                os.environ["KIVY_GL_BACKEND"] = "mock"
            os.execv(sys.executable, [sys.executable] + sys.argv[1:])
        # The app may have been running for a long time before this error —
        # print the traceback so the real cause is visible instead of a
        # one-line message that claims the client "failed to start".
        import traceback
        traceback.print_exc()
        print(f"sHades client crashed: {_e}", file=sys.stderr)
        sys.exit(1)
