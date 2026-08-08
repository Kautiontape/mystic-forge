# Comprehensive Rules Lookup — Design

Date: 2026-08-08
Status: Approved

## Problem

Mystic Forge is strong on "what does this card do and what did WotC say about it"
(oracle text, rulings, combos) and has nothing on "what does the rulebook say."
Interaction questions that hinge on the stack, layers, replacement-effect
ordering, or state-based actions fall back to the client model's memory or web
search — exactly the situation the `instructions=` block was written to prevent
for card text. Nothing can look up or search CR rule numbers (702.2b deathtouch,
601.2 casting, layers in 613), and there is no keyword/glossary lookup short of
going through a card that has the keyword.

## Goals

- `rules_get`: resolve a rule number ("702.2b") or a keyword/glossary term
  ("deathtouch") to exact CR text.
- `rules_search`: ranked full-text search across all rules and glossary entries.
- Always current with zero maintenance, but resilient: serves from a local copy
  on cold start and keeps working when WotC is unreachable.
- Citable: every response carries the CR effective date.
- The server instructions steer rules questions to these tools, the same way
  card-text questions are steered to `scryfall_card_text`.

## Non-goals

- MTR / IPG / judge program documents (lower value unless running tournament
  REL; a later branch).
- B&R announcement history.
- Rules *reasoning*. The tools return authoritative text; the client model does
  the interpretation.

## Verified source facts

Checked 2026-08-08 against the live site and `MagicCompRules 20260807.txt`:

- `https://magic.wizards.com/en/rules` links exactly one `.txt` on
  `media.wizards.com`; the filename is dated per release
  (`MagicCompRules 20260807.txt`) and **contains a literal space that must be
  percent-encoded** or the download fails.
- The file is ~977KB UTF-8 (not ~200KB as earlier notes claimed). Line 3 reads
  "These rules are effective as of August 7, 2026."
- Layout: intro → table of contents → body sections `1. Game Concepts` through
  `9. Casual Variants` → `Glossary` → `Credits`. The `Glossary` and `Credits`
  markers each appear twice — once in the TOC, once opening the body section —
  so the parser anchors on the *second* occurrence of each.
- ~3,160 numbered rule lines. Formats: section `1. Game Concepts`, subsection
  `100. General`, rule `702.2. Deathtouch` (trailing period), subrule
  `702.2a Deathtouch is a static ability.` (no trailing period). Subrule
  letters skip `l` and `o`.
- Glossary entries are a term on its own line followed by a definition that
  cites its rules ("See rule 702.2, “Deathtouch.”"), so the keyword→rule
  mapping is derived from the definitions, not hardcoded.

## Design

### 1. `rulebook.py`: parser and index

A new module following the `watchlist_*.py` precedent: pure, synchronous, no
network, unit-testable against a fixture file.

- `parse(text) -> RulesIndex`
  - Splits the body between the second `Glossary` marker and the second
    `Credits` marker; rules body runs from the first body section heading to
    the glossary.
  - `rules`: ordered map of number → entry. Rule headings (`702.2. Deathtouch`)
    carry a title; subrules (`702.2a …`) attach to their parent; subsections
    (`100. General`) and sections (`1. Game Concepts`) keep child lists.
  - `glossary`: case-insensitive term → definition (original casing preserved
    for display), with rule references extracted from each definition via a
    `rule NNN(.N)(x)` regex.
  - `effective_date`: parsed from the "effective as of" line.
- `RulesIndex.search(query, limit)`: tokenized scoring over rules plus glossary
  entries — term frequency, an all-terms-present boost, and an adjacency bonus
  for phrase-like matches. Deterministic tie-break by rule number. No stemming,
  no external dependencies; the corpus is ~3K short documents and reparses in
  well under a second, so there is no persistence layer.

### 2. Data acquisition: vendored snapshot + background refresh

- A snapshot of the CR txt is committed at the repo root as
  `MagicCompRules.txt` (flat-file precedent: `watchlist_words.txt`).
- Disk cache path comes from env `MYSTIC_FORGE_CR` (default `cr_cache.txt` in
  the working directory, mirroring `MYSTIC_FORGE_DB`).
- First rules call loads the cache if present and parseable, else the vendored
  snapshot, and serves immediately. Startup never blocks on the network.
- A background refresh runs at most once per 24h, triggered lazily by rules
  tool calls: fetch the rules page, regex out the `media.wizards.com … .txt`
  URL, percent-encode spaces, and compare the filename date against the loaded
  version. Only if newer: download, parse, atomically swap the in-memory
  index, and best-effort write the cache file.
- Failure handling: any network error keeps serving the current index and logs
  a warning. A downloaded file that parses to implausibly few rules (<1,000)
  or no glossary is rejected as malformed rather than swapped in.

### 3. Tools in `server.py`

Thin wrappers in the existing `_scryfall_get` / `_scryfall_error` style.

- `rules_get(ref)`
  - `"702.2b"` → that subrule plus its parent heading line for context.
  - `"702.2"` → the rule heading plus all its subrules, full text.
  - `"702"` → the subsection heading plus a child list of rule numbers and
    titles only (the full text of 702 is a large fraction of the document).
  - `"1"`–`"9"` → the section heading plus its subsection list.
  - A trailing period on any ref is tolerated.
  - Non-numeric ref → case-insensitive glossary lookup; the response is the
    definition plus the full text (with subrules) of each rule the definition
    cites.
  - Responses that would exceed ~10KB fall back to child titles instead of
    full text, with a note to call `rules_get` on a specific number.
  - Unknown ref → error string with nearest-match rule numbers or terms via
    `difflib` (already imported in `server.py`).
- `rules_search(query, limit=10, max 25)`
  - Ranked hits `{ref, text, kind: rule | glossary}` plus a total-match count.
- Both tools include `effective_date` in every successful response so answers
  can be cited against a dated CR version. Error paths return strings,
  matching the `_scryfall_error` convention.

### 4. Steering

- The `instructions=` block gains: use `rules_get`/`rules_search` for
  Comprehensive Rules questions — rule numbers, keywords, and interaction
  questions about the stack, layers, replacement effects, or state-based
  actions — instead of memory or web search, and cite rule numbers (precedent:
  commit `aa04b32` did the same for card text).
- Both tool descriptions carry the same steer.

## Error handling

- Network failures never surface to the client while a parsed index exists;
  the only user-visible failure is a cold start where both the cache and the
  vendored file are unreadable, which returns an error string.
- Unknown refs and empty search results return helpful strings (suggestions /
  "no matches"), not exceptions.

## Testing

- Fixture: a truncated slice of the real CR (TOC, a couple of body sections
  including 702 excerpts, glossary, credits) committed under `tests/`.
- Parser: rule and subrule attachment, section/subsection nesting, the `l`/`o`
  subrule-letter skip, smart quotes and em dashes, glossary extraction with
  rule references, effective date.
- Lookup: every ref form above, trailing-period tolerance, case-insensitive
  glossary, size-cap fallback, fuzzy-miss suggestions.
- Search: ranking basics, limit clamp, glossary hits flagged as such.
- Refresh (mocked httpx): URL discovery including the `%20` encoding,
  newer-date swap, same-date no-op, network failure keeps the index, malformed
  download rejected.
- No test touches the network, per `conftest.py` convention.

## Docs

- README: add `rules_get` and `rules_search` to the tool list.

## Follow-up (future branches)

- MTR / IPG parsing for tournament-REL questions.
- B&R announcement history (when and why a card's legality changed).
