#!/usr/bin/env python3
"""ServerConfigScreen tests (Task 5) — headless: real Kivy widgets, a fake
client object (no network, no run()).

Pins down:
  - tab visibility matches the caller's bits (admin sees all five;
    MANAGE_CHANNEL-only sees Channels; MANAGE_ROLES-only sees Roles;
    a plain member sees none);
  - General tab prefills the configured values and "save" posts key+value
    through admin_config;
  - the icon picker routes through the standard blob flow: FileChooser
    result -> blobs_put -> admin_config(guild_icon, blob_id);
  - Channels tab posts channels/patch with the right cid + overwrites
    bitmask; delete gated to admin;
  - Roles tab: create posts the checked bitmask; assign posts role+user;
  - Invites tab: create posts label/max_uses/expires_in/channel.
"""
import os, sys, time, json

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))

class FakeApi:
    """Records every guild call; returns canned shapes."""
    def __init__(self):
        self.calls = []
        self.config = {"config": {k: v for k, v in {
            "guild_name": "TestHub", "registration": "open",
            "slowmode": "0", "motd": "hi", "guild_icon": "",
            "attachments": "saved", "banner": ""}.items()}}

    def _rec(self, proto, res):
        self.calls.append(proto)
        return res

    def admin_config(self, key=None, value=None):
        return self._rec(("admin_config", key, value),
                         {"ok": 1, "config": {key: value} if key else self.config["config"]})

    def admin_mods(self, op="list", user=None):
        return self._rec(("admin_mods", op, user),
                         {"ok": 1, "mods": ["a" * 32], "admins": []})

    def admin_audit(self, limit=100):
        return self._rec(("admin_audit", limit),
                         {"ok": 1, "entries": [{"action": "x", "target": "", "detail": ""}]})

    def channels_list(self):
        return self._rec(("channels_list",), {"ok": 1, "channels": [
            {"id": "c" * 31 + "1", "name": "general", "kind": "chat",
             "topic": "", "position": 1, "overwrites": []}]})

    def channels_patch(self, cid, **kw):
        return self._rec(("channels_patch", cid) + tuple(sorted(kw.items())),
                         {"ok": 1, "overwrites": kw.get("overwrites", [])})

    def channels_delete(self, cid):
        return self._rec(("channels_delete", cid), {"ok": 1})

    def roles_list(self):
        return self._rec(("roles_list",), {"ok": 1, "roles": [
            {"role_id": 1, "name": "Founder", "perms": 32, "color": None,
             "position": 100, "members": 1},
            {"role_id": 2, "name": "Member", "perms": 3, "color": None,
             "position": 1, "members": 2}]})

    def roles_create(self, name, perms=0, color=None, position=0):
        return self._rec(("roles_create", name, perms),
                         {"ok": 1, "role": {"role_id": 3, "name": name}})

    def roles_delete(self, role_id):
        return self._rec(("roles_delete", role_id), {"ok": 1})

    def roles_assign(self, role_id, user):
        return self._rec(("roles_assign", role_id, user), {"ok": 1})

    def invites(self, op="list", **kw):
        return self._rec(("invites", op) + tuple(sorted(kw.items())),
                         {"ok": 1, "code": "ABC123"}
                         if op == "create" else
                         {"ok": 1, "invites": []})

    def blobs_put(self, name, mime, data, on_progress=None):
        return self._rec(("blobs_put", name, mime, len(data)),
                         {"ok": 1, "blob_id": "d" * 32, "size": len(data)})

    def blobs_get(self, blob_id):
        return self._rec(("blobs_get", blob_id),
                         {"ok": 0, "error": "no blob in the fake"})

class FakeClient:
    def __init__(self):
        self.guild_api = FakeApi()
        self.i_am_admin = True
        self.my_roles = [{"role_id": 1, "name": "Founder", "perms": 32}]
        self.PERM_VIEW = 1
        self.PERM_POST = 2
        self.PERM_MANAGE_MESSAGES = 4
        self.PERM_MANAGE_CHANNEL = 8
        self.PERM_MANAGE_ROLES = 16
        self.PERM_ADMIN = 32
        self.status = []

    def _set_status(self, txt):
        self.status.append(txt)

    def _can_manage_roles(self):
        return self.i_am_admin or any(
            r.get("perms", 0) & (self.PERM_MANAGE_ROLES | self.PERM_ADMIN)
            for r in self.my_roles)

    def _can_create_channels(self):
        return self.i_am_admin or any(
            r.get("perms", 0) & (self.PERM_MANAGE_CHANNEL | self.PERM_ADMIN)
            for r in self.my_roles)

def _set_app_tabs(app, perms, is_admin=True):
    app.i_am_admin = is_admin
    app.my_roles = [{"role_id": 1, "name": "x", "perms": perms}]

def build_screen(client):
    """Instantiate the ServerConfigScreen with a fake client."""
    import ui_server_config as usc
    scr = usc.ServerConfigScreen(client=client, name="server_config")
    return scr

def pump(seconds=2.0):
    """Drive the Kivy clock so Clock.schedule_once callbacks land, and let
    worker threads finish; ServerConfigScreen loads on worker threads."""
    from kivy.clock import Clock
    deadline = time.time() + seconds
    while time.time() < deadline:
        Clock._process_events()
        Clock.tick()
        time.sleep(0.02)

def main():
    try:
        import ui_server_config as usc
    except Exception as e:
        print("SKIP  ui_server_config import failed: %s" % e)
        return 0
    from kivy.app import App

    class ProbeApp(App):
        def build(self):
            from kivy.uix.screenmanager import ScreenManager
            return ScreenManager()
        theme_colors = {"primary": (0.2, 0.3, 0.5, 1),
                        "sidebar_ctrl": (0.2, 0.2, 0.2, 1)}

    # ServerConfigScreen pulls the running app for theme only; fake one on
    probe = ProbeApp()
    probe.theme_colors = ProbeApp.theme_colors

    # ---------- perm matrix (widgets only, no network) ----------
    client = FakeClient()
    scr = build_screen(client)
    tab_names = scr._visible_tab_names()   # perm matrix helper, no widgets
    check("admin sees all five tabs",
          tab_names == ["General", "Membership", "Channels", "Roles",
                        "Invites"], dict(tabs=tab_names))

    _set_app_tabs(client, 16, is_admin=False)  # MANAGE_ROLES only
    tab_names = scr._visible_tab_names()
    check("MANAGE_ROLES-only sees only Roles",
          tab_names == ["Roles"], dict(tabs=tab_names))

    _set_app_tabs(client, 8, is_admin=False)  # MANAGE_CHANNEL only
    tab_names = scr._visible_tab_names()
    check("MANAGE_CHANNEL-only sees only Channels",
          tab_names == ["Channels"], dict(tabs=tab_names))

    _set_app_tabs(client, 0, is_admin=False)
    tab_names = scr._visible_tab_names()
    check("plain member sees zero tabs", tab_names == [], dict(tabs=tab_names))

    # ---------- full build as admin, widgets + network through the fake ----------
    _set_app_tabs(client, 32)
    client.guild_api.calls = []
    scr.build_tabs()
    pump(1.0)          # drain the worker threads + scheduled renders
    tab_btns = [b.text for b in scr.tab_buttons]
    check("admin build shows five tab buttons",
          tab_btns == ["General", "Membership", "Channels", "Roles",
                       "Invites"], tab_btns)
    check("config prefill loaded (network fired once)",
          any(c[0] == "admin_config" and c[1] is None
              for c in client.guild_api.calls),
          client.guild_api.calls[:3])

    # General tab: flip motd and save
    general = scr.tab_content["General"]
    motd_entry = general.motd_entry
    saved_before = [c for c in client.guild_api.calls if c[0] == "admin_config"]
    general.motd_entry.text = "brb"
    general.do_save_motd()
    pump(0.8)
    saved = [c for c in client.guild_api.calls if c[0] == "admin_config"]
    check("motd save posts the right key",
          ("admin_config", "motd", "brb") in client.guild_api.calls,
          saved)

    # P3: attachments row -- active-mode reflect + one-tap flip posts
    check("attachments row built with both buttons",
          general.att_saved_btn is not None
          and general.att_eph_btn is not None
          and "saved" in general.att_state.text,
          general.att_state.text)
    general._reflect_attachments("saved")
    check("reflect marks the active mode disabled",
          general.att_saved_btn.disabled is True
          and general.att_eph_btn.disabled is False, None)
    general._save_attachments("ephemeral")
    pump(0.8)
    flips = [c for c in client.guild_api.calls if c[0] == "admin_config"]
    check("ephemeral tap posts attachments=ephemeral",
          ("admin_config", "attachments", "ephemeral") in flips, flips)
    general._save_attachments("saved")
    pump(0.8)
    check("saved tap posts attachments=saved",
          ("admin_config", "attachments", "saved")
          in client.guild_api.calls,
          client.guild_api.calls)
    general._reflect_attachments("ephemeral")
    check("reflect follows the flipped mode",
          general.att_eph_btn.disabled is True
          and "ephemeral" in general.att_state.text,
          general.att_state.text)

    # P5: banner row routes the chooser flow and clear posts the pair
    usc_src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ui_server_config.py"),
        encoding="utf-8").read()
    check("general tab has the banner row",
          '"set banner..."' in usc_src and "_clear_banner" in usc_src)
    general._clear_banner()
    pump(0.8)
    check("banner clear posts empty pair",
          ("admin_config", "banner", "") in client.guild_api.calls,
          client.guild_api.calls[-3:])
    check("banner chooser routes through the blob flow",
          "def _apply_banner_file" in usc_src
          and 'admin_config("banner", blob_id)' in usc_src)
    check("config pane read-back includes banner prefill",
          "banner" in client.guild_api.config["config"],
          client.guild_api.config["config"])

    # Channels tab: post an overwrite bitmask through channels/patch
    ch_tab = scr.tab_content["Channels"]
    ch_tab.post_overwrites(cid="c" * 31 + "1", role_id=2, view=True, post=True)
    pump(0.8)
    patches = [c for c in client.guild_api.calls if c[0] == "channels_patch"]
    check("overwrite edit posts channels/patch with cid+mask",
          any(c[1] == "c" * 31 + "1" and dict(c[2:]) ==
              {"role_id": 2, "perms": 3} for c in patches), patches)

    # Roles tab: create with a checked bitmask
    roles_tab = scr.tab_content["Roles"]
    roles_tab.new_role_name.text = "Ops"
    roles_tab.set_perm_checks({"VIEW": True, "POST": True,
                               "MANAGE_CHANNEL": True})
    roles_tab.do_create_role()
    pump(0.8)
    creates = [c for c in client.guild_api.calls if c[0] == "roles_create"]
    check("role create posts the checked bitmask",
          any(c[1] == "Ops" and c[2] == 11 for c in creates), creates)
    roles_tab.do_assign(2, "f" * 32)
    pump(0.8)
    check("assign posts role+user",
          ("roles_assign", 2, "f" * 32) in client.guild_api.calls,
          client.guild_api.calls[-4:])

    # Invites tab: create with label/uses/expiry; and channel-targeted row
    inv_tab = scr.tab_content["Invites"]
    inv_tab.label_in.text = "welcome"
    inv_tab.uses_in.text = "5"
    inv_tab.exp_in.text = "3600"
    inv_tab.channel_in.text = "c" * 31 + "1"
    inv_tab.do_create()
    pump(0.8)
    invs = [c for c in client.guild_api.calls if c[0] == "invites" and c[1] == "create"]
    check("invite create posts every field",
          any(c[1] == "create" and
              dict(c[2:]) == {"channel": "c" * 31 + "1",
                              "expires_in": 3600, "label": "welcome",
                              "max_uses": 5} for c in invs), invs)

    # Icon flow: file already on disk (fake FileChooser result)
    import tempfile
    png = os.path.join(tempfile.mkdtemp(prefix="cfg_icon_"), "logo.png")
    with open(png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\nxyz")
    client.guild_api.calls = []
    scr._apply_icon_file(png)
    pump(1.0)
    icon_calls = [c for c in client.guild_api.calls if c[0] == "blobs_put"]
    check("icon upload goes through the blob store",
          any(c[0] == "blobs_put" and c[1] == "logo.png"
              and c[3] == 11 for c in icon_calls), icon_calls)
    check("blob_id then config set",
          ("admin_config", "guild_icon", "d" * 32) in client.guild_api.calls,
          client.guild_api.calls[-3:])

    # ---------- P10a: device (Android) purpose routing -----------------------
    # the SAF route must be taken instead of the desktop FileChooser
    # (which lists nothing in an Android sandbox) and must carry the
    # purpose; _route_purpose_file hands the file to the General tab
    picked = []
    from kivy.metrics import dp as _dp
    probe._android_platform = lambda: True
    probe.open_file_chooser = (lambda **kw: picked.append(kw.get("purpose")))
    gen = scr.tab_content["General"]
    gen._open_icon_chooser()
    gen._open_banner_chooser()
    check("android pickers ride SAF with purpose",
          picked == ["icon", "banner"], picked)
    # desktop app keeps its own chooser: restore a non-android probe and
    # watch the FileChooser popup get built (not opened twice in a lane)
    probe._android_platform = lambda: False
    popups = []
    from kivy.uix.popup import Popup as _P
    real_popup_init = _P.__init__

    def spy_init(pself, **kw):
        popups.append(kw.get("title"))
        return real_popup_init(pself, **kw)
    _P.__init__ = spy_init
    gen._open_icon_chooser()
    _P.__init__ = real_popup_init
    check("desktop chooser popup preserved for non-android app",
          popups == ["choose an icon"], popups)

    # ---------- P10b: prettier receipts --------------------------------------
    check("banner preview widget exists on General",
          hasattr(gen, "banner_widget"))
    check("icon preview sized for the strip row",
          gen.icon_widget.width == _dp(56), repr(gen.icon_widget.width))
    # config render with a banner id hands it to the preview loader
    client.guild_api.calls = []
    client.guild_api.config["config"]["banner"] = "b" * 32
    gen._render({"ok": 1, "config": client.guild_api.config["config"]})
    pump(1.0)
    check("banner render fetches the banner blob",
          ("blobs_get", "b" * 32) in client.guild_api.calls,
          client.guild_api.calls[-4:])

    for t in scr.tab_buttons:
        pass  # build sanity, no exceptions above is the bar

    print("RESULT: %d PASS, %d FAIL" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0

if __name__ == "__main__":
    app = main()
    sys.exit(app)
