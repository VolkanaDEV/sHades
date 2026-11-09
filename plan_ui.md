# plan_ui.md — Discord-like interaction & administration (v1 design)

Companion to plan.md (the guild/ledger design). Everything here rides the same
transport as the rest of the API: **JSON over RNS Link requests** on
`nomadnetwork.node`. HTML, HTTP and IP-socket APIs remain anti-patterns; the
guild never becomes a web server.

Status: U1-U5 are DONE (icons; receipts/reactions/search; context menus +
mod endpoints + channel deletion). U4-U8 are designed here, not built.

## U0 — foundations that already exist

- `/api/v1/*` JSON envelope, identified-link auth (`viewer_of`).
- `channels(kind)` = chat / site / term; `receipts`, `reactions` tables;
  `banned`/`muted` moderation; `moderators.txt` boot whitelist.
- Stream: one subscription per link; `msg`, `edit`, `del`, `typing`,
  `presence` (with `active_channel`), `reaction`, `site_patch`, `term_data`.

## U1 — icon set (DONE)

Default Kivy font (Roboto) lacks ☰/⌕/⚙ → tofu boxes. Top-bar icons are
canvas-drawn widgets (`HamburgerIcon`, `SearchIcon`, `GearIcon`); geometry is
pure math in `ui_geom.py` (Kivy-free, unit-tested in `tests/test_icons_geom.py`).
Rule going forward: **no non-ASCII glyphs in rendered UI strings** — draw or
ASCII-fallback (`tests` grep `kv_md.kv` for non-ASCII in CI).

## U2 — receipts / reactions / search (DONE)

Per-user read watermarks (`receipts`), unread badges on channel buttons,
reaction chips on messages (toggle, ASCII shortcodes), `⌕` search popup.

## U3 — context menus (DONE)

**Framework.** One `ContextMenu` widget: a Popup anchored at pointer position
(usually a vertical list of actions), fed `(label, enabled, callback)` rows.
Desktop right-click: `on_touch_down` with `touch.button == "right"`; mobile
long-press: `Clock.schedule_once(0.5s)` on touch_down, cancelled on touch_up.
One mixin `ContextMenuMixin` any widget can inherit; one code path, two input
gestures. Touch ripple stays on left-press; right-click must not trigger the
left action.

**Menus.**

| surface | actions |
|---|---|
| message | copy text, reply (quote into input), react, edit (own), delete (own) |
| member-list row / username | profile, mention (@name → input), mute (mod), ban (mod) |
| channel button | open, copy channel id, mute channel (client-local), delete channel (mod) |
| guild header | copy invite link (`shades://host:port/node_hash`) |

Server work that landed with it: `/api/v1/whoami` (drives moderator-affordance
visibility), `/api/v1/mod/ban|unban|mute|unmute` (moderator gate, 32-hex user
hash validation, self-moderation rejected), `/api/v1/channels/delete`
(cascade: messages/attachments/reactions/receipts/members/state + site files +
term sessions; last-channel protection; `channel_deleted` stream event).
Client-local: muted-channel persistence (badges suppressed).

## U4 — slash-commands (DONE, one dispatcher, two frontends)

A single server-side dispatcher `_dispatch_command(viewer, line)` shared by
the term console (`_term_execute` wraps it) and chat:

- `/api/v1/command {input}` → runs it, returns the output **ephemerally to
  the requester** (never posted to the channel). Input is sanitized exactly
  like the console (ANSI/control-strip) BEFORE dispatch — the term path
  sanitizes in term/data, and this endpoint must too (a gap the tests caught:
  unsanitized /msg content reached the ledger).
- client input bar: text starting `/` goes to `/command`; output renders as
  an ephemeral console row in the grid. `/help` prepends client-local
  commands. The client refreshes channels/list every ~15 s so renames,
  topics and badges stay current.
- command table: `create <name> [kind]` (site/term: mod), `delete <channel>`
  (mod), `rename <channel> <name>` (mod), `topic <channel> <text>` (mod;
  `channels.topic` column via _migrate_schema, in list/info),
  `ban/unban/mute/unmute <user>` (mod; by 32-hex hash or exact username via
  `_resolve_user`). Client-local only: `/me` (italic post), `/join`,
  `/search <q>` (opens the popup), `/invite` (copy share-link), `/help`.
- permission errors are per-command plain output; unknown commands say so.
- deferred to later phases: `pin` (needs a pins table), `slowmode` (needs
  channel settings storage), `roles` (U5).

Tests: `tests/test_command.py` (dispatcher via the JSON endpoint: ephemeral
contract, name/space moderation, create/rename/topic/delete, quit/clear
inertness, sanitisation, rate limit) + integ slash checks over the mesh.

## U5 — roles + admin whitelist (DONE)

- tables: `roles(role_id, name UNIQUE, color, position, perms)`,
  `user_roles(user_hash, role_id)`, `channel_overwrites(channel_id, role_id,
  perms)` with role_id 0 = @everyone. MANAGE_*/ADMIN live on the role itself
  (guild-wide); VIEW/POST live in the per-channel overwrites.
- boot: `load_role_seeds(STORAGE)` — admin.txt -> Admin role (all bits,
  position 100), moderators.txt -> Moderator (MANAGE_MESSAGES|MANAGE_CHANNEL,
  position 50). INSERT OR IGNORE by name; re-runnable.
- `has_perm(viewer, cid, bit)`: guild bits delegate to
  `has_role_perm(viewer, bit)`; VIEW/POST = @everyone overwrite (default
  VIEW|POST) UNION role.perms UNION per-role overwrite; ADMIN short-circuits.
  `has_role_perm` also honours the legacy MODERATORS set for
  MANAGE_MESSAGES|MANAGE_CHANNEL so pre-roles moderators keep working.
- enforcement matrix (all server-side): ingest POST (silent drop),
  channels/list VIEW (hidden channels absent), legacy /api/messages VIEW,
  react/receipts VIEW (via the message's channel), search VIEW (scoped 403,
  unscoped result filtering), site/get + term/open VIEW, rename/topic/delete
  + channels/patch MANAGE_CHANNEL, ban/mute guild-wide is_moderator, roles
  endpoints MANAGE_ROLES. ADMIN short-circuits.
- endpoints: `/api/v1/roles/list|create|delete|assign|unassign`,
  `/api/v1/channels/patch {channel, topic?, overwrites?}` (overwrites is
  {role_id: perms}, perms null deletes the row), `/api/v1/whoami` extended
  with is_admin + roles.
- client: PERM_* constants mirror the server (UI gating only);
  whoami -> my_roles/i_am_admin; `_can_manage_roles()` gates a "roles..."
  item on the member context menu -> `_open_roles_popup` (role list with
  [x]/[  ] assign toggles + create-role row). Role colors on usernames and
  the full channel-overwrite editor are deferred to U8.

Tests: `tests/test_roles.py` (36 checks: seeding, union matrix, deny/grant
over writes, API gating, patch semantics, slash permission) + integ U5 section
over the mesh (seeding, role CRUD, hidden-channel list filter, victim POST
denied then re-allowed by role assignment).

## U6 — invites

The real join path is already "share host:port + node hash" (connect-by-
address). Two layers:

1. **Share-link (client-only, phase A: DONE).** `shades://<host>:<port>/<node_hash>`
   — right-click guild header → "copy invite" (U4) builds it via
   `invite.build_invite`; the connect screen parses it via `invite.parse_invite`
   (new Kivy-free `invite.py`, stdlib only): paste into the hub-address field,
   focus loss / enter splits it into `ip:port` + node-hash fields (explicitly
   typed hashes win), and `connect_to_guild` re-parses as a fallback so Connect
   works on a raw paste. Tolerates SHADES case, whitespace, missing port,
   missing hash, bracketed IPv6, trailing slash; invalid hash/port are dropped
   to None (auto-discovery covers the rest). The link carries the RNS
   *interface* endpoint only — no HTTP anywhere; after the dial everything
   rides RNS links.
2. **Join gate (phase B, optional):** guild-side `invites` table (code, uses,
   expiry) + registration policy setting (`open` | `code`); first identified
   link presents `/api/v1/join {code}` to register. Default stays open.

## U7 — friends + DMs (DONE, client-local)

- roster: `dms.py` (Kivy-free, stdlib) loads/saves `friends.json`
  (add dedupes by hash and re-nicknames, remove filters, junk entries are
  filtered on load, corrupt file -> []). Hash validation: 32-hex only.
- DM transport: the client NOW RUNS A REAL LXMRouter (U7 addition — the
  client previously had no LXMF inbound path at all):
  `LXMRouter(identity=client_identity, storagepath=appdata/lxmf)` +
  `register_delivery_identity` + `register_delivery_callback` +
  immediate `announce(delivery.hash)` so friends can hear us. Inbound DMs =
  everything arriving at our lxmf.delivery destination (guild chat never
  uses that path — it rides the API link). Router init failure just
  disables DMs; `on_stop` tears the router down via `exit_handler()`.
- send: `RNS.Identity.recall(peer_hash)` -> their lxmf.delivery dest ->
  `LXMessage(dest, our_delivery, text, title="sHades dm", DIRECT)` through
  `router.handle_outbound` on a worker thread. Unknown identity (peer never
  announced) -> clear status message, no history row.
- history: `dms/<user_hash>.json`, {ts, dir in|out, text[:4000]}, 200-row
  cap, atomic tmp+rename writes.
- UI: friends section heads the member sidebar (gold header, green dot when
  the friend is in the guild presence map, gray otherwise); "message" and
  "add/remove friend" items on both the member-row and message-row context
  menus; DM popup = thread + input, refreshes live when a DM lands while
  it's open, status line otherwise.
- deferred-lambda gotcha (caught by flake8): an `except ... as e` name is
  DELETED when the block exits — never reference `e` inside a
  Clock.schedule_once lambda scheduled from that block; bind it first.

Tests: `tests/test_dms.py` (11: roster dedupe/rename/remove/validation,
persistence + corruption, junk filtering; DM history append/cap/validate/
corrupt recovery). The LXMF wire path itself needs two live clients —
not covered by integ; verify manually on two machines.

## U8 — server config panel (admin only)

Server: `admin.txt` → ADMIN; panel endpoints gated by `has_perm(ADMIN)`:
`/api/v1/admin/config {name?}`, `/api/v1/admin/mods list|add|remove`
(rewrites moderators.txt), `/api/v1/admin/audit` (see below), registration
policy (U6), default slowmode. **Audit log:** `audit_log` table (ts, actor,
action, target, detail) written by ban/mute/delete/channel/role mutations;
viewable in the panel and as a Micron `/page/sHades/audit.mu` for NomadNet
users.

Client: "server settings" behind the guild-header menu (admin only — menu
entry hidden unless the guild says `you_are_admin` in `/api/v1/presence` or a
new `/api/v1/whoami`): server name, moderator management, audit viewer,
channel create/delete/rename, role editor, policy toggles.

## Build order

| phase | scope | test strategy |
|---|---|---|
| U3 | context-menu framework + message/member/channel menus (existing endpoints) | mock-GL probe + geometry-style pure helpers for menu construction |
| U4 | `/api/v1/command` + slash table + client input routing | dispatcher unit tests, permission matrix, integ |
| U5 | roles, overwrites, admin.txt, permission checks everywhere | permission-matrix suite (largest), boot seeding test |
| U6a | invite share-link (client only) | parser unit tests |
| U7 | friends roster + LXMF DM view | roster unit tests, mock-GL probe |
| U8 | admin panel + audit log | admin gating tests, audit write tests |
| U6b | join gate (optional) | gate tests |

Rules carried through every phase: identified-link auth only; permissions
checked server-side (the client menu is a convenience, never the gate); JSON
over RNS only; no new client-side trust of payload identity; every new table
through `SCHEMA` (auto-created), every new column through `_migrate_schema`.
