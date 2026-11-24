---
version: alpha
name: sHades
description: >
  A midnight-blue interface for a mesh-native chat client. Warm white keeps
  conversation readable, amber calls attention to actions and selection,
  and teal marks live network state.
  Every colour pair in this document is validated against WCAG 2.1 AA.
colors:
  primary: "#F5B75F"
  primary-hover: "#FFD090"
  on-primary: "#101722"
  accent: "#58D6C3"
  accent-hover: "#7CE5D5"
  on-accent: "#101722"
  bg-main: "#0B1017"
  bg-sidebar: "#0E151F"
  bg-raised: "#111925"
  bg-overlay: "#1B2C3A"
  bg-input: "#172231"
  bg-input-focus: "#1C2C3D"
  border: "#637A87"
  border-subtle: "#2A3B49"
  text: "#F4F0E7"
  text-muted: "#B8C4D1"
  text-dim: "#91A3B4"
  on-background: "#F4F0E7"
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
    height: 48px
  input-field-focus:
    backgroundColor: "{colors.bg-input-focus}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 12px
    height: 48px
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
    height: 48px
  channel-item-active:
    backgroundColor: "{colors.bg-raised}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    height: 48px
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
    backgroundColor: "{colors.bg-input-focus}"
    textColor: "{colors.text}"
    typography: "{typography.body-lg}"
    height: 56px
  top-bar-icon:
    backgroundColor: "{colors.bg-input-focus}"
    textColor: "{colors.text}"
    size: 40px
  site-nav-bar:
    backgroundColor: "{colors.bg-input-focus}"
    textColor: "{colors.text-dim}"
    typography: "{typography.label}"
    height: 40px
  input-bar:
    backgroundColor: "{colors.bg-raised}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    height: 56px
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
    height: 96px
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

The visual identity is **midnight signal**: deep blue surfaces, warm white
conversation, amber navigation and actions, and teal live state. The
channel and member drawers have visible controls in the top bar.

Two rules govern everything below:

1. **Amber is a signal.** Amber (`{colors.primary}`) marks the primary action,
   the active channel, and the author's own messages. Body text stays warm
   white so long conversations remain easy to read. Teal marks connection
   and presence.
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
| `accent` | `accent` | live state, presence |
| `text` | `text` | body copy |
| `text-muted` | `text_muted` | secondary copy |
| `text-dim` | `text_dim` | timestamps, hints |

`border`, `border-subtle`, `error`, `success`, `primary-hover`, `accent-hover`
and `on-accent` are new tokens — see *Components* for where they are needed.

## Colors

Every pair below was computed with the WCAG 2.1 relative-luminance formula
and verified at 4.5:1 or better (text) and 3:1 or better (UI boundaries).

### Surfaces and foreground

| Implementation token | Hex | Role |
|---|---|---|
| `bg_main` | `#0B1017` | app background |
| `bg_sidebar` | `#0E151F` | server, channel and member navigation |
| `panel` | `#111925` | conversation surface |
| `topbar`, `panel_darker` | `#131E2B` | header and composer |
| `sidebar_ctrl`, `input_bg` | `#172231` | selection and fields |
| `input_bg_focus` | `#1C2C3D` | focused field |
| `msg_own` | `#1B2C3A` | own-message surface |
| `text` | `#F4F0E7` | body text, 15.52:1 on `panel` |
| `text_muted` | `#B8C4D1` | secondary text, 9.98:1 on `panel` |
| `text_dim` | `#91A3B4` | timestamps, 6.81:1 on `panel` |
| `primary` | `#F5B75F` | primary actions and active navigation |
| `accent` | `#58D6C3` | live and presence state |
| `border` | `#637A87` | field boundaries, 3.15:1 on focused input |
| `border_subtle` | `#2A3B49` | dividers |
| `aurora_a` | `#F4B75E`, alpha 0.22 | amber aurora orb |
| `aurora_b` | `#59D6C4`, alpha 0.18 | teal aurora orb |
| `aurora_c` | `#7F66F2`, alpha 0.20 | violet aurora orb |

The aurora tokens are translucent by design (alpha 0.18–0.22): the config
screen's hex fields carry no alpha, so saving a theme preserves each
key's live alpha instead of resetting it to opaque (see `save_config`).

These are the actual default values in `DEFAULT_THEME_COLORS`. A saved custom
palette in `ui_config.json` continues to take precedence until the user
chooses **Reset** in Appearance.

### Rules

- Warm white carries long-form content; amber identifies the action or
  selected item. Keep teal for live network state.
- Separate dark surfaces by fill and a narrow divider. Keep text contrast
  at least 4.5:1 and interactive boundaries at least 3:1.
- The active channel gets a raised fill and a narrow amber rail; own posts
  get a distinct fill and amber rail.

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

## Layout and interaction

- A 56px header keeps **Channels** (canvas hamburger), the channel name,
  **People**, search, and profile visible. The channel and member drawers
  also open by edge swipe.
- While a site page is on screen, a 40px nav bar sits under the header,
  outside the page content: **Back** and **Forward** controls (40px tall,
  amber when usable, dimmed when not) and the current page path as a
  breadcrumb (`#channel /path`, `text_dim` 13px). It collapses to 0px on
  chat channels. The in-page `< Back` link keeps working as well.
- The left drawer combines a 64px server rail and a 220px channel list;
  the right drawer is up to 200px wide. Opening one closes the other.
  Each drawer caps its width on narrow windows. Choosing a channel closes
  the left drawer below 720px.
- The 56px composer (grown from 48px to hold the 42px pfp) contains a
  40px input that grows with multi-line text to a 160px cap, with the bar
  growing to match it, and two 40px buttons.
  Rows grow with message content and embeds; own posts use the raised
  `msg_own` fill and a 2px amber rail.
- The connection screen scrolls vertically. Its form caps at 420px wide,
  has two required 44px fields, and reveals the two optional hashes below
  the main controls. The four controls occupy 206px including spacing.
  The banner is decorative and keeps its aspect ratio.
- Selected channel rows are 48px tall with a raised fill and amber rail.
  Presence uses the teal accent; the 11px metadata text is `text_dim`.
- The Appearance screen generates fields from `THEME_KEYS`. A complete
  palette is saved as hex in `ui_config.json`; Reset restores these defaults.

### Buttons

One widget, `FancyMDButton` (`client_widgets.py`), carries every
action button in the app. Hover swaps the background to the hover
token, press runs the `tap dip` (`ui_fx.tap_feedback`), and disabled
dims the whole button to 0.45 opacity — KivyMD's own disabled wash
is only ~9% darker, so the widget dims explicitly instead of relying
on it. Hover is a state, not motion, so it still applies under
reduced motion while the press dip is skipped. Text colour lives on
the `MDButtonText` child and is not owned by the widget. `danger`
has no hover token in the spec, so hover keeps its base colour; a
`danger-hover` token would be needed to change that (proposal, not
added — see below).

| Variant | Base | Hover | Pressed | Disabled |
|---|---|---|---|---|
| `primary` | `primary` (`#F5B75F`) | `primary-hover` (`#FFD090`) | tap dip (opacity 0.55, 0.06s + 0.28s return) | opacity 0.45 |
| `accent` | `accent` (`#58D6C3`) | `accent-hover` (`#7CE5D5`) | tap dip | opacity 0.45 |
| `secondary` | `bg-raised` (`panel`, `#111925`) | `bg-overlay` (`msg_own`, `#1B2C3A`) | tap dip | opacity 0.45 |
| `danger` | `error` (`#FF6B6B`) | unchanged (no hover token) | tap dip | opacity 0.45 |
| `ghost` | `bg-main` (`#0B1017`) | `bg-raised` (`panel`, `#111925`) | tap dip | opacity 0.45 |
| `outlined` | transparent | faint `bg-overlay` tint (alpha 0.35) | tap dip | opacity 0.45 |

`primary-hover`, `accent-hover` and `error` are not keys of
`theme_colors` yet, so the widget falls back to the DESIGN.md hex
values above whenever the theme lacks them; a missing key never
raises. Proposal (not done here): add those three keys to
`DEFAULT_THEME_COLORS` and to `THEME_KEYS`, so the Appearance
screen can edit them and a custom theme can recolour hover and
danger states. That is safe for existing saved themes —
`_load_ui_config` only overwrites keys already in `theme_colors` —
but it adds three config rows, so it is a separate change.

## Motion

One motion language across the app (`ui_fx.py` is the implementation;
durations below are the spec). Only opacity moves for entrances, never
position or size, so a reveal never shifts layout.

| Token | Duration | Use |
|---|---|---|
| `screen fade` | 0.18s | every ScreenManager switch (connect, onboarding, main, profile, config, site editor, server config), `FadeTransition` |
| `drawer slide` | 0.22s | both swipe drawers (channels, members) sliding 0 <-> open width, `out_quint` (`ui_fx.slide_property`) |
| `popup fade` | 0.15s | every `Popup` opening, opacity 0 -> 1 (`ui_fx.popup_entrance`) |
| `reveal` | 0.42s | messages, site pages and screen sections fading in (`ui_fx.reveal`) |
| `stagger` | 0.45s each, 0.06s apart | lists revealing one row after another (`ui_fx.stagger`) |
| `tap dip` | 0.06s down to 0.55 opacity + 0.28s back | press acknowledgement (`ui_fx.tap_feedback`) |

Easing tokens: `EASE_OUT` (`out_quint`) for things arriving,
`EASE_IN_OUT` (`in_out_cubic`) for things drifting,
`EASE_SPRING` (`out_back`) for small playful settles.

Reduced motion: when `ui_fx.set_reduced_motion(True)` is on, every effect
lands in its final state instantly: the screen fade duration becomes 0,
`reveal` sets opacity 1 at once, `tap_feedback` does nothing, the drawer
slide lands at its open/closed width at once, the popup fade opens at
opacity 1 at once, while the underlying switch or state change still
happens exactly as before.
The live ScreenManager is registered with `ui_fx` at build time so the
flag retimes its transition without touching any screen-switch call site.

## Implementation checks

- Keep default RGBA values in `DEFAULT_THEME_COLORS` and reference them
  through `app.theme_colors` in KV. Reassign the whole `DictProperty` after
  editing so bindings update.
- Draw unsupported symbol icons on the canvas (`ui_geom.py`). Keep KV
  strings ASCII-only.
- Check widget geometry after Kivy's first layout pass. The mobile layout
  smoke test is `tests/test_ui_layout.py`; it checks readable fields,
  nonoverlap, and exclusive drawers on a real window.
- Keep buttons at least 40px tall with a usable hit area. Images preserve
  their aspect ratio. Expandable content goes inside a `ScrollView`.
- The palette is user configurable. Do not overwrite a saved custom theme
  during upgrade; users can choose Reset to adopt the new defaults.
