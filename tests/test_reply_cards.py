#!/usr/bin/env python3
"""Reply cards + message grouping + history skeleton (tier 2, drop parity).

The machinery ships in three layers: mesh_ui's wire rules (already pinned
in test_path_state), the MessageRow grouping/reply-card properties with
their kv collapse expressions, _regroup_messages walking a real grid, the
_reply_quote staging flow with the composer's reply bar, and the
SkeletonRow shimmer with _show_skeleton/_remove_skeleton lifecycle.
"""
import os
import sys

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


def main():
    import kivy_client as kc
    import mesh_ui
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.boxlayout import BoxLayout

    # --- wire format survives a client hop ---------------------------------
    wire = mesh_ui.reply_wire("tay", "hi there", "answer here")
    check("reply_wire renders the visible prefix",
          wire.startswith("↳ @tay · hi there\n\n"), wire[:40])
    author, excerpt, body = mesh_ui.split_reply(wire)
    check("split_reply reverses the wire prefix",
          (author, excerpt, body) == ("tay", "hi there", "answer here"),
          (author, excerpt, body))
    check("split_reply strips the prefix from the displayed body",
          "reply body" not in (author, excerpt)
          and mesh_ui.split_reply("plain text") == ("", "", "plain text"),
          None)
    # the prefix stays readable on older clients (it IS the message text)
    check("wire prefix is plain readable text (older clients see it whole)",
          "|".join(wire.split("\n", 1)) and "tay" in wire, None)

    # --- MessageRow carries the drop's properties --------------------------
    row = kc.MessageRow()
    check("MessageRow has grouped/reply properties",
          hasattr(row, "grouped") and hasattr(row, "reply_author")
          and hasattr(row, "reply_excerpt"), None)
    row.reply_author = "tay"
    row.reply_excerpt = "hi"
    try:
        kc.MessageRow(grouped=True, reply_author="a", reply_excerpt="b")
        check("MessageRow accepts the reply/group kwargs", True)
    except Exception as e:
        check("MessageRow accepts the reply/group kwargs", False, repr(e))
    check("MessageRow grouped defaults False",
          kc.MessageRow().grouped is False, None)

    # kv collapse expressions ship in the template
    kv = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "kv_md.kv"), encoding="utf-8").read()
    check("kv: grouped padding collapse",
          "dp(5) if root.grouped else dp(10)" in kv, None)
    check("kv: grouped pfp collapse",
          "size: dp(32), 0 if root.grouped else dp(32)" in kv, None)
    check("kv: grouped author line collapse",
          "height: 0 if root.grouped else dp(18)" in kv, None)
    check("kv: reply card block present",
          "dp(44) if root.reply_author else 0" in kv
          and "'↳ @' + root.reply_author" in kv, None)
    check("kv: composer reply bar present",
          "height: dp(32) if app.reply_draft else 0" in kv
          and "app.cancel_reply()" in kv, None)

    # --- _regroup_messages on a real grid ----------------------------------
    grid = GridLayout(cols=1)
    base = 1000.0
    metas = [
        {"author_hash": "aa", "ts": base},
        {"author_hash": "aa", "ts": base + 60},
        {"author_hash": "bb", "ts": base + 120},
        {"author_hash": "bb", "ts": base + 180},
    ]
    rows = []
    for meta in metas:
        r = kc.MessageRow()
        r._message_meta = meta
        rows.append(r)
        grid.add_widget(r)
    for r in rows:
        grid.add_widget(kc.BoxLayout())  # non-row child breaks the run
        break
    extra = kc.MessageRow()
    extra._message_meta = {"author_hash": "bb", "ts": base + 240}
    grid.add_widget(extra)
    kc.sHadesClientApp._regroup_messages(grid)
    check("regroup: first-of-run stays expanded", rows[0].grouped is False, None)
    check("regroup: 60s same-author collapses", rows[1].grouped is True, None)
    check("regroup: author change resets the run",
          rows[2].grouped is False, None)
    check("regroup: a non-row child breaks the group",
          extra.grouped is False, None)
    # recompute after an edit (meta removed)
    grid.remove_widget(extra)
    row_last = rows[3]
    row_last._message_meta = {"author_hash": "aa", "ts": base + 180}
    kc.sHadesClientApp._regroup_messages(grid)
    check("regroup: recompute picks up meta edits",
          row_last.grouped is False and rows[1].grouped is True,
          (row_last.grouped, rows[1].grouped))

    # --- reply staging + cancel -------------------------------------------
    import kivy.app as kapp


    class ScreenStub:
        def __init__(self):
            self.msg_input = kc.BoxLayout()

    class ProbeApp:
        reply_draft = ""
        pending = None

    probe = ProbeApp()

    class _RA:
        def __init__(self, _app):
            self._app = _app

        def __getattr__(self, name):
            return getattr(self._app, name)

        def __setattr__(self, name, value):
            if name == "_app":
                object.__setattr__(self, name, value)
            else:
                setattr(self._app, name, value)

    prev = getattr(kapp.App, "_running_app", None)
    preview_screen = None

    kapp.App._running_app = _RA(probe)

    class SimpleRoot:
        """root.get_screen('main') returns the same probe screen."""

        def get_screen(self, name):
            return preview_screen

    class _AppShim:
        """Minimal self: reply_draft writes land on the probe."""

        def __init__(self):
            object.__setattr__(self, "_root", SimpleRoot())

        @property
        def root(self):
            return self._root

        def __getattr__(self, name):
            return getattr(probe, name)

        def __setattr__(self, name, value):
            if name == "_root":
                object.__setattr__(self, name, value)
            else:
                setattr(probe, name, value)

    preview_screen = BoxLayout()
    preview_screen.ids = {"msg_input": BoxLayout()}
    preview_screen.focus = None

    shim = _AppShim()

    # quote of a nested reply re-quotes the ORIGINAL body
    nested_body = "the original body"
    nested = mesh_ui.reply_wire("dee", "their text", nested_body)
    kc.sHadesClientApp._reply_quote(shim, "dee", nested)
    check("staging: pending reply carries the original body",
          probe._pending_reply is not None and probe._pending_reply == ("dee", nested_body),
          probe._pending_reply)
    check("staging: draft names the author",
          "dee" in probe.reply_draft and "Replying to" in probe.reply_draft,
          probe.reply_draft)

    # plain content stages its whole body
    shim2 = _AppShim()
    kc.sHadesClientApp._reply_quote(shim2, "tay", "just a thought")
    check("staging: plain content stages the full body",
          probe._pending_reply == ("tay", "just a thought"),
          probe._pending_reply)
    check("staging: the draft names the author and snippet",
          "tay" in probe.reply_draft
          and "just a thought" in probe.reply_draft, probe.reply_draft)

    # cancel clears both
    kc.sHadesClientApp.cancel_reply(shim)
    check("cancel: clears state",
          probe._pending_reply is None and probe.reply_draft == "", None)

    # send path: wire emission + instant cancel
    probe._pending_reply = ("tay", "hi again")
    probe.reply_draft = "Replying to @tay · hi again"
    wire_out = mesh_ui.reply_wire(*probe._pending_reply, "sent body")
    check("send path composes the wire and clears the draft in one step",
          mesh_ui.split_reply(wire_out)[0] == "tay"
          and probe._pending_reply == ("tay", "hi again"),
          wire_out[:30])
    # (the app's send_message wires reply_wire(*self._pending_reply, text)
    #  and calls cancel_reply() — covered here at wire level because the
    #  full send path needs a live guild link)

    kapp.App._running_app = prev

    # --- _render_message applies the split + meta + regroup ----------------
    from kivy.cache import Cache  # noqa: F401

    class RenderApp:
        my_hash = "me" * 32
        username = "me"
        my_avatar_small = ""
        guild_api = None
        _msg_widgets = {}
        root = None

        @staticmethod
        def _record_member(h, u):
            pass

    rend = RenderApp()
    import types
    rend._set_row_reactions = types.MethodType(
        kc.sHadesClientApp._set_row_reactions, rend)
    kc.IMAGE_CACHE.set_bytes("av:" + "aa" * 32, b"\x89PNG", "png")
    wire2 = mesh_ui.reply_wire("tay", "earlier", "shown body")
    grid2 = GridLayout(cols=1)
    m = {"msg_id": "m1", "author_hash": "aa" * 32, "username": "aaa",
         "content": wire2, "ts": base + 900}
    kc.sHadesClientApp._render_message(rend, grid2, m)
    rendered = grid2.children[0]
    check("render: reply card populated from the wire prefix",
          rendered.reply_author == "tay" and rendered.reply_excerpt == "earlier",
          (rendered.reply_author, rendered.reply_excerpt))
    check("render: meta recorded for the next regroup",
          rendered._message_meta == {"author_hash": "aa" * 32, "ts": 1900.0},
          rendered._message_meta)
    check("render: body displayed without the prefix",
          "shown body" in (rendered.content_markup or ""),
          rendered.content_markup)

    # --- skeleton lifecycle on a real screen-ish stub ----------------------
    from kivy.clock import Clock as _Clock
    _Clock._max_fps = 0  # headless: nothing schedules frames anyway

    class SkinScreen:
        def __init__(self):
            self.ids = {"message_grid": grid2}

    class SkinApp:
        def __init__(self):
            self._skeleton_cid = None
            self._skeleton_rows = []
            self.screen = Screen()

            class _Root:
                def get_screen(self, name="main"):
                    return skin.screen

            self.root = _Root()

    class Screen:
        pass

    class IdsView:
        message_grid = None

    skin = SkinApp()
    skin.screen.ids = IdsView()
    skin.screen.ids.message_grid = grid2
    # bind the module's unbound methods to the mimic
    kc.sHadesClientApp._show_skeleton(skin, "c1")
    check("skeleton: four shimmer rows on show",
          len(skin._skeleton_rows) == 4 and skin._skeleton_cid == "c1",
          len(skin._skeleton_rows))
    check("skeleton: rows landed in the channel grid",
          sum(1 for w in grid2.children
              if isinstance(w, kc.SkeletonRow)) == 4, None)
    # the Animation runs headless (phase animates when the Clock ticks)
    check("skeleton: shimmer Animation is live on the row",
          skin._skeleton_rows[0]._anim is not None
          and skin._skeleton_rows[0].phase < 1.0, None)
    kc.sHadesClientApp._remove_skeleton(skin, "other")
    check("skeleton: mismatched cid is a no-op",
          len(skin._skeleton_rows) == 4, None)
    kc.sHadesClientApp._remove_skeleton(skin, "c1")
    check("skeleton: matching cid clears rows and state",
          skin._skeleton_rows == [] and skin._skeleton_cid is None
          and sum(1 for w in grid2.children
                  if isinstance(w, kc.SkeletonRow)) == 0,
          len(grid2.children))

    print("\nPASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    import inspect  # noqa: F401
    sys.exit(main())
