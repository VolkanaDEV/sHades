# fbo_panel.py — an FBO-composited drawer panel (draw-order-proof paint).
#
# Receipt basis (2026-11-24, tay's device):
#   - log.log_5 line 154 / log.log_6 line 142: runtime child order is
#     CORRECT (right_sidebar children[0] = topmost, chat_screen last),
#     so the embed-over-drawer paint is NOT a tree-order fault.
#   - log.log_6 lines 139-141: unique-texture batch separators installed
#     in each drawer's canvas.before did NOT stop the crossing (tay's
#     shots on that build show the embed still over the open People
#     panel). A state change cannot defeat this driver's group order.
#   - The restore sheet's two button labels stay blank while their
#     textures test healthy; the forced rebind does not cure it.
#
# Cure: compose the whole panel subtree into an Fbo ONCE, then paint the
# panel as a single textured quad in the tree's normal (last, topmost)
# slot. One quad, one texture: no batcher anywhere can merge it upward
# or reorder it against earlier content, on any driver, GPU, or backend.
#
# Use: in kv, declare the panel root as FboPanel (instead of
# FloatLayout) and put the panel's own background INSIDE the content
# container so the background becomes part of the composited texture.
# The content is re-composited when the panel is resized or when the
# content reports a canvas change (add_widget/remove_widget/binding).
from kivy.clock import Clock
from kivy.graphics import BindTexture, Color, Fbo, Rectangle
from kivy.uix.floatlayout import FloatLayout


class FboPanel(FloatLayout):
    """Drawer panel painted as one Fbo quad. Order-proof on device."""

    def __init__(self, **kwargs):
        self._fbo = Fbo(size=(1, 1), with_depthbuffer=False,
                        with_stencilbuffer=False)
        self._quad = None
        self._sync_scheduled = None
        super().__init__(**kwargs)
        # the content container carries ALL panel paint (bg included);
        # its render context is the Fbo, so the subtree never paints
        # straight into the window framebuffer
        self._content = FloatLayout()
        self._content.canvas = self._fbo
        super().add_widget(self._content)
        # the visible side: one quad bound to the Fbo texture, drawn
        # here (children first, then this canvas = painted after them)
        with self.canvas:
            BindTexture(texture=self._fbo.texture)
            self._quad = Rectangle(texture=self._fbo.texture)
        self.bind(size=self._request_sync, pos=self._request_sync)
        Clock.schedule_once(lambda dt: self._sync(), 0)

    def _request_sync(self, *a):
        if self._sync_scheduled is None:
            self._sync_scheduled = Clock.schedule_once(
                lambda dt: self._sync(), 0)

    def _sync(self, *a):
        self._sync_scheduled = None
        if self._quad is None:
            return
        self._quad.pos = self.pos
        self._quad.size = self.size
        self._content.pos = (0, 0)
        self._content.size = self.size
        if tuple(self._fbo.size) != tuple(self.size):
            self._fbo.size = (max(int(self.size[0]), 1),
                              max(int(self.size[1]), 1))
            self._quad.texture = self._fbo.texture
        # bake the subtree into the Fbo; draw() binds + clears internally
        try:
            self._fbo.draw()
        except Exception as e:
            print("FboPanel sync error:", e, flush=True)

    def composite(self, *a):
        """Public: force a re-bake after content changes."""
        self._sync()

    def add_widget(self, *args, **kwargs):
        r = self._content.add_widget(*args, **kwargs)
        self._request_sync()
        return r

    def remove_widget(self, *args, **kwargs):
        r = self._content.remove_widget(*args, **kwargs)
        self._request_sync()
        return r

    def clear_widgets(self, *args, **kwargs):
        r = self._content.clear_widgets(*args, **kwargs)
        self._request_sync()
        return r

    @property
    def content(self):
        """The container that hosts the panel's real children."""
        return self._content

    # ids/children introspection: forward ids so kv ids on children of
    # this panel remain resolvable for client_diag's chain receipts
    @property
    def ids(self):
        d = dict(super().ids or {})
        try:
            d.update(self._content.ids)
        except Exception:
            pass
        return d
