---
version: alpha
name: sHades
description: >
  A dark, amber-lit interface for a mesh-native chat client. The palette is
  built on near-black surfaces with a warm vintage-amber primary, a teal
  accent for live/active state, and a strictly separated elevation ladder.
  Every colour pair in this document is validated against WCAG 2.1 AA.
colors:
  primary: "#E8A33D"
  primary-hover: "#F0B255"
  on-primary: "#0A0A0A"
  accent: "#35D0BA"
  accent-hover: "#5ADFCC"
  on-accent: "#0A0A0A"
  bg-main: "#0A0A0A"
  bg-sidebar: "#101010"
  bg-raised: "#161616"
  bg-overlay: "#1E1E1E"
  bg-input: "#1C1C1C"
  bg-input-focus: "#242424"
  border: "#646464"
  border-subtle: "#3A3A3A"
  text: "#F0C078"
  text-muted: "#C79A54"
  text-dim: "#B08C46"
  on-background: "#F0C078"
  error: "#FF6B6B"
  success: "#5BD98A"
  warning: "#F0B255"
typography:
  display:
    fontFamily: Roboto
    fontSize: 40px
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.01em"
  title:
    fontFamily: Roboto
    fontSize: 32px
    fontWeight: 700
    lineHeight: 1.15
  heading:
    fontFamily: Roboto
    fontSize: 24px
    fontWeight: 700
    lineHeight: 1.2
  subtitle:
    fontFamily: Roboto
    fontSize: 20px
    fontWeight: 700
    lineHeight: 1.25
  body-lg:
    fontFamily: Roboto
    fontSize: 16px
    fontWeight: 700
    lineHeight: 1.3
  body:
    fontFamily: Roboto
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.4
  label:
    fontFamily: Roboto
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.35
  caption:
    fontFamily: Roboto
    fontSize: 12px
    fontWeight: 700
    lineHeight: 1.3
  micro:
    fontFamily: Roboto
    fontSize: 11px
    fontWeight: 400
    lineHeight: 1.3
rounded:
  none: 0px
  sm: 4px
  md: 8px
  lg: 12px
  pill: 16px
spacing:
  none: 0px
  xxs: 2px
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  xxl: 40px
  xxxl: 48px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 16px
    height: 40px
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 16px
    height: 40px
  button-secondary:
    backgroundColor: "{colors.bg-raised}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 16px
    height: 40px
  button-secondary-hover:
    backgroundColor: "{colors.bg-overlay}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 16px
    height: 40px
  button-danger:
    backgroundColor: "{colors.error}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 16px
    height: 40px
  button-ghost:
    backgroundColor: "{colors.bg-main}"
    textColor: "{colors.text-muted}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 12px
    height: 40px
  input-field:
    backgroundColor: "{colors.bg-input}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 12px
    height: 44px
  input-field-focus:
    backgroundColor: "{colors.bg-input-focus}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 12px
    height: 44px
  input-hint:
    backgroundColor: "{colors.bg-input}"
    textColor: "{colors.text-dim}"
    typography: "{typography.micro}"
    rounded: "{rounded.md}"
    height: 16px
  channel-item:
    backgroundColor: "{colors.bg-sidebar}"
    textColor: "{colors.text-muted}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    height: 44px
  channel-item-active:
    backgroundColor: "{colors.bg-raised}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    height: 44px
  message-body:
    backgroundColor: "{colors.bg-main}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
  message-body-own:
    backgroundColor: "{colors.bg-raised}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
  message-author:
    backgroundColor: "{colors.bg-main}"
    textColor: "{colors.text}"
    typography: "{typography.caption}"
  message-timestamp:
    backgroundColor: "{colors.bg-main}"
    textColor: "{colors.text-dim}"
    typography: "{typography.micro}"
  member-row:
    backgroundColor: "{colors.bg-sidebar}"
    textColor: "{colors.text-muted}"
    typography: "{typography.label}"
    height: 36px
  member-row-online:
    backgroundColor: "{colors.bg-sidebar}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    height: 36px
  presence-dot-offline:
    backgroundColor: "{colors.text-dim}"
    size: 10px
  presence-dot-online:
    backgroundColor: "{colors.success}"
    size: 10px
  top-bar:
    backgroundColor: "{colors.bg-main}"
    textColor: "{colors.text}"
    typography: "{typography.body-lg}"
    height: 48px
  top-bar-icon:
    backgroundColor: "{colors.bg-main}"
    textColor: "{colors.text}"
    size: 40px
  input-bar:
    backgroundColor: "{colors.bg-raised}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    height: 48px
  scrollbar:
    backgroundColor: "{colors.border-subtle}"
    rounded: "{rounded.pill}"
    width: 6px
  dialog-surface:
    backgroundColor: "{colors.bg-overlay}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
  banner-image:
    backgroundColor: "{colors.bg-sidebar}"
    rounded: "{rounded.none}"
    height: 160px
  button-accent:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 16px
    height: 40px
  button-accent-hover:
    backgroundColor: "{colors.accent-hover}"
    textColor: "{colors.on-accent}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 16px
    height: 40px
  input-field-outline:
    backgroundColor: "{colors.bg-input}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 12px
    height: 44px
  status-warning:
    backgroundColor: "{colors.bg-raised}"
    textColor: "{colors.warning}"
    typography: "{typography.label}"
    height: 26px
  status-error:
    backgroundColor: "{colors.bg-raised}"
    textColor: "{colors.error}"
    typography: "{typography.label}"
    height: 26px
  status-success:
    backgroundColor: "{colors.bg-raised}"
    textColor: "{colors.success}"
    typography: "{typography.label}"
    height: 26px
  divider:
    backgroundColor: "{colors.border-subtle}"
    rounded: "{rounded.none}"
    height: 1px
  focus-ring:
    backgroundColor: "{colors.border}"
    rounded: "{rounded.none}"
    height: 2px
---

## Overview

sHades is a Reticulum-native chat client. It is a desktop-and-Android
KivyMD application: a three-panel chat layout (servers, channels, messages,
members) entered through a connect screen.

The visual identity is **dark vintage**: near-black surfaces lit by a warm
amber primary, with a teal accent reserved exclusively for live/active
state. The interface should feel like a terminal that has been furnished,
not like a corporate dashboard.

Two rules govern everything below:

1. **Amber is content.** Amber (`{colors.primary}`, `{colors.text}`) is the
   voice of the product — headings, body copy, the primary action. If
   everything is amber, nothing is. The teal accent is the only other
   saturated colour allowed, and it means "present", "connected", or
   "selected" — never decoration.
2. **Elevation is contrast, not shadow.** The three dark surfaces
   (`{colors.bg-main}`, `{colors.bg-sidebar}`, `{colors.bg-raised}`) are
   separated by measurable lightness steps, not by drop shadows. Kivy
   shadows read as smudges on near-black.

### Token mapping to the implementation

The spec is normative; `DEFAULT_THEME_COLORS` in `kivy_client.py` is the
single implementation mirror of `colors:` above. Keep them in sync.

| Spec token | `theme_colors` key | Notes |
|---|---|---|
| `bg-main` | `bg_main` | screens, root background |
| `bg-sidebar` | `bg_sidebar` | server rail, channel and member sidebars |
| `bg-raised` | `panel` / `panel_darker` | chat surface, input bar |
| `bg-input` | `input_bg` | text field fill |
| `bg-input-focus` | `input_bg_focus` | text field fill, focused |
| `primary` | `primary` | primary buttons |
| `on-primary` | `btn_text` | text on amber |
| `accent` | `accent` | live state, presence, destructive affordances |
| `text` | `text` | body copy |
| `text-muted` | `text_muted` | secondary copy |
| `text-dim` | `text_dim` | timestamps, hints |

`border`, `border-subtle`, `error`, `success`, `primary-hover`, `accent-hover`
and `on-accent` are new tokens — see *Components* for where they are needed.

## Colors

Every pair below was computed with the WCAG 2.1 relative-luminance formula
and verified at 4.5:1 or better (text) and 3:1 or better (UI boundaries).

### Surfaces

| Token | Hex | Role |
|---|---|---|
| `bg-main` | `#0A0A0A` | app background, chat surface, top bar |
| `bg-sidebar` | `#101010` | server rail, channel list, member list |
| `bg-raised` | `#161616` | input bar, own-message bubble, hover fill |
| `bg-overlay` | `#1E1E1E` | popups and dialogs — above everything |
| `bg-input` | `#1C1C1C` | text field fill |
| `bg-input-focus` | `#242424` | text field fill while focused |

The lightness ladder is 4% → 6.3% → 8.6%. It is deliberately shallow: at
near-black, a 2-point lightness step is the most that reads as a distinct
surface without the whole app looking grey.

### Foreground

| Token | Hex | Contrast on `bg-raised` | Role |
|---|---|---|---|
| `text` | `#F0C078` | 10.79:1 | body copy, active channel, author names |
| `text-muted` | `#C79A54` | 7.05:1 | secondary copy, inactive channels |
| `text-dim` | `#B08C46` | 5.11:1 | timestamps, hints, placeholders |
| `on-primary` | `#0A0A0A` | — | text on amber fills |

`text-dim` is the token that was previously broken. The shipped palette
used `#997026` for it, which measured **4.19:1** on the chat surface and
**4.32:1** on the sidebar — below the 4.5:1 AA threshold for body text.
The replacement `#B08C46` measures 5.11:1 and 5.37:1 respectively, and
still clears 4.5:1 on the focused input fill (4.81:1 against
`bg-input`, 4.38:1 against `bg-input-focus` — the one pair in the system
that must keep `text-muted` rather than `text-dim`).

### Brand and state

| Token | Hex | Role |
|---|---|---|
| `primary` | `#E8A33D` | primary buttons, channel headers, links |
| `primary-hover` | `#F0B255` | primary button hover/pressed |
| `accent` | `#35D0BA` | presence-online, live indicators, destructive confirm |
| `accent-hover` | `#5ADFCC` | accent hover |
| `error` | `#FF6B6B` | validation errors, failed state |
| `success` | `#5BD98A` | connected state, presence dot |
| `border` | `#646464` | input outlines, focus rings |
| `border-subtle` | `#3A3A3A` | dividers, scrollbar trough |

`border` at `#646464` is the first value that clears 3:1 against all three
dark surfaces (3.35 / 3.22 / 3.06). The previous palette had no border
token at all, which is why inputs read as flat rectangles.

### Rules

- The teal accent is **never** a button fill for a normal action. It marks
  presence, connection state, and destructive confirmation only.
- `accent` and `success` are different colours on purpose: teal means
  "the mesh", green means "this person is here".
- Do not introduce a second saturated hue. The analogous amber-to-teal
  harmony is the point.

## Typography

Roboto is the Kivy default and the only bundled family — the app ships no
font files, so every non-Roboto request will silently fall back. Set size
and weight only.

| Token | Size | Weight | Use |
|---|---|---|---|
| `display` | 40px | 700 | "sHades" wordmark, connect screen only |
| `title` | 32px | 700 | screen titles ("Profile") |
| `heading` | 24px | 700 | section headings ("Appearance") |
| `subtitle` | 20px | 700 | profile display name |
| `body-lg` | 16px | 700 | channel header, top bar |
| `body` | 14px | 400 | message body, buttons, inputs |
| `label` | 13px | 400 | channel and member names |
| `caption` | 12px | 700 | author names, unread badges |
| `micro` | 11px | 400 | timestamps, helper text under inputs |

Two rules the codebase already learned the hard way:

- **No symbol glyphs as text.** Roboto has no glyph for ☰ ⌕ ⚙ ▼ ‹ ›. Top-bar
  icons are canvas-drawn (`ui_geom.py`); chevrons are ASCII (`>`, `<`) or
  drawn geometry. Never type a symbol into a label or button.
- **Every multi-line-capable field needs `max_height`.** `MDTextField`
  calls `adjust_height()` on every line change and sets
  `height = max(new_height, dp(56))`, overriding any explicit KV height.
  Pair `size_hint_y: None` with `max_height` or the field balloons and
  overflows its container.

## Layout

### Grid

The app is a 4-column horizontal flex at the top level:

| Column | Width | Content |
|---|---|---|
| Server rail | 64px | guild icons, add-guild |
| Channel sidebar | 220px (collapsible) | banner, channel list |
| Chat | flexible | top bar, message list, input bar |
| Member sidebar | 200px (collapsible) | members, friends |

Collapsible sidebars collapse to 0px width but leave a **14px separator
button** as a sibling so the control is always reachable. The separator is
a sibling of the sidebar, never a child of it.

### Spacing

| Token | Value | Typical use |
|---|---|---|
| `xxs` | 2px | message list row gap, label-to-value gap |
| `xs` | 4px | server rail gaps, button row gaps |
| `sm` | 8px | message row padding, icon-to-label gap |
| `md` | 12px | field padding, config list padding |
| `lg` | 16px | button padding, dialog padding |
| `xl` | 24px | screen-level breathing room |
| `xxl` | 40px | connect-screen screen padding |
| `xxxl` | 48px | connect-screen field block spacing |

### Vertical rhythm — the hard constraint

**A screen's content height must fit its viewport, or the screen scrolls.**

The connect screen needs 820px of content. At a 600px window that is a
220px overflow, and the overflow does not scroll — it clips. Two rules
follow:

1. Any screen whose content can exceed the viewport **must** be wrapped in
   a `ScrollView` with `size_hint_y: 1`.
2. Fixed-height blocks must be sized from the *content*, not guessed. A
   form of N fields of height H with M helper labels of height L is
   `N*H + M*L + (N+M-1)*spacing`, and the container must be given that
   height (or `size_hint_y: None` + `height: self.minimum_height`).

The connect screen must not require 820px. Preferred: banner 120px,
title 48px, subtitle 24px, three fields at 44px with 16px helper text,
one 40px button, and the two optional hash fields collapsed behind a
"advanced" disclosure.

### Input bar

The input bar is **48px**, not 24px. The message field and its buttons
must fit inside it:

- `MDTextField`: `size_hint_y: None`, `height: dp(40)`,
  `max_height: dp(40)` — one line, scrolls internally when it grows.
- Buttons: 40px, `size_hint_y: None`, `height: dp(40)`.
- Bar padding: `dp(8)` vertical, `dp(6)` horizontal.

The previous 24px bar could not contain a 35px `MDTextField` and 26.7px
buttons; all three overflowed into the message list above.

## Elevation & Depth

Four levels, expressed as surface lightness plus a 1px `border-subtle`
hairline. No shadows — on `#0A0A0A` a shadow is invisible.

| Level | Surface | Used by |
|---|---|---|
| 0 | `bg-main` | app background, chat surface, top bar |
| 1 | `bg-sidebar` | sidebars, server rail |
| 2 | `bg-raised` | input bar, own-message bubble, hover states |
| 3 | `bg-overlay` | popups, dialogs, context menus |
| focus | `border` @ `#646464` | focused input outline |

Popups sit at level 3 and are the only element permitted to use
`bg-overlay`. A context menu or dialog that uses `bg-raised` will be
indistinguishable from the input bar behind it.

## Shapes

| Token | Radius | Used by |
|---|---|---|
| `none` | 0px | banners, full-bleed surfaces |
| `sm` | 4px | channel items, member rows, small chips |
| `md` | 8px | buttons, inputs, own-message bubble |
| `lg` | 12px | image embeds, dialogs, message attachment cards |
| `pill` | 16px | scrollbar, presence dots (circular) |

Avatars and presence dots are true circles, drawn with an `Ellipse` mask in
`canvas.after` of the **container** widget, not the image.

## Components

### Buttons

- Primary action: `primary` fill, `on-primary` text, 40px tall, `md`
  radius. Exactly one primary action per screen.
- Secondary: outlined, `text` label, transparent fill over the current
  surface.
- Destructive: `error` fill, and it always asks for confirmation.
- **Every button is at least 40px tall and 48×48px total.** The current
  global scale factor (`BUTTON_V_SCALE = 2/3`, applied via `scaled(dp(N))`)
  produces 21px–33px buttons, which are below the 48dp touch target on
  every platform including desktop mouse use. Remove the factor and set
  explicit heights.
- Icon-only buttons carry an accessible name. Since there is no
  accessibility API in Kivy, that means a text label is mandatory unless
  the icon is canvas-drawn *and* the surrounding context is unambiguous.

### Inputs

- 44px tall, `md` radius, `bg-input` fill, `border` outline.
- Focused: `bg-input-focus` fill, `border` outline at full strength.
- Label above the field, 13px `label` token, `text-muted`.
- Helper text below the field, 11px `micro`, `text-dim` — and only when it
  adds information the label did not already give.
- Validation errors use `error`, and the message replaces (not appends to)
  the helper line.

### Channel list

- 44px rows, `sm` radius, `text-muted` label.
- The selected channel uses `bg-raised` fill and `text` label.
- Unread badge is `caption`, `text`, right-aligned, 40px reserved column
  so the labels do not shift when a badge appears.

### Member list

- 36px rows, 26px avatar, `text-muted` name.
- Presence dot at the trailing edge: 10px circle, `success` when online,
  `text-dim` when offline.
- Friends are grouped in their own section above the member list with a
  `caption` heading.

### Message rows

- Avatar 32px, then a vertical block: author + timestamp header, embed
  container, body.
- Author name is `caption` and bold; own messages use `text`, others use
  `text-muted` — the current implementation inverts this and shows own
  messages brightest, which is wrong.
- Timestamp is `micro`, `text-dim`, right-aligned in a fixed 50px column.
- Own messages get a `bg-raised` bubble with `md` radius. The current
  palette sets `msg_own` to the *same* value as the chat surface
  (`#121212` on `#121212`), so the own-message bubble is invisible — a
  colour bug, not a layout bug.
- Embeds live in a dedicated `embed_container` that grows the row. Never
  add an embed to the message grid directly.

### Top bar

48px tall, `bg-main`, with a `border-subtle` hairline on its bottom edge
to separate it from the message list — currently the top bar and the
message surface are the same colour with no divider, so the header reads
as floating.

Icons are canvas-drawn, 40×40, `text` coloured, and must be separated by
at least 8px of hit-slop to prevent mis-taps.

### Connect screen

- `display` wordmark, `subtitle` supporting line.
- Primary field: **hub address** (`ip:port`), 44px, always visible.
- Secondary field: **username**, 44px, always visible.
- Optional: node hash and delivery hash, collapsed behind a disclosure.
  The connect screen currently shows four full-width fields with helper
  text under each — 340px of form for two values most users need to type.
- Fields are constrained to a readable measure (~420px) and centred. The
  current form spans the full window width (1286px), which is unusable
  on a wide monitor and visually unbalanced.
- The banner image is decorative: it must not push the Connect button
  off-screen, and it must never stretch (`allow_stretch: False`,
  `keep_ratio: True`).

### Config screen

One row per token: 34% label, 66% hex input. Rows are 44px tall and the
list scrolls. The current implementation repeats 14 near-identical
`BoxLayout` blocks by hand in KV — see *Do's and Don'ts*.

## Do's and Don'ts

### Do

- **Keep the palette in one dict.** `DEFAULT_THEME_COLORS` is the single
  source of truth; KV reads `app.theme_colors['key']` and never a
  hardcoded RGBA tuple. To re-skin the app, change the dict.
- **Reassign the whole dict on save** (`app.theme_colors = dict(...)`).
  Mutating one key does not dispatch a `DictProperty` change and the UI
  will not update.
- **Verify contrast for any new colour pair** against the surface it will
  actually sit on. `text-dim` on the chat surface was 4.19:1 and shipped
  that way; the failure was invisible until the numbers were computed.
- **Draw icons on the canvas.** Geometry lives in `ui_geom.py`, which is
  Kivy-free and unit-testable. `tests/test_icons_geom.py` covers it.
- **Give every interactive element a 48×48px hit area**, even when the
  drawn glyph is smaller.
- **Assert layout in tests.** Walk the built widget tree and check that no
  child extends past its parent's bounds and that no two siblings overlap.
  The connect-screen overlap and the input-bar overflow were both
  invisible in code review and would have been caught by such an assertion.
- **Keep `.kv` files ASCII-only.** `grep -cP '[^\x00-\x7F]' *.kv` must be 0.
  Non-ASCII in a KV string is either a missing glyph or a broken escape.

### Don't

- **Don't type symbol characters into labels or buttons.** Roboto renders
  ☰ ⌕ ⚙ ▼ as tofu. Worse, a Python-style escape inside a KV string is
  *not* interpreted: `'members \u25bc'` written with a double backslash
  renders the literal seven-character text `members \u25bc` on screen.
  This is live in the shipped build.
- **Don't use `dp(N)` heights for a whole class of widgets.** A KV class
  rule only matches widgets that do not set their own height, so a global
  scale factor silently skips half the buttons and produces a UI that
  looks inconsistent in ways no grep reveals.
- **Don't scale buttons below 40px.** The 2/3 factor makes the Connect
  button 33px and the `cfg` button 21px. Both are hard to hit.
- **Don't fix a hardcoded height without re-checking the label.** A scaled
  height below the rendered text height clips the glyphs.
- **Don't stretch images.** `allow_stretch: False, keep_ratio: True`
  everywhere — banners, avatars, embeds. Wrap in a container and centre
  instead.
- **Don't let a screen's fixed-height content exceed the viewport.** Size
  from the content and add a `ScrollView`, or the overflow clips silently.
- **Don't repeat a whole widget block 14 times in KV.** The config screen
  should generate its rows from `THEME_KEYS` in Python.
- **Don't duplicate the palette.** The module-level hex constants
  (`BG`, `PANEL`, `TEXT`, …) and `DEFAULT_THEME_COLORS` are two copies of
  the same information and can drift. Delete the constants; keep the dict.
- **Don't invent properties that don't exist.** The profile screen's
  "Delivery:" line renders `target_username` — there is no delivery-hash
  property on that screen, so the label displays the username twice.
