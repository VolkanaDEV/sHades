# sHades Bot API

Bots are server-side API clients: a program that talks to a guild over
its own RNS link, authenticated by a per-bot API key, acting as its
owner. A bot is never a user — it has no username, no presence entry,
and no identity of its own in the guild. Everything a bot does is
attributable to the node that created it.

This doc describes the API as implemented (Groups 1–3). File/line
references are to the tree this doc ships with.

## Registry

- Table `bots` (`store.py:31-34` `BOTS_DDL`, added to `SCHEMA` at
  `store.py:99` and to pre-existing DBs by `migrate_schema`,
  `store.py:120`): `bot_id` (primary key), `owner_hash`, `name`,
  `key_hash`, `perms`, `created_at`, `revoked_at`, `last_seen`.
- Helpers (`store.py:1158-1218`): `bot_create`, `bot_list_by_owner`,
  `bot_get`, `bot_revoke`, `bot_update`, `bot_touch_last_seen`.
  `bot_revoke` is idempotent — it keeps the first `revoked_at`.
- **Owner link (accountability):** every bot row carries the
  `owner_hash` of the node that created it. A bot authenticates *as*
  its owner for channel VIEW/POST checks, posts carry the owner as
  author, and the audit trail records the bot (see Attribution).
- **Keys:** `bot_id` is `secrets.token_hex(16)` (32 hex chars);
  the API key is `"sk-" + secrets.token_hex(32)` (64 hex chars of
  entropy). Only `sha256(api_key)` is stored (`key_hash`); the raw key
  is shown exactly once, in the create response, and is never
  retrievable afterwards. There is no rotation endpoint — rotate by
  revoking the old bot and creating a new one.
- **Last seen:** every successful bot request (subscribe, poll, post,
  react, channels, channel_info) updates `last_seen` once per request
  (`store.bot_touch_last_seen`). Revoked bots are never touched.

## Permissions

Bitmask, `BOT_PERMS` in `sHades.py:85-87`, `BOT_PERM_ALL = 511`.
Bits 1–32 gate which stream event kinds a bot receives; bits 64–256
gate what it may do. `BOT_PERM_BITS` in `guild_api.py:26-37` is the
client-side copy with the plain-language labels the Bots screen shows.

| Bit | Name | Meaning |
|----:|------|---------|
| 1 | MSG | Receive message events (`msg`) |
| 2 | EDIT | Receive edit events (`edit`) |
| 4 | DELETE | Receive delete events (`delete`) |
| 8 | REACTION | Receive reaction events (`reaction`) |
| 16 | PRESENCE | Receive presence events (`presence`) |
| 32 | TYPING | Receive typing events (`typing`) |
| 64 | POST | May post messages |
| 128 | REACT | May react to messages |
| 256 | READ_CHANNELS | May read the channel list / channel info |

Validation: `perms` must be an int in 0–511 (bools rejected);
`name` is 1–64 chars after stripping (`sHades.py:3939-3945`,
`BOT_NAME_MAX = 64`).

## Endpoints

All endpoints are JSON over the guild's RNS link — there is no HTTP
anywhere. Management endpoints authenticate as the owner's link
identity (`viewer`); data-path endpoints authenticate with
`bot_id` + `api_key` **in the request data**, because a bot's link
identity is its own RNS identity, not the owner's.

### Management — `/api/v1/bots/*` (`sHades.py:3974-4063`)

| Endpoint | Request | Response |
|----------|---------|----------|
| `create` | `{name, perms}` | `{ok:1, bot, api_key, gateway}` — `api_key` only here, never again |
| `list` | `{}` | `{ok:1, bots:[...]}` — the caller's own bots, revoked included |
| `info` | `{bot_id}` | `{ok:1, bot}` |
| `update` | `{bot_id, name?, perms?}` | `{ok:1, bot}` — at least one field required |
| `revoke` | `{bot_id}` | `{ok:1, bot}` — idempotent |

`bot` shape (`_bot_row`, `sHades.py:3908`): `bot_id`, `name`,
`owner_hash`, `perms`, `created_at`, `revoked_at`, `last_seen`.
No endpoint ever returns `key_hash` or the raw key.

Errors: missing `bot_id` → 400; unknown `bot_id` → 404; another
owner's bot → 403; bad name/perms → 400. Audit: `bot_create`,
`bot_update` (only when something actually changed, detail lists the
changes), `bot_revoke` (only the first revoke).

`gateway` (`_bot_gateway`, `sHades.py:3916`) is the guild's
`node_hash` / `delivery_hash` (from the guilds row via
`store.get_guild`, `store.py:1145`; `None` until the guild has booted
and saved its row) plus an `endpoints` dict. **Known gap:** the
`endpoints` dict lists only `subscribe`, `poll`, `post`, `react` —
the two bot-only channel endpoints below are NOT in it, because the
Group 1 test pins that dict exactly and the endpoints were added in
Group 2. The endpoints exist and work; they are listed here and in
`examples/bot_example.py`. Do not "fix" the dict by editing the test.

### Stream — subscribe / poll

- `POST /api/v1/stream/subscribe` (`sHades.py:1917`):
  `{channel, cursor?, bot_id, api_key}` → `{ok:1, channel, kind, seq,
  cursor, typing}`. Bot credentials in the data; bad, revoked or
  unknown credentials → **403, never 404**, so the response cannot
  leak whether a `bot_id` exists. The owner must hold VIEW on the
  channel, else 403. One subscription per link — subscribing replaces
  whatever the link streamed before.
- `POST /api/v1/stream/poll` (`sHades.py:1978`):
  `{cursor?, timeout_s?}` (default wait 4 s, cap 10 s) →
  `{ok:1, channel, events, cursor, seq, reset, typing}`.
  `reset: true` means the ring rotated past the cursor — resync
  (the returned cursor is the head). A revoked bot polling gets 403
  and its subscription is dropped.

### Data path — `/api/v1/bots/*` (`sHades.py:4084-4175`)

Auth is `_resolve_bot` (`sHades.py:334`): row must exist, not be
revoked, and `hmac.compare_digest(stored key_hash, sha256(key))`.
Any failure → 403, never 404. The required bit is checked per
endpoint (`_bot_for_data`, `sHades.py:3961`); a missing bit → 403.

| Endpoint | Bit | Request | Response |
|----------|-----|---------|----------|
| `post` | POST (64) | `{bot_id, api_key, channel, content, attachments?}` | `{ok:1, msg_id}` |
| `react` | REACT (128) | `{bot_id, api_key, msg, emoji}` | the shared react response (toggle) |
| `channels` | READ_CHANNELS (256) | `{bot_id, api_key}` | `{ok:1, channels:[...]}` — exactly the owner's VIEWable channels, with `can_post` + `unread` as the owner |
| `channel_info` | READ_CHANNELS (256) | `{bot_id, api_key, channel}` | `{ok:1, channel:{id, name, kind, manifest, created, topic, seq}}` |

`post` rules: the owner must hold VIEW + POST on the channel;
content must be non-empty (unless attachments are present);
attachments are `[{name, mime, data}]` with `data` base64, at most
`MAX_ATTACHMENTS` items, each under `MAX_ATTACHMENT_SZ`. Unknown
channel → 404; empty content → 400. `react` toggles through the
shared `_react_as` path with the owner as actor, so the reaction is
stored and emitted exactly as the owner's own (the owner's VIEW on
the message's channel is enforced there).

## Push filtering (the bandwidth point)

`_bot_event_allowed` (`sHades.py:1605`) maps event kinds to bits
(`_BOT_EVENT_BITS`, `sHades.py:1598`): `msg`→1, `edit`→2,
`delete`→4, `reaction`→8, `presence`→16, `typing`→32. The filter is
enforced in **both** delivery paths:

- `_push_stream` (`sHades.py:1655`, the ticket wake-up): a bot is
  only woken when the batch holds at least one event it may receive
  that is newer than the subscription's `ticket_seq` watermark. The
  watermark advances to the head on a skipped wake-up, so a stale
  allowed event sitting after the poll cursor cannot re-wake the bot
  for every later event — including disallowed ones. A **reset
  ticket is still sent** on a ring gap: it carries no event content,
  only "your cursor is stale, resync", and without it the bot's cursor
  could never advance past a rotated ring. The push path never
  advances the poll cursor; the ticket only prompts a delta-fetch.
  A revoke is re-checked on every push (PK lookup), so a revoked
  bot's flow stops on the next event.
- `_stream_collect` (`sHades.py:1762`, poll / gap recovery): takes an
  optional `perms` argument and returns only events the bot may
  receive. The cursor still advances only to the last returned event;
  disallowed events are skipped over when the next allowed one
  arrives (or on resync).

**Never delivered to bots, with any perms (even 511):**
`site_patch`, `term_data`, `channels_changed`, `channel_deleted`,
`posts_purged` — and unknown kinds are denied by default. There is
no bit that opts into them.

**Typing:** the typing snapshot rides the push ticket and the
subscribe/poll responses, gated the same way as typing events
(`_typing_snapshot_for`, `sHades.py:1611`): a bot sees it only with
the TYPING bit (humans always).

**Presence:** bot subscriptions carry `user_hash = owner_hash` plus
`bot_id` / `bot_perms` / `ticket_seq`, and are kept **out of
`SUB_BY_USER`** (`sHades.py:1917` subscribe handler). That index is
the presence machinery's per-user view — a bot subscribing as its
owner would clobber the owner's own subscription and surface the
bot's channel as the owner's `active_channel`. Bots never appear in
presence (`_presence_payload` reads `SUB_BY_USER` only).

## Attribution

A bot post is ingested as the owner: the message row carries
`author_hash = owner_hash`, indistinguishable from a human post by
the owner (`sHades.py:4084` `api_v1_bots_post`). The bot is on the
record only in the audit trail: `_audit(owner_hash, "bot_post" |
"bot_react", bot_id, msg_id)` — actor = owner hash, target = bot_id,
detail = msg_id (`sHades.py:3266` `_audit`). Same for reactions:
stored and emitted as the owner's own. The management audit actions
are `bot_create`, `bot_update`, `bot_revoke`.

## Rate limiting

Bot posts are rate-limited per bot, not per owner: `rate_ok` is keyed
`"bot:" + bot_id` (`sHades.py:4084`), 10 posts per 60 s per channel
by default (`RATE_MAX_POSTS = 10`, `RATE_WINDOW_S = 60`,
`sHades.py:50-51`). A chatty bot cannot silence its owner, or vice
versa. Exceeding it → `{ok:0, error:"rate limited", code:429}`.
`last_seen` is touched once per successful bot request.

## Running a bot on your own hardware

You run your own bot on your own hardware; the guild server only
supplies the gateway and the API key.

1. **Create the bot** in the client: Profile → Bots → name it, tick
   the permissions it needs, Create. Copy the API key **now** — it is
   shown once and never again. The screen also shows the gateway
   block (hub address, guild node + delivery hashes, endpoint
   paths). Note the gateway gap above: `channels` and `channel_info`
   live at `/api/v1/bots/channels` and `/api/v1/bots/channel_info`
   even though the create response's `endpoints` dict omits them.
2. **Your bot needs its own RNS identity + link** to the guild
   (the same link setup the client uses: `RNS.Identity` +
   `RNS.Link`, see `GuildAPI._request_json` in `guild_api.py:195` —
   JSON bytes over the link, `link.request(path, data=payload)`).
   The bot's link identity is its own, which is why `bot_id` +
   `api_key` travel in the request data rather than being derived
   from the link.
3. **Subscribe, then poll in a loop:** `POST
   /api/v1/stream/subscribe {channel, bot_id, api_key}` (optionally
   `cursor` to resume), then `POST /api/v1/stream/poll {cursor,
   timeout_s}` repeatedly, advancing the cursor to each response's
   `cursor`. On `reset: true`, resubscribe / resync. Polls are
   long: the server waits up to `timeout_s` (default 4 s) for an
   event before answering empty.
4. **Post / react** with `POST /api/v1/bots/post` and
   `/api/v1/bots/react`, passing `bot_id` + `api_key` in every
   request. Keep the key out of source control — read it from the
   environment (see `examples/bot_example.py`).
5. **Revoke** from Profile → Bots when the bot is retired or its key
   leaks (two-tap confirm on the Revoke button). Revoke is
   immediate: the next push, poll, or data-path request fails with
   403, and the subscription is dropped.

## Client

- `GuildAPI` (`guild_api.py:495-513`): `bots_list()`,
  `bots_create(name, perms)`, `bots_info(bot_id)`,
  `bots_update(bot_id, name=None, perms=None)`,
  `bots_revoke(bot_id)` — thin wrappers over `_request_json`.
- **Bots screen** (`kivy_client.py:1576` `BotsScreen`, registered as
  `"bots"`, layout in `kv_md.kv:516`): entry via a "Bots" button on
  the Profile screen (`kv_md.kv:472`, `open_bots`,
  `kivy_client.py:6601`). Lists the user's bots (name, owner hash
  truncated + Copy, plain-language perm labels, created/last seen;
  revoked rows dimmed at opacity 0.6 with actions disabled), a create
  form (name + 9 permission checkboxes), a one-time key box with an
  ONCE warning + gateway block + Copy/Dismiss (dismissing refreshes
  the list — the key is gone for good), two-tap confirm revoke on the
  button itself, and an inline per-permissions editor per bot.

## Tests

- `tests/test_bots_registry.py` — registry + management endpoints.
- `tests/test_bots_stream.py` — auth, stream filtering (both paths),
  data path, presence exclusion, rate limiting, last_seen.
- `tests/test_bots_screen.py` — Bots screen against a fake GuildAPI
  (Xvfb).
