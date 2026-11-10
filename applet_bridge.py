#!/usr/bin/env python3
"""Micron Applet Bridge (v0) — sandboxed client-side compute for micron pages.

Implements micron_applet_bridge_spec.md (v0): untrusted WebAssembly applets
run inside wasmtime with fuel metering, epoch-deadline wall clock, a linear
memory cap, and a closed import allowlist. No WASI, no filesystem, no
network, no ambient authority of any kind. Failure modes are REFUSED (before
instantiation) and TRAP (per call); neither blocks static micron rendering.

Dependencies: wasmtime (pinned in CI server-tests deps). Stdlib otherwise.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

try:
    import wasmtime
    from wasmtime import Config, Engine, Func, FuncType, Linker, Memory, Module, Store, ValType
    HAVE_WASMTIME = True
except ImportError:  # pragma: no cover - CI installs wasmtime; keep import safe
    HAVE_WASMTIME = False
except OSError as e:
    # The wasmtime wheel is pure-python but loads a native library at import
    # time. If the wheel does not match the platform (e.g. the py3-none-any
    # wheel, which ships only a Windows DLL), the import raises OSError, not
    # ImportError. Treat that as "wasmtime unavailable" so the caller sees a
    # clean refusal instead of a traceback.
    print("wasmtime unavailable: %s" % e)
    HAVE_WASMTIME = False


DEFAULT_FUEL = 1_000_000
DEFAULT_EPOCH_TICKS = 20          # 20 x 100 ms increments = 2 s wall clock
DEFAULT_MEMORY_CAP = 16 * 1024 * 1024
DEFAULT_SIZE_CAP = 256 * 1024

# v0 allowlist: closed under naming. Anything else is REFUSED at load.
ALLOWED_IMPORTS = frozenset({("env", "render_runs")})


@dataclass
class AppletGrant:
    """What one page's applet may consume. Produced/verified from the page manifest."""
    sha256: str
    fuel: int = DEFAULT_FUEL
    epoch_ticks: int = DEFAULT_EPOCH_TICKS
    memory_cap: int = DEFAULT_MEMORY_CAP
    size_cap: int = DEFAULT_SIZE_CAP


class AppletRefused(Exception):
    """Load-time refusal (spec section 6 steps 1-3). Never instantiated."""


class MicronAppletBridge:
    """One loaded applet instance per page. One store per applet."""

    def __init__(
        self,
        epoch_interval: float = 0.1,
        run_epoch_thread: bool = True,
        on_refused=None,
        on_trap=None,
    ):
        if not HAVE_WASMTIME:
            raise AppletRefused("wasmtime not installed")
        cfg = Config()
        cfg.consume_fuel = True
        cfg.epoch_interruption = True
        self.engine = Engine(cfg)
        self.epoch_interval = epoch_interval
        self.on_refused = on_refused or (lambda reason: None)
        self.on_trap = on_trap or (lambda reason: None)
        self._epoch_stop = threading.Event()
        self._epoch_thread = None
        if run_epoch_thread and epoch_interval > 0:
            self._start_epoch_thread()
        self.grant: AppletGrant | None = None
        self.instance = None
        self.outputs: list[bytes] = []
        self.render_error: str | None = None

    # -- epoch wall clock ------------------------------------------------
    def _start_epoch_thread(self):
        def tick():
            while not self._epoch_stop.wait(self.epoch_interval):
                self.engine.increment_epoch()
        self._epoch_thread = threading.Thread(target=tick, daemon=True)
        self._epoch_thread.start()

    def close(self):
        self._epoch_stop.set()
        self.instance = None
        self.grant = None

    # -- load pipeline (order = spec section 6) --------------------------
    def load(self, module_bytes: bytes, grant: AppletGrant):
        # 1. Hash
        digest = hashlib.sha256(module_bytes).hexdigest()
        if digest != grant.sha256:
            self.on_refused(
                f"hash mismatch: manifest {grant.sha256[:12]}.. != "
                f"bytes {digest[:12]}..")
            raise AppletRefused(
                f"module sha256 {digest[:16]}.. does not match "
                f"manifest {grant.sha256[:16]}..")
        # 2. Size
        if len(module_bytes) > grant.size_cap:
            self.on_refused(f"module too big: {len(module_bytes)} > {grant.size_cap}")
            raise AppletRefused(f"module size {len(module_bytes)} exceeds cap {grant.size_cap}")
        # 3. Imports (enumerate BEFORE instantiation; unknown = refuse)
        module = Module(self.engine, module_bytes)
        bad = [(i.module, i.name) for i in module.imports
               if (i.module, i.name) not in ALLOWED_IMPORTS]
        if bad:
            self.on_refused(f"unknown import(s): {bad}")
            raise AppletRefused(f"unknown import(s) {bad}; allowlist is {sorted(ALLOWED_IMPORTS)}")
        # 4. Instantiate with store already limited.
        #    wasmtime v50 charges fuel to INSTANTIATION itself (default store
        #    fuel is 0 -> "all fuel consumed" before a single instruction of
        #    the module runs). Set the budget BEFORE instantiate; recharge
        #    per call in call_run.
        self.store = Store(self.engine)
        self.store.set_limits(memory_size=grant.memory_cap)
        self.store.set_epoch_deadline(grant.epoch_ticks)
        self.store.set_fuel(grant.fuel)
        linker = Linker(self.engine)
        linker.define(
            self.store, "env", "render_runs",
            # access_caller=True: the host callback MUST receive the wasmtime
            # Caller to reach the applet's own memory (default False would
            # prepend nothing and hand us raw wasm args instead, v50 API).
            Func(self.store, FuncType([ValType.i32(), ValType.i32()], []),
                 self._render_runs, access_caller=True),
        )
        try:
            self.instance = linker.instantiate(self.store, module)
        except Exception as e:  # WasmtimeError: e.g. memory minimum exceeds limits
            self.on_trap(f"instantiation failed: {e}")
            raise
        exports = self.instance.exports(self.store)
        mem = exports.get("mem")
        run = exports.get("run")
        if not isinstance(mem, Memory) or run is None:
            self.close()
            raise AppletRefused("module missing required exports 'mem' and 'run'")
        self.grant = grant
        self.outputs = []
        self.render_error = None
        return self

    # -- imports ---------------------------------------------------------
    def _render_runs(self, caller, ptr: int, ln: int):
        try:
            mem = caller.get("mem")
            data = mem.read(caller, ptr, ptr + ln)
            self.outputs.append(bytes(data))
        except Exception as e:  # bounds-checked; a page cannot crash the client
            self.render_error = f"render_runs read failed: {e}"

    # -- per-tick call ----------------------------------------------------
    def call_run(self, state: int = 0) -> dict:
        """Spec step 5/6: fuel + deadline reset per call; one `run(state)` call."""
        if self.instance is None or self.grant is None:
            raise AppletRefused("no applet loaded")
        self.outputs = []
        self.render_error = None
        self.store.set_fuel(self.grant.fuel)
        self.store.set_epoch_deadline(self.grant.epoch_ticks)
        try:
            rc = self.instance.exports(self.store)["run"](self.store, state)
            used = self.grant.fuel - self.store.get_fuel()
            return {"trapped": False, "return": rc, "outputs": b"".join(self.outputs),
                    "fuel_used": used, "fuel_cap": self.grant.fuel}
        except wasmtime.Trap as t:
            self.on_trap(f"run trapped: {t}")
            return {"trapped": True, "error": str(t), "outputs": b"".join(self.outputs)}
        except wasmtime.WasmtimeError as e:
            self.on_trap(f"run failed: {e}")
            return {"trapped": True, "error": str(e), "outputs": b"".join(self.outputs)}
