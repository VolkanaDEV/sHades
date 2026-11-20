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

## P6 — motd display (BUILT 2026-11-20, v0.36.18 `93d2ec0`)

- Sidebar strip (id motd_strip) under the banner: muted one-line motd
  from the whoami payload's NEW guild.motd field (added when the plan
  was priced: "verify the payload carries it; if not, add the field"
  — it did not, so the field rides whoami now; the admin panel's
  existing motd key feeds it, zero new endpoints).
- Tapping opens a full-text popup (long motd never truncates).
- Receipts: guild-icon lane 18/0 (2 new motd checks, file made
  flake8-clean), selfheal lane 20/0 (bind -> hide-on-empty -> full
  popup text -> kv placement), battery red=none, pair
  44742331577/44742331549. NEXT ARROW after the device verdicts:
  P9 config-entry check (verify _open_admin_panel lands on
  ServerConfigScreen, keep the rail gesture), then P10a device
  uploads.

## P7 — Node page admin (admin tab)

- tay (2026-11-19): the site/node page EXISTS as a #site channel
  (renameable, same machinery as all other channels). The landing
  binding question is closed; no new store, no new endpoints.
- Admin editor flow: pick/see the site channel(s) -> fetch its page
  markup (site/get) -> edit multiline MICRON (the tay LAW grammar)
  -> preview through micron.py + SitePage render -> publish via
  site/put. Channel rename rides the existing channels/patch.
- EXTENDED 2026-11-19 (tay's site-channel asks, three arrows in
  build order — receipts this same day):
  - **P7a (bug, built)**: fresh site channels now seed a default
    template /index.mu. Receipts: `_seed_default_site` +
    `DEFAULT_SITE_INDEX_MU` in sHades.py (micron grammar receipts:
    headings `>`-prefixed at micron.py:184; write path, handler
    registration and manifest-free default index all reuse the
    site/put machinery); client `_site_empty_page` replaces the
    eternal "loading site…" with an honest empty page + the named
    error (the fetch-miss branch had no failure leg — sHades.py
    site/get 404s left the placeholder on screen forever). Lane:
    tests/test_sites.py 10 checks (seed exists/parses, chat
    unseeded, overwrite republishes, duplicate create 409 never
    reseeds). Battery green. Ships as 0.36.12 together with the
    0.36.11 softinput lane (tests-only companion commit).
  - **P7b (BUILT 2026-11-20, v0.36.16 `df4c6c2`)**: site/put and
    site/manifest_put open as a wiki: moderator OR the new
    channel-scoped PERMS["SITE_EDIT"]=64 (PERM_ALL 63->127,
    channels/patch validates and stores the widened mask; the bit is
    absent from _GUILD_MANAGE_BITS, so has_perm resolves it through
    the channel_overwrites union exactly like VIEW/POST; overwrite
    rows stay writable from the Channels/Roles tabs). Gate order in
    both handlers: channel parsed -> kind=site verified ->
    moderator OR SITE_EDIT -> 403 "site edit permission required".
    Receipts: tests/test_sites.py 9 new checks, lane 19/0 (0 failed),
    battery red=none, CI pair 44735628294/44735628178 superseded
    mid-flight by the P7c push (concurrency cancel-in-progress); the
    permission bit ships to the device inside the 0.36.17 artifact.
  - **P7c (the editor UI, BUILT 2026-11-20, v0.36.17 `29a425b`)**: P7's editor
    flow + a wikipedia-style [ Edit page ] affordance on rendered pages,
    keyed off the new site/get "can_edit" stamp (moderator OR the bit;
    zero new endpoints). Lands in SiteEditorScreen: micron source
    TextInput, [Preview] (the block loop factorized into
    _micron_blocks_into, media as inert text), [Publish] via site/put
    (success returns to the page and reloads; refusal stays on the
    editor with the server error). Receipts: test_sites lane 22/0,
    selfheal lane 19/0 (site editor contract), battery red=none, pair
    44736705426/44736705424.

## P8 — Announcements (BUILT 2026-11-20, v0.36.19)

- Channel kind "announce" (new tuple entry): the open default never
  confers POST there — moderators/admins always; explicit grants only
  (@everyone overwrite row carrying POST, a role perm, or a role
  overwrite). The union logic factored into _effective_perms; chat
  semantics unchanged (lane-guarded).
- Push emit + catchup inherit unchanged (the msg event still rides
  ingest; the lane asserts the ring).
- channels_list stamps can_post per channel (the site-edit verdict
  shape; old servers default True). The client mirrors it into the
  composer Properties: read-only input + "announcements only" hint for
  non-grants, "Announce" send label for holders; ledger fetch + live
  push run for announce channels (the chat flow, widened).
- Receipts: tests/test_announce.py 13/0 (create, default-deny, mod
  grant, @everyone grant, role grant, stamps on both kinds across
  four viewers, emit, ledger read); selfheal lane 22/0 (+2
  composer/flow contracts); battery red=none.

## P9 — Config entry point (VERIFIED 2026-11-20, contracts added
## CI-only: admin-panel lane 11/0)

- The gesture tay named was already in production: the guild header
  (CtxHeaderLabel, kv rule at the drawer top, right-click /
  long-press) carries "copy invite link", "server settings"
  (admin-gated), "copy boot log"; the settings action lands on
  ServerConfigScreen via the ScreenManager (popup fallback pre-boot).
- Receipts: tests/test_admin_panel.py +3 (header carries the entry,
  its action opens the panel, hidden for non-admins) — lane 11/0.
  Routing itself was covered by the panel-drive tests.

## P10 — Admin config: prettier + device uploads (SHIPPED 0.36.7
## `359c145`; pick-verdict hardening 0.36.10 `6236c00`)

- tay's message closes the admin-gate saga: admin.txt seed worked, the
  config screen opened on-device.
- P10a (device upload lane): BUILT — open_file_chooser carries
  avatar/purpose ("icon"/"banner"); _android_platform() rides the
  proven SAF picker (the desktop popup unchanged); _on_file_selected
  routes purpose through _route_purpose_file into
  tab_content["General"]._apply_icon_file / _apply_banner_file
  (blobs_put -> admin_config). Receipts:
  tests/test_server_config_screen.py "P10a: device (Android) purpose
  routing" (android pickers ride SAF with purpose + General tab
  handing).
- P10b (prettier General tab): BUILT — same ship (icon preview,
  banner preview strip, spacing) per `359c145`.
- Device follow-up owned by 0.36.10 `6236c00` ("verdicts on the
  admin screen + silent-death hardening + SAF redraw").
- Carried small: any NEW aesthetic asks go through this section.

## P11 — Auto-update (tay directive, 2026-11-20: pull from GitHub
## releases; contingency update with a server-side toggle riding
## SSH-over-RNS, "which is mostly what GitHub is, after all")

PLANNED, priced, build order below. No code until tay calls it.

- **Arrow 1 (the version bridge): the running client does not know
  its own version** — nothing carries it in-process today; the APK
  filename and the manifest's versionName are the only carriers (the
  byte-verify law: buildozer.spec is not packaged into private.tar).
  Price: read versionName via jnius PackageManager on-device (the
  proven bridge), plus a CI-injected APP_VERSION constant for the
  desktop builds (PyInstaller env at package time). Without this no
  updater can compare versions. ~30 lines + one static contract.
- **Arrow 2 (the release channel gains a server artifact)**: releases
  carry APKs only today. CI gains one build.yml job: package the
  server bundle (git archive of the tree, sHades.py + deps) and write
  sha256 lines for every asset INTO the release body (the updaters
  parse the body; GitHub computes nothing client-side). ~30 workflow
  lines. Must land BEFORE any updater points at it.
- **P11a — the updater core (shared, transport-free)**: version
  compare (tuple parse of "0.36.19"), fetch, sha256 verify against
  the body'd line, stage into a sibling dir, atomic swap, os.execv
  self-restart, previous bundle kept for rollback, boot-failure
  fallback (staged-but-unimportable tree falls back to the previous
  bundle before serving). ~120 lines + rollback lane.
- **P11b — server auto-update from GitHub, toggled**: admin_config
  `update_channel` = off|github|rns (default OFF) + `update_repo`
  (default eldeucebag/sHades). A soft in-process check (boot + a
  bounded interval); verdicts land on the diagnosis channel + the
  admin UI's own id'd label (the status-mirror rule); apply only when
  the toggle says so and the sha matches. ~80 lines wiring + ~120
  contract lines (toggle off default-deny, on-apply, sha-mismatch
  refuse, fallback).
- **P11c — client auto-update from GitHub**: the isolated HTTPS
  module reaches api.github.com only (outbound helper; HTTP never
  enters the sHades protocol surfaces — the anti-pattern law holds
  for OUR stack, GitHub is an external gateway tay named). Flow: soft
  check -> "update available" verdict (banner + config-screen label,
  never a silent self-modify — Android requires install consent
  anyway) -> background download through the proven MediaStore bridge
  into the public Downloads dir -> sha256 verify -> open the install
  intent (ACTION_VIEW + FLAG_GRANT_READ), user taps Install. Mismatch
  refuses and prints. ~120 lines + a fake-jnius functional lane (the
  receipt-lanes recipe) asserting the intent ARGUMENTS.
- **P11d — the contingency (RNS bundle carrier)**: tay's framing is
  exact — git is SSH + blobs, so the mesh-carrier shape is the RNS
  Link + Resource protocol, NOT HTTP-over-something. The wheel's
  rncp proves the pattern (listener with an identity allowlist +
  send/fetch over links); we adopt the PATTERN on raw RNS
  primitives (~140 lines), not rncp's code (its license carries an
  AI-training clause worth not entangling). A "software channel"
  destination: a pinned peer serves the server bundle (and the APK);
  the server pulls from a pinned peer when `update_channel` = rns.
  Trust = the peer's RNS identity hash pinned in admin_config
  (`update_channel_peer`); the link's peer identity is verified
  before accepting bytes; then the same P11a core verifies, stages,
  applies. The toggle covers the contingency exactly as tay priced
  it. Lane: a fake link/resource session asserting the peer-pin
  enforcement + sha gate.
- Build order: **1** version bridge -> **2** CI release job ->
  **3** P11a core + rollback -> **4** P11b server wiring + toggle ->
  **5** P11c client -> **6** P11d carrier. Each its own ship chain;
  3 and 4 go together (the core is useless unwired).
- **Chain 1 BUILT 2026-11-20, v0.36.20 `914dd30`**: kivy_client
  APP_VERSION + _client_version() (jnius PackageManager on-device,
  constant fallback); sHades SERVER_VERSION;
  tests/test_update_meta.py 8/0 (three-way lockstep lane: spec ==
  APP_VERSION == SERVER_VERSION, plus both _client_version halves —
  no-jnius fallback and a fake-jnius shim returning the APK's
  versionName). build.yml gains the server-bundle artifact job;
  release.yml (new) attaches bundle + .sha256 to a published release
  off the release TAG. Probes: the pairing lane CAUGHT the bump
  before commit (constants 0.36.20 vs spec 0.36.19 red), battery
  red=none after the bump. Artifacts of this chain verify via the
  0.36.19 table + the pairing lane.
- **Chain 2 BUILT 2026-11-20, v0.36.21 `c164f8b`**: updater.py (the
  shared core: parse_version, github transport, safe extraction,
  py_compile stage probe, stage->backup->swap, check_and_apply),
  update_run.py (the handover launcher owning boot + rollback:
  marker pending_version, update_backup/, one retry, exit 2 for a
  human), sHades wiring (SERVER_CONFIG_KEYS += update_channel
  off|github|rns + update_check_minutes 360; the reaper-loop branch +
  worker thread; boot check; admin-only /api/v1/update_status).
  Receipts: test_updater 33/0 (hostile members; zero fetches under
  off/rns on a live http seam; the happy path; mismatch refusal; the
  launcher rollback as a REAL subprocess — BROKEN->OLD runlog),
  battery red=none, lockstep lane green at 0.36.21.
- Standing risk named in the plan: auto-update is the one feature
  whose worst failure bricks the updater's own host — the toggle
  defaults OFF, the apply gates on sha + staged-import probe, and
  rollback is kept, before any of this is trusted on tay's box.

## Build order (addendum 2026-11-20)

P1, P2, P3, P5, P9 in the v0.34.0-0.36.x ships (done: admin gate
closed by tay's admin.txt seed 2026-11-19; bootlog diagnosis channel
v0.36.5, external_primary v0.36.6; the P9 gesture verified
CI-only 2026-11-20). P6 (0.36.18) + P7a (0.36.12) + P7b (0.36.16) +
P7c (0.36.17) shipped; device verdicts on the 0.36.17/0.36.18
artifacts are owed from tay's installs. P10 closed at 0.36.7/0.36.10
(see above). P8 (0.36.19) built this day. P4 parked until tay calls
it. P11 PLANNED above (version bridge -> CI release job -> core ->
server toggle -> client -> RNS carrier); tay's Continue starts it.
PR merge stays tay's.

[END PLAN]
