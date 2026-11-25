"""Channel list state: kinds, ordering, badges, mute, and the per-channel composer props.

Moved verbatim out of kivy_client.py's sHadesClientApp during
the phase-2 client split (methods keep their bodies, decorators and
call order; they reach the rest of the app through ``self`` exactly
as before). The app class inherits this mixin, so every method stays
reachable as ``app.<name>`` and ``kc.sHadesClientApp.<name>``.
"""

from client_widgets import (ChannelButton, VoiceRow,
                            _app_data_dir)
import RNS
import json
import os
import threading
from client_util import hexstr

# Kivy (guarded so the module can be imported for linting without Kivy present)
try:
    from kivy.clock import Clock
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e


class ChannelsMixin:
    """Channel list state: kinds, ordering, badges, mute, and the per-channel composer props."""

    def _channel_cid(self, name):
        """Convert a channel name to its RNS destination hash ID,
        matching the guild's ensure_channel() formula."""
        return hexstr(RNS.Identity.full_hash(b"main_guild:" + name.encode()))

    def _channel_name(self, cid):
        """Inverse of the channel map (name -> cid): the ticket push
        (v0.31) arrives keyed by cid, the fetch path speaks names."""
        for name, c in self._channels.items():
            if c == cid:
                return name
        return None

    def _channel_kind(self, cid):
        return self._channel_kinds.get(cid, "chat")

    def _sync_composer_props(self, name, cid):
        """P8: mirror the channel's post truth into the composer's
        kivy Properties (announce channels read-only for non-grants)."""
        kind = self._channel_kind(cid)
        if kind == "announce":
            can = self._channel_can_post.get(cid, True)
        else:
            can = True
        self.composer_readonly = (kind == "announce" and not can)
        self.composer_send_label = ("Announce" if kind == "announce"
                                    else "Send")

    def _refresh_channels_ui(self):
        self._ensure_general_channel()

    def _update_channel_badges(self):
        """Push unread counts onto the sidebar buttons (UI thread)."""
        try:
            grid = self.root.get_screen("main").ids.channel_grid
        except Exception:
            return
        for child in grid.children:
            if isinstance(child, ChannelButton):
                cid = self._channels.get(child.channel_name)
                unread = 0 if cid in self._muted_channels \
                    else self._channel_unread.get(cid, 0)
                child.badge = unread

    def _post_channel_reorder(self):
        """Send the visual order (top -> bottom) to the server once."""
        try:
            main = self.root.get_screen("main")
            grid = main.ids.channel_grid
        except Exception:
            return
        buttons = [w for w in reversed(grid.children)
                   if isinstance(w, ChannelButton)]
        order = []
        for b in buttons:
            cid = self._channels.get(b.channel_name)
            if cid and cid not in order:
                order.append(cid)

        def work():
            try:
                res = self.guild_api.channels_reorder(order)
            except Exception as e:
                msg = repr(e)
                Clock.schedule_once(
                    lambda dt: self._set_status("reorder failed: %s" % msg),
                    0)
                return
            if res.get("ok"):
                for i, cid in enumerate(res.get("order") or order, start=1):
                    self._channel_order[cid] = i
            else:
                Clock.schedule_once(
                    lambda dt: self._set_status("reorder: %s"
                                                % res.get("error")), 0)
        threading.Thread(target=work, daemon=True).start()

    def _add_channel(self, name):
        main = self.root.get_screen("main")
        grid = main.ids.channel_grid
        btn = ChannelButton(channel_name=name,
                            selected=name == main._current_channel)
        btn.bind(on_press=lambda inst: self._switch_channel(name))
        # task 7: build in server order. channels/list carries position;
        # Kivy layouts fill rows in reversed-children order, so the child
        # index of visual row i is (visible_count - i). Insert into the
        # first visual slot whose row holds a channel with a HIGHER position
        # (or an unpositioned one), else the end.
        my_pos = self._channel_pos.get(name)
        if my_pos is None:
            grid.add_widget(btn)
            return
        existing = [w for w in reversed(grid.children)
                    if isinstance(w, ChannelButton)]
        insert_at = None
        for visual_row, b in enumerate(existing):
            pos = self._channel_pos.get(b.channel_name)
            if pos is None or pos > my_pos:
                insert_at = len(existing) - visual_row
                break
        if insert_at is None:
            grid.add_widget(btn)
        else:
            grid.add_widget(btn, index=insert_at)
        self._voice_attach_row(name)

    def _voice_attach_row(self, name):
        """item 6: a voice-kind channel's roster row rides under its
        button. Called after the button exists; a late kind update
        (channels/list arrives after a seed) attaches here too."""
        cid = self._channels.get(name)
        if getattr(self, "_channel_kinds", {}).get(cid) != "voice":
            return
        if not hasattr(self, "_voice_row_for"):
            return
        if getattr(self, "_voice_rows", {}).get(cid) is not None:
            return     # already attached; roster pushes refresh it
        row = self._voice_row_for(name, cid)
        grid = self.root.get_screen("main").ids.channel_grid
        btn_idx = None
        for i, w in enumerate(grid.children):
            if isinstance(w, ChannelButton) and w.channel_name == name:
                btn_idx = i
                break
        if btn_idx is None:
            grid.add_widget(row)
            return
        # children render in reversed order under this grid's layout:
        # insert directly BEFORE the button's child index so the row
        # draws under it (same math family as _add_channel's insert)
        grid.add_widget(row, index=btn_idx)

    def _voice_row_slot(self, name):
        """Child index that visually lands the voice row directly under
        its channel button, derived from _add_channel's insert math."""
        grid = self.root.get_screen("main").ids.channel_grid
        existing = [w for w in reversed(grid.children)
                    if isinstance(w, (ChannelButton, VoiceRow))]
        my_pos = self._channel_pos.get(name)
        for visual_row, b in enumerate(existing):
            pos = self._channel_pos.get(getattr(b, "channel_name", ""))
            if pos is None or (my_pos is not None
                               and pos is not None and pos > my_pos):
                return len(existing) - visual_row
        return len(existing)

    def _has_channel_button(self, name):
        """True when the sidebar already holds a button for `name`."""
        try:
            grid = self.root.get_screen("main").ids.channel_grid
        except Exception:
            return False
        return any(isinstance(w, ChannelButton)
                   and w.channel_name == name for w in grid.children)

    def _remove_channel_button(self, name):
        """Remove a sidebar button (H7c seed reconcile).

        If the removed channel was the visible one, fall back to the
        first channel the guild actually has.
        """
        try:
            main = self.root.get_screen("main")
            grid = main.ids.channel_grid
        except Exception:
            return
        for child in list(grid.children):
            if isinstance(child, ChannelButton) \
                    and child.channel_name == name:
                grid.remove_widget(child)
        self._channels.pop(name, None)
        if getattr(main, "_current_channel", None) == name:
            names = list(self._channels)
            if names:
                self._switch_channel(names[0])

    def _toggle_mute_channel(self, name, cid):
        if cid in self._muted_channels:
            self._muted_channels.discard(cid)
            self._set_status("#%s unmuted" % name)
        else:
            self._muted_channels.add(cid)
            self._set_status("#%s muted (unread badge hidden)" % name)
        try:
            cfg = os.path.join(_app_data_dir(), "muted_channels.json")
            with open(cfg, "w") as f:
                json.dump(sorted(self._muted_channels), f)
        except Exception as e:
            print("muted-channel persist failed:", repr(e))
        self._update_channel_badges()

    def _can_create_channels(self):
        """UI gate: any of my roles carries MANAGE_CHANNEL or ADMIN (the
        Founder role carries both; server enforces the same bits)."""
        return bool(self.i_am_admin) or any(
            r.get("perms", 0) & (self.PERM_MANAGE_CHANNEL | self.PERM_ADMIN)
            for r in self.my_roles)

    def _can_manage_roles(self):
        """UI gate: any of my roles carries MANAGE_ROLES or ADMIN."""
        return any(r.get("perms", 0) & (self.PERM_MANAGE_ROLES |
                                        self.PERM_ADMIN)
                   for r in self.my_roles)

    def _ensure_general_channel(self):
        # only add once
        if not hasattr(self, "_channels_added"):
            self._channels_added = True
            for name in ("general", "announcements", "random"):
                self._add_channel(name)
