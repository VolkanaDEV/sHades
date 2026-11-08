# plan_ui.md — Discord-like interaction & administration (v1 design)

Companion to plan.md (the guild/ledger design). Everything here rides the same
transport as the rest of the API: **JSON over RNS Link requests** on
`nomadnetwork.node`. HTML, HTTP and IP-socket APIs remain anti-patterns; the
guild never becomes a web server.

Status: U1-U4 are DONE (icons; receipts/reactions/search; context menus +
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

## U5 — roles + admin whitelist

**Storage.**

```sql
CREATE TABLE roles (role_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE COLLATE NOCASE,
                    color TEXT,          -- 3-hex micron-style, optional
                    position INTEGER);   -- higher = more powerful
CREATE TABLE user_roles (user_hash TEXT, role_id INTEGER,
                         PRIMARY KEY (user_hash, role_id));
CREATE TABLE channel_overwrites (channel_id TEXT, role_id INTEGER,
                                 perms INTEGER,   -- bitmask; role_id 0 = @everyone
                                 PRIMARY KEY (channel_id, role_id));
```

**Permission bitmask** (single int, checked in one place `has_perm(viewer,
channel, bit)`): VIEW 1, POST 2, MANAGE_MESSAGES 4, MANAGE_CHANNEL 8,
MANAGE_ROLES 16, ADMIN 32. A member's perms = union of their roles'
overwrites for the channel, falling back to the `@everyone` overwrite;
`ADMIN` short-circuits everything.

**Admin whitelist.** `admin.txt` next to `moderators.txt` (same format, one
canonical user hash per line) seeds `ADMIN` at boot. `moderators.txt` seeds a
`Moderator` role with MANAGE_MESSAGES|MANAGE_CHANNEL. The existing
`is_moderator()` becomes `has_perm(viewer, channel, MANAGE_MESSAGES)`; all
existing mod call sites switch to it.

**Enforcement points.** `ingest()` → POST check; `channels/list` → VIEW filter;
react/receipts → VIEW; manage endpoints → respective bits. Site/term `put` →
MANAGE_CHANNEL. Existing behavior (open posting) preserved: default
`@everyone` overwrite = VIEW|POST.

**API.** `/api/v1/roles/list|create|delete|assign|unassign`,
`/api/v1/channels/patch {channel, overwrites|name|topic}`,
`/api/v1/channels/delete {channel}` (soft: `channels.kind='deleted'`? no —
hard delete + cascading message/receipt/reaction cleanup, guild choice).

**Client.** Role color drives username/member-list colors (micron-style 3-hex
like `theme.accent`); admin gets role editor; usernames in menus show roles.

Tests: permission matrix unit tests (the highest-value suite in this plan —
every bit × every endpoint), admin-whitelist boot test, overwrite fallback
tests.

## U6 — invites

The real join path is already "share host:port + node hash" (connect-by-
address). Two layers:

1. **Share-link (client-only, phase A):** `shades://<host>:<port>/<node_hash>`
   — right-click guild header → "copy invite"; client parses the string on the
   connect screen (prefills fields). No server changes.
2. **Join gate (phase B, optional):** guild-side `invites` table (code, uses,
   expiry) + registration policy setting (`open` | `code`); first identified
   link presents `/api/v1/join {code}` to register. Default stays open.

## U7 — friends + DMs (client-local)

DMs are plain LXMF client-to-client today; the guild stays out. Friends =
client-side roster in app data (`friends.json`: user_hash, nickname, note).
Right-click user → add friend; friends section above the member list; opening
a friend opens an LXMF DM conversation view (direct LXMRouter exchange,
existing client router). Presence for friends comes from the guild presence
payload when the friend is online there (best-effort).

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
