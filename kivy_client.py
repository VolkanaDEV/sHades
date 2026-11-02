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
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Kivy (guarded so the module can be imported for linting without Kivy present)
# ---------------------------------------------------------------------------
try:
    from kivy.app import App
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label
    from kivy.uix.image import Image
    from kivy.uix.screenmanager import ScreenManager, Screen
    from kivy.core.window import Window
    from kivy.clock import Clock
    from kivy.properties import (
        StringProperty, ObjectProperty, BooleanProperty,
    )
    from kivy.lang import Builder
    from kivy.uix.behaviors import ButtonBehavior
    from kivy.metrics import dp, sp
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e

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
        deadline = time.time() + timeout
        while receipt.response is None and time.time() < deadline:
            time.sleep(0.05)
        if receipt.response is None:
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

    def register(self, username, profile_node=""):
        return self._request("/api/register",
                             {"username": username, "profile_node": profile_node})

    def avatar_upload(self, blob):
        return self._request("/api/avatar", {"avatar": blob})

    def avatar_get(self, user_hash):
        return self._request("/api/avatar_get", {"user": user_hash})

    def fetch_file(self, path):
        """Fetch raw bytes from a /file/... request handler on the guild."""
        receipt = self.link.request(path, data={})
        deadline = time.time() + 15
        while receipt.response is None and time.time() < deadline:
            time.sleep(0.05)
        if receipt.response is None:
            return None
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

    class MessageEmbed(Image, ButtonBehavior):
        """Clickable image embed."""
        source_url = StringProperty("")
        pass

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

        def on_content_markup(self, instance, value):
            # Rebuild content line after markup set
            pass


class ChannelButton(ButtonBehavior, BoxLayout):
    channel_name = StringProperty("")


# ---------------------------------------------------------------------------
# KV UI
# ---------------------------------------------------------------------------
KV = r"""
#:import dp kivy.metrics.dp

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
        allow_stretch: True
        keep_ratio: True

    BoxLayout:
        orientation: 'vertical'
        size_hint_x: 1
        spacing: dp(2)
        BoxLayout:
            size_hint_y: None
            height: dp(18)
            Label:
                text: root.username
                color: (1, 1, 1, 1) if root.is_own else (0.9, 0.9, 0.9, 1)
                bold: True
                font_size: sp(13)
                text_size: self.width, None
                size_hint_x: None
                width: dp(140)
                halign: 'left'
                valign: 'middle'
                shorten: True
            Label:
                text: root.ts
                color: (0.6, 0.63, 0.67, 1)
                font_size: sp(11)
                text_size: self.width, None
                size_hint_x: 1
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
        allow_stretch: True
        keep_ratio: True
    Label:
        text: root.username
        color: (1, 1, 1, 0.9)
        font_size: sp(13)
        halign: 'left'
        valign: 'middle'


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
            height: dp(220)
            TextInput:
                id: node_hash
                hint_text: 'Guild node_hash (32 hex)'
                multiline: False
                size_hint_y: None
                height: dp(44)
                background_color: 0.22, 0.24, 0.28, 1
                foreground_color: 1, 1, 1, 1
                cursor_color: 1, 1, 1, 1
            TextInput:
                id: delivery_hash
                hint_text: 'Guild delivery_hash (32 hex)'
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
                background_color: 0.22, 0.24, 0.28, 1
                foreground_color: 1, 1, 1, 1
                cursor_color: 1, 1, 1, 1
        Button:
            text: 'Connect'
            size_hint_y: None
            height: dp(50)
            background_color: 0.36, 0.41, 0.59, 1
            color: 1, 1, 1, 1
            on_press: app.connect_to_guild(node_hash.text, delivery_hash.text, username.text)
        Label:
            id: status
            text: ''
            color: 1, 0.4, 0.4, 1
            font_size: sp(13)
            size_hint_y: None
            height: dp(30)


<MainScreen>:
    BoxLayout:
        orientation: 'horizontal'
        canvas.before:
            Color:
                rgba: 0.14, 0.16, 0.19, 1
            Rectangle:
                pos: self.pos
                size: self.size

        # ---------------- left: channels (collapsable) ----------------
        BoxLayout:
            id: left_sidebar
            size_hint_x: None
            width: dp(220) if root.left_open else dp(0)
            canvas.before:
                Color:
                    rgba: 0.18, 0.2, 0.23, 1
                Rectangle:
                    pos: self.pos
                    size: self.size
            ScrollView:
                do_scroll_x: False
                GridLayout:
                    id: channel_grid
                    cols: 1
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(2)
                    padding: [dp(6), dp(6)]
            Button:
                text: 'channels \u25bc' if root.left_open else 'channels \u25b6'
                size_hint_y: None
                height: dp(34)
                background_color: 0.24, 0.26, 0.3, 1
                color: 1, 1, 1, 0.9
                on_press: root.toggle_left()

        # ---------------- center: chat ----------------
        BoxLayout:
            orientation: 'vertical'
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

        # ---------------- right: members (collapsable) ----------------
        BoxLayout:
            id: right_sidebar
            size_hint_x: None
            width: dp(200) if root.right_open else dp(0)
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
            ScrollView:
                do_scroll_x: False
                GridLayout:
                    id: member_grid
                    cols: 1
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(2)
                    padding: [dp(6), dp(6)]
            Button:
                text: 'members \u25bc' if root.right_open else 'members \u25b6'
                size_hint_y: None
                height: dp(34)
                background_color: 0.24, 0.26, 0.3, 1
                color: 1, 1, 1, 0.9
                on_press: root.toggle_right()
"""


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
if KIVY:
    class ConnectScreen(Screen):
        pass

    class MemberRow(BoxLayout):
        username = StringProperty("")
        avatar_source = StringProperty("")

    class MainScreen(Screen):
        left_open = BooleanProperty(True)
        right_open = BooleanProperty(True)

        def toggle_left(self):
            self.left_open = not self.left_open

        def toggle_right(self):
            self.right_open = not self.right_open

    Builder.load_string(KV)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
class sHadesClientApp(App):
    title = "sHades"

    guild_api = ObjectProperty(None, allownone=True)
    reticulum = ObjectProperty(None, allownone=True)
    link = ObjectProperty(None, allownone=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
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

    # -- lifecycle -------------------------------------------------------
    def build(self):
        if Window:
            Window.clearcolor = (0.14, 0.16, 0.19, 1)
        sm = ScreenManager()
        sm.add_widget(ConnectScreen(name="connect"))
        sm.add_widget(MainScreen(name="main"))
        return sm

    def on_start(self):
        # Try to resume a saved session
        sess = self._load_session()
        if sess:
            self.node_hash = sess.get("node_hash")
            self.delivery_hash = sess.get("delivery_hash")
            self.username = sess.get("username", "user")
            self._open_main()
            Clock.schedule_once(lambda dt: self._connect_guild(), 0.5)

    def _open_main(self):
        """Switch to the main chat screen."""
        try:
            self.root.current = "main"
            self._refresh_channels_ui()
        except Exception as e:
            print("open main error:", repr(e))

    # -- session ---------------------------------------------------------
    def _session_path(self):
        d = _app_data_dir()
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "session.json")

    def _save_session(self):
        try:
            with open(self._session_path(), "w") as f:
                json.dump({
                    "node_hash": self.node_hash,
                    "delivery_hash": self.delivery_hash,
                    "username": self.username,
                }, f)
        except Exception:
            pass

    def _load_session(self):
        try:
            with open(self._session_path()) as f:
                return json.load(f)
        except Exception:
            return None

    # -- connect ---------------------------------------------------------
    def connect_to_guild(self, node_hash, delivery_hash, username):
        node_hash = (node_hash or "").strip().lower()
        delivery_hash = (delivery_hash or "").strip().lower()
        username = (username or "user").strip() or "user"
        ok_node = bool(re.fullmatch(r"[0-9a-f]{32}", node_hash))
        ok_delivery = bool(re.fullmatch(r"[0-9a-f]{32}", delivery_hash))
        if not ok_node or not ok_delivery:
            self._set_status("Invalid hashes (need 32 hex each)")
            return
        self.node_hash = node_hash
        self.delivery_hash = delivery_hash
        self.username = username
        self._set_status("Connecting...")
        threading.Thread(target=self._connect_guild, daemon=True).start()

    def _connect_guild(self):
        try:
            storage = os.path.join(_app_data_dir(), "rns")
            os.makedirs(storage, exist_ok=True)
            reticulum = RNS.Reticulum(configdir=storage)
            self.reticulum = reticulum

            # identity
            ipath = os.path.join(storage, "identity")
            if os.path.exists(ipath):
                self.client_identity = RNS.Identity.from_file(ipath)
            else:
                self.client_identity = RNS.Identity()
                self.client_identity.to_file(ipath)

            # client delivery destination (for identification + LXMF)
            self.client_dest = RNS.Destination(
                self.client_identity, RNS.Destination.OUT,
                RNS.Destination.SINGLE, "lxmf", "delivery")
            self.my_hash = hexstr(self.client_dest.hash)

            # link to guild node
            nh = bytes.fromhex(self.node_hash)
            nid = RNS.Identity.recall(nh)
            nd = RNS.Destination(nid, RNS.Destination.OUT,
                                 RNS.Destination.SINGLE, "nomadnetwork", "node")
            link = RNS.Link(nd)
            for _ in range(200):
                if link.status == RNS.Link.ACTIVE:
                    break
                time.sleep(0.1)
            if link.status != RNS.Link.ACTIVE:
                raise RuntimeError("link never active")
            link.identify(self.client_identity)
            self.link = link

            self.guild_api = GuildAPI(reticulum, link)
            self._save_session()

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
                st = self.guild_api.state(cid)
                if st.get("ok"):
                    seq = st.get("seq", 0)
                    last_ts = st.get("last_ts", 0.0)
                    if seq != self._seq or last_ts != self._last_ts:
                        self._seq = seq
                        self._last_ts = last_ts
                        self._fetch_messages(cid)
                        self._refresh_members_ui(cid)
                # also (re)discover channels: just ensure 'general' exists
                Clock.schedule_once(lambda dt: self._ensure_general_channel(), 0)
            except Exception as e:
                print("poll error:", e)
            self._poll_stop.wait(3.0)

    def _fetch_messages(self, channel, since_id=None, since_ts=None):
        res = self.guild_api.messages(channel, since_id=since_id, since_ts=since_ts, limit=100)
        if not res.get("ok"):
            return
        msgs = res.get("messages", []) or []
        updates = res.get("updates", []) or []

        def apply(dt):
            # All widget access must happen on the main thread.
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
        key = "av:" + author
        path = IMAGE_CACHE.get(key)
        if path:
            avatar_source = path
        else:
            # fetch avatar
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
                emb = MessageEmbed(source_url=url, size_hint_y=None, height=dp(150))
                # Kivy AsyncImage loads http directly
                from kivy.uix.image import AsyncImage
                emb.add_widget(AsyncImage(source=url, allow_stretch=True, keep_ratio=False))
                grid.add_widget(emb)
            elif ext in (".mp3", ".ogg", ".wav", ".flac"):
                grid.add_widget(Label(text="[audio] " + url, size_hint_y=None, height=dp(20),
                                      color=(0.7, 0.73, 0.77, 1), font_size=sp(12)))
            elif ext in (".mp4", ".webm"):
                grid.add_widget(Label(text="[video] " + url, size_hint_y=None, height=dp(20),
                                      color=(0.7, 0.73, 0.77, 1), font_size=sp(12)))

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
        main.ids.channel_header.text = "#" + name
        main._current_channel = name
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
        if not text or not self.guild_api:
            return
        cid = getattr(main, "_current_channel", "general")
        inp.text = ""

        def work():
            res = self.guild_api.post(cid, text)
            if res.get("ok"):
                # optimistically show
                msg_id = res.get("msg_id")
                m = {"msg_id": msg_id, "author_hash": self.my_hash,
                     "username": self.username, "content": text,
                     "ts": now(), "channel_id": cid}
                Clock.schedule_once(lambda dt: self._render_message(
                    main.ids.message_grid, m), 0)
            else:
                print("post failed:", res)
        threading.Thread(target=work, daemon=True).start()

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
        print(f"sHades client failed to start: {_e}", file=sys.stderr)
        sys.exit(1)
