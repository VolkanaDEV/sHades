#!/usr/bin/env python3
"""Micron Applet Bridge v0 tests — no RNS mesh needed, no kivy needed.

Pins the sandbox laws from micron_applet_bridge_spec.md:
  1. allowlisted import binds, echo applet emits output, fuel is metered;
  2. unknown import refuses BEFORE instantiation;
  3. fuel exhaustion stops a runaway module;
  4. hash mismatch refuses before instantiation;
  5. linear-memory cap is enforced at instantiation;
  6. epoch deadline stops a runaway module on wall clock.
"""
import os
import sys
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name
          + (("  | " + str(extra)) if extra and not cond else ""))


try:
    import wasmtime  # noqa: F401  (availability probe only)
    HAVE = True
except ImportError:
    HAVE = False
except OSError as e:
    # Platform-mismatched wheel: the native lib fails to load at import.
    print("wasmtime unavailable: %s" % e)
    HAVE = False

if HAVE:
    from applet_bridge import MicronAppletBridge, AppletGrant, AppletRefused
    from wasmtime import wat2wasm

    def grant_for(b, **kw):
        return AppletGrant(sha256=hashlib.sha256(b).hexdigest(), **kw)

    ECHO = wat2wasm("""
(module
  (import "env" "render_runs" (func $rr (param i32 i32)))
  (memory (export "mem") 16)
  (data (i32.const 64) "hello")
  (func (export "run") (param i32) (result i32)
    (call $rr (i32.const 64) (i32.const 5))
    (i32.const 1))
)
""")
    RUNAWAY = wat2wasm("""
(module
  (memory (export "mem") 1)
  (func (export "run") (param i32) (result i32)
    (loop $l (br $l))
    (unreachable))
)
""")
    UNKNOWN_IMPORT = wat2wasm("""
(module
  (import "env" "missing_fn" (func $m))
  (memory (export "mem") 1)
  (func (export "run") (param i32) (result i32)
    (call $m)
    (i32.const 0))
)
""")
    BIG_MEMORY = wat2wasm("""
(module
  (memory (export "mem") 32)
  (func (export "run") (param i32) (result i32)
    (i32.const 0))
)
""")
else:
    ECHO = RUNAWAY = UNKNOWN_IMPORT = BIG_MEMORY = None

SMALL = dict(fuel=250_000, epoch_ticks=2, memory_cap=1 << 20, size_cap=65_536)

if HAVE:
    # 1. allowlist binds, echo emits, fuel metered
    try:
        b = MicronAppletBridge(run_epoch_thread=False)
        r = b.load(ECHO, grant_for(ECHO, **SMALL)).call_run()
        check("1a echo output via render_runs", r["outputs"] == b"hello", r)
        check("1b fuel metered (used>0, under cap)",
              0 < r["fuel_used"] <= SMALL["fuel"], r.get("fuel_used"))
        check("1c not trapped", r["trapped"] is False, r)
        check("1d run returned", r["return"] == 1, r)
        b.close()
    except Exception as e:
        check("1 echo happy path", False, repr(e))

    # 2. unknown import refuses pre-instantiation
    refused = None
    try:
        b = MicronAppletBridge(run_epoch_thread=False)
        b.load(UNKNOWN_IMPORT, grant_for(UNKNOWN_IMPORT, **SMALL))
        b.close()
    except AppletRefused as e:
        refused = e
    except Exception as e:
        refused = f"wrong exc: {e!r}"
    check("2 unknown import REFUSED", refused is not None and "missing_fn" in str(refused), refused)

    # 3. fuel stops the runaway
    t0 = time.time()
    try:
        b = MicronAppletBridge(run_epoch_thread=False)
        r = b.load(RUNAWAY, grant_for(RUNAWAY, **SMALL)).call_run()
        b.close()
        check("3 runaway fuel-trapped", r["trapped"] is True, r)
        check("3b trap mentions fuel", "fuel" in r.get("error", "").lower(), r.get("error"))
        check("3c stopped inside wallclock", time.time() - t0 < 5.0, round(time.time() - t0, 2))
    except Exception as e:
        check("3 runaway fuel-trapped", False, repr(e))

    # 4. hash mismatch refused
    try:
        b = MicronAppletBridge(run_epoch_thread=False)
        b.load(ECHO, AppletGrant(sha256="00" * 32, **SMALL))
        b.close()
        check("4 hash mismatch REFUSED", False, "load succeeded")
    except AppletRefused as e:
        check("4 hash mismatch REFUSED", "does not match" in str(e), e)

    # 5. memory cap enforced at instantiation
    capped = False
    try:
        b = MicronAppletBridge(run_epoch_thread=False)
        b.load(BIG_MEMORY, grant_for(BIG_MEMORY, **SMALL))
        b.close()
    except AppletRefused:
        pass  # missing exports path (memory dropped) still a refusal, acceptable
    except Exception:
        capped = True  # instantiation error = the expected cap enforcement
    check("5 memory cap enforced", capped, "module with 2MiB memory vs 1MiB cap")

    # 6. epoch deadline stops runaway on wall clock
    t0 = time.time()
    try:
        b = MicronAppletBridge(epoch_interval=0.05, run_epoch_thread=True)
        r = b.load(RUNAWAY, grant_for(RUNAWAY, fuel=10**12, epoch_ticks=2)).call_run()
        b.close()
        check("6 epoch deadline trapped", r["trapped"] is True, r)
        check("6b wallclock bound", time.time() - t0 < 3.0, round(time.time() - t0, 2))
    except Exception as e:
        check("6 epoch deadline trapped", False, repr(e))

print()
print(f"applet bridge: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED checks:", FAIL)
sys.exit(1 if FAIL else 0)
