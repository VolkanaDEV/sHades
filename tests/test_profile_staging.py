#!/usr/bin/env python3
"""Staged-profile + live slash palette regression tests (tier-2).

tay (2026-11):
  - profile staging is the very first screen on a fresh install (no
    extant profile saved), served with the hub destination/node fields
    as a replacement for the standalone connect screen
  - '/' pops the command palette for live selections

Source-level contracts like tests/test_fresh_install.py: the wiring
must exist and stay put, so a refactor that drops the fresh-install
routing, the hub pass-through, or the palette trigger fails here in CI
before a device ever shows the stale screen.

Run:  python tests/test_profile_staging.py
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


def py_src():
    with open(os.path.join(REPO, "kivy_client.py"), encoding="utf-8") as f:
        return f.read()


def kv_src():
    with open(os.path.join(REPO, "kv_md.kv"), encoding="utf-8") as f:
        return f.read()


def main():
    py = py_src()
    kv = kv_src()

    # --- 1. fresh-install routing: staging precedes any auto-connect -----
    fresh = "Fresh install: tay (2026-11), profile staging must be the"
    i_fresh = py.find(fresh)
    check("fresh-install branch exists in on_start", i_fresh > 0)
    seg = py[i_fresh:i_fresh + 1400] if i_fresh > 0 else ""
    check("staging screen first, profile-aware fallthrough",
          'self.root.get_screen("onboarding").stage = 0' in seg
          and 'self.root.current = "onboarding"' in seg
          and "self._portable_profile_path()" in seg)
    j_auto = seg.find("_auto_connect_default_hub()")
    j_stage = seg.find('self.root.current = "onboarding"')
    j_profile = seg.find("_portable_profile_path()")
    check("profile check precedes the staging landing",
          0 < j_profile < j_stage,
          "profile=%s stage=%s" % (j_profile, j_stage))
    check("auto-connect only behind the profile-exists check",
          0 < j_profile < j_auto
          and seg.count("_auto_connect_default_hub()") == 1,
          "profile=%s auto=%s" % (j_profile, j_auto))
    check("staging resets to stage 0",
          'self.root.get_screen("onboarding").stage = 0' in seg)

    # --- 2. hub fields served on the staged profile screen ----------------
    for fid in ("onb_hub", "onb_node", "onb_delivery"):
        check("kv id %s on the stager" % fid, kv.count("id: %s" % fid) == 1)
    check("hub fields gated to stage 2 together",
          kv.count("height: dp(44) if root.stage == 2 else 0") >= 4)
    check("stager card height stage-aware",
          "min((dp(700) if root.stage == 2 else dp(560))" in kv)
    hub_hint = kv.find("Hub address (ip:port), empty = community hub")
    check("hub hint names the community default", hub_hint > 0)

    # --- 3. advance() stashes the staged hub ------------------------------
    for wid in ("onb_hub", "onb_node", "onb_delivery"):
        check("advance() collects %s" % wid,
              ('_staged("%s")' % wid) in py)
    check("advance() passes the stash to the app",
          "app._onboard_hub = (_staged(\"onb_hub\")" in py)

    # --- 4. complete_onboarding honors hub_data ---------------------------
    check("complete_onboarding takes hub_data",
          "def complete_onboarding(self, name, hub_data=None):" in py)
    check("hub_data unpacks with default",
          'hub, node_hash, delivery_hash = (hub_data or ("", "", ""))' in py)
    check("typed hub reaches connect_to_guild",
          "self.connect_to_guild(hub, self.username," in py)
    check("empty hub keeps community default",
          py.count("self._auto_connect_default_hub()")
          >= 1)

    # --- 5. the recovery sheet carries the stash on both exits ------------
    check("save-kit exit passes _onboard_hub",
          'onboarding_name, getattr(self, "_onboard_hub", None))' in py)
    check("skip exit passes _onboard_hub",
          py.count('getattr(self, "_onboard_hub", None)') == 2)

    # --- 6. live '/' trigger ----------------------------------------------
    check("live watcher exists", "def _on_live_slash(self, inst, value):" in py)
    check("bare / only", 'if text == "/":' in py)
    check("watcher opens the palette with a seed",
          'self.open_command_palette(seed="/")' in py)
    check("watcher bound to the channel input",
          "_msg.bind(text=self._on_live_slash)" in py)
    check("pick write-back is suppressed",
          "self._suppress_live_slash = True" in py)
    check("suppression clears next frame",
          'lambda _dt: setattr(self, "_suppress_live_slash", False)' in py)
    check("palette tracks open state on dismiss",
          "popup.bind(on_dismiss=lambda *_: "
          "setattr(self, \"_palette_popup\", None))" in py)
    check("palette accepts a seed", "def open_command_palette(self, "
          "seed=None):" in py)
    check("seed pre-fills the filter", "if seed:" in py)

    # --- 7. profile-screen fields carry the in-box placeholder (tay) ------
    # kivymd 2.0.0 delivers the hint as an MDTextFieldHintText CHILD
    # (add_widget override wires _hint_text_label + the canvas.after
    # "hint-text-rectangle"); the legacy hint_text property is dead and
    # was the reason the stager fields shipped labelless.
    check("stager fields ride the native hint child",
          kv.count("MDTextFieldHintText:") == 10)
    SIBS = ("MDTextField:", "MDButton:", "BoxLayout:", "Label:")
    for fid in ("onb_hub", "onb_node", "onb_delivery", "welcome_name"):
        i = kv.find("id: %s" % fid)
        marks = [kv.find(n, i + 10) for n in SIBS]
        b = min(x for x in marks if x > i)
        check("%s rides the hint child in-block" % fid,
              "MDTextFieldHintText:" in kv[i:b])
    check("no dead hint_text property anywhere in kv",
          "hint_text: '" not in kv)

    # --- 8. profile-screen delete account (tay, plan_admin P1) -----------
    # Server purge machinery existed; the client flow is the arrow:
    # danger row -> two type-to-confirm stage-gates whose literals are
    # byte-equal to what sHades.py validates -> sequential worker ->
    # local wipe (profile files + session + cached avatar) ->
    # staging gate-back.
    check("danger row on the profile screen",
          "id: profile_delete" in kv
          and "on_press: app.open_confirm_delete_account()" in kv)
    check("stage-1 handler exists with the posts literal",
          "def open_confirm_delete_account" in py
          and '!= "DELETE MY POSTS"' in py)
    check("stage-2 handler exists with the leave literal",
          "def open_confirm_leave_guild" in py
          and '!= "LEAVE GUILD"' in py)
    server = open(os.path.join(REPO, "sHades.py"),
                  "rb").read().decode("utf-8")
    check("client literals byte-equal the server payloads",
          'data.get("confirm") != "DELETE MY POSTS"' in server
          and 'data.get("confirm") != "LEAVE GUILD"' in server
          and '{"confirm": "DELETE MY POSTS"}' in py
          and '{"confirm": "LEAVE GUILD"}' in py)
    wp = py.find("def purge_account_worker")
    check("worker posts then leaves, in order",
          0 < wp < py.find("def finish_account_purge", wp)
          and py.find("account_delete_posts", wp)
          < py.find("account_leave", wp)
          and "threading.Thread(target=work, daemon=True).start()" in py)
    check("failed step aborts before the local wipe",
          "Post purge failed" in py and "Leave failed" in py)
    check("local wipe drops both profile files and the session",
          "finish_account_purge" in py
          and "self._portable_profile_path()" in py
          and "self._onboarding_path()" in py
          and "self._session_path()" in py)
    check("wipe drops my cached avatar and resets identity state",
          "IMAGE_CACHE.forget(key)" in py
          and 'key = "av:" + (getattr(self, "my_hash", "") or "")' in py
          and "self.i_am_admin = False" in py)
    check("gate-back routes to staging at stage 0",
          'root.get_screen("onboarding").stage = 0' in py
          and 'root.current = "onboarding"' in py)
    check("ImageCache.forget ships and unlinks the cached file",
          "def forget(self, key):" in py
          and "os.unlink(path)" in py)
    # the purge relies on the same file the boot gate checks at
    # :2866 -- keep both sides of that invariant honest
    check("boot gate and purge agree on the profile file",
          "os.path.isfile(self._portable_profile_path())" in py)

    print()
    print("P=%d F=%d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
