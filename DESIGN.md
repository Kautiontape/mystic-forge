# Mystic Forge — Web Design Language

Catppuccin skin, Primer grammar. The palette is Catppuccin (Latte day /
Macchiato night); the *rules* below come from GitHub Primer's interface
guidelines and Refactoring UI's hierarchy heuristics. Every page change
should be checkable against this file.

## Voice (typography)

| Voice | Stack | Used for | Never for |
|---|---|---|---|
| Display serif | Iowan/Palatino/Georgia | page title, card names, italic notes | buttons, labels, prose |
| UI sans | Avenir Next/Seravek/system-ui | everything interactive + labels + prose | numbers |
| Data mono | ui-monospace stack | prices, deltas, dates, codes, tables | headings, buttons |

Title lockup: `h1` at `line-height:1.08`, `text-wrap:balance` (no dangling
words), and a **length-aware step-down** — titles longer than ~16 chars render
one size smaller (`h1.long`). Every page has a one-line **subtitle** in
`--sub` directly under the title; the title never floats alone.

## Interaction tiers (the button rule)

**One filled primary per view.** Everything else recedes into its role:

1. **Primary** (`.btn.primary`, mauve fill) — the single thing you came to do.
   Board: *Add card*. Shared board: *Make my own copy*. Dialogs: the verb.
2. **Secondary** (`.btn`, bordered) — rare; destructive gets `.danger`.
3. **Invisible / text** (`.textlink`) — navigation and utilities: Alerts,
   History, Back, Share. No border, no fill; hover underlines.
4. **Tabs** (`.shops` underline style) — view settings, not actions. Switching
   the price source is *looking*, not *doing*, so it never looks like a button.
5. **Token chip** (`.chip`) — an inline copyable value (share code, ntfy
   topic): mono, bordered, click-to-copy. It looks like data because it is.

If a control can't say which tier it's in, it's in the wrong markup.

## Enclosure budget

Borders are spent on **content containers only**: cards, dialogs, tables, the
history rail. Controls, labels, captions, and stat tiles use spacing,
alignment, and type weight to group — never boxes. (Stat tiles keep a soft
surface, no border.) When a region feels crowded, remove enclosure before
shrinking type.

## Layout grammar

- Masthead: title lockup left; a quiet right-cluster (`.mright`) floats top
  right with text links + theme toggle. Title text flows around it.
- Actions row under the subtitle: primary + text utilities left, view tabs
  right (`margin-left:auto`).
- Verdict banner → stat tiles → content grid. Explanatory text (freshness,
  legend) lives in the subtitle, never among controls.

## Color rules

- Catppuccin only; mauve is the sole action accent, used once per view.
- Status text uses the `-text` variants in Latte (`--green-text` etc.) —
  the raw pastels are for strokes/fills, not words.
- Color is never the only carrier: deltas pair glyph (▼▲) + text; hits pair
  glow + "buy window" copy; buy-good means **down is green**.
- `--overlay` is a border color, not a text color; body text ≥ `--sub`.

## Data display

- Money always two decimals, currency symbol from the active shop
  (cardmarket €; targets are always USD and say so away from tcgplayer).
- Deltas show absolute + percent. Mono everywhere numbers appear.
- Charts: 2px line, blue (green when at target, muted when bought), no more
  than three gridlines, labels in `.axis`/`--sub`, dashed peach target line,
  dashed lavender bought line.

## Motion & state

- No full-page navigations: every internal link and mutation morphs `.wrap`
  in place (View Transition crossfade); card entrance animation replays as
  feedback. Genuine navigations inherit the themed `html` background.
- Dialogs: backdrop click, ×, and Esc all close; destructive actions confirm
  inline (never `confirm()`/`alert()`/`prompt()`).

## Accessibility floors

- Tap targets ≥ 44px on mobile (min-height, not font inflation).
- Inputs ≥ 16px on mobile (iOS zoom).
- Interactive non-buttons get `tabindex`, `role`, Enter/Space, and
  `:focus-visible` rings. Text contrast ≥ 4.5:1 in both themes.
