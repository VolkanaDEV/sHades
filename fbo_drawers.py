# fbo_drawers.py — FBO-composited drawers + root-slot quad (v4 medicine).
#
# Receipt basis (2026-11-24 tay's device, logs 4-6 + shots on v3):
#   - runtime child order is CORRECT (right_sidebar children[0], the
#     topmost slot; chat_screen bottom) - the paint-over is not a
#     tree-order fault. log.log_5 line 154, log.log_6 line 142.
#   - unique-texture batch separators in the drawers' canvas.before did
#     NOT stop the embed from painting over the open People panel
#     (log.log_6 139-141 armed; the v3 shots show the crossing).
#   - restore-sheet button labels blank while their textures test
#     healthy and forced rebinds run - rebind is falsified as a cure.
#
# Mechanism summary consistent with every receipt: the device's
# ANGLE/Vulkan (PowerVR) compose draws later-submitted groups under
# earlier content for some group pairs. The only client-side paint that
# cannot lose that race is a single quad whose texture was baked ahead
# of time, drawn in the ABSOLUTE LAST paint slot Kivy offers: the
# app root's canvas.after group.
#
# install(app) does, per drawer (left_clip / right_clip):
#   1. swap the clip's canvas to an Fbo -> the whole pane subtree bakes
#      into one texture (bg color via fbo clearcolor, content on top,
#      in one self-contained render);
#   2. clear the panel's canvas.before bg rect group (the fbo texture
#      now carries the bg color, so the window-side untextured rect
#      that could merge with chat-side rects is GONE);
#   3. draw a Color+Rectangle quad bound to the fbo texture in the
#      ROOT's canvas.after - the last group of the last frame order;
#   4. keep size/pos synced, re-bake on an interval (member list
#      updates, avatar loads) and on resize.
# Lazy: called at every client_diag report() until it sticks.
from kivy.clock import Clock
from kivy.graphics import Color, Fbo, Rectangle

_STATE = {"installed": False, "tried": False, "fbs": {}}

TARGET_IDS = ("chat_screen", "left_sidebar", "right_sidebar")
CLIP_IDS = ("left_clip", "right_clip")


def _walk_widgets(widget, out, cap=1200):
    if len(out) >= cap:
        return
    out.append(widget)
    for ch in getattr(widget, "children", []):
        _walk_widgets(ch, out, cap)


def _ids(root):
    widgets = []
    _walk_widgets(root, widgets)
    found = {}
    for w in widgets:
        try:
            for k, v in w.ids.items():
                if k not in found and v is not None:
                    found[k] = v
        except Exception:
            pass
    return found


def _sync_fbo(clip, fbo, quad, panel, tag):
    try:
        if quad is not None and clip.parent is not None:
            # panel coordinate: the quad lives at the clip's window pos
            quad.pos = clip.to_window(*clip.pos)
            quad.size = clip.size
        if clip.parent is None:
            return
        size = (max(int(clip.size[0]), 1), max(int(clip.size[1]), 1))
        if tuple(fbo.size) != size:
            fbo.size = size
        fbo.draw()
    except Exception as e:
        print("DIAG fbo-drawer sync %s error %s" % (tag, e), flush=True)


def install(app):
    """Compose both drawer panes into Fbos drawn in root canvas.after."""
    root = getattr(app, "root", None)
    if root is None:
        return
    found = _ids(root)
    clips = [i for i in CLIP_IDS if i in found]
    if not clips:
        return  # main screen not built yet (onboarding); retry later
    # the sidebar bg color for the fbo clear
    bg = (0.09, 0.09, 0.12, 1)
    try:
        tc = getattr(app, "theme_colors", None) or {}
        bg = tc.get("bg_sidebar", bg)
    except Exception:
        pass
    installed = 0
    for cid in clips:
        if cid in _STATE["fbs"]:
            installed += 1
            continue
        clip = found[cid]
        try:
            panel = clip.parent  # the BoxLayout drawer frame
            fbo = Fbo(size=(max(int(clip.width), 1), max(int(clip.height), 1)),
                      with_depthbuffer=False, with_stencilbuffer=False)
            fbo.clearcolor = bg
            # 1. bake the pane subtree: the clip's render context is now
            # the fbo; its children (stencil ops + pane + content) render
            # into the texture instead of the window
            clip.canvas = fbo
            # 2. kill the window-side untextured bg rect (merge surface):
            # the bg now rides inside the fbo via clearcolor
            try:
                panel.canvas.before.clear()
            except Exception:
                pass
            # 3. the quad in the ROOT's canvas.after: absolute last paint
            quad = Rectangle(texture=fbo.texture)
            with root.canvas.after:
                Color(1, 1, 1, 1)
                quad
            _STATE["fbs"][cid] = {"fbo": fbo, "quad": quad,
                                  "clip": clip, "panel": panel}
            clip.bind(size=lambda _w, _v, c=cid: _resize(c),
                      pos=lambda _w, _v, c=cid: _resize(c))
            _resize(cid)
            installed += 1
            print("DIAG fbo-drawer %s installed size=%s bg=%s"
                  % (cid, tuple(fbo.size), tuple(bg)), flush=True)
        except Exception as e:
            print("DIAG fbo-drawer %s error %s" % (cid, e), flush=True)
    if installed == len(clips):
        _STATE["installed"] = True
    # 4. re-bake interval (member updates, avatar texture loads)
    if not _STATE["tried"]:
        _STATE["tried"] = True
        Clock.schedule_interval(_tick, 1.0)


def _resize(cid):
    st = _STATE["fbs"].get(cid)
    if not st:
        return
    _sync_fbo(st["clip"], st["fbo"], st["quad"], st["panel"], cid)


def _tick(dt):
    if not _STATE["fbs"]:
        return
    for cid, st in list(_STATE["fbs"].items()):
        clip = st["clip"]
        try:
            if clip.parent is None:
                continue
            # skip closed/zero-width drawers (nothing to re-bake)
            if clip.width < 2 or clip.height < 2:
                continue
            _sync_fbo(clip, st["fbo"], st["quad"], st["panel"], cid)
        except Exception as e:
            print("DIAG fbo-drawer %s tick error %s" % (cid, e), flush=True)
