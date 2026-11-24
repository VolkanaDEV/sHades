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
# Receipt shape, every line starts with DIAG or MEDICINE so the boot log
# greps clean:
#   DIAG <tag> root:<...> | main:<...> | sidebar-idx:<n> <m> | labels:<x/y
#   MEDICINE resize <wxH> checked=<n> rebuilt=<n> ids=[...]
from kivy.clock import Clock
from kivy.core.window import Window

_DIAG = {"scheduled": None}


def _ids(children, cap=14):
    out = []
    for ch in list(children)[:cap]:
        out.append("%s(%s)" % (type(ch).__name__, getattr(ch, "id", "-")))
    return out


def _walk_labels(widget, out, cap=400):
    if len(out) >= cap:
        return
    if isinstance(widget, __import__("kivy.uix.label", fromlist=["Label"]).Label):
        out.append(widget)
    for ch in getattr(widget, "children", []):
        _walk_labels(ch, out, cap)


def report(app, tag):
    """One receipt line: root order, main-screen order, sidebar indices."""
    try:
        root = app.root
        if root is None:
            print("DIAG %s root=None" % tag, flush=True)
            return
        parts = ["root:" + ",".join(_ids(root.children))]
        main = None
        try:
            main = root.get_screen("main")
        except Exception:
            main = None
        if main is not None:
            kids = list(main.children)
            parts.append("main:" + ",".join(_ids(kids)))
            # sidebar paint positions among the shared parent's children
            # (lower index in children == painted LATER in kivy, i.e. above)
            idx = {}
            for i, ch in enumerate(kids):
                for sub in getattr(ch, "children", []):
                    sid = getattr(sub, "id", None)
                    if sid in ("chat_screen", "left_sidebar", "right_sidebar"):
                        idx["%s-idx" % sid] = i
            parts.append("paint-order-children-descending:" + ",".join(
                "%s@%d" % (k, v) for k, v in sorted(idx.items())))
        labels = []
        if main is not None:
            _walk_labels(main, labels)
        blanks = [type(w).__name__ for w in labels
                  if getattr(w, "texture", None) is None]
        parts.append("labels:%d blank-tex:%d" % (len(labels), len(blanks)))
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
            if tex is None or getattr(tex, "width", 0) == 0:
                try:
                    w.texture_update()
                    rebuilt += 1
                    if len(names) < 12:
                        names.append(w.text[:24])
                except Exception:
                    pass
    except Exception as e:
        print("MEDICINE %s error:%s" % (tag, e), flush=True)
        return (checked, rebuilt)
    print("MEDICINE %s checked=%d rebuilt=%d sample=%s"
          % (tag, checked, rebuilt, names), flush=True)
    return (checked, rebuilt)


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
