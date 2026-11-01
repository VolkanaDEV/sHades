# sHades v2.2 — Guild Store-and-Forward Channel Server

Supersedes plan_v1.md (broken — see plan_review.md). Goal: a Reticulum-native Discord clone.

Model:
- **DMs are plain LXMF** — user-to-user in their preferred client (Sideband, NomadNet
  conversations). The guild is never involved. No design needed; this is native LXMF.
- **Channel chats live on the guild**: messages flow to the guild as LXMF, the guild
  stores them, and the guild's NomadNet page for that specific channel IS the chatroom,
  laid out like Discord. Channel messages are not delivered into members' clients.
- **Usernames are registered at the guild level** (username ↔ destination hash), so
  chats show names, not hashes. Usernames in chat are clickable links to that user's
  own NomadNet node as their profile.
- **Messages are editable and deletable** (tombstoned), and every channel carries a
  per-channel revision counter (the "atomic counter") so any client — the auto-refresh
  NomadNet partial or the future Kivy app — can detect change cheaply and re-render.
- **The Kivy client** (later layer) adds pfps, embedded images/audio/video, embedded
  Micron-subset markup, and native rendering off the guild API (§14).

## 1. Architecture: one process, one identity, three destinations

```
 member's preferred client                          any NomadNet peer
 (Sideband / NomadNet)                                    │
   │  DMs: plain LXMF peer-to-peer ──▶ other user (guild NOT involved)
   │                                                      │ browse
   │  channel post: LXMF ──────────▶┌ guild process (sHades.py) ──────────────┐
   │  "#general hello"              │ lxmf.delivery ─ delivery_callback ─▶ SQLite│
   │  (DIRECT, or via the           │    ▲ verified: a propagation node delivers │
   │   propagation network)         │    │ locally any message addressed to its  │
   │                                │    │ own delivery dest (lxmf_propagation)  │
   └───────────────────────────────▶│ nomadnetwork.node ─ /page/*.mu handlers:  │
                                    │   index.mu:   channel list (the sidebar)  │
                                    │   channel.mu: header + message stream     │
                                    │     (auto-refresh partial) + compose bar  │
                                    │   messages.mu: the stream (the partial)   │
                                    │   update.mu:   compose ingest             │
                                    │   edit.mu / delete.mu: message lifecycle  │
                                    │   settings.mu: username + profile node    │
                                    │   members.mu:  who's active here          │
                                    │ lxmf.propagation: guild-bound traffic     │
                                    │   parked on far propagation nodes when no │
                                    │   direct path exists lands here           │
                                    └───────────────────────────────────────────┘
```

One RNS.Identity backs all three destinations (destinations are cheap
hash-of-identity+aspects objects):

| Destination | Created by | Role |
|---|---|---|
| `lxmf.delivery` | `router.register_delivery_identity(identity, display_name=GUILD_NAME)` | Ingest: members post to channels by messaging the guild |
| `lxmf.propagation` | `router.enable_propagation()` | Reachability: posts sent while no direct path exists ride the propagation network and are delivered locally on arrival (verified: `lxmf_propagation` checks `destination_hash in self.delivery_destinations` first → `lxmf_delivery(...)` → delivery callback). Also makes the guild a store-and-forward peer for the mesh. |
| `nomadnetwork.node` | `RNS.Destination(identity, IN, SINGLE, "nomadnetwork", "node")` + `register_request_handler("/page/...")` | The chatroom UI any NomadNet peer can browse (this is what makes the guild a *server*; v1 never had it) |

Optional deployment: the guild can ALSO set a well-connected upstream propagation
node (`router.set_outbound_propagation_node(...)`) so guild-bound messages parked on
far-away propagation nodes get pulled home. Not required for v2.2.

## 2. What a channel is (and is not)

A channel is a **message stream stored and served by the guild**, not an RNS destination:

- `channel.id = full_hash(f"{guild}:{name}")` — used as the SQLite key and in page
  requests only. It is NOT a routable address (destination hashes derive from
  identity+aspects; a bare hash used as an aspect does not come back out — verified).
- Producers address a channel two ways, both stock-client compatible:
  1. **LXMF ingest:** leading `#channel` line in message content (`#general hello`).
     No prefix → `#general`. This is the Discord-channel-over-LXMF wire convention —
     the Kivy client will use the exact same convention.
  2. **Page compose:** link constant `channel=<id>` submitted with the message field.
- `FIELD_GROUP` (0x0B) exists in lxmf 0.9.4 as a bare constant with no semantics —
  reserved for the Kivy client to carry the channel id structurally.
- Channels are NOT separate LXMF destinations: one delivery identity per router
  (`LXMRouter.py:330`), and N announced destinations would spam mesh announce tables.
- DMs never touch the guild: members exchange LXMFs directly, client to client.

## 3. Data model (SQLite, WAL)

```sql
CREATE TABLE guilds      (id TEXT PRIMARY KEY, name TEXT, node_hash TEXT, delivery_hash TEXT, prop_hash TEXT);
CREATE TABLE channels    (id TEXT PRIMARY KEY, name TEXT UNIQUE, created REAL);
CREATE TABLE channel_state (channel_id TEXT PRIMARY KEY, seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE members     (channel_id TEXT, member_hash TEXT, last_post REAL,
                          PRIMARY KEY (channel_id, member_hash));
CREATE TABLE users       (dest_hash TEXT PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE,
                          profile_node TEXT, avatar_path TEXT, registered_at REAL);
CREATE TABLE messages    (id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id TEXT UNIQUE,
                          channel_id TEXT, ts REAL, author_hash TEXT, content TEXT,
                          edited_at REAL, deleted_at REAL);
CREATE TABLE attachments (msg_id TEXT, idx INTEGER, name TEXT, mime TEXT,
                          size INTEGER, path TEXT, PRIMARY KEY (msg_id, idx));
```

- **`channel_state.seq` is the revision counter** ("atomic counter"): bumped inside the
  SAME SQLite transaction as every insert/edit/delete on that channel. Readers get
  change detection in one tiny comparison — no polling of message bodies, ever.
- `msg_id = full_hash(channel_id + author_hash + ts + content)` — stable id for edits,
  tombstones and dedupe. Edits keep msg_id and bump `edited_at`/content; deletes keep
  the row and set `deleted_at` (tombstone — Discord behavior).
- `users.username` is the guild-level directory: username ↔ destination hash,
  case-insensitive unique. `profile_node` (optional 32-hex NomadNet node hash) powers
  the clickable username link; `avatar_path` (optional, set by the Kivy client, §14)
  powers pfps.
- `attachments` rows reference blobs on disk under `attachments/` (content-addressed
  names), written by ingest or the upload API.
- Open `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`. The daemon is the only
  writer — no per-request RNS/LXMRouter instances, ever.

## 4. Ingest paths (both converge on `ingest()`)

**A. LXMF delivery** — `router.register_delivery_callback(on_message)`. Verified the
callback fires once per accepted inbound message with the LXMessage object
(`LXMRouter.py:1805`, after dedupe). Handler:

1. `sender = RNS.hexrep(message.source_hash, delimit=False)`
2. `content = message.content` (bytes or str); parse leading `#channel`
3. attachments: if the LXMF carries the attachment envelope (§14), store blobs and
   reference them
4. `ingest(channel_id, sender, content, ts=message.timestamp, attachments=[...])`

**B. Page compose** — the guild's own `/page/sHades/update.mu` request handler
receives `field_msg` + `var_channel`; author = `remote_identity` of the RNS request
(verified: request handlers receive `remote_identity`). Same `ingest()` call.
NomadNet-only users participate without ever hand-crafting an LXMF.

`ingest()` — all in one transaction:
1. username = lookup in `users` by author hash; if unregistered, auto-register the
   placeholder `user-<hash[:6]>` (renamed in settings — zero friction, zero login)
2. insert ledger row (dedupe on msg_id) + attachment rows
3. upsert `members` (channel_id, author, last_post)
4. `UPDATE channel_state SET seq = seq + 1 WHERE channel_id = ?`  ← wakes every client

## 5. Usernames & profiles (guild-level directory)

- **Register/rename:** `settings.mu` — fields `` `<username`> ``, `` `<profile_node`> ``
  + `` `[Save`:/page/sHades/save_settings.mu`username|profile_node] ``. The record is
  keyed by the requester's `remote_identity` (their destination hash) — possession of
  the identity IS login.
- **Uniqueness:** `username UNIQUE COLLATE NOCASE`; on conflict the save page shows an
  error and keeps the old name.
- **Canonical user key (implementation note):** a link identifies with the raw
  identity (hash of the public key), but LXMF attributes posts to the
  `lxmf.delivery` destination hash — two different hashes for one human. The guild
  normalizes: `viewer = Destination(identified_identity, OUT, SINGLE, "lxmf",
  "delivery").hash`, so page-compose, `/api`, and LXMF posts all land on one key.
- **Profile node hash:** user pastes the 32-hex hash of their own NomadNet node.
  Validate format (32 hex chars); optionally probe `:/page/index.mu` on save. Empty is
  fine — Sideband-only users just get plain-text names.
- **Avatar:** cannot be uploaded from the NomadNet TUI (text-only forms). Kivy users
  upload via `/api/avatar` (verified: `Link.request` escalates oversized payloads to
  link resources automatically — RNS/Link.py:514-517). Guild caps size (e.g. 64 KiB),
  stores under `avatars/`. Clients without an avatar render a colored initial —
  Discord-without-image behavior everywhere.
- **Rendering rule (every chat surface):** if `profile_node` exists →
  `` `[<username>`<profile_node>] `` (bare 32-hex target = that node's home page —
  verified in Browser URL grammar). Otherwise plain escaped username. Never render a
  raw destination hash as a name.
- Profiles themselves are self-hosted: the user's own NomadNet node serves their page;
  the guild only links to it.

## 6. Channel pages: the chatroom (Discord-like layout, verified Micron)

Every dynamic page starts:

```
#!c=0
#!bg=223
```

(`#!c=0` kills the 12h peer cache; 3-hex colors only — 6-digit values are ignored.)

**channel.mu — one page per channel, the Discord layout in TUI form:**

```
> #general                                     ← channel header (heading)
-
`{:/page/sHades/messages.mu`8`channel=<id>}  ← message stream, auto-refresh 8s
-
`B333`<msg`>`b  `[Send`:/page/sHades/update.mu`msg|channel=<id>]
[Channels](/page/index.mu)  [Members](/page/sHades/members.mu`channel=<id>)]
```

**messages.mu — the stream (works standalone AND as the partial above):** per message,
Discord-style two-line render:

```
`!<username>!` `F555<HH:MM>`f
<escaped content, one Micron line per source line>   [+ [edit] [del] if owner]
```

- bold username, dim timestamp (`` `F555 `` + text + `` `f `` reset — verified
  formatting runs); tombstoned messages render as dim `<message deleted>`
- username link per §5 rendering rule
- newest last; cap at last ~50 messages per render, `[Load older]` link paging by
  `var_before=<msg_id>`
- **Escaping + Micron subset (mandatory, one rule everywhere):** message content may
  carry an inline Micron SUBSET — bold `` `!..!` ``, italic `` `*..*` ``, underline
  `` `_.._` ``, color `` `Fxxx ``/`` `Bxxx ``, links `` `[label`target] ``, code
  `` `= `` blocks. The guild's page renderer AND the Kivy renderer implement the SAME
  whitelist; every other character is escaped (verified escape semantics,
  MicronParser `parse_line`/`make_output`: escape `\` first, then `` ` ``, `[`, `<`,
  `>`, `{`, and prefix-escape lines starting with `>`, `-`, `#`, `\`, `` ` ``, `<`).
  Unescaped content would let any message inject links/formatting into every viewer.
  Content cap: 2000 chars.
- **members.mu** — the channel's active posters (username links, last-post time).
- **index.mu** — guild header + channel list
  (`` `[#general`:/page/sHades/channel.mu`channel=<id>] `` per channel) + settings
  link. This is the sidebar equivalent.
- No images exist in the NomadNet TUI (verified — no image syntax at all): no pfps, no
  inline media on guild pages. Attachments and media are downloads there
  (`` `[filename`/file/<path>] ``); the Kivy client renders them inline (§14).

## 7. Message lifecycle: edit & delete

- **Who:** author only (v2.2), matched by `remote_identity == author_hash`; a
  moderators list gates everyone's messages later.
- **Where:** guild page links on own messages —
  `` `[edit`:/page/sHades/edit.mu`msg=<id>] `` renders an edit form pre-filled with
  current content (`` `<content`<existing>> ``), save rewrites content + sets
  `edited_at`; `` `[del`:/page/sHades/delete.mu`msg=<id>] `` sets `deleted_at`
  (tombstone, content retained internally for moderation, rendered as deleted).
- **Over LXMF:** stock clients have no edit/delete protocol — edits/deletes are
  guild-surface actions (pages now, `/api` for Kivy). This matches Discord: edits are
  a client-app feature against the server ledger.
- Every edit/delete bumps `channel_state.seq` (same transaction) → §8 machinery
  repaints everyone.
- Edits of messages with attachments keep attachment rows unless explicitly removed.

## 8. Live updates: the per-channel revision counter

- **One source of truth:** `channel_state.seq`, bumped transactionally on every
  mutation (§4, §7). Atomicity is free — it rides SQLite's transaction.
- **NomadNet pages (subframe rendering):** the auto-refresh partial
  (`` `{...`8`channel=<id>} ``) re-requests the stream on an interval; pid-targeted
  links (`p:<n>`) repaint only the stream after a compose/edit/delete. The page does
  not need the counter (the refetch IS the check), but the counter is what keeps a
  cheap future optimization possible (render "N new" banners instead of full reloads).
- **Kivy client (incremental render):** poll `GET /api/state?channel=<id>` →
  `{"seq": N, "last": "<msg_id>"}` (~20 bytes) every 2-5 s over a persistent link. On
  change: `GET /api/messages?channel=<id>&since_seq=<my seq>` → delta of
  new/edited/tombstoned rows → render only the delta ("graphed & rendered upon
  update").
- **True push (later phase):** request/response in RNS is client-pull, but RNS links
  are bidirectional — clients can hold a long-lived link to the guild and the guild
  can emit a tiny notify packet on it per mutation. More machinery; ship the counter
  first, add link-push as an optimization. NomadNet pages can never be pushed (the
  browser pulls), which is why the counter is the canonical mechanism.

## 9. Skeleton (only verified calls — rns 1.1.3, lxmf 0.9.4)

```python
#!/usr/bin/env python3
import os, time, sqlite3, threading
import RNS, LXMF

STORAGE = os.path.expanduser("~/.sHades")
os.makedirs(STORAGE, exist_ok=True)
GUILD_NAME = "sHades Hub"

reticulum = RNS.Reticulum(configdir=os.path.join(STORAGE, "rns"))  # RNS.initialize() does NOT exist in 1.1.3
ipath = os.path.join(STORAGE, "identity")
identity = RNS.Identity.from_file(ipath) if os.path.exists(ipath) else RNS.Identity()
if not os.path.exists(ipath): identity.to_file(ipath)

router = LXMF.LXMRouter(identity=identity, storagepath=STORAGE)
guild_dest = router.register_delivery_identity(identity, display_name=GUILD_NAME)  # lxmf.delivery
router.register_delivery_callback(on_message)                                      # fires per inbound LXM
router.enable_propagation()                                                        # mesh reachability

node_dest = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, "nomadnetwork", "node")
for path, fn in PAGE_ROUTES.items():   # index/channel/messages/update/edit/delete/settings/save/members
    node_dest.register_request_handler(path, response_generator=fn, allow=RNS.Destination.ALLOW_ALL)

def announce_all():
    time.sleep(6)  # let interfaces settle (LXMRouter.NODE_ANNOUNCE_DELAY pattern)
    router.announce()                                  # delivery dest, display name in app_data
    router.announce_propagation_node()                 # delayed-thread announce, safe to call
    node_dest.announce(app_data=GUILD_NAME.encode())   # NomadNet directory format (Node.py:announce)

def run():
    threading.Thread(target=announce_all, daemon=True).start()
    while True:
        time.sleep(300)
        announce_all()   # announce tables expire; keep the guild visible

# on_message(lxm) -> parse "#channel" -> ingest()  [bumps channel_state.seq]
# serve_page(path, data, request_id, link_id, remote_identity) -> render Micron from SQLite
#   submitted fields arrive in `data`: "field_msg", "var_channel", "field_username", ...
if __name__ == "__main__":
    run()
```

Deployment note: if a NomadNet client runs on the same box, both processes should use
Reticulum's shared instance (`enable_shared_instance = yes` in the guild's rns config)
so they share interfaces instead of fighting over ports. The guild itself never needs
the NomadNet program — it *is* the node.

## 10. Member experience

- **DMs:** plain LXMF to the other person's hash/name in the preferred client. Guild
  not involved.
- **Posting to a channel:** either message the guild hash (`#general hello`) from the
  preferred client, or type in the compose bar on the guild's channel page. Same
  ledger, same chatroom.
- **Reading a channel:** always on the guild's page for that channel (Kivy later).
  Channel messages are not pushed into members' LXMF clients — the guild page is the
  Discord window; the preferred client stays the DM surface.
- **Editing/deleting:** [edit]/[del] on your own messages on the page.
- **Identity:** first post auto-registers `user-xxxxxx`; set your real username (+ your
  node hash for a clickable profile) in settings once. No logins anywhere.
- If the guild is unreachable at post time, the client holds/retries the LXMF, or it
  rides any propagation node and lands when the guild sees it (verified local-delivery
  path in `lxmf_propagation`).

## 11. Build phases

1. **Skeleton:** RNS + router + node dest + announce + SQLite (incl. channel_state)
   + read-only index page. Verify: a second NomadNet instance browses the guild over
   loopback/autopeer.
2. **Ingest + ledger:** delivery_callback parse, page compose, WAL, escaping, seq bumps.
   Verify: LXMF sent to the guild hash with `#test` appears on the page; seq advanced.
3. **Chatroom UX:** channel.mu + messages.mu partial + update.mu compose + members.mu.
   Verify: two NomadNet clients; post from one (page) and one (client LXMF); stream
   updates on both within one refresh interval.
4. **Usernames & profiles:** settings/save pages, uniqueness, placeholder
   auto-register, clickable profile links. Verify: rename flows through to chat
   rendering; link opens the target's node page.
5. **Message lifecycle:** edit/delete pages, tombstones, owner authz, seq on edits.
   Verify: edit from client A appears reformatted in client B within one refresh.
6. **Ops + Kivy groundwork:** pagination, attachments as `/file/` downloads,
   per-sender rate limits, moderation (ban), upstream prop node option,
   `/api/state` + `/api/messages` + `/api/avatar` handlers (§14).

## 12. Known limits (design-accepted)

- Channel history is viewable only on guild surfaces; members' LXMF clients don't hold
  channel transcripts. The Kivy client fetches from the guild instead of storing all.
- The guild is a single point of availability for channel chat (it is the host). Posts
  survive guild downtime via LXMF's own retry/propagation on the ingest side.
- The username registry is guild-authoritative: names are unique per guild, not
  mesh-global. Edits/deletes are guild-authoritative too (server ledger, like Discord).
- Mesh announce rate limits apply to the guild's three announcements (no per-channel
  announces — exactly what v1 got wrong).
- NomadNet surfaces are text-only, period: no pfps, no inline media there; media is
  downloads on pages, inline only in the Kivy client.

## 13. Version pins

`rns==1.1.3`, `lxmf==0.9.4`, `nomadnet==0.9.8` (PyPI latest lxmf 1.2.x changes stamp
enforcement; re-verify `enable_propagation`/stamp flow before upgrading).

## 14. Future: Kivy client

Guild-side groundwork ships in phase 6; the client arrives after. Zero architectural
change — the guild's ledger, wire convention and request plumbing stay as-is.

- **Post:** LXMF to guild delivery hash, `#channel` first line (same wire convention),
  or `/api/post` over RNS request.
- **pfps next to names:** `/api/avatar` (upload; verified oversized payloads ride link
  resources automatically — RNS/Link.py:514-517) + `avatar_path` on `users`; fetch via
  `/file/avatars/<hash>` or the api. Fallback = colored initial, rendered client-side.
- **Embedded media (images, songs, videos):** two arrival paths, one storage:
  1. LXMF post carrying the attachment envelope — lxmf 0.9.4 defines
     `FIELD_FILE_ATTACHMENTS`/`FIELD_IMAGE`/`FIELD_AUDIO` as bare constants with NO
     core structure (verified: zero handling in LXMessage.py), so the envelope is
     ours: msgpack list of `[name, mime, bytes]` under `FIELD_FILE_ATTACHMENTS`
     (match Sideband's exact shape when client interop matters — verify against
     Sideband source at that point);
  2. `/api/upload` with msg_id + files (link-resource escalation makes big blobs
     work).
  Guild stores content-addressed blobs + `attachments` rows; serves via `/file/` and
  `/api/attachment?msg=&idx=`. Kivy renders images inline, audio/video via player.
- **Embedded Micron markup in messages:** the §6 subset (bold/italic/underline/colors/
  links/code) is the canonical message markup — the guild pages whitelist-render it,
  the Kivy client implements the same whitelist natively (a small Micron-subset
  parser). One format, two renderers.
- **Edit/delete:** `/api/edit`, `/api/delete` — same authz (link identity == author),
  same tombstones, same seq bumps.
- **Live updates:** poll `/api/state` → delta fetch → incremental render (§8);
  optional link-push later.
- **DMs:** unchanged — plain LXMF, client-side.
