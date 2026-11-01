# Review of plan.md — "Option B: Dynamic Page Server"

**Verdict: not implementable as written.** The concept (guild = LXMF store-and-forward node + Micron page server backed by SQLite) is sound, and executable pages are a real NomadNet mechanism — but the plan invents most of the APIs on both the LXMF and NomadNet sides. Verified against the installed stack (rns 1.1.3, lxmf 0.9.4, nomadnet 0.9.8) and reproduced live: the script crashes at startup before writing a single page, and even if that were patched, every form, link, partial, and image in the UI would be dead markup.

## 1. Verified startup blockers (all reproduced live)

| # | Plan code | Reality | Evidence |
|---|---|---|---|
| B1 | `router.register_propagation_node(guild_dest)` (line 118) | Method does not exist in lxmf 0.9.4 **or** latest 1.2.1 → `AttributeError` | Live repro: `'LXMRouter' object has no attribute 'register_propagation_node'`; method list in `LXMF/LXMRouter.py` |
| B2 | `RNS.Transport.announce(guild_dest)` (line 121) | `Transport.announce` was removed; RNS 1.1.3 has only `Destination.announce()` → `AttributeError` | Live repro: `type object 'Transport' has no attribute 'announce'` |
| B3 | Second `register_delivery_identity()` for the user identity (lines 117 + 131) | "Currently only one delivery identity is supported per LXMF router instance" (`LXMRouter.py:330-333`) → returns `None` → next line crashes on `user_lxm_identity.hash` | Live repro: `second register returned: None` |
| B4 | `original_handle_inbound = router.handle_inbound` (line 196) | No such method exists (`hasattr` = False) → patch is dead code; nothing ever calls it | Live repro: `hasattr checks: False False` |

A real propagation node is `LXMRouter(identity=..., storagepath=...)` + `router.enable_propagation()` (`LXMRouter.py:533`). Note the router creates its **own** `lxmf.propagation` destination from its identity (`LXMRouter.py:169`) — a custom `lxmf.propagation.guild.main_guild` destination cannot plug into the LXMF client sync protocol, which expects the standard `lxmf.propagation` hash.

## 2. Verified NomadNet-side blockers

**B5 — The env vars are invented.** NomadNet executes page scripts with only: `PATH`, `link_id`, `remote_identity`, and one env var per submitted field named `field_<name>` / `var_<name>` (`nomadnet/Node.py:162-176`). `REMOTE_PEER`, `REQUEST_PATH`, `QUERY_STRING`, `REQUEST_METHOD` do not exist. Every script's `parse_qs(os.environ.get("QUERY_STRING"))` reads nothing: no channel selection, no message submit, no settings save. (The `ENV_INJECTION` hack only papers over this by hardcoding paths inside the scripts.)

**B6 — The Micron is invented.** From `nomadnet/ui/textui/MicronParser.py` and the built-in Guide:

| Plan writes | Reality |
|---|---|
| `[Settings](/reticord/settings.py)` | Links are `` `[label`target] `` — backtick-separated. Markdown style renders as literal text. |
| `l[...]`, `c...` line prefixes | No such line prefixes — the `l`/`c` print literally. Alignment (`c`/`l`/`r`) exists only inside backtick formatting runs. |
| `!bold!F=58fChannels!f`, `!italic!...!i` | Formatting is `` `!bold!` `` / `` `*italic*` `` / `` `_underline_` `` inside backtick runs; colors are `` `F58f `` / `` `B333 `` (3 hex digits). |
| `#!/fg=ffffff`, `#!bg=2f3136` | Lines starting with `#` are **comments** (parser returns None, MicronParser.py:161). Page colors are set by `#!bg=xxx` — exactly 3 hex chars — checked in Browser.py:1094-1096; 6-digit values silently do nothing. |
| `{/reticord/channel.py?channel=X}` | Partials exist but are `` `{url} ``, `` `{url`refresh} ``, `` `{url`refresh`fields} `` — leading backtick, no `?` query strings. |
| `[Send](...?content=<user_input>)` | Fields submit via link field-lists: `` `[Send`:/page/reticord/update.mu`*] `` or `` `channel|content] ``; fields are declared `` `<name`default_value> ``. |
| `(/reticord/images/x.png\|w=100%\|h=auto...)` | **No image rendering exists in NomadNet 0.9.8 at all** — no image syntax in MicronParser (it's a urwid TUI). |

Also missing: dynamic pages need `#!c=0` as the first line — peers cache pages ~12h by default (Guide.py:329), so a chat page without it goes stale.

**B7 — The URLs are malformed.** Browser URL grammar (`Browser.py:716-745`): `hash` alone, `hash:/path`, or `:/path` (current node). A bare `/reticord/index.py` raises "Malformed URL" when a remote visitor clicks it. Paths must also carry the `/page/` prefix (`serve_page` does `path.replace("/page", pagespath, 1)`, Node.py:117).

**B8 — Remote users cannot see the guild at all.** Pages under `~/.nomadnetwork/storage/pages/` are served by *the operator's own local NomadNet client* — its `nomadnetwork.node` destination (`Node.py:18`). The guild's LXMF destinations have no request handlers registered, so "browse to your guild's destination hash" (How-to-Use step 3) fails. The LXMF side and the page side are two separate processes with two different destination hashes — the "guild is a single entity" premise is not achieved. Also: a node's default page is `/page/index.mu`, so `index.py` is never hit by default (Node.py:60-62).

**B9 — update.py is architecturally broken.** Per-request it runs `RNS.initialize()` + a fresh `LXMRouter` (line 371-372): multiple Reticulum instances on one machine, lxmf storage contention, shared identity files. And it builds the OUT destination from `user_identity` — since destination hashes derive from identity + aspects, that hash is neither the guild's channel destination nor the `addr_hash` stored in the DB (a third, unrelated value — `full_hash(f"{guild_id}:{name}")`). It sends into the void even locally. Channel destinations were never announced either, so no path ever exists for them.

## 3. Design flaws beyond the bugs

1. **Channel = separate LXMF destination is the wrong primitive.** One delivery identity per router (B3) makes N channel destinations unregistrable through LXMF; announcing N destinations is announce spam on a mesh with rate limits; and LXMF link handling would have to be reimplemented per destination. Simpler and interop-friendly: one `lxmf.delivery` destination for the guild; channel selected by convention in the message (leading `#channel` line or an LXMF field — `FIELD_GROUP`/`FIELD_THREAD` exist in 0.9.4+).
2. **Propagation is asserted, never built.** For peers to receive propagated messages they must `set_outbound_propagation_node()` + `request_messages_from_propagation_node()` (`LXMRouter.py:385-490`) — no client side exists in the plan. Decide whether v1 even needs propagation (browsing already reads the live SQLite store; propagation matters only for offline delivery).
3. **Micron injection.** User content is printed raw into pages — a message containing `` `[Click`...page] `` or `` `F00f `` rewrites every viewer's screen (links to arbitrary pages, layout, fake UI). Escape backticks, brackets, and line-leading `>`/`-`/`#`/`\`.
4. **SQLite across processes** (daemon + page scripts) with default journal → lock errors. WAL + `busy_timeout` at minimum.
5. **Unpinned deps.** "pip install rns lxmf" pulls lxmf 1.2.1, whose stamp enforcement differs from the 0.9.4 the plan targets. Pin versions.

## 4. What survives

- Executable pages are real: exec bit + shebang → executed with stdout served as the page (`Node.py:162-177`).
- The DB schema, settings flow, `users` table keyed by LXMF source hash, and the spec-mapping table's intent are all reasonable.
- The guild-as-single-entity goal is achievable — just not this way.

## 5. Recommended rebuild (much smaller than the plan)

One process, no symlinks, no subprocess pages, no env vars:

1. `RNS.Reticulum(...)`; guild identity; `LXMRouter(identity=guild_identity, storagepath=...)`; `register_delivery_identity(guild_identity, delivery_callback=...)` → route inbound LXMFs into SQLite (channel parsed from content/field).
2. Optional: `router.enable_propagation()` for store-and-forward of the same traffic.
3. Become a browsable node: `node_dest = RNS.Destination(guild_identity, IN, SINGLE, "nomadnetwork", "node")` + `node_dest.register_request_handler("/page/index.mu", response_generator=serve_page, allow=RNS.Destination.ALLOW_ALL)` — exactly what `nomadnet/Node.py:57-67` does. `serve_page(path, data, ...)` receives submitted fields in `data` (`field_<name>`/`var_<name>`) — render Micron in-process from SQLite. This makes the guild's node hash genuinely browsable by any NomadNet peer, which is the whole point.
4. UI in real Micron: `#!c=0` + `#!bg=2f3` headers; `` `{:/page/reticord/channel.mu`5} `` auto-refreshing partial for the live channel view; `` `[Send`:/page/reticord/update.mu`*] `` submit; `` `[name`<hash>:/page/profile.mu] `` for profile links; escape user content.
5. Images: cannot render in NomadNet; if wanted, save attachments and serve them as downloads via a `/file/...` request handler (browser downloads, Guide/Browser.py `download_local_file`), and use `FIELD_IMAGE`/`FIELD_FILE_ATTACHMENTS` for LXMF clients like Sideband.

Estimated delta: the LXMF section needs rewriting against real APIs (B1-B4), the page section is a rewrite (B5-B7), and B8 changes the serving model — so realistically plan.md describes ~20% of the working design.
