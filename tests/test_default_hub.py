#!/usr/bin/env python3
"""Default-hub auto-connect regression tests for the sHades Kivy client.

A fresh install (no saved session) must connect to the default community
hub without the user typing a server address. These tests cover the
failures that made that path crash or silently misbehave:

  1. `hub_address` / `hub_host` / `hub_port` were plain attributes that were
     never initialised in __init__. Any read before the first connect raised
     AttributeError -- which is exactly what a fresh boot does, because no
     session has been loaded yet.

  2. `_config_needs_update()` formatted the stored port with "%d" % port
     without checking that host AND port were both known. A session missing
     either one raised TypeError.

  3. `self.node_hash` is overloaded: it holds the GUILD's hash during
     connect, then is overwritten with THIS client's own node destination
     hash. Persisting it in the session saved the client's own hash, so the
     next resume looked up the wrong identity. The guild's hash now lives in
     its own `guild_node_hash` attribute.

  4. A saved session without a hub address must not be treated as a
     resumable session.

Run:  /usr/bin/python tests/test_default_hub.py
"""
import os
import sys

os.environ.setdefault("KIVY_GL_BACKEND", "mock")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

PASS, FAIL = [], []


def check(name, cond, extra: object = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


def main():
    import kivy_client as kc

    class _ThemeApp(kc.sHadesClientApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.theme_colors = dict(kc.DEFAULT_THEME_COLORS)

    app = _ThemeApp()

    # --- 1. the default hub is configured --------------------------------
    check("DEFAULT_HUB_ADDRESS is set",
          bool(kc.DEFAULT_HUB_ADDRESS), kc.DEFAULT_HUB_ADDRESS)
    check("DEFAULT_HUB_ADDRESS is host:port",
          ":" in kc.DEFAULT_HUB_ADDRESS and kc.DEFAULT_HUB_ADDRESS.count(":") == 1,
          kc.DEFAULT_HUB_ADDRESS)
    check("DEFAULT_HUB_HOST matches DEFAULT_HUB_ADDRESS",
          kc.DEFAULT_HUB_ADDRESS.startswith(kc.DEFAULT_HUB_HOST + ":"),
          "%s / %s" % (kc.DEFAULT_HUB_HOST, kc.DEFAULT_HUB_ADDRESS))
    check("DEFAULT_HUB_PORT is an int",
          isinstance(kc.DEFAULT_HUB_PORT, int), repr(kc.DEFAULT_HUB_PORT))
    check("DEFAULT_HUB_PORT is in the valid TCP range",
          1 <= kc.DEFAULT_HUB_PORT <= 65535, kc.DEFAULT_HUB_PORT)

    # --- 2. the hub attributes exist before any connect ------------------
    for attr in ("hub_address", "hub_host", "hub_port"):
        check("app.%s is initialised in __init__" % attr,
              hasattr(app, attr), "missing attribute")
    check("app.hub_address defaults to DEFAULT_HUB_ADDRESS",
          getattr(app, "hub_address", None) == kc.DEFAULT_HUB_ADDRESS,
          getattr(app, "hub_address", None))
    check("app.hub_port defaults to DEFAULT_HUB_PORT",
          getattr(app, "hub_port", None) == kc.DEFAULT_HUB_PORT,
          getattr(app, "hub_port", None))

    # --- 3. guild_node_hash is separate from node_hash -------------------
    check("app has guild_node_hash", hasattr(app, "guild_node_hash"),
          "missing attribute")
    check("guild_node_hash starts as None",
          app.guild_node_hash is None, repr(app.guild_node_hash))
    check("node_hash starts as None", app.node_hash is None, repr(app.node_hash))

    # --- 4. the fresh-install branch exists ------------------------------
    import inspect
    on_start_src = inspect.getsource(kc.sHadesClientApp.on_start)
    check("on_start has a fresh-install branch",
          "_auto_connect_default_hub" in on_start_src)
    check("on_start only resumes a session that has a hub_address",
          'sess.get("hub_address")' in on_start_src)

    check("app has _auto_connect_default_hub",
          hasattr(app, "_auto_connect_default_hub"))
    check("app has _connect_default_hub",
          hasattr(app, "_connect_default_hub"))
    check("app has _return_to_connect_screen (fallback)",
          hasattr(app, "_return_to_connect_screen"))

    # --- 5. the auto-connect targets the default hub ---------------------
    conn_src = inspect.getsource(kc.sHadesClientApp._connect_default_hub)
    check("_connect_default_hub uses DEFAULT_HUB_ADDRESS",
          "DEFAULT_HUB_ADDRESS" in conn_src)
    check("_connect_default_hub sets guild_node_hash",
          "guild_node_hash" in conn_src)
    check("_connect_default_hub saves the session",
          "_save_session" in conn_src)

    # --- 6. _config_needs_update tolerates a missing host or port -------
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cfgfile = os.path.join(tmp, "config")
        with open(cfgfile, "w") as f:
            f.write("[reticulum]\n  enable_transport = no\n\n[interfaces]\n"
                    "  [[TCPClientInterface]]\n    type = TCPClientInterface\n"
                    "    enabled = yes\n    target_host = 127.0.0.1\n"
                    "    target_port = 1234\n")

        app.hub_address = "1.2.3.4:5678"
        app.hub_host = "1.2.3.4"
        app.hub_port = 5678
        check("_config_needs_update returns True for a different port",
              app._config_needs_update(cfgfile) is True)

        # A session missing the port must not raise.
        app.hub_port = None
        try:
            result = app._config_needs_update(cfgfile)
            check("_config_needs_update tolerates hub_port=None",
                  isinstance(result, bool), repr(result))
        except Exception as e:
            check("_config_needs_update tolerates hub_port=None", False, repr(e))

        # A legacy config with enable_transport = yes must be rewritten.
        with open(cfgfile, "w") as f:
            f.write("[reticulum]\n  enable_transport = yes\n")
        app.hub_port = 5678
        check("_config_needs_update rewrites enable_transport = yes",
              app._config_needs_update(cfgfile) is True)

    # --- 7. the session stores the GUILD's hash, not our own ------------
    save_src = inspect.getsource(kc.sHadesClientApp._save_session)
    check("_save_session persists guild_node_hash",
          '"node_hash": self.guild_node_hash' in save_src)
    check("_save_session does NOT persist self.node_hash directly",
          '"node_hash": self.node_hash' not in save_src)

    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
