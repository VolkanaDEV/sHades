# sHades

A Reticulum-native Discord clone. A **guild** is one process that:

- receives channel posts as **LXMF messages** (`lxmf.delivery`, `#channel` first token),
- stores them in SQLite (the channel ledger) with edit/delete tombstones and a
  per-channel revision counter,
- **is a NomadNet node** (`nomadnetwork.node`): the per-channel Micron pages ARE the
  chatroom — header, auto-refreshing message stream, compose bar, members, settings,
- runs an **LXMF propagation node** (`lxmf.propagation`) so posts sent while no direct
  path exists still arrive (they ride the propagation network and are delivered locally),
- exposes `/api/*` endpoints (msgpack over RNS requests) for the future Kivy client.

DMs are plain LXMF, client-to-client. The guild is never involved.

## Run

    /usr/bin/python3 sHades.py                 # storage in ~/.sHades

First run creates the Reticulum identity, a standalone RNS config (AutoInterface for
LAN discoverability, `share_instance = no`), and writes `guild_info.json` with the
three destination hashes.

- **Browse:** in NomadNet, add the `node_hash` from guild_info.json as a node.
- **Post from an LXMF client:** message the `delivery_hash`; first token routes the
  channel — `#general hello there`. No token → general.
- **Identify for page actions:** posting/settings/editing through the pages need your
  identity — in NomadNet enable "identify on connect" for the guild node in your
  directory, or just post via LXMF (always identified by signature).
- To co-locate with a local NomadNet client on one RNS instance, set
  `share_instance = yes` plus a matching `instance_name` in both configs.

Options: `--storage`, `--name`, `--announce-delay`, `--rns-configdir`.

Run the verification suite (boots a real guild + client over a TCP loopback RNS mesh,
54 checks — ingest, seq, usernames, edit/delete, pages, escaping, attachments, avatars,
presence/ping pruning, rate limit, delta API, ban/unban, mute/unmute, settings):

    /usr/bin/python3 tests/integ_test.py

Unit tests (no mesh needed):

    /usr/bin/python3 tests/test_moderation.py   # ban / mute / delete authorisation
    /usr/bin/python3 tests/test_presence.py     # ping registry, expiry, spoof resistance
    /usr/bin/python3 tests/test_streams.py      # JSON v1: channels, stream, typing, presence
    /usr/bin/python3 tests/test_blobs_site.py   # JSON v1: blobs, blob posts, site channels
    /usr/bin/python3 tests/test_micron_render.py  # micron parser (site pages)
    /usr/bin/python3 tests/test_term.py         # term channels: console sessions

Setup / deployment:

    /usr/bin/python3 setup_sHades.py   # fresh community hub or join pre-existing hub

## JSON API v1 (Reticulum-native, no HTTP)

Everything rides RNS Link requests on `nomadnetwork.node` — same transport as the
Micron pages, never HTML/HTTP/TCP-sockets at the API layer. `/api/*` (msgpack) is
the legacy surface; `/api/v1/*` speaks **JSON**: request payload = `json.dumps(args)`
bytes, response = `{"ok": 1, ...}` or `{"ok": 0, "error", "code"}` JSON bytes. Auth is
the identified link identity (never payload-supplied hashes).

Channels have a **kind**: `chat` (message ledger), `site` (NomadNet micron
"website": manifest + micron pages + images/audio/video, rendered by the existing
Micron path), `term` (terminal stream; sessions land in a later phase). `site`/`term`
creation is moderator-only.

Endpoints:

    /api/v1/channels/list                      -> {channels: [{id, name, kind, seq, created}]}
    /api/v1/channels/create {name, kind, manifest?}  (site/term: mod only)
    /api/v1/channels/info {channel}            -> {channel: {..., manifest, seq}}
    /api/v1/stream/subscribe {channel, cursor?}      (one subscription per link —
                                                subscribing replaces; cursor=int)
    /api/v1/stream/poll {cursor?, timeout_s?}  -> {events, cursor, reset, typing}
    /api/v1/stream/unsubscribe
    /api/v1/typing {channel, active}
    /api/v1/presence                           -> {users: [{user_hash, username,
                                                   node_hash, active_channel, ...}]}
    /api/v1/blobs/put {name?, mime?, data_b64} -> {blob_id, size, mime, path}
    /api/v1/blobs/get {blob_id, offset?, len?} -> {data_b64, total, offset, eof}
    /api/v1/post {channel, content, attachments?}    (JSON chat post; attachments
                                                are {blob_id} refs or inline
                                                {name, mime, data_b64})
    /api/v1/site/put {channel, path, data_b64, mime?}   (mod only; micron pages,
                                                images, audio, video)
    /api/v1/site/get {channel, path, offset?, len?}
    /api/v1/site/manifest {channel}            -> {manifest}
    /api/v1/site/manifest_put {channel, manifest}       (mod only)
    /api/v1/term/open {channel}                -> {session}      (term channels only)
    /api/v1/term/data {session, data_b64}      (line-based console input)
    /api/v1/term/resize {session, rows, cols}
    /api/v1/term/close {session}
    /api/v1/receipts {channel, msg_id?}        -> {last_read_id, unread}
    /api/v1/react {msg, emoji}                 (toggle; ASCII shortcodes only)
    /api/v1/search {q, channel?, limit?}       (live messages, deleted excluded)
    /api/v1/command {input}                    (slash commands; ephemeral output)
    /api/v1/roles/list|create|delete|assign|unassign   (roles; MANAGE_ROLES)
    /api/v1/channels/patch {channel, topic?, overwrites?} (MANAGE_CHANNEL)

`channels/list` includes a per-viewer `unread` count from the receipts table.
Reactions are ASCII shortcodes (`+1`, `laugh`, ...) with one emoji per user per
message (toggle), capped at 16 distinct per message, and cleaned up when a
message is deleted; they flow as `reaction` stream events. Search excludes
tombstones and escapes LIKE wildcards.

Blobs are content-addressed (`blob_id` = sha256[:32], served at
`/file/blobs/<id>.<ext>`); site resources live under `/file/sites/<cid>/<path>`
with traversal guards. Blob and site gets are chunked (256 KB default, 1 MB max)
so large media reads stay bounded on slow links. `site/put` and `manifest_put`
emit `site_patch` events on the channel stream.

Stream rules (server-enforced "only the active channel flows"):

- one subscription per link; a poll older than 120 s is dropped as dead;
- event kinds: `msg` (full row), `edit`/`del`, `typing` (transitions only, 5 s TTL),
  `presence` (guild-wide: full snapshot when the online set changes),
  `site_patch` (site resource or manifest update), `term_data` (console output;
- cursor = last event seq; the per-channel replay ring holds 200 events, and a
  cursor older than that returns `reset: true` + the head seq to resync to;
- polls long-poll up to 10 s (`timeout_s` capped) and are woken instantly on events.

## Term channels: the guild console

Term channels host an in-process, line-based console — no pty, no subprocess,
no shell; the command table in `_term_execute` is the whole surface. Sessions
(`/api/v1/term/*`) are owned by the opening user, capped (4/user, 32 guild-wide),
rate-limited (30 commands/min), idle-closed after 300 s, and ANSI-stripped in
both directions (input and output — `clear` is a structured event flag, never
an escape sequence). Input is line-buffered server-side; output rides
`term_data` events on the channel stream.

Commands: `help`, `who`, `channels`, `msg <channel> <text>` (posts through the
normal ingest path — rate limits and moderation apply), `info <channel>`,
`presence`, `uptime`, `clear`, `quit`.

The Kivy client shows a `TermPage` console for term channels; the main input
bar feeds the session, output appends live from the stream.

## Receipts, reactions, search

- **Receipts** (`receipts` table): `⌕`-adjacent to the channel ledger, each
  viewer keeps a monotonic last-read message id; `channels/list` returns
  per-viewer `unread` counts, the sidebar shows badges, and the client marks
  the newest message read whenever it fetches a channel (never for pagination
  prepends).
- **Reactions** (`reactions` table): toggle via `/api/v1/react`, one emoji
  per user per message, ASCII shortcodes only (`+1`, `laugh`, ...), 16
  distinct per message max, removed when the message is deleted. Summaries
  ride `msg_row_json` and `reaction` stream events; the client renders
  tappable chips under each message and a preset picker popup.
- **Search**: `/api/v1/search` — live (non-deleted) messages, channel-scoped
  optional, LIKE wildcards escaped, limit ≤ 50. The client's `⌕` top-bar
  button opens a results popup.

The Kivy client streams only the visible channel (`_stream_loop`); the 3 s
reconciler poll and the 4 s ping loop are unchanged.

## Slash commands (U4)

One dispatcher (`_dispatch_command`) serves both the term console and chat
`/`-commands — commands exist in exactly one place. `/api/v1/command {input}`
runs it and returns the output **ephemeral** to the requester (nothing is
posted). Input is ANSI/control-stripped like the console; errors come back as
plain output; the 30/min console rate limit applies.

Server commands: `help`, `who`, `channels`, `msg <channel> <text>`,
`info <channel>`, `presence`, `uptime`, `create <name> [kind]` (site/term:
mod), `delete <channel>` (mod), `rename <channel> <name>` (mod),
`topic <channel> <text>` (mod; stored in `channels.topic`, visible in
list/info), `ban/unban/mute/unmute <user>` (mod; by 32-hex hash or exact
username). Client-local only: `/me` (italic post), `/join <channel>`,
`/search <q>`, `/invite` (copies the share-link), and `/help` (prepends the
client-local list to the server's).

The client renders command output as an ephemeral console row (never
persisted, never in the ledger) and refreshes the channel index every ~15 s so
renames/topics/badges stay current.

## Roles and permissions (U5)

Permission bitmask (0-63): VIEW 1, POST 2, MANAGE_MESSAGES 4,
MANAGE_CHANNEL 8, MANAGE_ROLES 16, ADMIN 32. Two layers:

- **Roles** (`roles`, `user_roles`): guild-wide bits (MANAGE_*/ADMIN) live on
  the role itself; `role.perms` is one number. Boot seeding: every hash in
  `admin.txt` gets the Admin role (all bits), every hash in `moderators.txt`
  the Moderator role (MANAGE_MESSAGES|MANAGE_CHANNEL) — same one-hash-per-line
  format as before, and the legacy in-memory moderator set still counts for
  MANAGE_MESSAGES/MANAGE_CHANNEL so old deployments keep working.
- **Channel overwrites** (`channel_overwrites`, role 0 = @everyone):
  per-channel VIEW/POST grants UNIONed with the member's role bits. A channel
  with no @everyone row is open (VIEW|POST) — pre-roles behaviour; set
  `{"0": 0}` to lock one down, then grant roles.

Enforcement (server-side, no exceptions): `ingest` drops posts without POST
(silently, like mutes); `channels/list` omits channels without VIEW; the
message/react/receipt/search/site-read/term-open surfaces all require VIEW
(unscoped search filters results per-channel); rename/topic/delete and
`channels/patch` need MANAGE_CHANNEL; ban/mute keep the guild-wide moderator
check; all five roles endpoints need MANAGE_ROLES. ADMIN short-circuits every
check. `/api/v1/whoami` carries `is_admin` and the member's roles; the client
uses that only to show or hide the role manager popup (right-click a member ->
"roles..."), which lists roles, toggles assignment, and creates roles.

## Invites (U6a)

Right-click the guild header -> "copy invite" puts
`shades://<hub-ip>:<hub-port>/<node_hash>` on the clipboard — the RNS
*interface* endpoint plus the guild's node hash (no HTTP; the client dials
the hub over RNS and everything after that rides RNS links). Pasting an
invite into the connect screen's address field splits it automatically
(on focus loss or enter, and again at Connect press): address into the
address field, node hash into the node-hash field. Explicitly typed hash
fields win over the invite's. Parsing lives in `invite.py` (Kivy-free,
stdlib only) and is covered by `tests/test_invite.py`.

## Friends + DMs (U7, client-local)

Both are entirely client-side — the guild is not a party and never sees
them.

- **Friends roster**: `friends.json` in the app data dir
  (`{user_hash, nickname}` per entry; user_hash = the 32-hex lxmf.delivery
  hash). Right-click a member (or message author) -> "add friend" /
  "remove friend". Friends head the member sidebar with their own section;
  online state is best-effort from the guild presence map (green/gray).
- **DMs**: direct client-to-client LXMF — the popup is backed by a real
  `LXMRouter` on the client identity, which announces its own
  `lxmf.delivery` destination at connect; everything arriving there is a
  DM (guild chat rides the API link and never touches this path). "message"
  on the same menu opens the thread. Chat keeps working even if the router
  fails to initialise ("DMs disabled"). Threads persist client-local as
  `dms/<user_hash>.json` (200-message cap). Inbound DMs surface as a status
  line or refresh an open thread. Everything lives in `dms.py` (Kivy-free)
  and is covered by `tests/test_dms.py`.

Sending needs two things from the mesh, and the client handles both: the
peer's identity must be known (`RNS.Identity.recall` — they must have
announced) **and** the transport must have a path to their delivery
destination (`RNS.Transport.request_path` + a short wait). Without a path
LXMF silently never transmits, so the client reports "dm: no path to ...".

Note for mesh setup: two clients can only find each other if something
between them routes — a hub with `enable_transport = yes` relays announces
and answers path requests; a leaf-only node cannot. `tests/dm_peer.py` is a
standalone peer used by the integ test for exactly this reason.

## Admin panel and audit log (U8)

Admin-only (the **Admin** role — a moderator is not enough). Right-click the
guild header -> "server settings" opens the panel (the entry only appears
when `/api/v1/whoami` says `is_admin`; the server enforces it regardless).

Server endpoints (all JSON over RNS, admin-gated):

| endpoint | purpose |
|---|---|
| `/api/v1/admin/config` | read all settings, or read/write one (`{key, value}`) |
| `/api/v1/admin/audit` | recent audit rows, newest first (`{limit}`) |
| `/api/v1/admin/mods` | `{op: list\|add\|remove\|admin_add\|admin_remove, user}` |

Writable settings: `guild_name`, `registration` (`open`/`code`), `slowmode`
(seconds, 0 = off, admins exempt), `motd`. Unknown keys are rejected.

The whitelists stay file-backed: `moderators.txt` and `admin.txt` are
rewritten atomically, and `admin_add` also grants the Admin role. Adding a
moderator does **not** make someone an admin.

**Audit log** (`audit_log` table): every privileged mutation is recorded
with actor, action, target and detail — bans/mutes/unbans, channel
create/delete, role assign/unassign/delete, config changes and whitelist
edits. Reads are admin-only; the writer never raises (a failed audit must
not break the action it was recording).

## Site channels in the client

The client learns each channel's kind from `/api/v1/channels/list` and routes by
kind: `chat` renders the message ledger, `site` renders Micron pages, `term`
shows a placeholder until terminal sessions land. Site rendering (`micron.py` +
`_render_site_page`) is NomadNet Micron → Kivy — **never HTML**:

- supported grammar: `#!bg=` directives, `>` headings, `-` separators, `c`
  centered lines, `` `!bold!` `` / `` `*italic*` `` / `` `_under_` `` / `` `F8a8`f `` /
  `` `B333`b `` runs, `` `[label`:/target`vars] `` links (colon-prefixed same-node
  targets), `` `{:/path`8`vars} `` partials (as links), `` `= `` literal blocks;
- page navigation: links to `/file/sites/<cid>/*.mu` fetch that resource via
  `site/get` and render it (back stack via `< Back`); links to `/page/...` fetch
  the guild's own pages raw; everything else lands in the status line;
- media: a line that is only a link to `/file/...` media renders inline —
  images as `MessageEmbed` (bytes fetched over the link, cached locally),
  audio/video as tappable download rows;
- theming: manifest `theme.bg` (or the page's `#!bg=`) paints the page; the
  manifest is a token set, not CSS — Micron has no stylesheets;
- `site_patch` stream events re-theme/re-render the open page live.

## systemd (user unit)

    mkdir -p ~/.config/systemd/user
    cp sHades.service ~/.config/systemd/user/
    systemctl --user daemon-reload && systemctl --user enable --now sHades

## Files

    sHades.py              the guild server (single file)
    setup_sHades.py        fresh-hub / join-existing-hub setup script
    tests/integ_test.py    end-to-end verification (mesh)
    tests/test_moderation.py moderation unit tests
    tests/test_presence.py presence / ping-expiry unit tests
    plan.md                the design (v2.2) with verified-API citations
    plan_ui.md             the UI/interaction plan (context menus, roles,
                           slash-commands, admin panel — not yet built)
    plan_review.md         teardown of the original v1 plan (why it was rebuilt)
    plan_v1.md             original (superseded) design
