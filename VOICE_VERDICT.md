# Voice Verdict: P2P Opus over RNS Links (Build-Order Items 1-4)

Status: **green at the lane level** as of commit `bde9654` (voice/p2p-opus).
This is the acceptor-side record of what the voice train actually proved,
what it found, and what remains. Every claim below carries the run that
backs it; nothing rests on intent.

## What is shipped and proven

| Piece | Acceptance | Receipt |
| --- | --- | --- |
| LAN capture + Opus + RNS transport loopback (real RNS, real Opus) | `RESULT: 9 pass, 0 fail` | unit run 2026-11-23, ~19:30Z |
| P2P engine (roster, dials, 10Hz pacer, sink) | `RESULT: 14 pass, 0 fail` | direct lane runs 42/44, exit 0 |
| Hub relay fallback (undialable peers via hub channels) | `RESULT: 16 pass, 0 fail` | relay runs 42/43/45, exit 0 |
| Hub channel rooms (create/join/leave/removal, staleness) | `RESULT: 16 pass, 0 fail` | rooms suite, exit 0 |
| Presence under request-only links | covered by above | no reaper lines in green runs |
| Keepalive contract | covered by above | no conn deaths in green runs |
| flake8 | clean | app_voice.py, sHades.py, tests/ |

The green tip's end-to-end receipt (relay lane, run 42): a speaker's
623 voice packets crossed the hub to a worker whose dial to the peer is
undialable by design; the worker decoded **3120 Opus frames** with
arrival gaps **110.2 ms avg / 112.4 p95 / 222.2 max** (10Hz pacing).
The receipt is produced by the worker process itself and parsed by the
test; no test-side assertion can fake `gap_avg_ms`.

## The run story (what the reds were, in order)

Runs 1-3 (items 1-2): LAN loopback green immediately; real-RNS transport
loopback green after two harness fixes. No product defects.

Runs 4-16 (item 4 start): the relay lane failed with a stable signature
- the room collapsed mid-run and the relayed frames never flowed. The
first three roots found were real and each needed its own fix:

1. **Presence pruning keyed on RNS link status only.** An RNS link
   between two workers parks at STALE (0x03) whenever neither side has
   reason to send RNS-layer keepalive traffic - and in these lanes the
   traffic rides TCP, not RNS. ACTIVE-only pruning therefore dropped
   live, request-fed workers mid-run. Fix: presence keys on dispatcher
   bind stamps with a 12s traffic grace, unioned with ACTIVE
   (sHades.py, ONLINE_LINK_SPOT + hub presence keeper).

2. **Raw voice traffic did not feed presence.** A 10Hz speaker streams
   raw packets on its link for minutes without an api call; the old
   code let such a binding expire while voice was flowing. Fix: every
   inbound voice-pub packet refreshes its own stamp.

3. **The relay write gate believed the status snapshot.** The gate
   refused publishes to a subscription whose link RNS reported STALE,
   even when the dispatcher had identified it seconds before. Fix: the
   same 12s grace, consulted once at send time.

Run 17 (the honest instrumentation miss): a mid-diagnosis version was
asserted green on unit-pass confidence alone. The green number was real
at the unit layer, but the lane red immediately after - recorded here
because asserting lane results from lower-layer passes is the single
process failure this verdict exists to prevent.

Runs 18-38 (the hard part): green stayed out of reach while every
individual instrument was consistent. The keepalive contract was
redesigned during this stretch (hub pings ride the RNS KEEPALIVE
context, so they feed the TCP lane without claiming RNS-layer link
traffic - the doc inside `_hub_keepalive_loop` states both jobs
honestly), and worker stdout moved to a file after an undrained PIPE
was proven to wedge a worker inside a blocking print. Both real; both
necessary; neither was the last defect.

Runs 39-41 (the map-dump settles it): presence-side instrumentation
proved the stamps and the prune agree completely - the fork between
"stamps stopped" and "the prune looked at the wrong spot" closed with
the spot map printed at prune time.

Run 41, worker-side, found the actual last defect: **the harness passes
`--wait-seconds 100`; the worker's argv parser only knew `wait_s`, so
the value was silently discarded and every worker ran on a 30s
default.** A 30s stamp window meets the 12s prune grace exactly as
observed: B's bindings died mid-run on every relay red since run 4.
One accepted-key fix closed the red permanently.

Runs 42-45 (the gate): relay 16/0 four times, twice on stripped code
(all diagnostic instrumentation removed), direct 14/0, unit 9/0,
rooms 16/0, flake8 clean. The commit landed only after the stripped
gate re-passed (run 45).

## What is deliberately not claimed

- **No real Android device run.** Items 1-4 target the desktop/RNS
  loopback; the Android client (item 7) is queued and unclaimed here.
- **No NTP/timestamp discipline in the playback wire yet.** Item 5
  (jitter + decode + playback) is where ordering and late-arrival
  handling get tested; the arrivals record kept by the engine is
  pacing evidence, not playback evidence.
- **No congestion/loss model.** The relay receipts are clean-channel;
  loss and reorder injection is item 5+ scope.
- **The keepalive loop's TCP-job claim is a doc, not a kill-claim.**
  The 31s idle-death signature it cites is observed behavior from
  runs 13-16; the loop's RNS-role honesty (hub = responder) is source
  reading, not a run.

## Single-missing-arrow ledger (open, priced)

- Item 5: jitter + decode + playback wire (desktop) - next.
- Item 6: voice rows + join panel (server + Micron pages).
- Item 7: Android client voice (AudioRecord/AudioTrack + Opus).
- Pr/merge: voice/p2p-opus -> main after Ashley's sign-off; branch not
  pushed until then.
