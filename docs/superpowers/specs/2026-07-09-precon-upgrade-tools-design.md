# Precon Upgrade Tools — Design

- **Date:** 2026-07-09
- **Status:** Approved (design), pending implementation plan
- **Component:** `server.py` (Mystic Forge MCP server)
- **Branch:** `feat/precon-upgrade-tools`

## Problem

A user asked Claude (via the Mystic Forge connection) for "the percentage of cards
cut/added" for a preconstructed deck. Claude fabricated an EDHREC page and produced
false numbers across several turns.

Root cause: the server already exposes precon **contents** (`precon_search`,
`precon_decklist`, `precon_export`, all via MTGJSON), but nothing exposes precon
**upgrade statistics** — which cards the community cuts and adds, and at what rate.
With the box contents available but the upgrade data missing, the model filled the
gap by inventing it.

This design adds that missing data as first-class, citable tools, and makes the
numbers auditable so a human can review whether upgrade decisions were sound.

## Goals

1. Surface EDHREC's community cut/add data for a precon as a real tool.
2. Provide an exact, honest cut/added diff between a precon and a specific deck.
3. Put provenance on every number so a reviewer can trace and trust each claim.
4. Handle precons that build as multiple commanders without mis-attributing data.
5. Steer the model toward these tools (instead of hallucinating) for "cut/added"
   questions.

## Non-goals

- Cross-checking precon contents against a second source (e.g. taw's repo). MTGJSON
  is already integrated and authoritative for contents; the reviewer aids we need
  are provenance and alt-commander awareness, not a second contents source.
- Replacing or changing the existing MTGJSON precon tools.
- Deriving a synthetic cut percentage for EDHREC cut cards (see Decision D1).

## Key findings from API investigation

All verified against live endpoints on 2026-07-09. A non-existent slug returns
HTTP 403 from EDHREC's CDN (not 404); a real page returns 200.

### Precon index — `json.edhrec.com/pages/precon.json`

- `header`: `"Precon Upgrade Guide"`
- `container.json_dict.cardlists`: one cardlist per set (53 at time of writing).
  Each cardview is a precon with:
  - `name` — display name (e.g. `"Avengers Assemble"`, `"World Shaper"`)
  - `url` — `"/precon/<slug>"`
- This index is the slug source: fuzzy-match the user's query against `name`.

### Precon page — `json.edhrec.com/pages/precon/<slug>.json`

Top-level keys: `archidekt`, `deck`, `precon_image`, `precon_commander_counts`,
`header`, `panels`, `description`, `container`.

- `deck`: the physical box contents — `{ "commander": [names],
  "cards": { "Land": [[name, count], ...], "Creature": [...], ... } }`.
  This is the same box regardless of which face commander is chosen.
- `precon_commander_counts`: the face commanders this box can build, e.g.
  ```json
  [
    {"value": "Hearthhull, the Worldseed", "count": 12322,
     "href": "/precon/world-shaper/hearthhull-the-worldseed"},
    {"value": "Szarel, Genesis Shepherd", "count": 2764,
     "href": "/precon/world-shaper"}
  ]
  ```
  The commander whose `href` equals `/precon/<slug>` (no sub-path) is the one the
  **base page's** cut/add tables describe. Note this is *not* necessarily the most
  built commander — World Shaper's base page is Szarel (2764) even though Hearthhull
  (12322) is more popular. The tool must state which commander the tables belong to.
- `container.json_dict.cardlists` tags: `topcommanders`, `cardstoadd`,
  `landstoadd`, `cardstocut`, `landstocut`.

### The cut/add asymmetry (central to accuracy)

- **Adds** (`cardstoadd`, `landstoadd`) carry a real percentage. Cardview fields:
  `name`, `synergy`, `inclusion`, `num_decks`, `potential_decks`.
  Percentage = `inclusion / potential_decks`. Example: Icetill Explorer =
  `1143 / 2737` = **42%**.
- **Cuts** (`cardstocut`, `landstocut`) do **not** carry a percentage. Every cut
  cardview has `inclusion: 0, num_decks: 0, potential_decks: 0` and instead an
  `unpopularity` score in `[0, 1]`, descending (World Breaker `0.715`,
  Groundskeeper `0.673`, ...). This is a **ranking**, not a "% of decks that cut it."
  Rendering it with a `%` would fabricate precision — the exact failure we are
  fixing. See Decision D1.

### Alt-commander sub-pages — `json.edhrec.com/pages/precon/<slug>/<commander-slug>.json`

- Returns HTTP 200 with `header` like `"World Shaper Precon Upgrades for
  Hearthhull, the Worldseed"` and the same five cardlist tags, with
  **commander-specific** add/cut data.
- The `href` values in `precon_commander_counts` give these sub-paths directly.

### MTGJSON deck data (for the diff tool) — `mtgjson.com/api/v5/decks/<fileName>.json`

Already used by `precon_decklist`. Returns `data` with `name`, `code`, `type`,
`commander: [{count, name}]`, `mainBoard: [...]`, `sideBoard: [...]`.

## Tool 1 — `edhrec_precon_upgrade`

Placed in the EDHREC section of `server.py`, as a sibling to `edhrec_average_deck`.

### Input

```python
class PreconUpgradeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    precon: str          # precon name or slug, e.g. "World Shaper" / "world-shaper"
    commander: Optional[str] = None  # pick a face commander when the box has several
    limit: int = 20      # max cards per add/cut section, ge=1 le=50
```

### Behavior

1. **Slug resolution.** If `precon` already looks like a slug (matches
   `^[a-z0-9-]+$`), try `/pages/precon/<precon>.json` directly; if that 403s/404s,
   fall back to index resolution. For any input with spaces or capitals, go straight
   to index resolution: fetch the precon index (`/pages/precon.json`, cached like the
   MTGJSON deck list — 24h) and fuzzy-match `precon` against cardview `name`. On no
   match, return a helpful message listing the closest names. Cache the index in a
   module-level dict mirroring `_deck_list_cache`.
2. **Fetch base page** `/pages/precon/<slug>.json`.
3. **Commander selection.**
   - Read `precon_commander_counts`.
   - Default commander = the entry whose `href == "/precon/<slug>"` (this is the one
     the base page's tables describe — note `deck.commander` is unreliable here, as
     it names the same box commander on every sub-page). If no entry matches exactly,
     do not guess a single commander: render the base page's tables and list all
     commanders in the header block so attribution stays honest.
   - If `commander` is provided, fuzzy-match it against the `value` fields; if it
     maps to a sub-path `href`, fetch that sub-page instead and use its cardlists.
4. **Render** (Markdown), in this order:
   - Title from `header`; note the set/`code` if available.
   - **Commanders line:** if `precon_commander_counts` has >1 entry, list each
     `value (count decks)` and mark which one the tables below describe. If the user
     could switch, hint at the `commander` param.
   - **Cards to Add** and **Lands to Add**: `- **Name** — 42% (1143 of 2737 decks)`
     using `_pct(inclusion, potential_decks)`, plus `synergy` when present.
   - **Cards to Cut** and **Lands to Cut**: ranked list, `- **Name** — cut-rank
     score 0.72` labeled explicitly as EDHREC's unpopularity ranking, **no `%`**.
     A one-line note points to `precon_diff` for an exact cut percentage.
   - **Provenance footer:** source URL (`https://edhrec.com/precon/<slug>[/<cmd>]`)
     and "Based on N decks" where N is the selected commander's `count`.

### Errors

Reuse `_edhrec_error`. A 403 on a directly-supplied slug means "no such precon —
try a name and let the tool resolve it," surfaced as a friendly message.

## Tool 2 — `precon_diff`

Placed in the precon (MTGJSON) section of `server.py`, as a sibling to
`precon_decklist`.

### Input

```python
class PreconDiffInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    file_name: str                      # MTGJSON precon fileName (from precon_search)
    deck: Optional[str] = None          # Archidekt deck ID or URL (the upgraded deck)
    decklist: Optional[str] = None      # OR a pasted decklist ("1 Card Name" per line)
    canonicalize: bool = True           # normalize names via Scryfall before diffing
    # exactly one of deck / decklist must be provided
```

### Behavior

1. **Precon side:** fetch MTGJSON `/decks/<file_name>.json`; the baseline is
   `commander + mainBoard` as `{name: count}`.
2. **Upgraded side:**
   - If `deck`: fetch via `_archidekt_get`, extract in-deck cards using the same
     logic as `archidekt_deck` (respect `includedInDeck`; commander counts as
     in-deck; skip maybeboard).
   - If `decklist`: parse via `_parse_decklist`.
   - Validation error if neither or both are supplied.
3. **Canonicalize (default on):** batch both name sets through
   `_scryfall_post("/cards/collection")` and map to Scryfall canonical names, so
   "Fire // Ice" vs "Fire" style variance doesn't create phantom diffs. On lookup
   failure, fall back to raw case-insensitive name matching and note it.
4. **Diff by name:**
   - **Added** = in upgraded, not in precon.
   - **Cut** = in precon, not in upgraded.
   - **Kept** = intersection.
   - **Basic lands** (Plains/Island/Swamp/Mountain/Forest/Wastes) are diffed
     separately and reported in their own "Basic land changes" section (count
     deltas), so routine mana-base churn doesn't inflate the meaningful lists.
5. **Render:**
   - Header naming both decks.
   - `Added (N)`, `Cut (N)`, `Kept (N)` sections.
   - Summary: `precon size`, `% cut`, `% added`, `% changed`
     (`cut_count / precon_size`), all with raw counts shown.
   - **Provenance footer:** MTGJSON `fileName` and the Archidekt deck id/URL (or
     "pasted decklist").

### Errors

Reuse `_mtgjson_error` / `_archidekt_error` / `_scryfall_error` for the respective
fetch. Clear validation message when the deck/decklist inputs are wrong.

## Shared helpers and reuse

- Reuse: `_edhrec_get`, `_mtgjson_get`, `_archidekt_get`, `_parse_deck_id`,
  `_parse_decklist`, `_scryfall_post`, `_sanitize`, `_pct`, and the existing error
  formatters.
- New module-level cache `_precon_index_cache` (mirrors `_deck_list_cache`, 24h TTL)
  for `/pages/precon.json`.
- Factor the Archidekt "extract in-deck cards" loop out of `archidekt_deck` into a
  small helper (e.g. `_archidekt_in_deck_cards(data) -> dict[str, int]`) so both
  `archidekt_deck` and `precon_diff` share it. This is a targeted refactor of code
  we're already touching, not a broad rewrite.

## Server instructions update

Add to the `FastMCP(..., instructions=...)` string a line steering the model to
these tools, e.g.:

> "Use edhrec_precon_upgrade for how a precon is commonly upgraded (which cards the
> community cuts/adds and how often), and precon_diff to compute the exact cut/added
> cards between a precon and a specific deck. Never estimate cut/added percentages
> from memory."

## Decisions

- **D1 — EDHREC cuts are reported as a ranking, not a percentage.** EDHREC does not
  expose a clean cut rate; it exposes an `unpopularity` score. The tool renders cuts
  as a ranked list with that score, explicitly labeled, and points to `precon_diff`
  for a real cut percentage against an actual deck. Rationale: honesty and
  auditability over invented precision — the whole reason this feature exists.
- **D2 — Two identifier systems coexist.** EDHREC uses precon *name* slugs
  (`world-shaper`); MTGJSON uses *fileNames* (`WorldShaper_...`). `edhrec_precon_upgrade`
  resolves EDHREC slugs from the EDHREC index; `precon_diff` takes an MTGJSON
  `file_name` from `precon_search`. We do not attempt to unify them in v1.
- **D3 — Commander-name lookup is best-effort.** Slug resolution matches on precon
  name. If a user supplies only a commander name, they may need `precon_search`
  first. A future enhancement could map commander → precon; out of scope for v1.

## Testing strategy

- **`edhrec_precon_upgrade`:** live-endpoint tests against `world-shaper` (multi-
  commander) — assert real add percentages appear, cuts appear without a `%`, the
  commander line lists both faces, and the `commander="Hearthhull, the Worldseed"`
  path fetches the sub-page. One negative test for an unresolvable name.
- **`precon_diff`:** a fixture pairing a known MTGJSON precon `fileName` with a
  pasted decklist (deterministic, no Archidekt dependency) — assert Added/Cut/Kept
  membership, the percentage math, and that basic lands land in their own section.
  One test with `canonicalize=False` to check the fallback path.
- Follow existing test conventions in the repo if present; otherwise add a minimal
  `pytest` module for these two tools.

## Out of scope

- taw / second-source contents verification.
- Unifying EDHREC-slug and MTGJSON-fileName identifiers.
- Commander-name → precon resolution.
- Any change to the existing MTGJSON precon tools beyond the shared-helper refactor.
