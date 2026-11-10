# Micron Applet Bridge — Import Allowlist Spec (v0)

Status: PROPOSED. Companion to plan.md's site/channel design. Affects the
kivy client only; the server never executes page-supplied code (it already
does not: `micron.py` is a pure parser).

## 1. Purpose

Micron pages served over channels (gopher-style, top-down, via `site_get`)
gain a limited client-side compute surface. Compute runs as WebAssembly
applets embedded in the page payload, executed by the kivy client inside the
wasmtime sandbox. The sandbox is the boundary; the allowlist below is the
boundary in words.

Design lineage: NomadNet's own dynamic pages run a shebang interpreter ON THE
NODE with full ambient authority (executable bit + `#!/usr/bin/python3`).
This spec moves interactive compute to the CLIENT under a real sandbox, so
the client never runs ambient code for stranger content and the node never
runs stranger code.

## 2. Threat model

- A page is untrusted input, same class as a stranger message. It may be
  hostile. The question is never "is this page safe"; it is "what can a
  hostile page reach".
- Reachable: CPU (fuel), wall clock (epoch), linear memory (capped), the
  host imports named in its grant, and nothing else.
- Not reachable: filesystem, network, RNS identity keys, other channels'
  state, the user's terminal, other processes, native code.

## 3. Module contract (v0)

A page applet is a WebAssembly core module (no WASI imports):

- Required export: `mem` (Memory), used by the host to deliver page input
  bytes at offset 0 (input region), size carried by the page manifest.
- Required export: `run(state: i32) -> i32` (offset of first output run, or
  a bridge-defined status). Host calls it once per render tick.
- Optional export: none beyond `mem`/`run` in v0. Extra exports are ignored,
  not trusted.

## 4. Import allowlist (v0)

The host binds these, and only these. A module requesting anything else is
refused at load time, BEFORE instantiation (unknown-import refusal, never a
runtime trap for the user to puzzle over).

| Import                  | Signature                 | Grant  | v0 behavior                                    |
|-------------------------|---------------------------|--------|------------------------------------------------|
| `env.render_runs`       | (ptr: i32, len: i32)      | always | Host reads `len` bytes at `ptr` from the applet's own linear memory and appends them to the page's rendered output. |
| `env.get_state`         | reserved (not bound v0)   | -      | Not offered. Requesting it = refusal until v1. |
| `env.set_state`         | reserved (not bound v0)   | -      | Not offered. Requesting it = refusal until v1. |
| `env.fetch_page`        | reserved (not bound v0)   | -      | Not offered. Requesting it = refusal until v1. Reserved for signed capability grants. |

Rule: the allowlist is closed under naming. There is no dynamic import
resolution, no WASI, no `wasi_*`, no `wasi_unstable`, no host environment
leakage. A v1 module that needs state or paging gets it through signed
per-page capability grants declared in the channel manifest, never ambient.

## 5. Resource budgets (v0 defaults)

| Resource    | Law                            | Default       |
|-------------|--------------------------------|---------------|
| CPU         | wasmtime fuel per `run` call   | 1,000,000     |
| Wall clock  | epoch deadline per `run` call  | 20 ticks (2 s at 100 ms increments) |
| Linear memory | `Store.set_limits(memory_size)` | 16 MiB     |
| Module size | sha256-verified manifest bound | 256 KiB       |

Fuel exhaustion and epoch deadline are both traps surfaced to the user as
"page compute exceeded its budget", and the page still renders its static
micron content (compute failure must never blank the page).

## 6. Load pipeline (ordered, fail-closed)

1. Hash: module bytes sha256 must equal the manifest's recorded hash, else REFUSED.
2. Size: module bytes must be <= manifest size bound, else REFUSED.
3. Imports: enumerate module imports; every (module, name) must be in the
   allowlist; else REFUSED (documented listing, no instantiation).
4. Instantiate with the store already limited (memory cap), fuel preloaded
   (wasmtime v50 charges fuel to instantiation itself; default store fuel is
   0 and dies as "all fuel consumed" at instantiate), else TRAP surfaced.
5. Per call: set fuel to budget, set epoch deadline, call `run` once.
6. Output: bytes collected through `render_runs` are appended to the page
   render; the bridge never writes to disk.

## 7. Failure taxonomy (user-visible)

- REFUSED: page ships fine, applet banner "applet refused: <reason>".
- TRAP: page ships fine, applet banner "page compute stopped: <trap reason>".
- Neither blocks static micron rendering, channel messages, or other channels.

## 8. Transport

Applet bytes ride the existing partial mechanism: a micron partial whose
target has an `.wasm` media extension is fetched in chunks through
`site_get(channel, path, offset, length)`, reassembled, hash-checked, then
loaded. Chunk sizes follow existing site blob limits. The host renders ONLY
after hash verification passes.

## 9. Open questions for the owner

1. Should applets be opt-in per channel (manifest flag) or global default-off?
2. v1 state model: channel-scoped kv with per-page namespaces, signed grants?
3. Cache policy: keep verified modules in memory only (no disk cache) unless
   the owner prefers disk under the existing blobs directory rules?

## 10. Implementation receipt

Spike: `applet_bridge.py` + `tests/test_applet_bridge.py` (engine-agnostic
CLI test, `wasmtime` pinned in CI's server-tests deps). Six checks: allowlist
bind + echo output, unknown-import refusal pre-instantiation, fuel-exhaustion
trap, hash mismatch refusal, memory-cap enforcement, epoch deadline trip.
