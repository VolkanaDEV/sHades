# client_diag.py — draw-order + texture-state instrument (device receipts).
#
# Purpose: tay's verdict is that the device regressions are draw order,
# tarball-2 introduced. Static kv order is verified correct (drawers are
# the LAST children of the horizontal screen box: chat_screen 739 <
# left_sidebar 942 < right_sidebar 1124 in kv_md.kv, comment at line 733).
# The runtime/batch order on the phone's ANGLE/Vulkan path is what this
# module documents at every interesting moment, plus it carries the
# keyboard-resize texture-rebuild medicine (blank label recovery).
#
# v2 (log.log_4 instrumentation feedback):
#   - the v1 paint-order receipt printed EMPTY on device: kv `id:` lives
#     in the container's .ids dict, not on a .id attribute. v2 walks
#     .ids dicts and prints parent-index chains (lower index in a
#     parent's children list = painted ABOVE in kivy).
#   - the v1 label census counted empty-text icon labels as blank
#     (13-14 at boot, MEDICINE rebuilt them no-op with empty samples).
#     v2 counts/rebuilds only labels with NON-EMPTY text.
#
# Receipt shape, every line starts with DIAG or MEDICINE so the boot log
# greps clean:
#   DIAG <tag> labels:<x> blank-nonempty:<n> texts=[...]
#        | right_sidebar=BoxLayout@0<-BoxLayout@2<... | ...
#   MEDICINE <tag> checked=<n> rebuilt=<n> sample=[...]
from kivy.clock import Clock
from kivy.core.window import Window

_DIAG = {"scheduled": None}
TARGET_IDS = ("chat_screen", "left_sidebar", "right_sidebar")
_SEP = {}


def _walk_widgets(widget, out, cap=1200):
    if len(out) >= cap:
        return
    out.append(widget)
    for ch in getattr(widget, "children", []):
        _walk_widgets(ch, out, cap)


def _walk_labels(widget, out, cap=600):
    if len(out) >= cap:
        return
    try:
        from kivy.uix.label import Label
    except Exception:
        return
    if isinstance(widget, Label):
        out.append(widget)
    for ch in getattr(widget, "children", []):
        _walk_labels(ch, out, cap)


def _panel_chains(root):
    """Resolve kv ids -> widget -> parent index chains (paint order)."""
    widgets = []
    _walk_widgets(root, widgets)
    found = {}
    for w in widgets:
        try:
            for k, v in w.ids.items():
                if k in TARGET_IDS and k not in found and v is not None:
                    found[k] = v
        except Exception:
            pass
    chains = {}
    for k, w in found.items():
        hops = []
        cur = w
        while cur is not None and cur is not root:
            par = getattr(cur, "parent", None)
            if par is None:
                break
            try:
                idx = par.children.index(cur)
            except ValueError:
                break
            hops.append("%s@%d" % (type(par).__name__, idx))
            cur = par
        chains[k] = "<-".join(hops[:4])
    return chains


def install_batch_separators(app):
    """One unique-texture state change inside each drawer's canvas.before.

    Receipt basis (log.log_5 line 154, 2026-11-24): the RUNTIME child
    order is CORRECT on device (right_sidebar=FloatLayout@0 = topmost,
    left_sidebar@1, chat_screen@2 = bottom, all children of one
    BoxLayout in MainScreen) - yet the embed still paints over the
    sidebar in tay's shot. With child order ruled out by receipt, the
    remaining order surface is the render-batch layer: Kivy's batcher
    merges draws sharing identical state, and the PowerVR/ANGLE-Vulkan
    driver can then draw merged groups out of order. Inserting a 1px
    invisible rectangle bound to a UNIQUE per-drawer texture forces a
    state change before the drawer's paint, splitting the batches.
    If the paint-over disappears with this in, the mechanism is
    receipt-confirmed as batch-state reordering on the device driver.
    """
    widgets = []
    _walk_widgets(app.root, widgets)
    found = {}
    for w in widgets:
        try:
            for k, v in w.ids.items():
                if k in TARGET_IDS and k not in found and v is not None:
                    found[k] = v
        except Exception:
            pass
    for k, w in found.items():
        if k in _SEP:
            continue
        try:
            from kivy.graphics import Fbo, Color, Rectangle
            fbo = Fbo(size=(1, 1), with_depthbuffer=False,
                      with_stencilbuffer=False)
            with w.canvas.before:
                Color(1, 1, 1, 0)
                Rectangle(texture=fbo.texture, pos=(0, 0), size=(1, 1))
            _SEP[k] = fbo
            print("DIAG batch-separator %s tex_uid=%s"
                  % (k, getattr(fbo.texture, "uid", "?")), flush=True)
        except Exception as e:
            print("DIAG batch-separator %s error %s" % (k, e), flush=True)


def report(app, tag):
    """One receipt line: panel paint chains + non-empty blank census."""
    try:
        root = app.root
        if root is None:
            print("DIAG %s root=None" % tag, flush=True)
            return
        chains = _panel_chains(root)
        install_batch_separators(app)
        try:
            import fbo_drawers
            fbo_drawers.install(app)
        except Exception as e:
            print("DIAG fbo-drawers hook error %s" % e, flush=True)
        labels = []
        _walk_labels(root, labels)
        blanks = []
        for w in labels:
            tex = getattr(w, "texture", None)
            if tex is None and (w.text or "").strip():
                blanks.append((w.text or "")[:22])
        parts = ["labels:%d blank-nonempty:%d" % (len(labels), len(blanks))]
        if blanks:
            parts.append("texts=" + repr(blanks[:8]))
        for k in TARGET_IDS:
            if k in chains:
                parts.append("%s=%s" % (k, chains[k]))
        print("DIAG %s %s" % (tag, " | ".join(parts)), flush=True)
    except Exception as e:
        print("DIAG %s error:%s" % (tag, e), flush=True)


def medicine(app, tag):
    """Rebuild blank label textures (the keyboard-resize rebind pass)."""
    rebuilt, checked, names = 0, 0, []
    try:
        labels = []
        _walk_labels(app.root, labels, cap=600)
        for w in labels:
            checked += 1
            tex = getattr(w, "texture", None)
            if tex is None and (w.text or "").strip():
                try:
                    w.texture_update()
                    rebuilt += 1
                    if len(names) < 12:
                        names.append((w.text or "")[:24])
                except Exception:
                    pass
    except Exception as e:
        print("MEDICINE %s error:%s" % (tag, e), flush=True)
        return (checked, rebuilt)
    print("MEDICINE %s checked=%d rebuilt=%d sample=%s"
          % (tag, checked, rebuilt, names), flush=True)
    return (checked, rebuilt)


def force_rebind(widget, tag):
    """Walk a widget subtree and texture_update() every text Label.

    The sheet-open medicine: forced rebind at the exact moment a popup
    opens, regardless of current texture state (a blank-because-broken
    texture may still be non-None, and pixels are not readable cheaply
    per label on the device path). Cheap: a sheet carries few labels.
    """
    labels = []
    _walk_labels(widget, labels)
    rebuilt = 0
    names = []
    for w in labels:
        if (w.text or "").strip():
            try:
                w.texture_update()
                rebuilt += 1
                if len(names) < 10:
                    names.append((w.text or "")[:18])
            except Exception:
                pass
    print("MEDICINE %s force labels=%d rebuilt=%d sample=%s"
          % (tag, len(labels), rebuilt, names), flush=True)


def bind_resize(app):
    """Window size hook: keyboard ADJUST_RESIZE lands here on Android."""

    def _onsize(_win=None, _size=None):
        if _DIAG["scheduled"] is not None:
            return
        _DIAG["scheduled"] = Clock.schedule_once(_go, 0.25)

    def _go(dt):
        _DIAG["scheduled"] = None
        try:
            sz = Window.size
        except Exception:
            sz = None
        print("DIAG resize %s" % (sz,), flush=True)
        medicine(app, "resize")

    try:
        Window.bind(size=_onsize)
    except Exception as e:
        print("DIAG bind error %s" % e, flush=True)
    # one boot report after the first frames settle
    Clock.schedule_once(lambda dt: report(app, "boot"), 3)
    Clock.schedule_once(lambda dt: medicine(app, "boot"), 4)
