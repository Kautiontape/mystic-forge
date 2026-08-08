# Scryfall Pricing by Specific Printing — Design

- **Date:** 2026-08-08
- **Status:** Approved (design), pending implementation plan
- **Component:** `server.py` (Mystic Forge MCP server)
- **Branch:** `price-check-for-printings`

## Problem

A user asked whether the price tools can price a **specific printing**. They can't.
Both pricing tools operate on card *names* and let Scryfall or a cheapest-first sort
decide which physical card you're being quoted for.

`scryfall_price` (`server.py:316`) queries `/cards/search` with `!"Name"`,
`unique=prints`, `order=usd&dir=asc`, then prints the first `limit` (default 10)
printings. There is no way to name a set, collector number, or finish.

`scryfall_price_list` (`server.py:378`) builds identifiers as
`[{"name": n} for n in cards]` (`server.py:385`). A bare-name identifier lets
Scryfall pick the printing, so every price is for a printing the user did not choose.

Two further defects surfaced while investigating, both of which produce **wrong
numbers rather than missing features**, and both of which this design fixes:

1. **`order=usd&dir=asc` sorts null prices first.** Printings with no USD price sort
   ahead of every real price. For a card with many unpriced printings, the tool's
   entire output is unpriced rows.
2. **`scryfall_price_list` falls back across finishes.** `usd or usd_foil or
   usd_etched` (`server.py:400`) silently quotes a foil price for a card whose
   printing has no nonfoil price, and folds it into the total unlabelled.

A third gap: `_parse_decklist` (`server.py:1407`) already receives Archidekt/Moxfield
`(set) 123` suffixes and deliberately strips them (`server.py:1434-1435`). The
printing data the user needs is arriving at the server and being discarded.

## Goals

1. Let a user price one named printing of a card (set, collector number, finish).
2. Let a user price a pasted decklist at the printings written on its lines.
3. Never quote a price for a different finish or printing than the one asked for.
4. Make truncation, fallbacks, and missing prices visible in the output.

## Non-goals

- Cheapest-legal-printing search ("what would this deck cost to build?"). Decided
  against: it needs one `/cards/search` per unspecified card, and misprices users
  who own a more expensive copy. See Decision D2.
- Emitting finish markers from `format_archidekt`. That tool does not write `*F*`
  today; adding it is a separate change.
- Non-USD totals. EUR and tix stay display-only, as today.
- Price history or trends. Scryfall does not expose them.

## Key findings from API investigation

All verified against live `api.scryfall.com` on 2026-08-08.

### `/cards/collection` (POST) accepts printing-specific identifiers

Confirmed working in a single mixed request:

| Identifier sent | Card returned |
|---|---|
| `{"name": "Rhystic Study"}` | J22 #114, `usd` 69.53 |
| `{"set": "ltc", "collector_number": "284"}` | Sol Ring, LTC #284, `usd` 2.51 |
| `{"name": "Arcane Signet", "set": "otc"}` | OTC #252, `usd` 0.65 |
| `{"set": "unf", "collector_number": "240"}` | Plains, UNF #240 |
| `{"name": "Notarealcardxyz"}` | echoed in `not_found` |

Notes:

- `not_found` echoes back the **identifier object as sent**, not a name string. The
  existing code reads `item.get("name", str(item))` (`server.py:427`), which yields
  `str(dict)` for a set/collector-number identifier. Formatting must handle this.
- Response order matched request order in this test, but Scryfall does not guarantee
  it and `not_found` entries are omitted from `data`, so positional matching is
  unsafe. Results must be matched back to input lines explicitly.
- Hard cap of **75 identifiers per request**. The current tool caps *input* at 75
  and does not batch, so a 100-card deck cannot be priced today.

### `/cards/search` supports the filters we need

- `!"Sol Ring" set:ltc cn:284` with `unique=prints` → exactly 1 result.
- `-is:digital` on `!"Counterspell"`: 71 printings → 62. The 9 removed are
  MTGO/Arena-only printings that can never have a USD price.
- `is:foil -is:nonfoil` finds foil-only printings. Each card carries a `finishes`
  array (e.g. `["foil"]`, `["nonfoil","foil"]`) that states which finishes exist,
  independently of whether a price is populated.
- Collector numbers are **not always numeric**: observed `IFIYW-10` on a Secret Lair
  printing. Any collector-number field must be a string, not an int.

### The null-sort defect, reproduced

`scryfall_price` for `Counterspell`, using the tool's exact current query, returns
these as its 10 displayed rows:

```
Tempest Remastered (TPR)   usd=null  tix=0.39   digital=true
Vintage Masters (VMA)      usd=null  tix=0.04   digital=true
Masters Edition IV (ME4)   usd=null  tix=0.65   digital=true
Masters Edition II (ME2)   usd=null  tix=0.58   digital=true
Summer Magic / Edgar (SUM) usd=null  eur=2180.23
Secret Lair Drop (SLD)     usd=null
Through the Omenpaths (OMB) usd=null tix=2.03   digital=true
Magic Online Promos (PRM)  usd=null  tix=2.85   digital=true
Media and Collab (PMEI)    usd=null
Magic Online Promos (PRM)  usd=null  tix=0.58   digital=true
```

Every row has `usd=null`, six are digital-only, and because `cheapest_usd` is only
assigned when `usd` is truthy (`server.py:356`), the `Cheapest:` summary line never
prints. A user asking the price of Counterspell currently gets ten rows of MTGO
printings and no dollar figure. The real cheapest paper printing is DMR at $2.15.

### Pagination

`/cards/search` returns at most 175 cards per page. Most cards fit: Lightning Bolt
64 printings, Sol Ring 131. Basic lands do not: Forest has 876. Page 1 is sufficient
for every realistic pricing question, but the output must say when it is showing a
subset. See Decision D3.

## Design

### Part 1 — `scryfall_price`: pin a printing

`PriceInput` gains five optional fields:

| Field | Type | Default | Effect |
|---|---|---|---|
| `set_code` | `str?` (2–6 chars) | `None` | adds `set:<code>` to the query |
| `collector_number` | `str?` | `None` | adds `cn:<number>`; **requires** `set_code` |
| `finish` | `nonfoil \| foil \| etched`? | `None` | adds `is:<finish>`; selects the leading price column |
| `include_digital` | `bool` | `False` | when false, appends `-is:digital` |
| `order` | `usd \| released \| set \| name` | `usd` | passed through to Scryfall |

`collector_number` without `set_code` is a validation error, not a silent no-op —
collector numbers are only unique within a set. Enforced with a Pydantic
`model_validator`.

**Null-price sorting.** Scryfall's ordering is kept as the request parameter, but
results are re-sorted client-side before display: printings with a price in the
selected finish first (ascending), unpriced printings last. This is the fix for the
Counterspell case. The selected finish is `finish` if given, otherwise `nonfoil`.

**Single-printing mode.** When `set_code` and `collector_number` together resolve to
exactly one card, output switches from a list to a detail block: all price columns,
plus artist, collector number, rarity, frame/border, promo types, `finishes`, and the
Scryfall link — the fields that let someone confirm which physical copy they hold.

**Truncation is stated.** The header becomes `Showing N of M printings` whenever
`M > N`, and names the filters in effect, so a missing printing is explainable rather
than mysterious. `limit` keeps its current 1–50 range and default of 10.

### Part 2 — `scryfall_price_list`: price a real deck

**Input change.** `cards: list[str]` is replaced by `decklist: str` — raw pasted
text. Nothing in the repo calls this tool and no tests cover it (verified by grep);
MCP clients read the schema at call time, so there is no persisted call site to
break. A caller holding a name list migrates with `"\n".join(names)`, which parses
correctly since a bare name is a valid line.

**Parsing.** A new `_parse_decklist_entries(text) -> list[DecklistEntry]` where
`DecklistEntry` is a frozen dataclass:

```python
@dataclass(frozen=True)
class DecklistEntry:
    quantity: int
    name: str
    set_code: str | None
    collector_number: str | None   # string: collector numbers are not always numeric
    finish: str | None             # "foil" | "etched" | None
```

Line grammar handled:

```
[qty][x] Name [(set)] [collector] [*F*|*E*] [[Category{flags}]] [^Label,#hex^]
```

Extraction order, applied left to right on each line:

1. Skip blanks and lines starting with `//` or `#` (existing behavior).
2. Leading quantity via the existing `^(\d+)x?\s+(.+)$`; absent means 1.
3. Strip `^...^` labels and `[...]` category annotations.
4. Extract and remove the finish marker: `*F*` → foil, `*E*` → etched, also
   accepting case-insensitive `(foil)` and `(etched)`. `*F*`/`*E*` is the Moxfield
   export convention and is accepted by Archidekt's importer. Note that this repo's
   own `format_archidekt` does not currently emit these markers.
5. Extract set and collector number with an **anchored** pattern —
   `\((?P<set>[a-zA-Z0-9]{2,6})\)(?:\s+(?P<cn>[^\s\[\]^*]+))?\s*$` — so it only
   matches a trailing set/collector suffix. Anchoring is what keeps real card names
   containing parentheses safe: `Erase (Not the Urza's Legacy One)` and
   `B.F.M. (Big Furry Monster)` both contain spaces inside their parentheses and
   cannot match a 2–6 char alphanumeric set token.
6. Apply the **existing** residual strips from `_parse_decklist` (`server.py:1432-1435`)
   to whatever name remains.

Step 6 exists specifically to guarantee byte-identical names for current callers.

**`_parse_decklist` is preserved, not replaced.** It becomes a wrapper:

```python
def _parse_decklist(text: str) -> list[tuple[int, str]]:
    return [(e.quantity, e.name) for e in _parse_decklist_entries(text)]
```

Its two callers — `validate_decklist` (`server.py:1451`) and `precon_diff`
(`server.py:2183`) — are untouched and must observe no behavior change. A regression
test asserts this against a corpus of line formats.

**Identifier selection**, per entry:

| Entry has | Identifier sent | Result labelled |
|---|---|---|
| set + collector number | `{"set", "collector_number"}` | exact printing |
| set only | `{"name", "set"}` | Scryfall's pick within that set |
| neither | `{"name"}` | flagged as a default-printing fallback |

Identical identifiers are de-duplicated before the request and their results fanned
back out to every line that asked for them, so a deck listing the same printing
twice costs one slot rather than two.

**Batching.** Requests are chunked at 75 identifiers. The `max_length=75` input cap
is removed; a per-request cap on total lines (500) replaces it as a sanity bound.

**Matching results to lines.** Responses are keyed into a lookup dict — by
`(set, collector_number)` and by `(name.lower(), set)` and by `name.lower()` — and
each line looks itself up. No positional assumptions.

**Price selection.** This is the correctness fix:

- The entry's finish selects the column: `foil` → `usd_foil`, `etched` →
  `usd_etched`, unspecified → `usd`.
- **If that column is null, there is no cross-finish fallback.** The line goes to a
  "no price in requested finish" section, reporting which finishes the printing
  actually has (from `finishes`) and what those cost. The user is told, not guessed at.
- `quantity` multiplies the unit price into a line total.

**Output sections**, in order:

1. **Priced** — `qty x Name (SET #cn, finish) — $unit ea → $line_total`, most
   expensive line total first.
2. **Default printing used** — lines with no printing specified, showing which
   printing Scryfall chose. Included in the total.
3. **No price in requested finish** — excluded from the total, with the available
   finishes and their prices listed.
4. **Not found** — rendered from the identifier that was sent, not `str(dict)`.
5. **Total** — `$X.XX across N of M cards`, plus a count of lines in sections 2–4 so
   the total's coverage is explicit.

### Structure

New pure helpers, all independently testable without network access:

| Helper | Responsibility |
|---|---|
| `_parse_decklist_entries` | text → `list[DecklistEntry]` |
| `_entry_identifier` | `DecklistEntry` → Scryfall identifier dict |
| `_price_for_finish` | `(card, finish)` → `Decimal \| None`, no cross-finish fallback |
| `_index_collection_results` | response → lookup dict |
| `_format_printing_line` | card + entry → one display line |

The tools themselves keep only HTTP orchestration and section assembly, matching how
`_diff_key` and `_archidekt_in_deck_cards` are factored in the existing code.

**Money arithmetic** uses `Decimal`, not `float`. The current
`total = round(total + val, 2)` loop over floats (`server.py:415`) accumulates
representation error across 100 lines. Scryfall returns prices as strings; they parse
cleanly into `Decimal`.

## Decisions

- **D1 — Replace `cards` rather than adding `decklist` alongside it.** Two
  mutually-exclusive input modes on one tool means a longer description and a choice
  the model can get wrong. There is no caller to break.
- **D2 — Unspecified lines use Scryfall's default printing, flagged.** The
  alternative, cheapest-paper-printing, costs one search per unspecified card (~100
  extra requests for a bare decklist) and misprices anyone who owns a pricier copy.
  Rejected as the default and not offered as a parameter, per YAGNI.
- **D3 — Page 1 only, truncation stated.** Paginating an 876-printing basic land to
  display at most 50 rows is wasted work. The header names the total, so the user
  can narrow with `set_code`.
- **D4 — No cross-finish price fallback.** A wrong number presented confidently is
  worse than an absent one. This changes existing behavior deliberately.
- **D5 — `collector_number` is a string.** Verified non-numeric values exist.

## Testing

Following the existing suite's style — pure functions against fixture dicts, no HTTP
mocking (`tests/test_precon_diff.py`).

**Parser** (`_parse_decklist_entries`):
- bare name; `1 Name`; `1x Name`
- `1x Sol Ring (ltc) 284`; lowercase and uppercase set codes
- foil `*F*` and etched `*E*`, with and without a set suffix
- full Archidekt line with `[Category{flags}]` and `^Label,#hex^`
- non-numeric collector number (`IFIYW-10`)
- names containing parentheses: `Erase (Not the Urza's Legacy One)`,
  `B.F.M. (Big Furry Monster)` — set/collector must stay `None`
- `//` and `#` comment lines skipped

**Regression:** `_parse_decklist` output is unchanged across the full corpus above.
This is the test that protects `validate_decklist` and `precon_diff`.

**Identifier building** (`_entry_identifier`): each of the three shapes in the table.

**Price selection** (`_price_for_finish`):
- foil requested, `usd_foil` present → that value
- foil requested, `usd_foil` null but `usd` present → `None` (the anti-fallback test)
- etched requested → `usd_etched`
- no finish requested → `usd`

**Result matching** (`_index_collection_results`): out-of-order responses still match
their lines; `not_found` identifier objects render readably.

**Totals:** quantity multiplication; `Decimal` accumulation over a 100-line list
matching an exact expected sum; unpriced lines excluded from the total but counted in
the coverage figure.

**`scryfall_price` filters:** query-string construction for each combination of
`set_code` / `collector_number` / `finish` / `include_digital`; the
`collector_number`-without-`set_code` validation error; client-side null-last
re-sorting given a fixture list with mixed null and non-null prices.

## Documentation

`README.md:13-14` tool descriptions updated to state that both tools accept specific
printings.

## Risks

- **Behavior change for existing `scryfall_price_list` callers.** Mitigated: no
  in-repo callers, no tests, and name-list input still parses.
- **Parser regression affecting `validate_decklist` / `precon_diff`.** Mitigated by
  step 6 reusing the existing strips verbatim, plus the dedicated regression test.
- **Removing the cross-finish fallback shows "no price" where a number used to
  appear.** Intended. The output explains which finishes exist and what they cost.
