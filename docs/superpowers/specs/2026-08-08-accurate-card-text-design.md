# Accurate Card Text — Design

**Date:** 2026-08-08
**Branch:** `accurate-card-text`

## Problem

When Claude discusses cards from a fetched deck, it has names but not rules
text, so it fills the gap from memory. Memory is wrong often enough to matter —
distinct cards share similar names (two different Eriettes), and text gets
errata'd. A user-side "verify card text" memory does not fix this because it
says *what* to do with no *how*: Claude invents Moxfield searches and fails.

Two facts make this cheap to fix:

1. Archidekt's deck API already returns full oracle data for every card
   (`oracleCard.text`, `manaCost`, `types`, `superTypes`, `subTypes`,
   `power`/`toughness`, and `faces` for multi-faced cards). `archidekt_deck`
   currently discards all of it.
2. `scryfall_price_list` already contains the full bulk pipeline against
   Scryfall's `POST /cards/collection` (parse decklist → dedupe → chunk of 75 →
   fetch → index results), but formats prices only.

## Goals

- Claude can obtain exact oracle text for a whole deck or list in one or two
  tool calls, from any source (Archidekt, Moxfield paste, EDHREC output,
  ad-hoc list).
- Server instructions steer Claude to verify text before making rules claims.
- Missing/unmatched cards are reported explicitly, never silently dropped.

## Non-goals

- No Moxfield API integration (separate concern; decklist paste covers it).
- No change to `archidekt_export`, `format_archidekt`, or price tools.
- No caching layer; Scryfall and Archidekt round-trips are already fast and
  the collection endpoint batches 75 at a time.

## Design

### 1. New tool: `scryfall_card_text`

Bulk oracle-text lookup via `POST /cards/collection`.

**Input** (`CardTextInput`): one field, `cards` — decklist-style text, one
card per line. Same parser and constraints as `scryfall_price_list.decklist`
(`_parse_decklist_entries`; max 500 lines, max 50,000 chars). Plain names
work; full Archidekt/Moxfield lines (`1x Sol Ring (ltc) 284 *F* [Ramp]`) are
tolerated — quantity, finish, and category are ignored, set/collector pins
are passed through to Scryfall but oracle text does not vary by printing.

**Pipeline** (all existing helpers, no new plumbing):
`_parse_decklist_entries` → `_dedupe_identifiers` → `_chunk(…, 75)` →
`_scryfall_post("/cards/collection", …)` → `_index_collection_results` →
`_lookup_entry` per entry.

**Output**: one block per *unique requested card*, in request order,
formatted with the existing `_format_card(card, verbose=False)` — name, mana
cost, type line, full oracle text, P/T/loyalty, color identity; multi-faced
cards render each face (already handled). Duplicate lines for the same card
collapse to one block.

Entries Scryfall could not match go in an explicit trailing section:

```
## Not found (2)
- Erriette of the Charmed Aple
- Sol Rng
(/cards/collection requires exact names — retry misspelled names one at a
time with scryfall_named, which fuzzy-matches.)
```

**Tool description** states the intended use: "Use before discussing what
specific cards do — never rely on memory for card text."

### 2. `archidekt_deck`: `include_text` flag

`ArchidektDeckInput` gains `include_text: bool = False` — "Include mana
cost, type line, and full oracle text for every card. Set true whenever you
will discuss what cards do."

With the flag on, each card line gains indented detail beneath it, from data
already present in the deck response (zero extra HTTP calls):

```
## Enchantments (14)
1 Eriette of the Charmed Apple {1}{W}{B}
   Legendary Creature — Human Warlock 1/4
   At the beginning of your end step, each opponent loses 1 life for each
   Aura you control attached to a permanent that player controls...
```

Field mapping from `oracleCard`:

- Mana cost: `manaCost`; type line: `superTypes` + `types` + `subTypes`
  joined as `Super Type — Sub` (em-dash section only when subtypes exist).
- P/T from `power`/`toughness` when both present; loyalty from `loyalty`.
- Text: `text`.
- **Multi-faced cards:** top-level `text` is empty and `manaCost` is combined
  (`{2}{B} // {B}`); when `faces` (a list of face dicts with `name`,
  `manaCost`, `text`, type parts, `power`/`toughness`, `loyalty`) is
  non-empty, render each face separated by ` // `-titled sub-blocks instead
  of the top-level fields.

A card assigned to multiple categories repeats its text under each (rare;
simplicity wins). Default output is byte-identical to today.

### 3. Steering: server instructions + tool descriptions

Append to the `FastMCP(instructions=…)` block (server.py:78):

> Never state or reason about a card's rules text from memory — many distinct
> cards have similar names, and text gets errata'd. Before discussing what
> specific cards do, fetch exact text: `archidekt_deck` with
> `include_text=true` when the deck lives on Archidekt, `scryfall_card_text`
> for any list of names, `scryfall_named` for a single card.

The cue is repeated in both tools' descriptions because claude.ai surfaces
tool descriptions even when server instructions are truncated.

## Error handling

- Scryfall HTTP failures → existing `_scryfall_error` strings. A chunk
  failure fails the whole call with that message (matches
  `scryfall_price_list` behavior).
- Archidekt failures → existing `_archidekt_error` (unchanged path).
- Empty parse (no valid lines) → explicit "No card names found in input."

## Testing

Follow existing patterns in `tests/` (pure-helper tests plus async tool tests
that `monkeypatch.setattr(server, "_scryfall_post", fake)` /
`"_archidekt_get"`):

- `scryfall_card_text`: happy path (names → blocks in request order),
  duplicate-line collapse, DFC face rendering, not-found section wording,
  >75 identifiers split into two POST bodies, empty-input message.
- `archidekt_deck`: `include_text=false` output unchanged (regression),
  `include_text=true` renders mana/type/PT/text, `faces` path for a DFC,
  missing oracle fields degrade to the bare name line.

## Docs

README tool table gains `scryfall_card_text` and the `include_text` flag.

## Follow-up (outside this repo)

After deploy, replace the user's claude.ai "Verify card text" memory with one
naming the tools: "Verify MTG card text via archidekt_deck include_text /
scryfall_card_text / scryfall_named — never from memory."
