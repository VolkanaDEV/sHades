#!/usr/bin/env python3
"""Verification of the new embed/attachment features (mock Kivy backend).

Covers:
  * message history persistence round-trip (ephemeral blobs kept locally,
    server-stored paths re-fetchable);
  * loading placeholder add/remove;
  * MessageEmbed click-to-enlarge/minimize toggle;
  * plain Enter = send, Shift+Enter = newline logic.
"""
import os
os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import sys
import tempfile
import shutil
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
    from kivy.lang import Builder
    from kivy.metrics import dp
    from kivy.uix.gridlayout import GridLayout

    Builder.load_file("kv_md.kv")
    MessageRow = kc.MessageRow
    MessageEmbed = kc.MessageEmbed

    # --- persistence round-trip --------------------------------------
    tmpdir = tempfile.mkdtemp()
    try:
        app = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
        app._history = []

        # ephemeral: blob saved to local file
        ephem_local = app._save_attachment_blob(PNG_1PX, "shot.png")
        ephem_att = {"name": "shot.png", "mime": "image/png",
                     "kind": kc.ATT_EPHEMERAL, "local_path": ephem_local}
        # server-stored: path only
        srv_att = {"name": "srv.png", "mime": "image/png", "size": len(PNG_1PX),
                   "kind": kc.ATT_SERVER, "path": "/file/attachments/abc.png"}

        m1 = {"msg_id": "m1", "author_hash": "b" * 32, "username": "bob",
              "content": "e", "ts": 1, "channel_id": "c",
              "attachments": [ephem_att]}
        m2 = {"msg_id": "m2", "author_hash": "b" * 32, "username": "bob",
              "content": "s", "ts": 2, "channel_id": "c",
              "attachments": [srv_att]}
        app._add_to_history(m1)
        app._add_to_history(m2)
        app._save_history()

        # reload from disk
        reloaded = app._load_history()
        check("history persists 2 messages", len(reloaded) == 2,
              "%d reloaded" % len(reloaded))
        r1 = reloaded[0]
        ephem_r = r1["attachments"][0]
        check("ephemeral keeps local_path on disk",
              ephem_r.get("kind") == kc.ATT_EPHEMERAL
              and os.path.exists(ephem_r.get("local_path", "")),
              ephem_r)
        r2 = reloaded[1]
        srv_r = r2["attachments"][0]
        check("server-stored keeps path for re-fetch",
              srv_r.get("kind") == kc.ATT_SERVER
              and srv_r.get("path") == "/file/attachments/abc.png",
              srv_r)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # --- loading placeholder add/remove ------------------------------
    row = MessageRow(author_hash="a" * 32, username="bob", ts="12:00",
                     content="hi", content_markup="hi", avatar_source="",
                     msg_id="mX")
    grid = GridLayout(cols=1)
    grid.add_widget(row)
    app2 = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    app2._add_loading_placeholder(row)
    check("placeholder added to embed_container",
          len(row.ids.embed_container.children) >= 1,
          "%d children" % len(row.ids.embed_container.children))
    app2._remove_placeholder(row)
    check("placeholder removed", row._loading_placeholder is None)

    # --- MessageEmbed enlarge/minimize toggle -----------------------
    emb = MessageEmbed(source="/tmp/x.png", size_hint_y=None, height=dp(200))
    check("embeds start collapsed", emb.is_expanded is False)

    calls = []
    class FakeApp:
        def show_fullscreen_image(self, source, embed=None):
            calls.append(("show", source))
        def dismiss_fullscreen_image(self, embed):
            calls.append(("dismiss", embed))
            if embed is not None:
                embed.is_expanded = False

    # monkeypatch App.get_running_app
    orig = kc.App.get_running_app
    kc.App.get_running_app = staticmethod(lambda: FakeApp())
    try:
        emb.on_release()
        check("click expands: show_fullscreen_image called",
              calls == [("show", "/tmp/x.png")], str(calls))
        check("emb expanded after click", emb.is_expanded is True)
        emb.on_release()
        check("click again minimizes: dismiss_fullscreen_image called",
              calls[-1] == ("dismiss", emb), str(calls))
        check("emb collapsed after minimize", emb.is_expanded is False)
    finally:
        kc.App.get_running_app = orig

    # --- Enter / Shift+Enter logic ----------------------------------
    class FakeInp:
        focus = True

    class FakeMain:
        ids = type("ids", (), {"msg_input": FakeInp()})()

    class FakeRoot:
        def get_screen(self, name):
            return FakeMain()

    app3 = kc.sHadesClientApp.__new__(kc.sHadesClientApp)
    app3.root = FakeRoot()
    sent = []
    app3.send_message = lambda: sent.append("sent")

    # plain enter (no shift) -> send
    app3._on_key_down(None, 13, 0, "\r", [])
    check("plain enter sends", sent == ["sent"], str(sent))
    sent.clear()
    # shift+enter -> no send (newline handled by multiline input)
    app3._on_key_down(None, 13, 0, "\r", ["shift"])
    check("shift+enter does not send", sent == [], str(sent))

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
