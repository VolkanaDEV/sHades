#!/usr/bin/env python3
"""ui_server_config.py -- tabbed server config screen (U6 server side).

Own module: it must not import kivy_client (import cycle), so the client
object is passed in and only its guild_api / perm flags are used. Tabs
appear only when the caller's roles carry the matching bit; the server
enforces everything regardless (client gating is UX, not security).

Tabs are a plain button row (the plan's Android fallback; MDTabs stays
out of 0.33):
  General    admin only: guild name / motd / slowmode / registration,
             guild icon (blob store + admin/config), audit log view
  Membership admin only: moderator + admin whitelist (admin_mods)
  Channels   MANAGE_CHANNEL carriers: topic edit, per-role VIEW/POST
             overwrites, delete
  Roles      MANAGE_ROLES carriers: list/create/delete, PERM bitmask
             editor, assign/unassign
  Invites    admin only: list/create/revoke, label, max_uses, expiry,
             optional channel target

Every guild call blocks on the RNS link for up to the request timeout
(10 s), so loaders run on worker threads and rebuild widgets on the main
thread through Clock.schedule_once; load_gen guards against out-of-order
responses (the pattern proven in the U8 popup this screen replaces).
"""
import os
import threading
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.gridlayout import GridLayout
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

# mirrors the server PERMS dict (sHades: view..admin)
PERM_BITS = (
    ("VIEW", 1),
    ("POST", 2),
    ("MANAGE_MESSAGES", 4),
    ("MANAGE_CHANNEL", 8),
    ("MANAGE_ROLES", 16),
    ("ADMIN", 32),
)


def _scaled(v):
    return dp(v) * 1.0


def _row_label(text, w=0.5, h=26, fs=None):
    fs = fs or sp(11)
    lbl = Label(text=text, size_hint_x=w, halign="left", valign="middle",
                size_hint_y=None, height=_scaled(h), font_size=fs,
                color=(0.85, 0.87, 0.9, 1))
    lbl.bind(size=lambda inst, s: setattr(inst, "text_size", s))
    return lbl


def _btn(text, width=70, h=30, fs=None):
    fs = fs or sp(11)
    return Button(text=text, size_hint_x=None, width=_scaled(width),
                  size_hint_y=None, height=_scaled(h), font_size=fs,
                  pos_hint={"center_y": 0.5})


def _int_in(txt):
    try:
        return int((txt or "").strip() or 0)
    except ValueError:
        return 0


def _role_id_from(txt):
    try:
        return int((txt or "").strip())
    except (TypeError, ValueError):
        return None


class _Tab:
    """Shared scaffold: scroll grid + worker-thread fetch/post pattern."""
    title = ""

    def __init__(self, screen, client):
        self.screen = screen
        self.client = client
        self.load_gen = [0]
        self.grid = None
        self.box = None

    # -- widget scaffolding --------------------------------------------------
    def make_content(self):
        self.box = BoxLayout(orientation="vertical", spacing=dp(8))
        self._build_controls()
        scroll = ScrollView(size_hint=(1, 1))
        grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(3),
                          padding=[dp(2)] * 2)
        grid.bind(minimum_height=grid.setter("height"))
        scroll.add_widget(grid)
        self.grid = grid
        self.box.add_widget(scroll)
        self.reload()
        return self.box

    def _build_controls(self):
        """Top control rows live above the scroll view."""

    # -- network plumbing ----------------------------------------------------
    def reload(self):
        raise NotImplementedError

    def _fetch(self, view_fn, on_ok):
        gen = self.load_gen[0]

        def work():
            try:
                res = view_fn()
            except Exception as e:
                self._status("%r" % e)
                return
            if gen != self.load_gen[0]:
                return  # a newer load superseded this one
            Clock.schedule_once(lambda dt: on_ok(res), 0)
        threading.Thread(target=work, daemon=True).start()

    def _post(self, do_fn, on_ok=None, label="save"):
        def work():
            try:
                res = do_fn()
            except Exception as e:
                self._status("%s: %r" % (label, e))
                return
            if res.get("ok"):
                self._status("%s ok" % label)
                if on_ok:
                    Clock.schedule_once(lambda dt: on_ok(), 0)
            else:
                self._status("%s: %s" % (label, res.get("error")))
        threading.Thread(target=work, daemon=True).start()

    def _status(self, txt):
        # v0.36.10 (tay's boot_15 receipt): the admin picks ran silent --
        # _set_status mirrors to connect+profile only, and tay stands on
        # the admin screen, so "icon set"/"link dead" never surfaced.
        # Mirror onto config_status on THIS screen; keep the mirrors.
        try:
            lbl = self.screen.ids.get("config_status")
            if lbl is not None:
                lbl.text = str(txt)
        except Exception:
            pass
        try:
            self.screen.client._set_status(txt)
        except Exception:
            pass


# ---------------------------------------------------------------------------
class GeneralTab(_Tab):
    title = "General"

    CONFIG_FIELDS = ("guild_name", "motd", "slowmode", "registration")

    def _build_controls(self):
        self.entries = {}

        m_row = BoxLayout(size_hint_y=None, height=_scaled(32), spacing=dp(4))
        self.motd_entry = TextInput(hint_text="motd", multiline=False,
                                    size_hint_x=0.6)
        self.entries["motd"] = self.motd_entry
        m_save = _btn("save", 70, 30)
        m_save.bind(on_press=lambda _i: self.do_save_key("motd"))
        m_row.add_widget(self.motd_entry)
        m_row.add_widget(m_save)
        self.box.add_widget(m_row)

        n_row = BoxLayout(size_hint_y=None, height=_scaled(32), spacing=dp(4))
        n_entry = TextInput(hint_text="guild name", multiline=False,
                            size_hint_x=0.6)
        self.entries["guild_name"] = n_entry
        n_save = _btn("save", 70, 30)
        n_save.bind(on_press=lambda _i: self.do_save_key("guild_name"))
        n_row.add_widget(n_entry)
        n_row.add_widget(n_save)
        self.box.add_widget(n_row)

        s_row = BoxLayout(size_hint_y=None, height=_scaled(32), spacing=dp(4))
        s_entry = TextInput(hint_text="slowmode (s, 0=off)", multiline=False,
                            size_hint_x=0.6)
        self.entries["slowmode"] = s_entry
        s_save = _btn("save", 70, 30)
        s_save.bind(on_press=lambda _i: self.do_save_key("slowmode"))
        s_row.add_widget(s_entry)
        s_row.add_widget(s_save)
        self.box.add_widget(s_row)

        r_row = BoxLayout(size_hint_y=None, height=_scaled(32), spacing=dp(4))
        r_entry = TextInput(hint_text="registration: open|code",
                            multiline=False, size_hint_x=0.6)
        self.entries["registration"] = r_entry
        r_save = _btn("save", 70, 30)
        r_save.bind(on_press=lambda _i: self.do_save_key("registration"))
        r_row.add_widget(r_entry)
        r_row.add_widget(r_save)
        self.box.add_widget(r_row)

        # attachments persistence (P3): the live mode, one tap to flip.
        # Ephemeral means bytes ride the server RAM registry (TTL +
        # cap) instead of disk: new uploads only, flip-back is safe.
        a_row = BoxLayout(size_hint_y=None, height=_scaled(32),
                          spacing=dp(4))
        self.att_state = _row_label("attachments: saved", w=0.35)
        self.att_saved_btn = _btn("saved", 80, 30)
        self.att_eph_btn = _btn("ephemeral", 110, 30)
        self.att_saved_btn.bind(
            on_press=lambda _i: self._save_attachments("saved"))
        self.att_eph_btn.bind(
            on_press=lambda _i: self._save_attachments("ephemeral"))
        a_row.add_widget(self.att_state)
        a_row.add_widget(self.att_saved_btn)
        a_row.add_widget(self.att_eph_btn)
        self.box.add_widget(a_row)

        icon_row = BoxLayout(size_hint_y=None, height=_scaled(56),
                             spacing=dp(8))
        self.icon_widget = Image(size_hint_x=None, width=_scaled(56),
                                 allow_stretch=True, keep_ratio=True)
        pick = _btn("set icon...", 110, 30)
        clear = _btn("clear", 60, 30)
        pick.bind(on_press=lambda _i: self._open_icon_chooser())
        clear.bind(on_press=lambda _i: self._clear_icon())
        icon_row.add_widget(self.icon_widget)
        icon_row.add_widget(pick)
        icon_row.add_widget(clear)
        self.box.add_widget(icon_row)

        # P10b: banner preview strip -- the picked/committed banner is
        # visible in the tab instead of a blind flip
        self.banner_widget = Image(size_hint_y=None,
                                   height=_scaled(56),
                                   allow_stretch=True, keep_ratio=False)
        self.box.add_widget(self.banner_widget)
        banner_row = BoxLayout(size_hint_y=None, height=_scaled(40),
                               spacing=dp(8))
        bpick = _btn("set banner...", 110, 30)
        bclear = _btn("clear", 60, 30)
        bpick.bind(on_press=lambda _i: self._open_banner_chooser())
        bclear.bind(on_press=lambda _i: self._clear_banner())
        banner_row.add_widget(bpick)
        banner_row.add_widget(bclear)
        self.box.add_widget(banner_row)

    def do_save_key(self, key):
        entry = self.entries[key]
        val = entry.text  # read on the main thread; the worker must not

        def job():
            return self.client.guild_api.admin_config(key, val)
        self._post(job, on_ok=self.reload, label="save %s" % key)

    def _save_attachments(self, value):
        # main-thread decision; the worker only carries the string,
        # same shape as do_save_key
        def job():
            return self.client.guild_api.admin_config("attachments", value)
        self._post(job, on_ok=self.reload,
                   label="set attachments " + value)

    def _reflect_attachments(self, mode):
        mode = mode if mode in ("saved", "ephemeral") else "saved"
        self.att_state.text = "attachments: " + mode
        # the ACTIVE mode is disabled: pressing it would post a no-op
        self.att_saved_btn.disabled = (mode == "saved")
        self.att_eph_btn.disabled = (mode == "ephemeral")

    def do_save_motd(self):
        self.do_save_key("motd")

    def reload(self):
        self.load_gen[0] += 1
        self._fetch(lambda: self.client.guild_api.admin_config(),
                    self._render)

    def _render(self, res):
        if not res.get("ok"):
            self._status("config: %s" % res.get("error", "failed"))
            return
        cfg = res.get("config", {})
        for key in self.CONFIG_FIELDS:
            if key in cfg:
                try:
                    self.entries[key].text = str(cfg.get(key, ""))
                except Exception:
                    pass
        self._reflect_attachments(cfg.get("attachments", "saved"))
        icon = (cfg.get("guild_icon") or "").strip()
        if icon:
            self.screen.load_icon_into(self.icon_widget, icon)
        else:
            self.icon_widget.texture = None
        banner = (cfg.get("banner") or "").strip()
        if banner:
            self.screen.load_icon_into(self.banner_widget, banner)
        else:
            self.banner_widget.texture = None
        self._load_audit()

    def _load_audit(self):
        def work():
            return self.client.guild_api.admin_audit(50)

        def on_ok(res):
            if not res.get("ok"):
                return
            self.grid.clear_widgets()
            for e in res.get("entries") or []:
                self.grid.add_widget(_row_label(
                    "%s  %s  %s" % (e.get("action", ""),
                                    (e.get("target") or "")[:16],
                                    (e.get("detail") or "")[:40]),
                    h=20, fs=sp(10)))
        self._fetch(work, on_ok)

    # -- icon picker ---------------------------------------------------------
    def _open_icon_chooser(self):
        app = App.get_running_app()
        if app is not None and getattr(
                app, "_android_platform", lambda: False)():
            app.open_file_chooser(purpose="icon")
            return
        box = BoxLayout(orientation="vertical")
        chooser = FileChooserListView(
            path=os.path.expanduser("~"),
            filters=["*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif"])
        cancel = _btn("cancel", 90, 30)
        pick = _btn("use this file", 130, 30)
        buttons = BoxLayout(size_hint_y=None, height=_scaled(34),
                            spacing=dp(4))
        buttons.add_widget(cancel)
        buttons.add_widget(pick)
        box.add_widget(chooser)
        box.add_widget(buttons)
        popup = Popup(title="choose an icon", content=box,
                      size_hint=(0.85, 0.85))
        self._icon_chooser_popup = popup
        cancel.bind(on_press=lambda _i: popup.dismiss())
        pick.bind(on_press=lambda _i: self._icon_file_selected(chooser,
                                                               popup))
        popup.open()

    def _icon_file_selected(self, chooser, popup):
        selection = chooser.selection
        popup.dismiss()
        if selection:
            self.screen._apply_icon_file(selection[0])

    def _clear_icon(self):
        def job():
            return self.client.guild_api.admin_config("guild_icon", "")
        self._post(job, on_ok=self.reload, label="clear icon")

    # -- banner picker (P5): rides the guild-icon chooser flow ---------
    def _open_banner_chooser(self):
        app = App.get_running_app()
        if app is not None and getattr(
                app, "_android_platform", lambda: False)():
            app.open_file_chooser(purpose="banner")
            return
        box = BoxLayout(orientation="vertical")
        chooser = FileChooserListView(
            path=os.path.expanduser("~"),
            filters=["*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif"])
        cancel = _btn("cancel", 90, 30)
        pick = _btn("use this file", 130, 30)
        buttons = BoxLayout(size_hint_y=None, height=_scaled(34),
                            spacing=dp(4))
        buttons.add_widget(cancel)
        buttons.add_widget(pick)
        box.add_widget(chooser)
        box.add_widget(buttons)
        popup = Popup(title="choose a banner", content=box,
                      size_hint=(0.85, 0.85))
        self._banner_chooser_popup = popup
        cancel.bind(on_press=lambda _i: popup.dismiss())
        pick.bind(on_press=lambda _i: self._banner_file_selected(chooser,
                                                                 popup))
        popup.open()

    def _banner_file_selected(self, chooser, popup):
        selection = chooser.selection
        popup.dismiss()
        if selection:
            self.screen._apply_banner_file(selection[0])

    def _clear_banner(self):
        def job():
            return self.client.guild_api.admin_config("banner", "")
        self._post(job, on_ok=self.reload, label="clear banner")


# ---------------------------------------------------------------------------
class MembershipTab(_Tab):
    title = "Membership"

    def _build_controls(self):
        row = BoxLayout(size_hint_y=None, height=_scaled(32), spacing=dp(4))
        self.hash_entry = TextInput(hint_text="user hash (32 hex)",
                                    multiline=False, size_hint_x=0.6)
        addm = _btn("add mod", 80, 30)
        adda = _btn("add admin", 90, 30)
        addm.bind(on_press=lambda _i: self._do_op("add"))
        adda.bind(on_press=lambda _i: self._do_op("admin_add"))
        row.add_widget(self.hash_entry)
        row.add_widget(addm)
        row.add_widget(adda)
        self.box.add_widget(row)

    def reload(self):
        self.load_gen[0] += 1
        self._fetch(lambda: self.client.guild_api.admin_mods("list"),
                    self._render)

    def _render(self, res):
        if not res.get("ok"):
            self._status("membership: %s" % res.get("error", "failed"))
            return
        mods = res.get("mods") or []
        admins = res.get("admins") or []
        self.grid.clear_widgets()
        for h in sorted(set(mods) | set(admins)):
            kind = "admin" if h in admins else "mod"
            r = BoxLayout(size_hint_y=None, height=_scaled(26), spacing=dp(4))
            r.add_widget(_row_label("%s  %s..." % (kind, h[:12]), w=0.7))
            rm = _btn("remove", 70, 26, sp(10))
            rm.bind(on_press=lambda _i, hh=h, kk=kind: self._do_op(
                "admin_remove" if kk == "admin" else "remove", hh))
            r.add_widget(rm)
            self.grid.add_widget(r)

    def _do_op(self, op, target_hash=None):
        h = (target_hash or (self.hash_entry.text or "")).strip().lower()
        if not h:
            return

        def job():
            return self.client.guild_api.admin_mods(op, h)
        self._post(job, on_ok=self.reload, label=op)
        if not target_hash:
            self.hash_entry.text = ""


# ---------------------------------------------------------------------------
class ChannelsTab(_Tab):
    title = "Channels"

    def reload(self):
        self.load_gen[0] += 1
        self._fetch(self.client.guild_api.channels_list, self._render)

    def _render(self, res):
        if not res.get("ok"):
            self._status("channels: %s" % res.get("error", "failed"))
            return
        self.grid.clear_widgets()
        for ch in res.get("channels") or []:
            self.grid.add_widget(self._channel_row(ch))

    def _channel_row(self, ch):
        cid = ch["id"]
        row = BoxLayout(size_hint_y=None, height=_scaled(30), spacing=dp(2))
        row.add_widget(_row_label("#%s  %s" % (ch["name"],
                                               ch.get("kind", "chat")),
                                  w=0.35))
        topic = TextInput(text=ch.get("topic") or "", multiline=False,
                          size_hint_x=0.35)
        save_topic = _btn("topic", 60, 26, sp(10))
        save_topic.bind(on_press=lambda _i, c=cid, t=topic:
                        self._save_topic(c, t.text))
        ed = _btn("perms...", 70, 26, sp(10))
        ed.bind(on_press=lambda _i: self._open_perm_editor(ch))
        row.add_widget(topic)
        row.add_widget(save_topic)
        row.add_widget(ed)
        if getattr(self.client, "i_am_admin", False):
            dele = _btn("delete", 60, 26, sp(10))
            dele.bind(on_press=lambda _i, c=cid, n=ch["name"]:
                      self._delete(c, n))
            row.add_widget(dele)
        return row

    def _save_topic(self, cid, text):
        def job():
            return self.client.guild_api.channels_patch(cid, topic=text or "")
        self._post(job, on_ok=self.reload, label="topic")

    def post_overwrites(self, cid, role_id, view=True, post=True):
        perms = (1 if view else 0) | (2 if post else 0)

        def job():
            return self.client.guild_api.channels_patch(cid, role_id=role_id,
                                                        perms=perms)
        self._post(job, on_ok=self.reload, label="overwrites")

    def _open_perm_editor(self, ch):
        """Mini ACL editor: per-role VIEW/POST toggles -> channels/patch."""
        cid = ch["id"]
        box = BoxLayout(orientation="vertical", spacing=dp(4), padding=dp(6))
        scroll = ScrollView(size_hint=(1, 1))
        grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(2))
        grid.bind(minimum_height=grid.setter("height"))
        scroll.add_widget(grid)
        box.add_widget(scroll)
        state = {}

        def fill(res):
            if not res.get("ok"):
                return
            head = BoxLayout(size_hint_y=None, height=_scaled(26),
                             spacing=dp(2))
            head.add_widget(_row_label("role", w=0.45))
            head.add_widget(_row_label("VIEW", w=0.15))
            head.add_widget(_row_label("POST", w=0.15))
            grid.add_widget(head)
            for role in res.get("roles") or []:
                row = BoxLayout(size_hint_y=None, height=_scaled(26),
                                spacing=dp(2))
                row.add_widget(_row_label(role["name"], w=0.45))
                cv = CheckBox(size_hint_x=0.15)
                cp = CheckBox(size_hint_x=0.15)
                over = next((o for o in ch.get("overwrites") or []
                             if o.get("role_id") == role["role_id"]), None)
                cv.active = bool((over or {}).get("view"))
                cp.active = bool((over or {}).get("post"))
                state[role["role_id"]] = (cv, cp)
                row.add_widget(cv)
                row.add_widget(cp)
                grid.add_widget(row)

        def do_apply():
            entries = []
            for rid, (cv, cp) in state.items():
                if cv.active or cp.active:
                    entries.append({"role_id": rid,
                                    "view": 1 if cv.active else 0,
                                    "post": 1 if cp.active else 0})

            def job():
                return self.client.guild_api.channels_patch(
                    cid, overwrites=entries)
            self._post(job, on_ok=self.reload, label="overwrites")
            popup.dismiss()

        apply_btn = _btn("apply", 90, 30)
        apply_btn.bind(on_press=lambda _i: do_apply())
        box.add_widget(apply_btn)
        popup = Popup(title="permissions: #%s" % ch["name"], content=box,
                      size_hint=(0.85, 0.8))
        popup.open()
        self._fetch(lambda: self.client.guild_api.roles_list(), fill)

    def _delete(self, cid, name):
        def job():
            return self.client.guild_api.channels_delete(cid)
        self._post(job, on_ok=self.reload, label="delete %s" % name)


# ---------------------------------------------------------------------------
class RolesTab(_Tab):
    title = "Roles"

    def _build_controls(self):
        self._perm_checks = {}
        row = BoxLayout(size_hint_y=None, height=_scaled(32), spacing=dp(4))
        self.new_role_name = TextInput(hint_text="new role name",
                                       multiline=False, size_hint_x=0.25)
        row.add_widget(self.new_role_name)
        for label, _bit in PERM_BITS:
            cb = CheckBox(size_hint_x=None, width=_scaled(26),
                          size_hint_y=None, height=_scaled(32),
                          pos_hint={"center_y": 0.5})
            self._perm_checks[label] = cb
            row.add_widget(cb)
        create = _btn("create", 70, 30)
        create.bind(on_press=lambda _i: self.do_create_role())
        row.add_widget(create)
        self.box.add_widget(row)

        assign_row = BoxLayout(size_hint_y=None, height=_scaled(32),
                               spacing=dp(4))
        self.assign_user = TextInput(hint_text="user hash", multiline=False,
                                     size_hint_x=0.55)
        self.assign_role = TextInput(hint_text="role id", multiline=False,
                                     size_hint_x=0.25)
        go = _btn("assign", 70, 30)
        go.bind(on_press=lambda _i: self.do_assign(
            _role_id_from(self.assign_role.text), self.assign_user.text))
        go2 = _btn("unassign", 80, 30)
        go2.bind(on_press=lambda _i: self.do_unassign(
            _role_id_from(self.assign_role.text), self.assign_user.text))
        assign_row.add_widget(self.assign_user)
        assign_row.add_widget(self.assign_role)
        assign_row.add_widget(go)
        assign_row.add_widget(go2)
        self.box.add_widget(assign_row)

    def set_perm_checks(self, mapping):
        for label, val in mapping.items():
            cb = self._perm_checks.get(label)
            if cb is not None:
                cb.active = bool(val)

    def do_create_role(self):
        name = (self.new_role_name.text or "").strip()
        if not name:
            return
        mask = 0
        for label, bit in PERM_BITS:
            if self._perm_checks[label].active:
                mask |= bit

        def job():
            return self.client.guild_api.roles_create(name, perms=mask)
        self._post(job, on_ok=self.reload, label="role %s" % name)
        self.new_role_name.text = ""

    def do_assign(self, role_id, user):
        user = (user or "").strip().lower()
        if role_id is None or not user:
            return

        def job():
            return self.client.guild_api.roles_assign(role_id, user)
        self._post(job, on_ok=self.reload, label="assign")

    def do_unassign(self, role_id, user):
        user = (user or "").strip().lower()
        if role_id is None or not user:
            return

        def job():
            return self.client.guild_api.roles_unassign(role_id, user)
        self._post(job, on_ok=self.reload, label="unassign")

    def reload(self):
        self.load_gen[0] += 1
        self._fetch(self.client.guild_api.roles_list, self._render)

    def _render(self, res):
        if not res.get("ok"):
            self._status("roles: %s" % res.get("error", "failed"))
            return
        self.grid.clear_widgets()
        for role in res.get("roles") or []:
            bits = ", ".join(lbl for lbl, bit in PERM_BITS
                             if role["perms"] & bit) or "no perms"
            row = BoxLayout(size_hint_y=None, height=_scaled(26),
                            spacing=dp(2))
            row.add_widget(_row_label(
                "%s  [%s]  members=%s" % (role["name"], bits,
                                          role.get("members", "?")), w=0.85))
            dele = _btn("delete", 60, 26, sp(10))
            dele.bind(on_press=lambda _i, rid=role["role_id"]:
                      self._delete(rid))
            row.add_widget(dele)
            self.grid.add_widget(row)

    def _delete(self, role_id):
        def job():
            return self.client.guild_api.roles_delete(role_id)
        self._post(job, on_ok=self.reload, label="delete role")


# ---------------------------------------------------------------------------
class InvitesTab(_Tab):
    title = "Invites"

    def _build_controls(self):
        row = BoxLayout(size_hint_y=None, height=_scaled(32), spacing=dp(4))
        self.label_in = TextInput(hint_text="label (optional)",
                                  multiline=False, size_hint_x=0.35)
        self.uses_in = TextInput(hint_text="max uses", multiline=False,
                                 size_hint_x=0.15)
        self.exp_in = TextInput(hint_text="expires in s", multiline=False,
                                size_hint_x=0.15)
        self.channel_in = TextInput(hint_text="channel cid (optional)",
                                    multiline=False, size_hint_x=0.25)
        create = _btn("create", 70, 30)
        create.bind(on_press=lambda _i: self.do_create())
        row.add_widget(self.label_in)
        row.add_widget(self.uses_in)
        row.add_widget(self.exp_in)
        row.add_widget(self.channel_in)
        row.add_widget(create)
        self.box.add_widget(row)

    def reload(self):
        self.load_gen[0] += 1
        self._fetch(lambda: self.client.guild_api.invites("list"),
                    self._render)

    def _render(self, res):
        if not res.get("ok"):
            self._status("invites: %s" % res.get("error", "failed"))
            return
        self.grid.clear_widgets()
        for inv in res.get("invites") or []:
            expires = inv.get("expires_at") or 0
            exp_txt = "never" if not expires else time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(expires))
            ch = inv.get("channel_id")
            suffix = ("  -> #%s" % ch[:8]) if ch else ""
            txt = ("%s  %s  uses %s/%s  exp %s%s"
                   % (inv.get("code", ""), inv.get("label") or "-",
                      inv.get("uses", 0), inv.get("max_uses") or "*",
                      exp_txt, suffix))
            row = BoxLayout(size_hint_y=None, height=_scaled(26),
                            spacing=dp(2))
            row.add_widget(_row_label(txt, w=0.8, fs=sp(10)))
            cp = _btn("copy", 55, 26, sp(10))
            cp.bind(on_press=lambda _i, code=inv.get("code", ""):
                    self._copy_link(code))
            rv = _btn("revoke", 65, 26, sp(10))
            rv.bind(on_press=lambda _i, code=inv.get("code"):
                    self._revoke(code))
            row.add_widget(cp)
            row.add_widget(rv)
            self.grid.add_widget(row)

    def _copy_link(self, code):
        link = "shades://join/%s" % code
        app = App.get_running_app()
        if app is not None and hasattr(app, "_copy_text"):
            app._copy_text(link)
        else:
            self._status(link)

    def do_create(self):
        label = (self.label_in.text or "").strip()
        uses = _int_in(self.uses_in.text)
        exp = _int_in(self.exp_in.text)
        channel = (self.channel_in.text or "").strip()

        def job():
            return self.client.guild_api.invites(
                "create", label=label, max_uses=uses, expires_in=exp,
                channel=channel or None)
        self._post(job, on_ok=self.reload, label="invite %s" % label)

    def _revoke(self, code):
        def job():
            return self.client.guild_api.invites("revoke", code=code)
        self._post(job, on_ok=self.reload, label="revoke")


# ---------------------------------------------------------------------------
TAB_CLASSES = (GeneralTab, MembershipTab, ChannelsTab, RolesTab, InvitesTab)


class ServerConfigScreen(Screen):
    """One tabbed screen replacing the U8 popup. `client` is the app object
    (guild_api + flags); build_tabs() runs on every enter so the tabs match
    the caller's current roles."""

    def __init__(self, client=None, **kw):
        super().__init__(**kw)
        self.client = client
        self.tab_buttons = []
        self.tab_content = {}
        self._tab_contents = {}
        self.current_tab = None
        self._active = (0.36, 0.41, 0.59, 1)
        self._inactive = (0.24, 0.26, 0.3, 1)
        self._content_slot = None
        self._bar = None

    # -- perm matrix ---------------------------------------------------------
    def _visible_tab_names(self):
        c = self.client
        if c is None:
            return []
        is_admin = bool(getattr(c, "i_am_admin", False))
        names = []
        for tab_cls in TAB_CLASSES:
            title = tab_cls.title
            if title in ("General", "Membership", "Invites"):
                want = is_admin
            elif title == "Channels":
                want = is_admin or getattr(
                    c, "_can_create_channels", lambda: False)()
            else:  # Roles
                want = is_admin or getattr(
                    c, "_can_manage_roles", lambda: False)()
            if want:
                names.append(title)
        return names

    # -- build / switch ------------------------------------------------------
    def build_tabs(self):
        app = App.get_running_app()
        self._active = (app.theme_colors["primary"] if app
                        else (0.36, 0.41, 0.59, 1))
        self._inactive = (app.theme_colors["sidebar_ctrl"] if app
                          else (0.24, 0.26, 0.3, 1))

        self.clear_widgets()
        self.tab_buttons = []
        self.tab_content = {}
        self._tab_contents = {}

        root = BoxLayout(orientation="vertical", padding=dp(6))
        bar = BoxLayout(size_hint_y=None, height=_scaled(34), spacing=dp(4))
        back = _btn("< back", 90, 30, sp(12))
        back.bind(on_press=lambda _i: self._back())
        bar.add_widget(back)
        self._bar = bar

        self._content_slot = BoxLayout()
        root.add_widget(bar)
        root.add_widget(self._content_slot)
        self.add_widget(root)

        for tab_cls in TAB_CLASSES:
            if tab_cls.title not in self._visible_tab_names():
                continue
            tab = tab_cls(self, self.client)
            content = tab.make_content()  # starts the worker fetches
            self.tab_content[tab_cls.title] = tab
            self._tab_contents[tab_cls.title] = content
            btn = _btn(tab_cls.title, 70, 30, sp(12))
            btn.size_hint_x = 1.0  # the tab strip splits evenly
            btn.background_color = self._inactive
            btn.bind(on_press=lambda _i, t=tab_cls.title: self._show_tab(t))
            bar.add_widget(btn)
            self.tab_buttons.append(btn)
        if self.tab_buttons:
            self._show_tab(self.tab_buttons[0].text)

    def _show_tab(self, title):
        content = self._tab_contents.get(title)
        if content is None:
            return
        self.current_tab = title
        for b in self.tab_buttons:
            b.background_color = self._active if b.text == title \
                else self._inactive
        self._content_slot.clear_widgets()
        self._content_slot.add_widget(content)

    def _back(self):
        try:
            self.manager.current = "main"
        except Exception:
            pass

    # -- icon surface --------------------------------------------------------
    def load_icon_into(self, widget, blob_id):
        """Fetch a committed icon blob and hand its texture to an Image.
        Network on a worker thread; the widget on the main thread only."""
        if not blob_id:
            return

        def work():
            try:
                import io
                import base64
                res = self.client.guild_api.blobs_get(blob_id)
                if not res.get("ok"):
                    return
                raw = base64.b64decode(res.get("data_b64") or "")
                if not raw:
                    return
                from kivy.core.image import Image as CoreImage
                ci = CoreImage(io.BytesIO(raw), ext="png")
                Clock.schedule_once(
                    lambda _dt: self._set_texture(widget, ci), 0)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _set_texture(self, widget, core_image):
        try:
            widget.texture = core_image.texture
        except Exception:
            pass

    def _apply_status(self, txt):
        """Status on the ADMIN screen thread-safely: config_status here
        (what tay sees while picking) + the standard mirrors."""
        Clock.schedule_once(lambda dt, t=txt: self._apply_status_ui(t), 0)

    def _apply_status_ui(self, txt):
        try:
            lbl = self.ids.get("config_status")
            if lbl is not None:
                lbl.text = str(txt)
        except Exception:
            pass
        try:
            self.client._set_status(txt)
        except Exception:
            pass

    def _apply_icon_file(self, path):
        """FileChooser result: upload through the standard blob flow, then
        set guild_icon to the returned blob_id (the blob store carries the
        asset; admin/config only references it)."""
        name = os.path.basename(path)
        mime = "image/png"
        if name.lower().endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif name.lower().endswith(".webp"):
            mime = "image/webp"
        elif name.lower().endswith(".gif"):
            mime = "image/gif"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self._apply_status("icon read failed: %r" % e)
            return

        def work():
            # v0.36.10: every path reports; a raise can no longer kill the
            # worker silently (tay's "returns without setting").
            try:
                res = self.client.guild_api.blobs_put(name, mime, data)
            except Exception as e:
                self._apply_status("icon upload: %r" % e)
                return
            if not res.get("ok"):
                self._apply_status("icon upload: %s" % res.get("error"))
                return
            blob_id = res.get("blob_id")
            try:
                res2 = self.client.guild_api.admin_config("guild_icon",
                                                          blob_id)
            except Exception as e:
                self._apply_status("icon config: %r" % e)
                return
            if res2.get("ok"):
                self._apply_status("icon set")
                Clock.schedule_once(lambda dt: self._refresh_general(), 0)
            else:
                self._apply_status("icon config: %s" % res2.get("error"))
        threading.Thread(target=work, daemon=True).start()

    def _apply_banner_file(self, path):
        """FileChooser result: upload through the standard blob flow, then
        set banner to the returned blob_id (P5 mirrors _apply_icon_file;
        the strip shows it on the next whoami the client ingests)."""
        name = os.path.basename(path)
        mime = "image/png"
        if name.lower().endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif name.lower().endswith(".webp"):
            mime = "image/webp"
        elif name.lower().endswith(".gif"):
            mime = "image/gif"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self._apply_status("banner read failed: %r" % e)
            return

        def work():
            # v0.36.10: every path reports; a raise can no longer kill the
            # worker silently (tay's "returns without setting").
            try:
                res = self.client.guild_api.blobs_put(name, mime, data)
            except Exception as e:
                self._apply_status("banner upload: %r" % e)
                return
            if not res.get("ok"):
                self._apply_status("banner upload: %s" % res.get("error"))
                return
            blob_id = res.get("blob_id")
            try:
                res2 = self.client.guild_api.admin_config("banner", blob_id)
            except Exception as e:
                self._apply_status("banner config: %r" % e)
                return
            if res2.get("ok"):
                self._apply_status("banner set")
                Clock.schedule_once(lambda dt: self._refresh_general(), 0)
            else:
                self._apply_status("banner config: %s" % res2.get("error"))
        threading.Thread(target=work, daemon=True).start()

    def _refresh_general(self):
        tab = self.tab_content.get("General")
        if tab is not None:
            tab.reload()

    def on_enter(self):
        """(Re)build on entry: roles may have changed since last time, and
        at runtime kivy_client wires .client right before switching here."""
        if self.client is None:
            app = App.get_running_app()
            if app is not None and hasattr(app, "guild_api"):
                self.client = app
        if self.client is None:
            return
        self.build_tabs()
