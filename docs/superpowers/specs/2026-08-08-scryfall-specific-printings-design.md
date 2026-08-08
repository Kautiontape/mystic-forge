# Scryfall Pricing by Specific Printing — Design

- **Date:** 2026-08-08
- **Status:** Approved (design), pending implementation plan
- **Component:** `server.py` (Mystic Forge MCP server)
- **Branch:** `price-check-for-printings`
- **Tools affected:** `scryfall_price`, `scryfall_price_list`, `format_archidekt`,
  `archidekt_export`

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

The remaining gaps are all the same shape — printing data reaching this server and
being thrown away:

- `_parse_decklist` (`server.py:1407`) receives Archidekt/Moxfield `(set) 123`
  suffixes and deliberately strips them (`server.py:1434-1435`).
- `archidekt_export` (`server.py:1205-1253`) reads a deck payload carrying a
  `modifier` field of `Normal`/`Foil`/`Etched` per card and never reads it, so a foil
  deck exported through this server comes back all-nonfoil.
- `format_archidekt` has no way to express a finish at all.

The last two matter because they compound: a decklist round-tripped through this
server loses its foils, and would then be priced entirely at nonfoil prices by the
very tool this design is adding.

## Goals

1. Let a user price one named printing of a card (set, collector number, finish).
2. Let a user price a pasted decklist at the printings written on its lines.
3. Never quote a price for a different finish or printing than the one asked for.
4. Make truncation, fallbacks, and missing prices visible in the output.
5. Stop discarding finish information in the two tools that emit decklists, so a
   decklist round-tripped through this server still prices correctly.

## Non-goals

- Cheapest-legal-printing search ("what would this deck cost to build?"). Decided
  against: it needs one `/cards/search` per unspecified card, and misprices users
  who own a more expensive copy. See Decision D2.
- Validating that a requested finish exists on the chosen printing. Deferred to a
  later pass by explicit decision. See Decision D6 and the Risks section.
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

### Archidekt finish markers — verified end to end

Verified 2026-08-08 by exporting from and importing into the live site.

**Archidekt's own text export** produces this line for a foil commander:

```
1x Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel (fin) 382 *F* [Commander{top}]
```

So the canonical grammar is:

```
{qty}x {name} ({set}) {collector} *F* [{Category}{flags}] ^{Label},{#hex}^
```

The finish marker sits **after the collector number and before the category
annotation**. Matching this exactly makes our output round-trip-safe by construction,
since it is the site's own format. Note the card name here contains ` // ` — DFC
names must survive parsing.

**Import behavior**, confirmed by pasting a test block:

- `*F*` and `*E*` lines import, and the cards arrive with the correct Foil/Etched
  modifier applied.
- A marker placed before `[Category]` does not disturb category parsing — a line
  carrying both kept its category.
- Lines with no marker are unaffected.

**Archidekt does not validate the finish against the printing.** Importing
`1x Sol Ring (ltc) 284 *F*` — a nonfoil-only printing — succeeds and the card shows
as foil. It then has no price, and the foil status **cannot be toggled off** in the
UI; the card has to be deleted and re-added. This is the failure mode behind
Decision D6.

**The deck API exposes the source data.** Each entry in `/decks/{id}/` carries a
`modifier` field with values `Normal`, `Foil`, `Etched` (confirmed across three
public decks; one contained 6 `Foil` and 1 `Etched`). `archidekt_export`
(`server.py:1205-1253`) reads this payload and drops the field.

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
   accepting case-insensitive `(foil)` and `(etched)` for tolerance. `*F*`/`*E*` is
   Archidekt's own export syntax, verified above, and is also what Moxfield emits.
   Part 3 makes this repo emit it too, so this step is the exact inverse of
   `_finish_marker`.
5. Extract set and collector number with an **anchored** pattern —
   `\((?P<set>[a-zA-Z0-9]{2,6})\)(?:\s+(?P<cn>[^\s\[\]^*]+))?\s*$` — so it only
   matches a trailing set/collector suffix. Anchoring is what keeps real card names
   containing parentheses safe: `Erase (Not the Urza's Legacy One)` and
   `B.F.M. (Big Furry Monster)` both contain spaces inside their parentheses and
   cannot match a 2–6 char alphanumeric set token.
6. Apply the **existing** residual strips from `_parse_decklist` (`server.py:1432-1435`)
   to whatever name remains.

Step 6 exists specifically to preserve current behavior for existing callers.

**One intentional difference.** The legacy strip at `server.py:1435` is
`\s*\([a-z0-9]+\)$` — lowercase only — so `1x Sol Ring (LTC)` currently keeps the
set code glued to the name, and `validate_decklist` then reports `Sol Ring (LTC)` as
an unrecognized card. Step 5 is case-insensitive because Moxfield emits uppercase set
codes while Archidekt emits lowercase, and the new feature needs both. Uppercase set
codes therefore now strip correctly. This fixes a latent bug in `validate_decklist`
and `precon_diff` rather than regressing them, and the regression test asserts the
new behavior explicitly instead of pretending nothing changed.

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

**A failed batch does not fail the request.** Batching creates a failure mode the
single-request version could not have: with up to seven requests behind one call, a
transient 429 on the last one would discard six batches of good results. Batches are
therefore caught individually and the surviving results still render.

This forces a distinction that matters more than the resilience does. Entries from a
failed batch are **unchecked**, not **missing** — nothing was ever indexed for them,
so they would otherwise fall through the lookup and print under "Not found on
Scryfall," telling the user a real card does not exist because a request timed out.
They get their own output section, excluded from the total, naming the error. Only
when no batch at all succeeded does the tool return a bare error, since there are no
partial results worth rendering.

**Matching results to lines.** Responses are keyed into a lookup dict — by
`(set, collector_number)` and by `(name.lower(), set)` and by `name.lower()` — and
each line looks itself up. No positional assumptions.

Lookup degrades from the most specific key to the least, **except** when the line
named a collector number. A line that names one exact printing gets that printing or
nothing: degrading it to a looser key would return a different real printing of the
same card, whose price would then be reported as the user's under the
"priced at the printing you named" heading. That is precisely the failure Goal 3
forbids, and it is reachable whenever a list contains a second, resolvable line for
the same card — a typo alongside a correct line, say. Lines that named only a set, or
only a name, do degrade, since they never claimed a specific printing.

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

### Part 3 — stop discarding finish on the way out

Pricing by printing is worthless if the tools that *emit* decklists strip the finish
before the user can paste it back. Two tools do exactly that today.

**`format_archidekt`.** `DeckCardEntry` gains one optional field:

```python
finish: Optional[Literal["nonfoil", "foil", "etched"]] = None
```

When set to `foil` or `etched`, the emitted line carries `*F*` / `*E*` in the
verified position — after the collector number, before the category annotation.
`nonfoil` and `None` emit nothing.

**No feature flag is needed.** The original plan gated this behind
`include_finish: bool = False` to hedge against an unverified marker syntax. That
hedge is unnecessary now that the grammar is confirmed from Archidekt's own export.
A caller that does not set `finish` produces byte-identical output to today, so
`finish` being absent *is* the opt-in. This avoids a flag whose only job would be to
protect against a risk we have since eliminated.

**`archidekt_export`.** Maps the API's `modifier` field to a marker: `Foil` → `*F*`,
`Etched` → `*E*`, `Normal` and missing → nothing. Emitted in the same position, which
reproduces the site's own export byte for byte.

This one **defaults on**, with no flag (Decision D7). Archidekt is the source of the
data; handing back the marker it gave us is round-trip fidelity, not invention, and
the syntax is confirmed. Dropping it is silent data loss: a foil deck exported through
this server currently comes back all-nonfoil, and under Part 2 would then be priced
entirely at nonfoil prices — the precise bug this design exists to fix.

**Round-trip contract.** Part 2's parser and Part 3's formatters are now two halves of
one format and must agree on marker syntax and position. The shared grammar is the
verified line above. A round-trip test locks this down: format a set of entries, parse
the output back, assert the entries survive unchanged.

### Structure

New pure helpers, all independently testable without network access:

| Helper | Responsibility |
|---|---|
| `_parse_decklist_entries` | text → `list[DecklistEntry]` |
| `_entry_identifier` | `DecklistEntry` → Scryfall identifier dict |
| `_identifier_key` | identifier → stable comparable string, order-independent |
| `_dedupe_identifiers` | entries → unique identifiers, first-seen order |
| `_chunk` | list + size → batches, so the 75-cap loop is testable |
| `_price_for_finish` | `(card, finish)` → `Decimal \| None`, no cross-finish fallback |
| `_index_collection_results` | response → lookup dict |
| `_printing_label` | card + finish → `Counterspell (DMR #281, foil)` |
| `_available_finishes` | card → which finishes it has, and their prices |
| `_archidekt_line` | the single Archidekt line grammar, shared by both emitters |
| `_finish_marker` | `"foil"` → `"*F*"`, `"etched"` → `"*E*"`, else `""` |

`_finish_marker` is shared by `format_archidekt` and `archidekt_export` so the two
emitters cannot drift apart, and is the inverse of the parser's step 4.

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
- **D6 — No finish validation against the printing's `finishes` array.** Explicit
  decision by the repo owner: a caller has to ask for a foil to get one, so this is
  not a case the tool should police, and it can be revisited in a later pass. The
  hook is cheap to add later — `format_archidekt` already holds the Scryfall card
  from its batch lookup (`server.py:1317-1327`), so the check costs no extra
  requests and would follow the existing `# WARNING:` pattern (`server.py:1340`).
  Recorded as a known risk below rather than implemented now.
- **D7 — `archidekt_export` emits markers by default, with no flag.** Echoing back
  Archidekt's own `modifier` in Archidekt's own syntax is fidelity, not invention.
  This does change output for any deck containing foils; that change is the bug fix.

## Deferred

Not in scope, recorded so it is not rediscovered from scratch:

- Finish validation (D6), including the `include_set_codes` interaction in Risks.
- An "all foil" bulk mode on `format_archidekt`. Raised and deliberately postponed;
  it is the case that would most benefit from D6's validation, since one flag would
  stamp `*F*` across every card including printings that have no foil.
- Teaching `precon_export` and the EDHREC formatters about finishes. They emit
  suggested cards rather than owned ones, so finish is not meaningful there yet.

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
- the verified real-world line, as the canonical case:
  `1x Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel (fin) 382 *F* [Commander{top}]`
  → qty 1, name `Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel`,
  set `fin`, collector `382`, finish `foil`. This covers a DFC name containing ` // `
  on a line that must **not** be treated as a comment, a marker sitting between the
  collector number and the category, and a category carrying a `{top}` flag —
  simultaneously.

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

**Emitters** (`_finish_marker`, `format_archidekt`, `archidekt_export`):
- `foil` → `*F*`, `etched` → `*E*`, `nonfoil` and `None` → no marker
- marker position: after the collector number, before `[Category]`
- `format_archidekt` with no `finish` set on any entry produces output byte-identical
  to the current implementation, over a fixture covering categories, commander,
  maybeboard, and labels — this is the backward-compatibility guarantee
- `archidekt_export` maps `modifier` `Foil`/`Etched`/`Normal`/absent correctly, from a
  fixture deck payload shaped like the real API response

**Round trip:** entries → `format_archidekt` → `_parse_decklist_entries` → entries,
asserting quantity, name, set, collector number, and finish all survive. Includes the
Sephiroth line above, since a DFC name plus a marker plus a flagged category is the
case most likely to break.

## Documentation

- `README.md:13-14` tool descriptions updated to state that both pricing tools accept
  specific printings.
- `format_archidekt`'s docstring (`server.py:1295-1311`) gains the finish marker in
  its documented output format, since that docstring is what steers the model.
- `archidekt_export`'s docstring (`server.py:1186-1192`) likewise — its stated line
  format currently omits the marker it will now emit.

## Risks

- **Behavior change for existing `scryfall_price_list` callers.** Mitigated: no
  in-repo callers, no tests, and name-list input still parses.
- **Parser regression affecting `validate_decklist` / `precon_diff`.** Mitigated by
  step 6 reusing the existing strips verbatim, plus the dedicated regression test.
- **Removing the cross-finish fallback shows "no price" where a number used to
  appear.** Intended. The output explains which finishes exist and what they cost.
- **`archidekt_export` output changes for decks containing foils.** Accepted under
  D7 — the change is the fix, and it reproduces the site's own export.
- **Unvalidated finishes can produce an unrecoverable card (D6).** Archidekt accepts
  `*F*` on a nonfoil-only printing, shows the card with no price, and offers no way
  to turn foil off — the card must be deleted and re-added. The sharp edge is that
  `format_archidekt` chooses the printing itself from Scryfall's default lookup
  (`server.py:1345-1351`), so a caller asking for "a foil Sol Ring" with
  `include_set_codes=true` can be handed a printing *we* picked that has no foil.
  Reaching it needs `include_set_codes` on and an explicit `finish`, which is why it
  is deferred rather than blocking; an "all foil" mode would widen it considerably
  and should not ship before the validation does.
