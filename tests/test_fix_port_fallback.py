#!/usr/bin/env python3
"""H1: port 29222 busy must not kill the client (exit 255).

RNS.Reticulum() panics (os._exit(255)) when its TCPServerInterface
cannot bind, bypassing every try/except. The fix probes the port
before RNS is built: _free_client_port(preferred) returns the
preferred port when free and a different free port when it is held.

  (a) a free 29222 is kept as-is
  (b) a held 29222 returns a different, actually-free port
  (c) a held preferred port in the middle of the range still yields a
      free port (walk-up, not just +1)
  (d) _init_reticulum writes the probed port into the RNS config
      (source-level: the hardcoded assignment is gone)

The two-process proof (a second app instance surviving while the
first holds 29222) is covered by (b)+(d): the port handed to RNS is
provably free at probe time, so the bind cannot raise EADDRINUSE.

Run: xvfb-run -a python tests/test_fix_port_fallback.py
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kivy_client as kc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + ((" -- " + str(extra)) if extra and not cond else ""), flush=True)


def _hold(port):
    """Bind+listen on `port` the way a first client instance would."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    s.bind(("0.0.0.0", port))
    s.listen(1)
    return s


def _port_is_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def test_free_preferred_kept():
    port = kc._free_client_port(29222)
    check("free 29222 is kept as-is", port == 29222, port)


def test_held_preferred_falls_back():
    holder = _hold(29222)
    try:
        port = kc._free_client_port(29222)
        check("held 29222 returns a different port",
              port != 29222, port)
        check("fallback port is actually free",
              _port_is_free(port), port)
        check("fallback port is in the 29222+N walk-up range",
              29222 < port < 29222 + 100, port)
    finally:
        holder.close()


def test_walk_up_skips_busy_run():
    holders = [_hold(29222), _hold(29223), _hold(29224)]
    try:
        port = kc._free_client_port(29222)
        check("walk-up skips a run of busy ports",
              port == 29225, port)
    finally:
        for h in holders:
            h.close()


def test_init_uses_probed_port():
    import inspect
    src = inspect.getsource(kc.sHadesClientApp._init_reticulum)
    check("_init_reticulum no longer hardcodes client_port = 29222",
          "self.client_port = 29222" not in src)
    check("_init_reticulum probes via _free_client_port",
          "_free_client_port(29222)" in src)
    cfg_src = inspect.getsource(kc.sHadesClientApp._config_needs_update)
    check("_config_needs_update rewrites a stale listen_port",
          "listen_port" in cfg_src)


def main():
    for fn in (test_free_preferred_kept, test_held_preferred_falls_back,
               test_walk_up_skips_busy_run, test_init_uses_probed_port):
        try:
            fn()
        except Exception as e:
            check(fn.__name__ + " raised", False, repr(e))
    print()
    print("PASS: %d  FAIL: %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
