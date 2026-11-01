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
41 checks — ingest, seq, usernames, edit/delete, pages, escaping, attachments, avatars,
rate limit, delta API, ban/unban, mute/unmute, settings):

    /usr/bin/python3 tests/integ_test.py

Moderation unit tests (no mesh needed):

    /usr/bin/python3 tests/test_moderation.py

Setup / deployment:

    /usr/bin/python3 setup_sHades.py   # fresh community hub or join pre-existing hub

## systemd (user unit)

    mkdir -p ~/.config/systemd/user
    cp sHades.service ~/.config/systemd/user/
    systemctl --user daemon-reload && systemctl --user enable --now sHades

## Files

    sHades.py              the guild server (single file)
    setup_sHades.py        fresh-hub / join-existing-hub setup script
    tests/integ_test.py    end-to-end verification (mesh)
    tests/test_moderation.py moderation unit tests
    plan.md                the design (v2.2) with verified-API citations
    plan_review.md         teardown of the original v1 plan (why it was rebuilt)
    plan_v1.md             original (superseded) design
