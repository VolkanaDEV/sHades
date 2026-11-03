#!/usr/bin/env python3
"""Attachment rendering regression tests for the sHades Kivy client.

Three bugs were fixed here:
  1. the guild returns attachment metadata as dicts
     ({"name", "mime", "size", "path"}), but the client only understood the
     local [name, mime, blob] list form, so every attachment received from
     the guild was silently dropped;
  2. the client never downloaded the attachment bytes from the guild's
     /file/attachments/... handler;
  3. the optimistic local render passed the pending attachment as a flat
     list instead of a list of [name, mime, blob] triples, so the sender's
     own attachment was mangled.

Usage:  /usr/bin/python tests/test_attachments.py
"""
import os
import sys
import time
import functools

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082")


class FakeIds:
    def __init__(self, grid):
        self.message_grid = grid
        self.msg_input = FakeInput()


class FakeInput:
    text = ""


class FakeMain:
    _current_channel = "general"

    def __init__(self, grid):
        self.ids = FakeIds(grid)


class FakeRoot:
    def __init__(self, grid):
        self._main = FakeMain(grid)

    def get_screen(self, name):
        return self._main


class StubGuild:
    def __init__(self, files=None):
        self.files = files or {}
        self.posted = []

    def post(self, channel, content, attachments=None):
        self.posted.append((channel, content, attachments))
        return {"ok": 1, "msg_id": "sent-1"}

    def avatar_get(self, user_hash):
        return {"ok": 0}

    def fetch_file(self, path):
        return self.files.get(path)


class StubApp:
    """Minimal stand-in for sHadesClientApp (no RNS/guild needed)."""

    def __init__(self, kivy_client, grid, files=None):
        self.kc = kivy_client
        self.root = FakeRoot(grid)
        self.guild_api = StubGuild(files)
        self.my_hash = "a" * 32
        self.username = "me"
        self._members = {}
        self._msg_widgets = {}
        self._pending_attachment = None
        self.status = []
        # real implementations, bound to this stub
        self._render_message = functools.partial(
            kivy_client.sHadesClientApp._render_message, self)
        self._fetch_attachment = functools.partial(
            kivy_client.sHadesClientApp._fetch_attachment, self)
        self._add_attachment_embed = functools.partial(
            kivy_client.sHadesClientApp._add_attachment_embed, self)
        self._record_member = functools.partial(
            kivy_client.sHadesClientApp._record_member, self)
        self._refresh_avatar = functools.partial(
            kivy_client.sHadesClientApp._refresh_avatar, self)
        self._add_loading_placeholder = functools.partial(
            kivy_client.sHadesClientApp._add_loading_placeholder, self)
        self._remove_placeholder = functools.partial(
            kivy_client.sHadesClientApp._remove_placeholder, self)

    def _set_status(self, text):
        self.status.append(text)

    def _channel_cid(self, name):
        return "cid-" + name


def main():
    try:
        import kivy_client as kc
    except Exception as e:
        print("SKIP  kivy_client import failed: %s" % e)
        return 0
    if not kc.KIVY:
        print("SKIP  Kivy not available")
        return 0

    from kivy.clock import Clock
    from kivy.uix.gridlayout import GridLayout
    from kivy.lang import Builder

    # Load KV so MessageRow has embed_container id
    Builder.load_file("kv_md.kv")

    # --- 1. guild-style attachment metadata (dict) ------------------------
    grid = GridLayout(cols=1)
    app = StubApp(kc, grid, files={"/file/attachments/abc.png": PNG_1PX})
    m = {"msg_id": "m1", "author_hash": "b" * 32, "username": "bob",
         "content": "here you go", "ts": time.time(),
         "attachments": [{"name": "shot.png", "mime": "image/png",
                          "size": len(PNG_1PX),
                          "path": "/file/attachments/abc.png"}]}
    kc.sHadesClientApp._render_message(app, grid, m)

    deadline = time.time() + 5
    while time.time() < deadline:
        Clock.tick()
        # Embeds are now inside MessageRow's embed_container, not direct grid children
        if len(grid.children) >= 1:
            row = grid.children[0]
            if hasattr(row.ids, "embed_container") and len(row.ids.embed_container.children) > 0:
                break
        time.sleep(0.05)
    Clock.tick()
    row = grid.children[0]
    check("guild attachment metadata is fetched and rendered",
          len(row.ids.embed_container.children) == 1,
          "%d embeds in row" % len(row.ids.embed_container.children))

    # --- 2. local [name, mime, blob] attachment ---------------------------
    grid2 = GridLayout(cols=1)
    app2 = StubApp(kc, grid2)
    m2 = {"msg_id": "m2", "author_hash": "b" * 32, "username": "bob",
          "content": "local", "ts": time.time(),
          "attachments": [["shot.png", "image/png", PNG_1PX]]}
    kc.sHadesClientApp._render_message(app2, grid2, m2)
    Clock.tick()
    row2 = grid2.children[0]
    check("local [name, mime, blob] attachment renders an embed",
          len(row2.ids.embed_container.children) == 1,
          "%d embeds in row" % len(row2.ids.embed_container.children))

    # --- 3. non-image attachment shows a label ----------------------------
    grid3 = GridLayout(cols=1)
    app3 = StubApp(kc, grid3)
    m3 = {"msg_id": "m3", "author_hash": "b" * 32, "username": "bob",
          "content": "file", "ts": time.time(),
          "attachments": [["notes.txt", "text/plain", b"hello"]]}
    kc.sHadesClientApp._render_message(app3, grid3, m3)
    Clock.tick()
    row3 = grid3.children[0]
    check("non-image attachment renders a label",
          len(row3.ids.embed_container.children) == 1,
          "%d embeds in row" % len(row3.ids.embed_container.children))

    # --- 4. optimistic send keeps the attachment shape --------------------
    grid4 = GridLayout(cols=1)
    app4 = StubApp(kc, grid4)
    app4.root.get_screen("main").ids.msg_input.text = "with file"
    app4._pending_attachment = ["notes.txt", "text/plain", b"hello"]
    kc.sHadesClientApp.send_message(app4)
    deadline = time.time() + 5
    while time.time() < deadline:
        Clock.tick()
        if app4.guild_api.posted:
            break
        time.sleep(0.05)
    posted = app4.guild_api.posted[0] if app4.guild_api.posted else None
    check("send posts the attachment as [name, mime, blob]",
          posted is not None and posted[2] == [["notes.txt", "text/plain",
                                               b"hello"]],
          posted)
    Clock.tick()
    row4 = grid4.children[0]
    check("optimistically rendered message shows the attachment",
          len(row4.ids.embed_container.children) == 1,
          "%d embeds in row" % len(row4.ids.embed_container.children))

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
