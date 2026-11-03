#!/usr/bin/env python3
"""Embed layout regression tests.

Embeds must:
  * live inside the message row's embed_container (never leaked into the
    chat grid as separate rows);
  * be origined at the top-left of the row, next to the sender's username
    (above the message text);
  * make the row expand so subsequent messages appear beneath them, not
    overlapping.
"""
import os
os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import sys
import tempfile
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

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(PNG_1PX)
    tmp.close()

    row = MessageRow(author_hash="a" * 32, username="bob", ts="12:00",
                     content="hello there", content_markup="hello there",
                     avatar_source="", msg_id="m1")
    ec = row.ids.embed_container
    content_box = ec.parent

    check("embed_container exists on the row", ec is not None)
    check("embed_container is a direct child of the content box",
          ec in content_box.children)

    labels = [c for c in content_box.children if c.__class__.__name__ == "Label"]
    check("content label present in content box", len(labels) >= 1)

    row.width = dp(400)
    grid = GridLayout(cols=1)
    grid.width = dp(400)
    grid.add_widget(row)
    for _ in range(5):
        Clock.tick()
        grid.do_layout()
        row.do_layout()
        content_box.do_layout()

    embed_y = ec.y
    content_y = labels[0].y if labels else None
    check("embed_container origined above the content label (top-left near "
          "username)",
          embed_y is not None and content_y is not None and embed_y > content_y,
          "embed_y=%s content_y=%s" % (embed_y, content_y))

    h_before = row.height
    emb = MessageEmbed(source=tmp.name, size_hint_y=None, height=dp(200))
    ec.add_widget(emb)
    for _ in range(5):
        Clock.tick()
        grid.do_layout()
        row.do_layout()
        content_box.do_layout()

    check("row expands to contain the embed",
          row.height - h_before >= dp(200),
          "grew by %d" % (row.height - h_before))
    check("row tall enough for header + embed + content",
          row.height >= dp(18) + dp(200) + dp(6),
          "row.height=%d" % row.height)

    # Subsequent message row must land beneath this row.
    row2 = MessageRow(author_hash="a" * 32, username="bob", ts="12:01",
                      content="next message", content_markup="next message",
                      avatar_source="", msg_id="m2")
    row2.width = dp(400)
    grid.add_widget(row2)
    for _ in range(5):
        Clock.tick()
        grid.do_layout()
    check("subsequent message appears beneath the embed row",
          row2.y < row.y, "row.y=%s row2.y=%s" % (row.y, row2.y))

    try:
        os.unlink(tmp.name)
    except OSError:
        pass

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
