# UIX Review & Remediation Plan

Companion to [`DESIGN.md`](DESIGN.md) (the normative design system). This
document is the **audit**: what is wrong with the current client, the
evidence for each claim, and the order to fix it in.

Every finding below was verified empirically against the running app on
`DISPLAY=:0` (Kivy 2.3.1, KivyMD 2.0.0, Python 3.14.3, Mesa 26.0.3) by
walking the built widget tree and reading absolute geometry from Kivy's
layout engine — not by reading the source and guessing.

## Method, and one caveat about screenshots

Two techniques were used:

1. **Absolute geometry from the layout engine.** `widget.to_window(*widget.pos)`
   gives true on-screen coordinates. Overlap, overflow and clipping are
   computed from those rectangles. This is the authoritative signal.
2. **Pixel captures** via `Window.screenshot()` for colour, text and glyph
   inspection.

The pixel captures are **not** geometrically faithful on this machine: a
control experiment that drew a solid red rectangle at a known position
(Kivy `pos=(100,600)`, `size=(200,100)`) and then measured the PNG put
the marker at x=12–270 instead of x=100–300. The Mesa/`Window.screenshot()`
readback is skewed. Screenshots were therefore used only to judge colour,
text content and glyph shape; every position claim comes from the layout
engine. The diagonal banding visible in the captures is that skew, not a
rendering defect in the app.

## Severity scale

| Severity | Meaning |
|---|---|
| **P0** | Content is unreachable or actively misleading |
| **P1** | Feature is broken or indistinguishable from its background |
| **P2** | Usability/ergonomics failure, measurable |
| **P3** | Inconsistency or polish |

---

## P0 findings

### P0-1 — Connect screen overflows the viewport; banner is clipped

**Evidence.** The connect screen's content requires **820px** of vertical
space. Measured against real window sizes:

| Window | Content needed | Available | Overflow | Banner clipped by |
|---|---|---|---|---|
| 1280×720 | 820px | 720px | **100px** | 60px |
| 1024×600 | 820px | 600px | **220px** | 180px |
| 1366×738 | 820px | 738px | **82px** | 42px |

The banner `Image` is laid out at absolute `y=580…780` while the screen is
738px tall, so **42px of the banner renders off-screen**. At 1024×600,
180px of it is gone. The screen has no `ScrollView`, so the overflow
clips rather than scrolls.

**Fix.** Restructure the connect screen per `DESIGN.md` → *Connect screen*:
banner 120px, wordmark 48px, subtitle 24px, two required fields (hub
address, username) at 44px with 16px helper lines, the two optional hash
fields behind a disclosure, one 40px Connect button. Constrain the form to
~420px wide and centre it. Wrap the screen content in a `ScrollView` with
`size_hint_y: 1` so nothing can clip again.

**Files.** `kv_md.kv` → `<ConnectScreen>`.

### P0-2 — The hub-address field overlaps the "Connect to a guild" label

**Evidence.** The form's `BoxLayout` is `height: dp(340)`, but its ten
children need 375px. Kivy's `BoxLayout` does not clip — the last child
spills out of the container. Measured collision:

```
COLLISION MDTextField 'Hub address (ip:port, e.g. 192.168.1.50:29221)'
          x Label        'Connect to a guild'
          = 1286 x 15 px
```

A 1286×15px overlap across the full width of the window: the hub address
input is drawn *through* the heading above it.

**Fix.** Same as P0-1 — size the form from its content
(`height: self.minimum_height`) or from a computed height, and give the
connect screen a `ScrollView`.

**Files.** `kv_md.kv` → `<ConnectScreen>`.

---

## P1 findings

### P1-1 — `members \u25bc` renders as literal text

**Evidence.** `kv_md.kv:590` contains a **double** backslash:

```
text: 'members \\u25bc' if root.right_open else 'members \\u25b6'
```

The raw bytes confirmed on disk (`od -c` shows `\ \ u 2 5 b c`). Kivy
does not interpret `\uXXXX` escapes in KV strings, so the button draws the
literal seven characters `\u25bc`. The button-measurement script printed
the label as `members \u25bc`.

This is doubly wrong: the escape is broken, **and** Roboto has no ▼ or ▶
glyph — the app's own convention (and `DESIGN.md`) is canvas-drawn icons
or ASCII. Note the left sidebar already does this correctly
(`'channels v'` / `'channels >'`).

**Fix.** Use ASCII arrows to match the channels button, or draw a chevron
in `ui_geom.py` like the top-bar icons.

**Files.** `kv_md.kv:590`.

### P1-2 — The own-message bubble is invisible

**Evidence.** `DEFAULT_THEME_COLORS["msg_own"]` is `(0.07, 0.07, 0.07, 1)`
and `DEFAULT_THEME_COLORS["panel"]` is `(0.07, 0.07, 0.07, 1)` — **the
same value**. `<MessageRow>` draws a `RoundedRectangle` filled with
`msg_own` for own messages, on top of a chat area filled with `panel`.
The bubble is the same colour as the surface behind it, so it does not
exist visually.

**Fix.** Per `DESIGN.md`, own-message bubble fill becomes `bg-raised`
(`#161616`) against the `bg-main` (`#0A0A0A`) chat surface — a measurable
2-point lightness step. This is also why the palette needs a real elevation
ladder rather than one flat dark grey.

**Files.** `kivy_client.py` → `DEFAULT_THEME_COLORS`.

### P1-3 — The profile screen shows the username twice

**Evidence.** `kv_md.kv:312`:

```kv
text: 'Delivery: ' + root.target_username
```

`ProfileScreen` declares only `target_hash`, `target_username` and
`target_avatar` — there is no delivery-hash property, so the "Delivery:"
line renders the username again instead of a delivery hash. The label is
misleading, not merely redundant.

**Fix.** Either add a `target_delivery` `StringProperty`, populate it from
the active-user record, and render it — or remove the "Delivery:" line
until a real value exists.

**Files.** `kv_md.kv:312`, `kivy_client.py` → `ProfileScreen`.

---

## P2 findings

### P2-1 — Every button is below the 48dp touch target

**Evidence.** `ui_scale.BUTTON_V_SCALE = 2/3` and `scaled(dp(N))` is
applied to every button height. Measured heights on the main screen:

| Button | Height | Width |
|---|---|---|
| `+` (add server) | 32.0px | 56px |
| `cfg` | **21.3px** | 67px |
| `channels v` | 22.7px | 114px |
| `Send` | 26.7px | 79px |
| `+` (attach) | 26.7px | 56px |
| `members` | 22.7px | 154px |
| `Connect` (connect screen) | 33.3px | 100px |

Six buttons on the main screen are under 48dp in at least one dimension;
the top-bar icons are 40×40; the two `SlimButton` separators are **14px
wide**. The `cfg` button at 21px is the worst case.

**Fix.** Drop the 2/3 factor, set explicit 40px button heights and 44px
inputs, and give the 14px separators a wider hit area than their drawn
glyph.

**Files.** `ui_scale.py`, `kv_md.kv`, and the ~30 Python-built popups.

### P2-2 — The input bar is 24px tall and its contents overflow it

**Evidence.** The input bar `BoxLayout` is `height: dp(24)`. Its measured
children:

```
MDTextField  abs=(302,1)-(998,36)   h=35.0   TALLER THAN BAR
MDButton     abs=(1002,1)-(1081,28) h=26.7   TALLER THAN BAR
MDButton     abs=(1092,1)-(1148,28) h=26.7   TALLER THAN BAR
```

A 35px text field and two 26.7px buttons sit in a 24px bar, so all three
spill upward into the message list. The 35px height comes from
`MDTextField.adjust_height()`, which forces `height = max(new_height,
dp(56))` unless `max_height` is set — the KV sets `max_height: dp(80)`,
which is too tall for a 24px bar.

**Fix.** Input bar becomes 48px; the field is `size_hint_y: None`,
`height: dp(40)`, `max_height: dp(40)`; buttons 40px.

**Files.** `kv_md.kv` → `<MainScreen>` input bar.

### P2-3 — `text-dim` fails WCAG AA for body text

**Evidence.** Computed with the WCAG 2.1 relative-luminance formula:

| Pair | Ratio | Required | Result |
|---|---|---|---|
| `text_dim` (#997026) on chat panel (#121212) | **4.19:1** | 4.5:1 | **FAIL** |
| `text_dim` on sidebar (#0E0E0E) | **4.32:1** | 4.5:1 | **FAIL** |
| `text_dim` on bg_main (#0A0A0A) | 4.43:1 | 4.5:1 | **FAIL** |

`text_dim` is used for message timestamps (11px) and config helper text —
small text, so AA at 4.5:1 applies. All other pairs in the palette pass
(`text` on panel: 7.79, `text_muted` on panel: 5.84, `accent` on bg_main:
8.50, `btn_text` on `primary`: 8.21).

**Fix.** `text-dim` → `#B08C46`, which measures 5.11:1 on the chat surface
and 5.37:1 on the sidebar. Full palette and all validated ratios are in
`DESIGN.md`.

**Files.** `kivy_client.py` → `DEFAULT_THEME_COLORS`.

### P2-4 — No border token: inputs read as flat rectangles

**Evidence.** The palette has no border/hairline colour at all. Inputs are
distinguished only by a fill that differs from the background by 5% —
invisible on a dark display.

**Fix.** Add `border: #646464` (3.35:1 on `bg-main`, 3.22:1 on
`bg-sidebar`, 3.06:1 on `bg-raised` — all ≥3:1 for a UI boundary) and
`border-subtle: #3A3A3A` for dividers and the scrollbar.

**Files.** `kivy_client.py` → `DEFAULT_THEME_COLORS`, `kv_md.kv`.

### P2-5 — The top bar has no separator from the message list

**Evidence.** `topbar` is `(0.04, 0.04, 0.04, 1)` and `bg_main` is
`(0.04, 0.04, 0.04, 1)` — identical. The 48px header sits directly above
the chat surface with no edge, so it reads as floating chrome rather than
a header.

**Fix.** Add a 1px `border-subtle` hairline on the top bar's bottom edge.

**Files.** `kv_md.kv` → `<MainScreen>` top bar.

### P2-6 — Own-message author names are brighter than other people's

**Evidence.** `kv_md.kv:73`:
`color: app.theme_colors['text'] if root.is_own else app.theme_colors['text_muted']`

Own messages get the brighter `text`; everyone else gets the dimmer
`text_muted`. This inverts the convention: the *other* person's name is
the one you are reading.

**Fix.** Swap: own messages use `text-muted`, others use `text`.

**Files.** `kv_md.kv:73`.

---

## P3 findings

### P3-1 — Duplicate palette definitions

`kivy_client.py:113–123` defines module-level hex constants (`BG`,
`SIDEBAR`, `PANEL`, `TEXT`, …) that are each referenced **exactly once** —
their own definition. They are dead code and a drift hazard: the skill
notes warn that the two copies must stay in sync, but in practice nothing
reads the constants, so a future edit to them would silently do nothing.

**Fix.** Delete the constants; `DEFAULT_THEME_COLORS` is the only copy.

### P3-2 — The config screen repeats 14 widget blocks by hand

`kv_md.kv` → `<ConfigScreen>` contains fourteen near-identical
`BoxLayout`/`Label`/`MDTextField` triples, one per palette key. Adding a
token means adding a block and remembering to add a matching `c_<key>` id.

**Fix.** Generate the rows in Python from `THEME_KEYS`, keeping the
`c_<key>` id convention.

### P3-3 — Connect-screen form spans the full window width

Measured field width: **1286px** at a 1366px window. A username field
1286px wide is unusable on a wide monitor and visually unbalanced.

**Fix.** Constrain the form to ~420px and centre it.

### P3-4 — `kv_md.kv` is loaded twice in some contexts

`build()` calls `Builder.load_file(...)` exactly once, and the shipped app
does not double-load (verified: zero "loaded multiples times" warnings
from a real `app.run()`). But `App.build()` called manually *and*
`app.run()` both loading produces Kivy's
`"The file kv_md.kv is loaded multiples times"` warning and an
`Ignored class "SlimButton" re-declaration`. Test harnesses that call
`build()` before `run()` will hit this.

**Fix.** In tests, let `app.run()` own the build, or guard the load with a
module-level flag.

---

## Remediation status

All 15 findings are remediated. Each fix was re-verified with the same
method as the audit (absolute geometry from the layout engine on a real
`app.run()`), not by re-reading the source.

| # | Item | Verified fix |
|---|---|---|
| 1 | P1-1 `members \u25bc` literal text | ASCII arrows, matching the channels button |
| 2 | P1-2 invisible own-message bubble | `msg_own` → `#1E1E1E`, distinct from `panel` |
| 3 | P1-3 profile "Delivery:" shows username | `target_delivery` property renders the lxmf.delivery hash |
| 4 | P0-1 connect screen overflow + clipped banner | content 820px → 344px; ScrollView; banner 200→120px; clipped = 0 at 1280×720, 1024×600, 800×600, 1920×1080, 640×480 |
| 5 | P0-2 hub-address field overlaps heading | form sized from content; zero collisions at all five window sizes |
| 6 | P2-3 `text-dim` contrast failure | `#B08C46` (5.96:1 / 5.54:1) |
| 7 | P2-4 add border tokens | `border #646464`, `border_subtle #3A3A3A` |
| 8 | P2-1 remove the 2/3 button scale | 0 buttons under 40h/48w on main + connect screens |
| 9 | P2-2 input bar 24px → 48px | bar 48px, field 40px capped, 0 children overflow |
| 10 | P2-5 top bar divider | 1px `border_subtle` hairline on the top bar's bottom edge |
| 11 | P2-6 swap own/other author colour | own → `text_muted`, others → `text` |
| 12 | P3-1 delete dead hex constants | 11 constants removed; `DEFAULT_THEME_COLORS` is the single copy |
| 13 | P3-2 generate config rows from `THEME_KEYS` | rows generated in `ConfigScreen._ensure_color_rows`; also closes the gap the audit measured indirectly — the 14 KV rows covered only 14 of 17 keys (`btn_text`, `border`, `border_subtle` were silently uneditable because `save_config` skipped missing `c_<key>` ids). All 17 keys now render, save, validate and reset; Python-added widgets are tracked in `ConfigScreen._color_fields` instead of KV ids (KV ids only register KV-declared widgets) |
| 14 | P3-3 constrain connect form width | all fields 420px, centred |
| 15 | P3-4 guard the KV load in tests | harnesses let `app.run()` own the build |

### Verification runs

- **Connect screen** (fixed 3.0s probe after first layout): content 344px,
  viewport ≥450px at every tested size, Connect button fully on-screen,
  banner clipped 0px, 0 collisions, 0 undersized buttons, disclosure
  toggles 0 → 98 → 0. Sizes: 1280×720, 1024×600, 800×600, 1920×1080,
  640×480.
- **Main screen**: input bar 48px, children within the bar, 0 buttons
  under target, content overflow 0.
- **Config screen** (16 checks): 17 rows = 17 `THEME_KEYS` entries, labels
  match `THEME_LABELS`, prefill matches the live theme, save applies +
  persists (`ui_config.json`), invalid hex rejected without partial
  apply, reset restores defaults *and* refreshes the fields, generated
  labels re-tint on theme reassignment, ScrollView reaches the bottom row.
- **Gates**: `py_compile` OK, `flake8` clean (repo files +
  `tests/test_default_hub.py`), 16/16 server suites, 6/6 client suites,
  `tests/integ_test.py` OK.

### One caveat found while fixing P0-1

A probe that samples a screen's size too early can read 100×100 — the
pre-layout default, not a real defect. The connect-screen probe now waits
for `screen.width > 100` before measuring; an earlier run that did not
produced one spurious 100×100 report that vanished on every subsequent
run. Sample after the first layout pass, or gate on a real size.

## Regression tests to add

1. **Layout-overflow test** — build the app, walk every screen, assert no
   child extends past its parent's bounds and no two siblings overlap.
   This alone catches P0-1, P0-2 and P2-2.
2. **Touch-target test** — assert every `MDButton`/`Button` is ≥40px tall
   and ≥48px wide. Catches P2-1.
3. **Contrast test** — compute the WCAG ratio for every
   (foreground, background) pair used by `DEFAULT_THEME_COLORS` and assert
   ≥4.5:1 for text pairs. Catches P2-3 and any future palette edit.
4. **Glyph test** — assert every button label is ASCII and that no
   rendered string contains a literal `\u` sequence. Catches P1-1.
5. **Palette-uniqueness test** — assert no two tokens that must be
   visually distinct share a value (`msg_own` vs `panel`, `topbar` vs
   `bg_main`). Catches P1-2 and P2-5.

The existing `kivy-client` skill already ships
`scripts/measure_buttons.py` for the touch-target and clipping checks —
extend it rather than writing a new harness.
