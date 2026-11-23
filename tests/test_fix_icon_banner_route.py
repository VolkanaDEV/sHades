#!/usr/bin/env python3
"""Server icon & banner setting (the user: 'doesn't work at all, fails
silently on client') — probe-style test, run as a script under Xvfb.

Pins down the two root causes, both proven live before the fix:
  (a) Android routing: _route_purpose_file called _apply_icon_file /
      _apply_banner_file on the General TAB, but those methods live
      only on the ServerConfigScreen. Every Android pick raised
      AttributeError, caught in _on_file_selected and reported only
      via _set_status — invisible on the admin screen. The tab has
      no such methods; the screen does; the route must call the
      screen's, and a pick must reach blobs_put + admin_config.
  (b) Dead status mirror: the screen is built in Python (no kv
      rule), so self.ids was always empty and the v0.36.10 mirror
      onto ids["config_status"] never found a label. After
      build_tabs, ids["config_status"] must exist, and "icon set" /
      "banner set" must land in its text where the admin stands.
Also checks the previews actually show the picked files
(icon/banner widget textures set after the reload).

Run: xvfb-run -a ~/workspace/venvs/shades/bin/python \
       tests/test_fix_icon_banner_route.py
"""
import base64
import io
import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="shades_icon_banner_test_")
os.environ["HOME"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kivy_client as kc  # noqa: E402
from kivy.app import App  # noqa: E402
from kivy.clock import Clock  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS  " if cond else "FAIL  "
    tail = ("  | " + str(extra)) if extra and not cond else ""
    print(mark + name + tail, flush=True)


def make_png(color=(200, 30, 30)):
    from PIL import Image as PILImage
    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def make_jpg(color=(30, 30, 200)):
    from PIL import Image as PILImage
    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), color).save(buf, "JPEG")
    return buf.getvalue()


PNG_PATH = os.path.join(TMP, "test-icon.png")
with open(PNG_PATH, "wb") as f:
    f.write(make_png())
JPG_PATH = os.path.join(TMP, "test-banner.jpg")
with open(JPG_PATH, "wb") as f:
    f.write(make_jpg())

calls = []
state = {"icon_blob": None, "banner_blob": None}


class FakeAPI:
    def blobs_put(self, name, mime, data, on_progress=None):
        # on_progress: the standard upload path (blob_put_healed) passes
        # it exactly like the real GuildAPI.blobs_put signature
        calls.append(("blobs_put", name, mime, len(data)))
        blob = "a" * 32 if "icon" in name else "b" * 32
        return {"ok": 1, "blob_id": blob, "size": len(data)}

    def blobs_get(self, blob_id):
        calls.append(("blobs_get", blob_id))
        raw = make_png() if blob_id == "a" * 32 else make_jpg()
        return {"ok": 1, "data_b64": base64.b64encode(raw).decode("ascii")}

    def admin_config(self, key=None, value=None):
        if key is None:
            return {"ok": 1, "config": {
                "guild_name": "test", "motd": "", "slowmode": "0",
                "registration": "open", "attachments": "saved",
                "guild_icon": state["icon_blob"] or "",
                "banner": state["banner_blob"] or ""}}
        calls.append(("admin_config", key, value))
        if key == "guild_icon":
            state["icon_blob"] = value
        elif key == "banner":
            state["banner_blob"] = value
        return {"ok": 1}

    def admin_audit(self, limit=50):
        return {"ok": 1, "entries": []}

    def channels_list(self):
        return {"ok": 1, "channels": []}

    def roles_list(self):
        return {"ok": 1, "roles": []}

    def admin_mods(self, op, *a, **kw):
        return {"ok": 1, "mods": []}

    def invites(self, op, *a, **kw):
        return {"ok": 1, "invites": []}


def find_labels(widget, out=None):
    from kivy.uix.label import Label
    if out is None:
        out = []
    if isinstance(widget, Label):
        out.append(widget)
    for ch in getattr(widget, "children", []):
        find_labels(ch, out)
    return out


class Probe(kc.sHadesClientApp):
    def on_start(self):
        super().on_start()
        self.guild_api = FakeAPI()
        self.i_am_admin = True
        Clock.schedule_once(step1, 1.5)

    def _init_reticulum(self):
        return None


def step1(dt):
    app = App.get_running_app()
    app.root.current = "server_config"
    Clock.schedule_once(step2, 1.5)


def step2(dt):
    app = App.get_running_app()
    scr = app.root.get_screen("server_config")
    tab = scr.tab_content.get("General")
    check("General tab present", tab is not None)
    check("upload methods live on the screen, not the tab",
          hasattr(scr, "_apply_icon_file")
          and hasattr(scr, "_apply_banner_file")
          and not hasattr(tab, "_apply_icon_file")
          and not hasattr(tab, "_apply_banner_file"))
    check("status label exists in screen ids",
          scr.ids.get("config_status") is not None, list(scr.ids.keys()))
    # Android path: the route a system-picker result takes.
    try:
        app._route_purpose_file(PNG_PATH, "icon")
        routed_ok, routed_err = True, None
    except Exception as e:
        routed_ok, routed_err = False, repr(e)
    check("route icon pick: no error", routed_ok, routed_err)
    Clock.schedule_once(step3, 2.0)


def step3(dt):
    app = App.get_running_app()
    scr = app.root.get_screen("server_config")
    tab = scr.tab_content.get("General")
    check("icon pick reached blobs_put + admin_config",
          ("blobs_put", "test-icon.png", "image/png", 155) in calls
          and ("admin_config", "guild_icon", "a" * 32) in calls, calls)
    check("icon preview shows the picked file",
          tab.icon_widget.texture is not None)
    lbl = scr.ids.get("config_status")
    check("status label shows 'icon set'",
          lbl is not None and lbl.text == "icon set",
          lbl.text if lbl is not None else None)
    try:
        app._route_purpose_file(JPG_PATH, "banner")
        routed_ok, routed_err = True, None
    except Exception as e:
        routed_ok, routed_err = False, repr(e)
    check("route banner pick: no error", routed_ok, routed_err)
    Clock.schedule_once(step4, 2.0)


def step4(dt):
    app = App.get_running_app()
    scr = app.root.get_screen("server_config")
    tab = scr.tab_content.get("General")
    check("banner pick reached blobs_put + admin_config",
          ("blobs_put", "test-banner.jpg", "image/jpeg", 693) in calls
          and ("admin_config", "banner", "b" * 32) in calls, calls)
    check("banner preview shows the picked file",
          tab.banner_widget.texture is not None)
    lbl = scr.ids.get("config_status")
    check("status label shows 'banner set'",
          lbl is not None and lbl.text == "banner set",
          lbl.text if lbl is not None else None)
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)), flush=True)
    app.stop()
    sys.exit(1 if FAIL else 0)


def _watchdog():
    time.sleep(60)
    print("WATCHDOG: app did not exit; forcing exit", flush=True)
    os._exit(2)


if __name__ == "__main__":
    threading.Thread(target=_watchdog, daemon=True).start()
    Probe().run()
