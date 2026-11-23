"""Live stream/polling/term: streamer, keepalive, frames, presence, update cycle.

Moved verbatim out of kivy_client.py's sHadesClientApp during
the phase-2 client split (methods keep their bodies, decorators and
call order; they reach the rest of the app through ``self`` exactly
as before). The app class inherits this mixin, so every method stays
reachable as ``app.<name>`` and ``kc.sHadesClientApp.<name>``.
"""

from RNS.vendor import umsgpack as msgpack
import offline_store
import threading
from client_util import (dlog, md_to_kivy)

# Kivy (guarded so the module can be imported for linting without Kivy present)
try:
    from kivy.app import App
    from kivy.uix.boxlayout import BoxLayout
    from kivy.clock import Clock
    from kivy.uix.label import Label
    from kivy.metrics import dp
    from kivy.metrics import sp
    KIVY = True
except Exception as _e:  # pragma: no cover - Kivy missing at import time
    KIVY = False
    _KIVY_IMPORT_ERROR = _e


class StreamMixin:
    """Live stream/polling/term: streamer, keepalive, frames, presence, update cycle."""

    def on_voice_state(self, cid, members):
        """Overridden by the app class: direct-link wiring + UI land here."""
        return

    def _start_poll(self):
        # v0.28: no polling loops anywhere in the client. This one-shot
        # keeps what the first poll tick did (seed the UI once); every
        # later update arrives as a server push on the link.
        def bootstrap(dt):
            self._ensure_general_channel()
            if not getattr(self, "_ui_populated", False):
                self._ui_populated = True
                try:
                    main = self.root.get_screen("main")
                    name = getattr(main, "_current_channel", "general")
                    self._fetch_messages(name)
                    self._refresh_members_ui(self._channel_cid(name))
                except Exception:
                    pass
        Clock.schedule_once(bootstrap, 0)

    def _start_streamer(self):
        """Establish (or re-establish) the stream subscription on the link.

        v0.31 truth: the server binds the channel AT SUBSCRIBE TIME and
        replaces the binding on every call (one subscription per link,
        api_v1_stream_subscribe) -- pointing _stream_cid alone changes
        nothing on the wire. Callers:

        - connect / guild-link attach: prime the entry channel;
        - _switch_channel: re-point at the newly opened channel, letting
          the server replace the binding;
        - keepalive self-heal: after SUB_TTL_S of quiet the subscription
          is purged server-side; the 45s keepalive sees {ok: 0, code:
          409} and calls this again. _stream_cursor still holds the
          watermark then, so the subscribe replays the gap from the
          event log instead of jumping to head.

        Setting the cursor here follows the same rule as upload offsets:
        a subscribe with a kept cursor cannot lose or double-deliver
        what the push path already rendered (render dedupes by msg_id).
        """
        if not self.guild_api or not self._stream_cid:
            return
        try:
            res = self.guild_api.stream_subscribe(
                self._stream_cid, cursor=self._stream_cursor)
            if res.get("ok"):
                if self._stream_cursor is None:
                    self._stream_cursor = res.get("seq", 0)
            else:
                self._stream_cursor = None
        except Exception as e:
            print("stream subscribe failed:", repr(e))
            self._stream_cursor = None

    def _start_stream_keepalive(self):
        """v0.31: keep the stream subscription alive through quiet minutes.

        Receipted mechanics: SUB_TTL_S = 120 (sHades.py) drops
        subscriptions the poll path has not touched; the push path only
        refreshes subscriptions it is actively pushing to (sHades.py
        _push_stream refreshes s["at"] on push), so a channel with no
        traffic for two minutes is purged and the app goes silent until
        restart. The poll is the designed refresh (the server's own
        SUB_TTL comment names it); delivery stays push-only -- the tick
        issues a zero-wait poll purely to touch the subscription and
        self-heals a 409 by resubscribing from the watermark. The old
        v0.28 "no polling loops" rule stands: nothing here polls for
        content; a silent channel costs one empty request per 45s.
        """
        if getattr(self, "_stream_tick_event", None) is not None:
            return
        self._stream_tick_event = Clock.schedule_interval(
            self._stream_tick, 45)

    def _stream_tick(self, dt):
        # the network call goes on a thread; the tick itself is instant
        threading.Thread(target=self._stream_keepalive_once,
                         daemon=True).start()

    def _stream_keepalive_once(self):
        api = self.guild_api
        if not api or not self._stream_cid:
            return
        try:
            res = api.stream_poll(cursor=self._stream_cursor, timeout_s=0)
        except Exception:
            self._ka_miss()
            return
        if res.get("ok"):
            self._ka_fail = 0
            self._stream_cursor = res.get("cursor", self._stream_cursor)
            events = res.get("events") or []
            if events:
                # a quiet-channel poll can win a race with the push; the
                # application is idempotent (render dedupes by msg_id)
                self._apply_stream_events(events)
        elif res.get("code") == 409:
            # subscription expired (two quiet minutes): re-subscribe from
            # the watermark so the TTL-eaten gap replays
            Clock.schedule_once(lambda dt: self._start_streamer(), 0)
        else:
            # v0.31.2 zombie detection: a non-ok poll that is NOT the 409
            # subscribe-expiry is a dead-or-zombie link. One miss stays a
            # miss (uploads can legitimately tie up a link); two in a row
            # through the 45s keepalive = the link no longer answers.
            self._ka_miss()

    def _release_stream(self):
        """Drop this link's stream subscription: switching to a push-less
        channel kind (site/term) releases the one-subscription-per-link
        slot instead of letting the old channel flow into the void."""
        api = self.guild_api
        if not api:
            return
        try:
            api.stream_unsubscribe()
        except Exception as e:
            dlog("stream unsubscribe failed: %r" % (e,))

    def _resync_stream_channel(self):
        """Fetch the subscribed channel's history after a gap/reset."""
        try:
            main = self.root.get_screen("main")
            name = getattr(main, "_current_channel", "general")
            if self._channel_cid(name) == self._stream_cid:
                self._fetch_messages(name)
        except Exception:
            pass

    def _on_push_frame(self, plaintext, packet=None):
        """v0.28 push: the guild delivers stream frames down the link the
        moment events exist (RNS.Packet(link, bytes), context NONE, which
        is the only traffic this callback receives). Runs on an RNS
        thread: threadsafe updates + Clock marshalling only."""
        try:
            frame = msgpack.unpackb(plaintext, raw=False)
            if not isinstance(frame, dict) or frame.get("k") != "stream":
                return
            self._handle_stream_frame(frame)
        except Exception as e:
            print("push frame failed:", repr(e))

    def _handle_stream_frame(self, frame):
        """v0.31: the push-receive path, split out of the raw callback so
        frame handling is directly testable."""
        if frame.get("reset"):
            # replay window rotated past our cursor: resync
            self._stream_cursor = None
            Clock.schedule_once(
                lambda dt: self._resync_stream_channel(), 0)
            return
        if frame.get("ticket"):
            # ticket push (v0.31): the frame carries seq + refs only, so
            # no MTU ceiling exists above it. Pull the delta now through
            # the resource path -- the same path poll and history use --
            # instead of receiving bodies that can overflow the packet.
            self._stream_cursor = frame.get("seq", self._stream_cursor)
            cid = frame.get("channel")
            Clock.schedule_once(lambda dt: self._ticket_pull(cid), 0)
            events = frame.get("events") or []
            if events:                     # legacy frame: still applies
                self._apply_stream_events(events)
            return
        events = frame.get("events") or []
        if events:
            self._stream_cursor = frame.get("seq", self._stream_cursor)
            self._apply_stream_events(events)

    def _ticket_pull(self, cid):
        """Delta-fetch the channel a ticket pointed at. Current channel
        refreshes in place; other subscribed channels update through the
        same render path (unread/older state), exactly like poll events
        did."""
        if not cid:
            return
        name = self._channel_name(cid)
        if name:
            self._fetch_messages(name)

    def _apply_stream_events(self, events):
        """Dispatch stream events. Runs on the stream thread: UI changes go
        through _fetch_messages (which Clock-schedules) or Clock directly."""
        cid = self._stream_cid
        touched = False
        for ev in events:
            kind = ev.get("kind")
            data = ev.get("data") or {}
            if kind == "msg":
                if (data.get("msg") or {}).get("channel_id") == cid:
                    touched = True
                if isinstance(data.get("msg"), dict):
                    self._queue_cache(data["msg"])
            elif kind in ("edit", "del"):
                touched = True
            elif kind == "presence":
                users = {}
                for u in data.get("users") or []:
                    if not isinstance(u, dict):
                        continue
                    uh = (u.get("user_hash") or "").lower()
                    if uh:
                        users[uh] = {"username": u.get("username") or "",
                                     "node_hash": u.get("node_hash")}
                self._active_users = users
                self._presence_ok = True
                Clock.schedule_once(lambda dt: self._refresh_presence_ui(), 0)
            elif kind == "site_patch":
                # a site resource or the manifest changed under us: re-theme
                # and re-render the page we are on
                if isinstance(data.get("manifest"), dict):
                    self._site_theme = data["manifest"].get("theme") or self._site_theme
                if self._site_current:
                    Clock.schedule_once(lambda dt: self._site_load_page(
                        self._site_current[0], self._site_current[1]), 0)
            elif kind == "reaction":
                # update the affected row's chips in place (react does not
                # bump the channel seq, so the reconciler will not refetch)
                def apply_rxn(dt):
                    self._set_row_reactions(data.get("msg_id"),
                                            self._reactions_from_counts(
                                                data.get("msg_id"), data))
                Clock.schedule_once(apply_rxn, 0)
            elif kind == "channel_deleted":
                # a channel (possibly the one we are watching) was deleted
                def apply_del(dt):
                    dead = data.get("channel")
                    self._channel_kinds.pop(dead, None)
                    self._channel_can_post.pop(dead, None)
                    self._channel_unread.pop(dead, None)
                    self._term_sessions.pop(dead, None)
                    name = data.get("name") or "channel"
                    self._refresh_channels_from_guild()
                    try:
                        main = self.root.get_screen("main")
                        cur = self._channel_cid(
                            getattr(main, "_current_channel", "general"))
                        if cur == dead:
                            self._set_status("#%s was deleted" % name)
                            self._switch_channel("general")
                    except Exception:
                        pass
                Clock.schedule_once(apply_del, 0)
            elif kind == "term_data":
                # console output (server-sanitised); "clear" wipes the buffer
                def apply_term(dt):
                    if not self._term_page:
                        return
                    if data.get("clear"):
                        self._term_page.clear()
                    elif data.get("text"):
                        self._term_page.append(data["text"])
                Clock.schedule_once(apply_term, 0)
            elif kind == "channels_changed":
                # v0.28: channel create/rename/topic arrive as pushes; the
                # 3s state poller that used to notice them is deleted.
                Clock.schedule_once(
                    lambda dt: self._refresh_channels_from_guild(), 0)
            elif kind == "voice_state":
                # tay 2026-11-23: a voice roster (who is in, each member's
                # peer voice destination, mute flags). The room listens on
                # the channel stream, so members get the roster on push.
                room_cid = data.get("channel") or cid
                rooms = dict(getattr(self, "_voice_rooms", {}) or {})
                rooms[room_cid] = [
                    m for m in (data.get("members") or [])
                    if isinstance(m, dict)]
                self._voice_rooms = rooms
                members = list(rooms[room_cid])
                Clock.schedule_once(
                    lambda dt, c=room_cid, m=members:
                    self.on_voice_state(c, m), 0)
            # "typing" events are consumed by a later phase
        if touched:
            # full refresh of the visible channel; _render_message dedupes
            # by msg_id, so events already rendered are skipped
            self._resync_stream_channel()

    def _start_presence(self):
        """v0.28: the presence POLL was deleted. Presence now arrives as a
        guild-wide push event on the link (server \"presence\" kind);
        _apply_stream_events applies it. Nothing runs on an interval here."""
        self._active_users = {}

    def _refresh_presence_ui(self):
        """Rebuild the member sidebar from the presence list.

        The list is *rebuilt*, never just recoloured: members come straight from
        the guild's active-user list, so a client that stops pinging is dropped
        by the guild's reaper and then disappears from the sidebar on the next
        presence tick. Nothing accumulates.
        """
        try:
            main = self.root.get_screen("main")
        except Exception:
            return
        self._refresh_members_ui_main(getattr(main, "_current_channel", "general"))

    def _open_term_channel(self, name, cid):
        """Open (or reuse) a guild console session and show the terminal."""
        self._term_page = None
        try:
            main = self.root.get_screen("main")
            grid = main.ids.message_grid
            grid.clear_widgets()
            self._msg_widgets.clear()
            page = TermPage()
            self._term_page = page
            page.append("(connecting to the guild console…)\n")
            grid.add_widget(page)
        except Exception as e:
            print("term open error:", repr(e))
        if self._term_sessions.get(cid):
            return   # session survives channel switches until idle expiry

        def work():
            try:
                res = self.guild_api.term_open(cid)
            except Exception as e:
                print("term open failed:", repr(e))
                res = {"ok": 0, "error": repr(e)}
            if res.get("ok"):
                self._term_sessions[cid] = res["session"]
            else:
                err = res.get("error", "unknown")
                Clock.schedule_once(lambda dt: self._set_status(
                    "console: %s" % err), 0)
        threading.Thread(target=work, daemon=True).start()

    def _send_term_input(self, text):
        """Send one console line; echo it locally, output rides the stream."""
        cid = self._stream_cid
        sid = self._term_sessions.get(cid) if cid else None
        if not sid:
            self._set_status("console not connected")
            return
        if self._term_page:
            self._term_page.append("> " + text + "\n")

        def work():
            try:
                res = self.guild_api.term_data(sid, text + "\n")
                if not res.get("ok"):
                    err = res.get("error", "unknown")
                    if err == "unknown session":
                        self._term_sessions.pop(cid, None)
                    Clock.schedule_once(lambda dt: self._set_status(
                        "console: %s" % err), 0)
            except Exception as e:
                print("term data failed:", repr(e))
        threading.Thread(target=work, daemon=True).start()

    def _apply_update(self, u):
        # edit / delete delta
        msg_id = u.get("msg_id")
        content = u.get("content", "")
        deleted = bool(u.get("deleted_at"))
        # fork merge: mirror the delta into the offline cache
        try:
            c_cid = u.get("channel_id") or self._channel_cid(getattr(
                self.root.get_screen("main"), "_current_channel", "general"))
            guild = self._cache_guild()
            if guild and c_cid and msg_id:
                threading.Thread(target=offline_store.apply_update,
                                 args=(self._offline_path(), guild, c_cid,
                                       msg_id, content, deleted),
                                 daemon=True).start()
        except Exception:
            pass
        # find widget
        row = self._msg_widgets.get(msg_id)
        if not row:
            return
        Clock.schedule_once(lambda dt: self._do_update(row, content, deleted), 0)

    def _do_update(self, row, content, deleted):
        if deleted:
            try:
                grid = self.root.get_screen("main").ids.message_grid
                grid.remove_widget(row)
            except Exception:
                pass
            self._msg_widgets.pop(row.msg_id, None)
        else:
            row.content = content
            row.content_markup = md_to_kivy(content)


# Widget classes that moved with this feature: they were
# defined under `if KIVY:` in kivy_client.py and stay that
# way here, so they only exist when Kivy does.
if KIVY:
    class TermPage(BoxLayout):
        """Guild console view for term channels: one scrolling output label.
        Output arrives as term_data stream events (server-sanitised text);
        input goes through the main input bar -> /api/v1/term/data."""

        def __init__(self, **kw):
            super().__init__(**kw)
            self.orientation = "vertical"
            self.size_hint_y = None
            self.padding = [dp(8), dp(8), dp(8), dp(8)]
            self.bind(minimum_height=lambda w, h: setattr(w, "height", h))
            app = App.get_running_app()
            term_text_color = app.theme_colors.get(
                'text_muted', (0.85, 0.88, 0.9, 1)) if app else (0.85, 0.88, 0.9, 1)
            self.output = Label(markup=False, halign="left",
                                color=term_text_color,
                                font_size=sp(13))
            self.output.size_hint_y = None
            self.output.bind(width=lambda inst, w: setattr(
                inst, "text_size", (w, None)))
            self.output.bind(texture_size=lambda inst, ts: setattr(
                inst, "height", max(dp(20), ts[1] if ts else 0)))
            self.add_widget(self.output)

        def append(self, text):
            """Append output; keep the buffer bounded (last ~8000 chars)."""
            buf = (self.output.text or "") + text
            if len(buf) > 8000:
                buf = buf[-8000:]
            self.output.text = buf

        def clear(self):
            self.output.text = ""
