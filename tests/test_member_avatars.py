#!/usr/bin/env python3
"""Member-sidebar avatar regression tests (tier-2).

tay (2026-11-19, device 0.33.18): the userlist showed a white square
instead of the pfp. Root cause on disk: every MemberRow branch built
the row with avatar_source="" -- the sidebar never resolved from
IMAGE_CACHE and never fetched. The fix mirrors the message-row
machinery: build-time resolution, dedup fetch, _refresh_avatar
rebinding through a _member_widgets registry.

Run:  python tests/test_member_avatars.py
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


def main():
    py = py_src()

    # --- 1. the builder exists and resolves like the message rows ---------
    check("member row builder exists", "def _member_row(self, user_hash,"
          " uname, presence_color):" in py)
    i_m = py.find("def _member_row")
    seg = py[i_m:i_m + 2200] if i_m > 0 else ""
    check("resolve from IMAGE_CACHE", 'key = "av:"' in seg
          and "IMAGE_CACHE.get(key)" in seg)
    check("fetch avatar with dedup", "IMAGE_CACHE.begin_load(key)" in seg
          and "IMAGE_CACHE.end_load(k)" in seg)
    check("fetch path mirrors message rows", "guild_api.avatar_get(a)"
          in seg and 'IMAGE_CACHE.set_bytes(k, res["avatar"], "png")' in seg)
    check("fetch schedules the rebind",
          "self._refresh_avatar(k)" in seg)
    check("own avatar rides my_avatar_small",
          "avatar_source = self.my_avatar_small" in seg)
    check("no hard-coded empty avatar in the builder",
          'avatar_source=avatar_source' in seg)

    # --- 2. the registry drives the sidebar AND the rebind ----------------
    check("registry initialised next to the msg widgets",
          "self._member_widgets = {}        # user_hash -> MemberRow" in py)
    j_main = py.find("def _refresh_members_ui_main")
    m_seg = py[j_main:j_main + 3600] if j_main > 0 else ""
    check("sidebar rebuild resets the registry",
          'self._member_widgets = {}' in m_seg.split(
              "if self._friends:")[0])
    check("all three sidebar branches use the builder",
          m_seg.count("self._member_row(") == 3,
          "count=%d" % m_seg.count("self._member_row("))
    check("no raw MemberRow constructions left in the sidebar",
          "grid.add_widget(MemberRow(" not in m_seg)
    i_r = py.find("def _refresh_avatar")
    r_seg = py[i_r:i_r + 700] if i_r > 0 else ""
    check("avatar rebind covers the sidebar too",
          "list(self._msg_widgets.values()) + \\\n"
          "                list(self._member_widgets.values())" in r_seg)

    print()
    print("P=%d F=%d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
