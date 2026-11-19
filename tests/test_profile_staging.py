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
    # kivymd 2.0.0 leaves the native hint label unbuilt (probed: None at
    # rest AND focused), so the stager fields ride HintOverlayField, an
    # in-box placeholder that disappears on type.
    check("hint overlay class shipped",
          kv.count("HintOverlayField:") == 4 and "class HintOverlayField"
          in py)
    for fid in ("onb_hub", "onb_node", "onb_delivery", "welcome_name"):
        check("%s rides the overlay field" % fid, "HintOverlayField:"
              in kv[max(0, kv.find("id: %s" % fid) - 80):
                    kv.find("id: %s" % fid)])
    check("overlay hides once text exists",
          "lbl.opacity = 0 if (self.text or \"\") else 1" in py)
    check("overlay tracks hint_text",
          "lbl.text = self.hint_text or \"\"" in py)
    # 0.33.20: the label must be MOUNTED (0.33.19 shipped an unmounted
    # label -- created but never add_widget'ed; probed: parent_kind None)
    check("overlay label is mounted on the field",
          "self.add_widget(self._placeholder_lbl)" in py)
    check("overlay resyncs on size and pos",
          "size=self._sync_placeholder," in py
          and "pos=self._sync_placeholder)" in py)

    print()
    print("P=%d F=%d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
