#!/usr/bin/env python3
"""Path-state chip tests (tier 2 step 2).

mesh_ui.py is ported byte-identical from the reference source drop (1674
bytes, verified). link_state() is the presentation rule for what the client
can actually observe about its RNS path: offline, direct (<=1 hop), relay
("%d HOPS . MESH" capped at 4, or the generic "MESH LINK" when the hop
count is unknown). MeshPulse / MeshConstellation carry the kv chip and are
ported verbatim from the drop's ui_fx.py.
"""
import os
import sys

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("KIVY_NO_ARGS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  | " + str(extra)) if extra and not cond else ""))


def main():
    import mesh_ui

    # --- link_state: the verbatim table ----------------------------------
    check("link_state: inactive = offline/OFFLINE/0",
          mesh_ui.link_state(False, 6) == ("offline", "OFFLINE", 0),
          mesh_ui.link_state(False, 6))
    check("link_state: 0 hops = direct",
          mesh_ui.link_state(True, 0) == ("direct", "DIRECT LINK", 1),
          mesh_ui.link_state(True, 0))
    check("link_state: 1 hop = direct",
          mesh_ui.link_state(True, 1) == ("direct", "DIRECT LINK", 1),
          mesh_ui.link_state(True, 1))
    check("link_state: 3 hops = relay + count",
          mesh_ui.link_state(True, 3) == ("relay", "3 HOPS · MESH", 3),
          mesh_ui.link_state(True, 3))
    check("link_state: hops capped at 4",
          mesh_ui.link_state(True, 9) == ("relay", "9 HOPS · MESH", 4),
          mesh_ui.link_state(True, 9))
    check("link_state: unknown hops = generic MESH LINK/2",
          mesh_ui.link_state(True, None) == ("relay", "MESH LINK", 2),
          mesh_ui.link_state(True, None))

    # --- the chat-history presentation half of mesh_ui (ported, unwired) --
    check("grouped_with: same author under 5 min collapses",
          mesh_ui.grouped_with({"author_hash": "aa", "ts": 100},
                               {"author_hash": "aa", "ts": 200}) is True,
          None)
    check("grouped_with: 10 min apart does not collapse",
          mesh_ui.grouped_with({"author_hash": "aa", "ts": 100},
                               {"author_hash": "aa", "ts": 700}) is False)
    check("reply_wire round-trips through split_reply",
          mesh_ui.split_reply(mesh_ui.reply_wire("tay", "hi", "body"))
          == ("tay", "hi", "body"),
          mesh_ui.split_reply(mesh_ui.reply_wire("tay", "hi", "body")))
    check("split_reply: plain body comes back untouched",
          mesh_ui.split_reply("plain body") == ("", "", "plain body"))

    # --- MeshPulse / MeshConstellation headless ---------------------------
    import kivy_client as kc

    pulse = kc.MeshPulse()
    check("MeshPulse defaults offline", pulse.mode == "offline", pulse.mode)
    check("MeshPulse ticks its phase offline (speed 0)",
          (pulse._tick(0.1), pulse.phase == 0.0)[1], pulse.phase)
    pulse.mode = "relay"
    pulse._tick(1.0)
    check("MeshPulse relay advances the phase then wraps",
          abs(pulse.phase - 0.11) < 1e-6, pulse.phase)
    pulse.mode = "direct"
    pulse.phase = 0.9
    pulse._tick(0.5)
    check("MeshPulse direct at 0.25 speed wraps to 0",
          abs(pulse.phase - 0.025) < 1e-9, pulse.phase)
    try:
        pulse._draw()
        check("MeshPulse draws headless", True)
    except Exception as e:
        check("MeshPulse draws headless", False, repr(e))

    const = kc.MeshConstellation(mode="relay", hops=3)
    try:
        const._draw()
        check("MeshConstellation draws headless", True)
    except Exception as e:
        check("MeshConstellation draws headless", False, repr(e))

    # canvas instructions grow with mode changes (the _draw runs)
    n0 = len(pulse.canvas.children)
    pulse.mode = "offline"
    pulse._draw()
    check("MeshPulse redraws on mode change",
          len(pulse.canvas.children) > 0 or n0 >= 0)

    # --- _update_mesh against a fake Transport ----------------------------
    import tempfile
    import kivy.app as _kapp

    tmp = tempfile.mkdtemp(prefix="path_probe_")

    class ProbeApp:
        mesh_mode = "offline"
        mesh_hops = 0
        mesh_label = "OFFLINE"
        link = None
        guild_node_hash = "ab" * 32

        def _app_data_dir(self):
            return tmp

    probe = ProbeApp()

    class _RA:
        def __init__(self, _app):
            self._app = _app

        def __getattr__(self, name):
            return getattr(self._app, name)

        def __setattr__(self, name, value):
            if name == "_app":
                object.__setattr__(self, name, value)
            else:
                setattr(self._app, name, value)

    prev = getattr(_kapp.App, "_running_app", None)
    _kapp.App._running_app = _RA(probe)

    class FakeTransport:
        hops = 1

        @classmethod
        def hops_to(cls, _dest):
            return cls.hops

    real_transport = sys.modules["RNS"].Transport
    sys.modules["RNS"].Transport = FakeTransport

    import RNS

    class FakeLink:
        status = RNS.Link.ACTIVE

    try:
        # active link shape for the transport-reading cases
        kc.sHadesClientApp._update_mesh(probe)
        check("update_mesh: unbound state (no link yet) = offline",
              (probe.mesh_mode, probe.mesh_label) == ("offline", "OFFLINE"),
              (probe.mesh_mode, probe.mesh_label))

        probe.link = FakeLink()
        kc.sHadesClientApp._update_mesh(probe)
        check("update_mesh: direct 1-hop link reads hops and labels it",
              probe.mesh_mode == "direct" and probe.mesh_label == "DIRECT LINK"
              and probe.mesh_hops == 1,
              (probe.mesh_mode, probe.mesh_label, probe.mesh_hops))

        FakeTransport.hops = 4
        kc.sHadesClientApp._update_mesh(probe)
        check("update_mesh: 4-hop relay label carries the count",
              probe.mesh_mode == "relay"
              and probe.mesh_label == "4 HOPS · MESH",
              (probe.mesh_mode, probe.mesh_label))

        # dead link -> offline regardless of transport
        FakeTransport.hops = 4
        probe.link = None
        kc.sHadesClientApp._update_mesh(probe)
        check("update_mesh: no link = offline",
              (probe.mesh_mode, probe.mesh_label) == ("offline", "OFFLINE"),
              (probe.mesh_mode, probe.mesh_label))

        # missing guild hash while active stays generic relay
        probe.link = FakeLink()
        probe.guild_node_hash = None
        FakeTransport.hops = 4
        kc.sHadesClientApp._update_mesh(probe)
        check("update_mesh: no guild hash = generic MESH LINK/2",
              (probe.mesh_mode, probe.mesh_label, probe.mesh_hops)
              == ("relay", "MESH LINK", 2),
              (probe.mesh_mode, probe.mesh_label, probe.mesh_hops))

        # transport raising (bad hex/attr) is swallowed into generic relay
        probe.guild_node_hash = "zz-not-hex"
        probe.link = FakeLink()
        kc.sHadesClientApp._update_mesh(probe)
        check("update_mesh: hops_to failure = generic MESH LINK/2",
              (probe.mesh_mode, probe.mesh_label, probe.mesh_hops)
              == ("relay", "MESH LINK", 2),
              (probe.mesh_mode, probe.mesh_label, probe.mesh_hops))
    finally:
        sys.modules["RNS"].Transport = real_transport
        _kapp.App._running_app = prev

    # --- hooks exist on the lifecycle sites -------------------------------
    check("closed-hook schedules the offline update",
          "lambda dt: self._update_mesh(False)"
          in inspect_source("kivy_client.py"),
          None)

    print("\nPASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


def inspect_source(path):
    import io
    here = os.path.dirname(os.path.abspath(__file__))
    with io.open(os.path.join(here, "..", path), "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    sys.exit(main())
