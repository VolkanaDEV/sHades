# fbo_drawers.py — FBO-composited drawers (canvas-object bake dance).
#
# Receipt basis (2026-11-24 tay's device, logs 4-7):
#   - runtime child order is CORRECT (right_sidebar children[0], the
#     topmost slot; chat_screen bottom) - the paint-over is not a
#     tree-order fault (log 5 line 154, log 6 line 142).
#   - unique-texture batch separators did NOT stop the crossing
#     (log 6 139-141 armed; v3 shots show the crossing).
#   - restore-sheet pair labels blank with healthy textures; rebind
#     cure falsified (logs 5/6, log 4 EMPTY paint receipt).
#   - v5 side-load: install crashed on kivy 2.3.1 ('Fbo' object has no
#     attribute 'clearcolor', log 7 lines 106-107) so the composite
#     never engaged; the stencil strip regressed the screens white
#     (clip-bounds rects painted as opaque fills) and was reverted.
#
# DESIGN (kivy's own export_to_png dance, applied live):
#   The clip's whole canvas group is DETACHED from the window's render
#   tree (parent.canvas.indexof / remove - the kivy export pattern).
#   Once detached the pane never paints into the window framebuffer
#   again - its draw stream is gone from the window batcher entirely.
#   Each re-bake follows kivy's export_as_image exactly:
#       fbo.add(clip.canvas); fbo.draw(); fbo.remove(clip.canvas)
#   The canvas object travels as ONE group instruction, so its compiled
#   state is valid and re-renders with CURRENT widget values; the bake
#   is clip-local by construction (its own transform chain; the parent
#   translation is not applied - verified on desktop: the subtree bakes
#   bg + label glyphs + button, stable across ticks).
#   The visible panel = ONE textured quad bound to the fbo texture,
#   drawn in the ROOT's canvas.after (the absolute last paint slot).
#   One quad, one texture: nothing for a batcher to merge or reorder,
#   on any driver, GPU, or backend.
from kivy.clock import Clock
from kivy.graphics import Color, Fbo, Rectangle

_STATE = {"fbs": {}, "tick_live": False}

CLIP_IDS = ("left_clip", "right_clip")


def _walk_widgets(widget, out, cap=1600):
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


def _detach(clip, cid):
    """Take the clip's whole canvas group out of the window render."""
    parent_w = clip.parent
    if parent_w is None:
        return
    try:
        idx = parent_w.canvas.indexof(clip.canvas)
        if idx > -1:
            parent_w.canvas.remove(clip.canvas)
            print("DIAG fbo-drawer %s detached from parent canvas idx=%s"
                  % (cid, idx), flush=True)
    except Exception as e:
        print("DIAG fbo-drawer %s detach error %s" % (cid, e), flush=True)


def _bake(clip, fbo):
    fbo.add(clip.canvas)
    fbo.draw()
    fbo.remove(clip.canvas)


def install(app):
    """Composite both drawer panes into Fbos drawn in root canvas.after."""
    root = getattr(app, "root", None)
    if root is None:
        return
    found = _ids(root)
    clips = [i for i in CLIP_IDS if i in found]
    if not clips:
        return  # main screen not built yet (onboarding); retry later
    installed = 0
    for cid in clips:
        if cid in _STATE["fbs"]:
            installed += 1
            continue
        clip = found[cid]
        try:
            _detach(clip, cid)
            fbo = Fbo(size=(max(int(clip.width), 1),
                            max(int(clip.height), 1)),
                      with_depthbuffer=False, with_stencilbuffer=False)
            # visible side: ONE quad in the ROOT's canvas.after
            with root.canvas.after:
                Color(1, 1, 1, 1)
                q = Rectangle(texture=fbo.texture)
            st = {"fbo": fbo, "quad": q, "clip": clip}
            _STATE["fbs"][cid] = st
            clip.bind(size=lambda _w, _v, c=cid: _resize(c),
                      pos=lambda _w, _v, c=cid: _pos_tick(c))
            _sync(st, cid, bake=True)
            installed += 1
            print("DIAG fbo-drawer %s installed fbo=%s quad-in-root-after"
                  % (cid, tuple(fbo.size)), flush=True)
        except Exception as e:
            print("DIAG fbo-drawer %s install error %s" % (cid, e),
                  flush=True)
    if not _STATE["tick_live"]:
        _STATE["tick_live"] = True
        # cheap 20/s pos sync; re-bakes piggyback on size changes and
        # on the 1s content refresh
        Clock.schedule_interval(_tick, 0.05)
        Clock.schedule_interval(_reframe, 1.0)


def _resize(cid):
    st = _STATE["fbs"].get(cid)
    if not st:
        return
    _tick(0.0)


def _pos_tick(cid):
    st = _STATE["fbs"].get(cid)
    if not st:
        return
    _tick(0.0)


def _sync(st, cid, bake=False):
    clip = st["clip"]
    fbo = st["fbo"]
    quad = st["quad"]
    try:
        if clip.parent is None:
            # drawer removed: park the quad off-frame
            quad.size = (0, 0)
            return
        size = (max(int(clip.width), 1), max(int(clip.height), 1))
        if tuple(fbo.size) != size:
            fbo.size = size
            quad.texture = fbo.texture
            bake = True
        quad.pos = clip.to_window(*clip.pos)
        quad.size = clip.size
        if bake and clip.width >= 2:
            # a closed slide keeps width 0 -> skip baking, sync pos only
            _bake(clip, fbo)
    except Exception as e:
        print("DIAG fbo-drawer %s sync error %s" % (cid, e), flush=True)


def _tick(dt):
    if not _STATE["fbs"]:
        return
    for cid, st in list(_STATE["fbs"].items()):
        try:
            clip = st["clip"]
            quad = st["quad"]
            if clip.parent is None:
                quad.size = (0, 0)
                continue
            # pos sync every tick (cheap); re-bake when size changed
            size = (max(int(clip.width), 1), max(int(clip.height), 1))
            if tuple(st["fbo"].size) != size:
                _sync(st, cid, bake=True)
            else:
                quad.pos = clip.to_window(*clip.pos)
                if clip.width >= 2:
                    quad.size = clip.size
        except Exception as e:
            print("DIAG fbo-drawer %s tick error %s" % (cid, e), flush=True)


def _reframe(dt):
    """1s content refresh: member joins/avatars/spawned rows."""
    if not _STATE["fbs"]:
        return
    for cid, st in list(_STATE["fbs"].items()):
        try:
            _sync(st, cid, bake=True)
        except Exception as e:
            print("DIAG fbo-drawer %s reframe error %s" % (cid, e),
                  flush=True)
