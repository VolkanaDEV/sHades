# plan_admin.md — account purge + in-flight server settings

```
spec    : v1.1
date    : 2026-11-19
author  : V (VolkanaDEV)
status  : PLAN — tay drives with Continue
scope   : profile-screen delete-account; server toggles that bind live
          data flows; admin screen extension (toggles, banner, motd
          display, node page); rail context-menu entry point
```

## Substrate truth (probed 2026-11-19)

- Server purge machinery EXISTS: `/api/v1/account/delete-posts`
  (confirm literal "DELETE MY POSTS") empties ledger text, deletes
  attachments/reactions/edits, orphans blob + preview files off disk,
  checkpoints WAL, VACUUMs, emits posts_purged, writes audit rows
  (sHades.py:3676). `/api/v1/account/leave` (confirm literal
  "LEAVE GUILD") releases registration, roles, receipts, membership,
  the user row and the mod/admin whitelists (sHades.py:3744).
- Client wrappers EXIST: guild_api admin_config / account_export /
  account_delete_posts / account_leave (kivy_client.py:1032-1052).
- Admin screen EXISTS: ui_server_config.py, five tabs (General,
  Membership, Channels, Roles, Invites), role-bit gated client-side,
  server-enforced regardless. General tab already surfaces guild name,
  motd, slowmode, registration, guild icon, audit log.
- Missing arrows: (a) no profile-screen delete-account UI flow;
  (b) no config key binds attachment persistence — blobs always
  commit to disk; (c) General tab has no toggle for it.

## Substrate truth (probed 2026-11-19, round 2 — tay's admin asks)

- Rail discs + guild icons EXIST: GuildRailButton renders letter-tile ->
  ingested icon, active ring, per-server ctx menu (kivy_client.py:1860).
- The config ENTRY POINT tay asked for EXISTS: rail ctx menu item
  "server settings", admin-gated, currently -> _open_admin_panel
  (kivy_client.py:1930). P9 only repoints/keeps that binding.
- motd: server key + General-tab editor EXIST; NO client display
  surface yet (grep: motd appears client-side only in ui_server_config).
- Banner: no config key, no ingest, no display. Parallel to
  guild_icon across the whole chain (blob upload -> admin/config ->
  whoami payload -> client ingest).
- Node pages: the guild serves NomadNet MICRON through site channels;
  micron.py is the Kivy-free parser the client renders with
  (SitePage). Site store endpoints EXIST (site/put 2552, site/get
  2594, site/manifest 2619).

## P1 — Profile screen: Delete account (client only)

- KV: danger row on the profile screen ("Delete account", destructive
  color), id profile_delete.
- PY flow: press -> type-to-confirm dialog (server literals verbatim:
  first "DELETE MY POSTS", then "LEAVE GUILD") -> sequential guild_api
  calls -> local purge (portable profile file, avatar cache rows,
  guild registration state, identity-derived ui state) -> routing
  gate back to the staging/connect screen (the fresh-install path
  already owns this routing).
- Contracts: static-source lane (confirm literals byte-equal to the
  server payloads, handler wired, kv row present) + logic test for
  sequence (delete-posts before leave).
- Price: small. Pure UI + orchestration over proven endpoints.

## P2 — Server: attachment persistence toggle (the in-flight one)

- New SERVER_CONFIG_KEYS entry: "attachments": "saved" | "ephemeral"
  (default "saved" = today's behavior).
- Bindings on the next request, no restart (every upload reads the
  live config):
  - _blob_commit refuses disk writes when ephemeral; the chunk is
    kept in an in-RAM registry keyed by digest {bytes, ts, name,
    mime, by} with a TTL sweep (Clock-thread) and a hard cap.
  - chunked put/finish: same gate on finish; init/chunk never touch
    disk for ephemeral mode.
  - serve paths (blobs/get, att/get, /file/blobs/, message ingest)
    read the RAM registry first, disk second.
  - message ingest: in ephemeral mode the attachment copy step skips
    the attachments dir; posts persist, attachment bytes live by TTL.
  - flipping back to "saved" restores disk commits for NEW uploads;
    existing stored blobs keep serving.
  - validation in admin/config POST ("saved"|"ephemeral"), audit row
    "config.set attachments X -> Y" on every flip.
- Tests: server lane (ephemeral put/get round-trip never creates a
  file; TTL expiry sweep; flip-back semantics; audit row shape) +
  client lane.
- Price: medium. The gate points are 3 write sites + 3 read sites,
  all already named.

## P3 — Admin screen General tab: the new controls

- "Attachment persistence" toggle row (saved/ephemeral) driving
  admin/config POST; disabled state mirrors the admin gate.
- The row reads through admin_config GET like its siblings; writes
  ride the same worker-thread + Clock scheduling pattern as the tab.
- Contracts: test_server_config_screen extension (row exists, posts
  the right key/value pair).
- Price: small. UI rides the proven General-tab pattern.

## P4 — Message retention (priced, parked)

- "force ephemeral" for posts themselves: SERVER_CONFIG_KEYS
  "message_retention": "off" | "hours:N", sweep on the server tick.
- Deliberately NOT in this pass: touches channel seq, stream emit
  and the bump machinery; needs its own receipts round. The arrow
  is named so pricing stands: one config key + one sweep pass over
  messages + tests; larger blast radius than P2.

## P5 — Server banner (server + client)

- SERVER_CONFIG_KEYS "banner": "" | blob_id (same validation shape
  as guild_icon).
- Ingest chain mirrors _ingest_guild_icon: whoami payload carries
  banner blob -> client fetch -> banners/<host>_<blob head>.<ext>.
- Display: banner strip across the top of the sidebar (above the
  channel list), letter-tile fallback when unset; the chat pane
  keeps its own header.
- Admin upload rides the General tab's existing guild-icon flow.
- Tests: server lane (validation + audit), client lane (ingest +
  kv display block contract).
- Price: medium. One server key + one client ingest clone.

## P6 — motd display (client only)

- Sidebar strip under the banner: muted one-line motd from whoami
  payload (verify the payload carries it; if not, add the field).
- Tapping opens a full-text popup (long motd must not truncate).
- Tests: kv contract + whoami payload field contract.
- Price: small.

## P7 — Node page admin (admin tab)

- tay (2026-11-19): the site/node page EXISTS as a #site channel
  (renameable, same machinery as all other channels). The landing
  binding question is closed; no new store, no new endpoints.
- Admin editor flow: pick/see the site channel(s) -> fetch its page
  markup (site/get) -> edit multiline MICRON (the tay LAW grammar)
  -> preview through micron.py + SitePage render -> publish via
  site/put. Channel rename rides the existing channels/patch.
- Tests: micron round-trip (parse -> render), publish contract.
- Price: medium. UI pattern exists (General tab editor rows).

## P8 — Announcements (priced, parked)

- True announcements = a pinned broadcast channel type or a pinned
  message with a push emit to all members; real protocol surface
  (schema, emit, catchup) — its own receipts round. motd (P6) is
  the v1 stand-in.

## P9 — Config entry point (client only)

- The rail ctx menu already carries "server settings" (admin-gated)
  -> _open_admin_panel. Verify _open_admin_panel lands on
  ServerConfigScreen; keep the gesture tay named (right-click /
  long-press on the rail disc) as the entry point.

## P10 — Admin config: prettier + device uploads (tay, 2026-11-19: "Admin
## config works now, but it should be prettier & icon/banner selection
## should allow for uploading as well")

- tay's message closes the admin-gate saga: admin.txt seed worked, the
  config screen opened on-device.
- P10a (device upload lane, the functional miss): the blob chain is
  fully built (blobs_put -> blob_id -> admin_config["guild_icon"/
  "banner" -> load_icon_into previews); the chooser is desktop-only
  (FileChooserListView lists nothing in the Android sandbox) while the
  proven SAF picker routes only avatar/attach purposes. Missing
  arrow: purpose-routed picking -- open_file_chooser carries
  purpose=None|"icon"|"banner", _on_file_selected routes icon/banner
  to ServerConfigScreen.tab_content["General"]._apply_icon_file/
  _apply_banner_file via the screen manager, ui_server_config pick
  buttons take the SAF route when _android_platform() (desktop popup
  unchanged). Price: small (~50 lines, two files).
- P10b (prettier pass): General tab -- icon preview 56px, banner
  preview strip (own row, loads via load_icon_into when the config
  carries a banner), row spacing dp(4)->dp(8). Pure widget code, no kv.
- Contracts: server_config lane gains the purpose-routing recorder
  (FakeApp with _android_platform -> open_file_chooser(purpose) call
  tuple asserted) + banner preview widget presence; kivy_client static
  pins for the routing helper. Battery green.
- Ship: bump 0.36.7 (single bump), full gates, CI pair + watcher,
  byte-verify with the routing symbols in the want set.

## Build order

P1, P2, P3, P5 in the v0.34.0-0.36.x ships (done: admin gate closed by
tay's admin.txt seed 2026-11-19; bootlog diagnosis channel v0.36.5,
external_primary v0.36.6). Next: P10 (tay's device receipt), then P6,
P9->P7. P4, P8 parked until tay calls them. PR merge stays tay's.

[END PLAN]
