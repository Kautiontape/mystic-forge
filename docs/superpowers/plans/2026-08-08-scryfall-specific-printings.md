# Scryfall Specific-Printing Pricing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Price Magic cards at the exact printing and finish the user names, instead of at whatever printing Scryfall happens to pick.

**Architecture:** A decklist line like `1x Sol Ring (ltc) 284 *F*` is parsed into a `DecklistEntry`, turned into a Scryfall `/cards/collection` identifier that names that printing, and priced from the single price column matching the requested finish — with no fallback to a different finish. The same finish-marker grammar is emitted by the two tools that produce decklists, so a list can round-trip through this server without losing printing information. All parsing, identifier building, price selection, and formatting live in pure helpers so they test without network access.

**Tech Stack:** Python 3, `pydantic` v2 models, `httpx`, `mcp.server.fastmcp`, `pytest` with `asyncio_mode = auto`.

**Spec:** `docs/superpowers/specs/2026-08-08-scryfall-specific-printings-design.md`

---

## Background for the implementer

You are working in `server.py`, a single ~2280-line MCP server. It exposes tools to an LLM via `@mcp.tool(...)` decorators. Each tool takes one pydantic model and returns a **markdown string** — there is no JSON response shape to honor, so output formatting is free-form, but it is what the model reads, so it must be unambiguous.

Four things about this codebase that will not be obvious:

1. **Tests never touch the network.** Look at `tests/test_precon_diff.py`: they call helper functions directly with fixture dicts. Every helper in this plan is written to be pure for that reason. Do not add HTTP mocking.
2. **`pytest.ini` sets `asyncio_mode = auto`**, so `async def` tests work without a decorator. You will not need one — every test here is synchronous.
3. **`conftest.py` puts the repo root on `sys.path`**, so tests do `import server` and call `server._helper(...)`. Underscore-prefixed helpers are the normal thing to test here.
4. **Commit style is `topic: message`** (e.g. `scryfall: Add finish filter`). Do not add `Co-Authored-By` lines.

Run the full suite with `pytest` from the repo root. It should pass before you start.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `server.py` | The entire MCP server | Modified throughout — see per-task line refs |
| `tests/test_printing_prices.py` | Parser, identifier, price-selection, and result-matching helpers | **Create** |
| `tests/test_finish_markers.py` | Marker emission, `format_archidekt` / `archidekt_export` output, round trip | **Create** |
| `README.md` | Tool table | Modified (Task 11) |

Two test files rather than one: Tasks 2–7 are about *reading* printings, Tasks 8–10 about *writing* them. They fail for different reasons and are worth being able to run separately.

Within `server.py`, new helpers go directly above the tool that first uses them, matching how `_parse_decklist` sits above `validate_decklist` and `_format_card` sits above the Scryfall tools.

---

## Task 1: Imports and the shared finish vocabulary

Everything downstream needs `Decimal`, the dataclass, and the finish↔marker mapping. This task adds only that, so a failure here is unambiguous.

**Files:**
- Modify: `server.py:12-21` (imports), and add a new section after the constants block at `server.py:36`
- Test: `tests/test_finish_markers.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_finish_markers.py`:

```python
import server


def test_finish_marker_maps_finishes_to_archidekt_syntax():
    assert server._finish_marker("foil") == "*F*"
    assert server._finish_marker("etched") == "*E*"


def test_finish_marker_is_empty_for_nonfoil_and_none():
    assert server._finish_marker("nonfoil") == ""
    assert server._finish_marker(None) == ""
    assert server._finish_marker("") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_finish_markers.py -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_finish_marker'`

- [ ] **Step 3: Add the imports**

In `server.py`, replace lines 12-17:

```python
import re
import time
from typing import Optional, Dict, Any
from enum import Enum
from collections import Counter
from difflib import SequenceMatcher
```

with:

```python
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any
from enum import Enum
from collections import Counter
from difflib import SequenceMatcher
```

Then on line 20, add `model_validator` to the pydantic import:

```python
from pydantic import BaseModel, Field, ConfigDict, model_validator
```

- [ ] **Step 4: Add the finish vocabulary**

In `server.py`, immediately after the `MATCH_MARGIN` line (currently `server.py:36`) and before the `# ── Server ──` comment, add:

```python
# ── Card finishes ────────────────────────────────────────────────────────────
# Archidekt's own text export writes the finish as a marker after the collector
# number and before the category annotation, e.g.
#   1x Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel (fin) 382 *F* [Commander{top}]
# Verified against the live site 2026-08-08. Moxfield uses the same markers.

FINISH_MARKERS = {"foil": "*F*", "etched": "*E*"}
MARKER_FINISHES = {"F": "foil", "E": "etched"}

# Archidekt's deck API reports the finish as a per-card `modifier` field.
MODIFIER_FINISHES = {"Normal": "nonfoil", "Foil": "foil", "Etched": "etched"}

# Which Scryfall price column corresponds to each finish.
FINISH_PRICE_KEYS = {"nonfoil": "usd", "foil": "usd_foil", "etched": "usd_etched"}


def _finish_marker(finish: Optional[str]) -> str:
    """Archidekt finish marker for a finish name; '' when there is nothing to mark.

    Inverse of the marker step in _parse_decklist_entries. Shared by
    format_archidekt and archidekt_export so the two emitters cannot drift.
    """
    return FINISH_MARKERS.get(finish or "", "")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_finish_markers.py -v`
Expected: PASS, 2 tests

- [ ] **Step 6: Run the full suite**

Run: `pytest`
Expected: PASS — the existing precon tests must be unaffected.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_finish_markers.py
git commit -m "scryfall: Add shared finish vocabulary and marker helper"
```

---

## Task 2: `DecklistEntry` and the printing-aware parser

This is the load-bearing task. `_parse_decklist` (`server.py:1407`) currently returns `(qty, name)` tuples and throws printing data away; two existing tools depend on its exact behavior. We add a richer parser underneath it and make the old function a wrapper.

**Read the spec's "Part 2 — Parsing" section before starting** — the extraction order matters and step 6 exists specifically to protect the existing callers.

**Files:**
- Modify: `server.py:1407-1440` (replace `_parse_decklist`)
- Test: `tests/test_printing_prices.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_printing_prices.py`:

```python
import server


# ── _parse_decklist_entries ───────────────────────────────────────────────────

def test_parses_bare_name():
    (entry,) = server._parse_decklist_entries("Sol Ring")
    assert (entry.quantity, entry.name) == (1, "Sol Ring")
    assert entry.set_code is None
    assert entry.collector_number is None
    assert entry.finish is None


def test_parses_quantity_with_and_without_x():
    a, b = server._parse_decklist_entries("2 Sol Ring\n3x Arcane Signet")
    assert (a.quantity, a.name) == (2, "Sol Ring")
    assert (b.quantity, b.name) == (3, "Arcane Signet")


def test_parses_set_and_collector_number():
    (entry,) = server._parse_decklist_entries("1x Sol Ring (ltc) 284")
    assert entry.name == "Sol Ring"
    assert entry.set_code == "ltc"
    assert entry.collector_number == "284"


def test_set_code_is_lowercased_regardless_of_input_case():
    (entry,) = server._parse_decklist_entries("1x Sol Ring (LTC) 284")
    assert entry.name == "Sol Ring"
    assert entry.set_code == "ltc"


def test_parses_foil_and_etched_markers():
    a, b = server._parse_decklist_entries(
        "1x Counterspell (dmr) 281 *F*\n1x Arcane Signet (sld) 589 *E*"
    )
    assert (a.name, a.finish) == ("Counterspell", "foil")
    assert (b.name, b.finish) == ("Arcane Signet", "etched")


def test_parses_word_finish_forms():
    a, b = server._parse_decklist_entries("1x Sol Ring (foil)\n1x Sol Ring (Etched)")
    assert a.finish == "foil"
    assert b.finish == "etched"
    assert a.name == b.name == "Sol Ring"


def test_collector_number_may_be_non_numeric():
    (entry,) = server._parse_decklist_entries("1x Sol Ring (sld) IFIYW-10")
    assert entry.set_code == "sld"
    assert entry.collector_number == "IFIYW-10"


def test_parses_the_verified_archidekt_export_line():
    # Captured verbatim from Archidekt's own text export, 2026-08-08.
    # Exercises a DFC name containing ' // ', a marker between the collector
    # number and the category, and a category carrying a {top} flag — at once.
    line = ("1x Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel "
            "(fin) 382 *F* [Commander{top}]")
    (entry,) = server._parse_decklist_entries(line)
    assert entry.quantity == 1
    assert entry.name == "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel"
    assert entry.set_code == "fin"
    assert entry.collector_number == "382"
    assert entry.finish == "foil"


def test_parses_full_archidekt_line_with_label():
    line = "1x Cultivate (m21) 177 [Ramp] ^Test,#2ccce4^"
    (entry,) = server._parse_decklist_entries(line)
    assert entry.name == "Cultivate"
    assert entry.set_code == "m21"
    assert entry.collector_number == "177"
    assert entry.finish is None


def test_card_names_containing_parentheses_are_not_mistaken_for_set_codes():
    text = "1x Erase (Not the Urza's Legacy One)\n1x B.F.M. (Big Furry Monster)"
    a, b = server._parse_decklist_entries(text)
    assert a.name == "Erase (Not the Urza's Legacy One)"
    assert a.set_code is None
    assert b.name == "B.F.M. (Big Furry Monster)"
    assert b.set_code is None


def test_comment_and_blank_lines_are_skipped():
    text = "# a comment\n\n// another\n1x Sol Ring\n"
    entries = server._parse_decklist_entries(text)
    assert len(entries) == 1
    assert entries[0].name == "Sol Ring"


# ── _parse_decklist backward compatibility ────────────────────────────────────

def test_parse_decklist_still_returns_qty_name_tuples():
    text = (
        "1 Sol Ring\n"
        "2x Arcane Signet\n"
        "1x Cultivate (m21) 177 [Ramp] ^Test,#2ccce4^\n"
        "1x Counterspell (dmr) 281 *F*\n"
        "# comment\n"
        "Rhystic Study\n"
    )
    assert server._parse_decklist(text) == [
        (1, "Sol Ring"),
        (2, "Arcane Signet"),
        (1, "Cultivate"),
        (1, "Counterspell"),
        (1, "Rhystic Study"),
    ]


def test_parse_decklist_now_strips_uppercase_set_codes():
    # Deliberate behavior change. The legacy strip was lowercase-only
    # (server.py:1435), so this previously yielded "Sol Ring (LTC)" and made
    # validate_decklist report a real card as unrecognized. Moxfield emits
    # uppercase set codes, so the new parser is case-insensitive.
    assert server._parse_decklist("1x Sol Ring (LTC)") == [(1, "Sol Ring")]


def test_parse_decklist_strips_trailing_number_without_a_set_code():
    # Legacy behavior preserved: the bare trailing-digits strip still applies.
    assert server._parse_decklist("1x Sol Ring 284") == [(1, "Sol Ring")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_printing_prices.py -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_parse_decklist_entries'`

- [ ] **Step 3: Write the implementation**

In `server.py`, replace the whole of `_parse_decklist` (currently lines 1407-1440, from `def _parse_decklist(` through `return cards`) with:

```python
@dataclass(frozen=True)
class DecklistEntry:
    """One parsed decklist line, including the printing it names."""
    quantity: int
    name: str
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    finish: Optional[str] = None


_QTY_RE = re.compile(r"^(\d+)x?\s+(.+)$")
_LABEL_RE = re.compile(r"\s*\^[^^]*\^")
_CATEGORY_RE = re.compile(r"\s*\[[^\]]*\]")
_MARKER_RE = re.compile(r"\s*\*([FE])\*", re.IGNORECASE)
_WORD_FINISH_RE = re.compile(r"\s*[(\[](foil|etched)[)\]]", re.IGNORECASE)
# Anchored to the end so it can only match a trailing printing suffix, never a
# card name that happens to contain parentheses (e.g. "B.F.M. (Big Furry
# Monster)" — the inner text has spaces and cannot match a set token).
_SET_CN_RE = re.compile(r"\s*\((?P<set>[a-zA-Z0-9]{2,6})\)(?:\s+(?P<cn>[^\s\[\]^*]+))?\s*$")


def _parse_decklist_entries(text: str) -> list[DecklistEntry]:
    """Parse a decklist into entries that keep set, collector number, and finish.

    Handles the Archidekt/Moxfield grammar:
      [qty][x] Name [(set)] [collector] [*F*|*E*] [[Category{flags}]] [^Label,#hex^]

    Bare names and quantity-only lines still parse; the printing fields are just
    None. Lines starting with '#' or '//' are comments.
    """
    entries: list[DecklistEntry] = []
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue

        match = _QTY_RE.match(line)
        if match:
            qty = int(match.group(1))
            rest = match.group(2).strip()
        else:
            qty = 1
            rest = line

        # Labels and categories carry no pricing information.
        rest = _LABEL_RE.sub("", rest)
        rest = _CATEGORY_RE.sub("", rest)

        # Finish marker, before the set suffix so that "(foil)" is consumed here
        # rather than being mistaken for a 4-character set code below.
        finish: Optional[str] = None
        marker = _MARKER_RE.search(rest)
        if marker:
            finish = MARKER_FINISHES[marker.group(1).upper()]
            rest = _MARKER_RE.sub("", rest, count=1)
        else:
            worded = _WORD_FINISH_RE.search(rest)
            if worded:
                finish = worded.group(1).lower()
                rest = _WORD_FINISH_RE.sub("", rest, count=1)

        # Set and collector number, anchored at the end of what remains.
        set_code: Optional[str] = None
        collector: Optional[str] = None
        suffix = _SET_CN_RE.search(rest)
        if suffix:
            set_code = suffix.group("set").lower()
            collector = suffix.group("cn")
            rest = rest[:suffix.start()]

        # Legacy residual strips, kept verbatim so validate_decklist and
        # precon_diff see the names they have always seen.
        name = rest
        name = re.sub(r"\s*\^[^^]*\^", "", name)
        name = re.sub(r"\s*\[[^\]]*\]", "", name)
        name = re.sub(r"\s+\d+$", "", name)
        name = re.sub(r"\s*\([a-z0-9]+\)$", "", name)
        name = name.strip()

        if name:
            entries.append(DecklistEntry(qty, name, set_code, collector, finish))
    return entries


def _parse_decklist(text: str) -> list[tuple[int, str]]:
    """Parse a decklist into (quantity, card_name) tuples.

    Thin wrapper over _parse_decklist_entries, kept for validate_decklist and
    precon_diff, which do not care about printings.
    """
    return [(e.quantity, e.name) for e in _parse_decklist_entries(text)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_printing_prices.py -v`
Expected: PASS, 14 tests

- [ ] **Step 5: Run the full suite — this is the regression gate**

Run: `pytest`
Expected: PASS. `tests/test_precon_diff.py` and `tests/test_precon_upgrade.py` exercise `_parse_decklist` through `precon_diff`. If either fails, the parser changed behavior it should not have — fix the parser, do not edit those tests.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_printing_prices.py
git commit -m "decklist: Parse set, collector number, and finish from lines"
```

---

## Task 3: Identifier building

Turns an entry into the `/cards/collection` identifier that names its printing.

**Files:**
- Modify: `server.py` — add directly below `_parse_decklist`
- Test: `tests/test_printing_prices.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_printing_prices.py`:

```python
# ── _entry_identifier ─────────────────────────────────────────────────────────

def test_identifier_uses_set_and_collector_number_when_both_present():
    entry = server.DecklistEntry(1, "Sol Ring", "ltc", "284", None)
    assert server._entry_identifier(entry) == {"set": "ltc", "collector_number": "284"}


def test_identifier_uses_name_and_set_when_only_set_present():
    entry = server.DecklistEntry(1, "Arcane Signet", "otc", None, None)
    assert server._entry_identifier(entry) == {"name": "Arcane Signet", "set": "otc"}


def test_identifier_falls_back_to_name_alone():
    entry = server.DecklistEntry(1, "Rhystic Study", None, None, None)
    assert server._entry_identifier(entry) == {"name": "Rhystic Study"}


def test_identifier_ignores_finish():
    # Scryfall identifiers have no finish dimension — one card object carries
    # every finish's price, so finish only affects which column we read later.
    entry = server.DecklistEntry(1, "Counterspell", "dmr", "281", "foil")
    assert server._entry_identifier(entry) == {"set": "dmr", "collector_number": "281"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_printing_prices.py -k identifier -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_entry_identifier'`

- [ ] **Step 3: Write the implementation**

In `server.py`, add immediately after the `_parse_decklist` wrapper:

```python
def _entry_identifier(entry: DecklistEntry) -> dict:
    """Scryfall /cards/collection identifier naming this entry's printing.

    Set plus collector number pins one printing exactly. Set alone lets Scryfall
    choose within that set. Neither means Scryfall picks the default printing,
    which the caller is responsible for flagging in its output.
    """
    if entry.set_code and entry.collector_number:
        return {"set": entry.set_code, "collector_number": entry.collector_number}
    if entry.set_code:
        return {"name": entry.name, "set": entry.set_code}
    return {"name": entry.name}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_printing_prices.py -k identifier -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_printing_prices.py
git commit -m "scryfall: Build collection identifiers from decklist entries"
```

---

## Task 4: Finish-aware price selection

The correctness fix. The current code does `usd or usd_foil or usd_etched` (`server.py:400`), which quotes a foil price for a nonfoil request. This helper refuses to do that.

**Files:**
- Modify: `server.py` — add below `_entry_identifier`
- Test: `tests/test_printing_prices.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_printing_prices.py`:

```python
# ── _price_for_finish ─────────────────────────────────────────────────────────

from decimal import Decimal

FOIL_ONLY = {
    "name": "Sol Ring",
    "set": "sld",
    "collector_number": "2417",
    "finishes": ["foil"],
    "prices": {"usd": None, "usd_foil": "48.21", "usd_etched": None},
}

ALL_FINISHES = {
    "name": "Arcane Signet",
    "set": "sld",
    "collector_number": "589",
    "finishes": ["nonfoil", "foil", "etched"],
    "prices": {"usd": "29.04", "usd_foil": None, "usd_etched": "26.31"},
}


def test_price_for_finish_reads_the_matching_column():
    assert server._price_for_finish(ALL_FINISHES, "nonfoil") == Decimal("29.04")
    assert server._price_for_finish(ALL_FINISHES, "etched") == Decimal("26.31")
    assert server._price_for_finish(FOIL_ONLY, "foil") == Decimal("48.21")


def test_price_for_finish_defaults_to_nonfoil():
    assert server._price_for_finish(ALL_FINISHES, None) == Decimal("29.04")


def test_price_for_finish_never_falls_back_to_another_finish():
    # The whole point: a foil-only printing has no nonfoil price, and the old
    # `usd or usd_foil or usd_etched` chain would have quoted $48.21 here.
    assert server._price_for_finish(FOIL_ONLY, "nonfoil") is None
    # And a missing foil price does not silently become the nonfoil price.
    assert server._price_for_finish(ALL_FINISHES, "foil") is None


def test_price_for_finish_handles_missing_and_malformed_data():
    assert server._price_for_finish({}, "nonfoil") is None
    assert server._price_for_finish({"prices": None}, "nonfoil") is None
    assert server._price_for_finish({"prices": {"usd": ""}}, "nonfoil") is None
    assert server._price_for_finish({"prices": {"usd": "n/a"}}, "nonfoil") is None


def test_price_for_finish_returns_decimal_not_float():
    # Totals sum over ~100 lines; float would accumulate representation error.
    assert isinstance(server._price_for_finish(ALL_FINISHES, "nonfoil"), Decimal)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_printing_prices.py -k price_for_finish -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_price_for_finish'`

- [ ] **Step 3: Write the implementation**

In `server.py`, add immediately after `_entry_identifier`:

```python
def _price_for_finish(card: dict, finish: Optional[str]) -> Optional[Decimal]:
    """USD price of this card in exactly the requested finish, or None.

    Deliberately does NOT fall back to another finish. A missing price is
    reported as missing; quoting a foil price for a nonfoil request produces a
    confidently wrong number, which is worse than no number.
    """
    key = FINISH_PRICE_KEYS.get(finish or "nonfoil", "usd")
    raw = (card.get("prices") or {}).get(key)
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_printing_prices.py -k price_for_finish -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_printing_prices.py
git commit -m "scryfall: Select price by finish without cross-finish fallback"
```

---

## Task 5: Matching results back to lines

Scryfall does not guarantee response order and omits not-found cards from `data`, so positional matching is unsafe. This builds a lookup keyed several ways and resolves each entry against it.

Note the DFC wrinkle: a line may say `Sephiroth, Fabled SOLDIER` while Scryfall returns `Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel`. Face names are indexed too.

**Files:**
- Modify: `server.py` — add below `_price_for_finish`
- Test: `tests/test_printing_prices.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_printing_prices.py`:

```python
# ── _index_collection_results / _lookup_entry ─────────────────────────────────

SOL_RING_LTC = {
    "name": "Sol Ring", "set": "ltc", "collector_number": "284",
    "finishes": ["nonfoil"], "prices": {"usd": "2.51"},
}
COUNTERSPELL_DMR = {
    "name": "Counterspell", "set": "dmr", "collector_number": "281",
    "finishes": ["nonfoil", "foil"], "prices": {"usd": "2.15", "usd_foil": "2.17"},
}
SEPHIROTH_FIN = {
    "name": "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel",
    "set": "fin", "collector_number": "382",
    "finishes": ["nonfoil", "foil"], "prices": {"usd": "12.00", "usd_foil": "20.00"},
}


def test_lookup_matches_by_set_and_collector_number():
    index = server._index_collection_results([SOL_RING_LTC, COUNTERSPELL_DMR])
    entry = server.DecklistEntry(1, "Sol Ring", "ltc", "284", None)
    assert server._lookup_entry(index, entry) is SOL_RING_LTC


def test_lookup_is_insensitive_to_response_order():
    reversed_index = server._index_collection_results([COUNTERSPELL_DMR, SOL_RING_LTC])
    entry = server.DecklistEntry(1, "Sol Ring", "ltc", "284", None)
    assert server._lookup_entry(reversed_index, entry) is SOL_RING_LTC


def test_lookup_matches_by_name_and_set():
    index = server._index_collection_results([COUNTERSPELL_DMR])
    entry = server.DecklistEntry(1, "Counterspell", "dmr", None, None)
    assert server._lookup_entry(index, entry) is COUNTERSPELL_DMR


def test_lookup_matches_by_bare_name_case_insensitively():
    index = server._index_collection_results([COUNTERSPELL_DMR])
    entry = server.DecklistEntry(1, "cOUNTERSPELL", None, None, None)
    assert server._lookup_entry(index, entry) is COUNTERSPELL_DMR


def test_lookup_matches_a_dfc_by_its_front_face_name():
    index = server._index_collection_results([SEPHIROTH_FIN])
    entry = server.DecklistEntry(1, "Sephiroth, Fabled SOLDIER", None, None, None)
    assert server._lookup_entry(index, entry) is SEPHIROTH_FIN


def test_lookup_matches_a_dfc_by_its_full_name():
    index = server._index_collection_results([SEPHIROTH_FIN])
    entry = server.DecklistEntry(
        1, "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel", None, None, None)
    assert server._lookup_entry(index, entry) is SEPHIROTH_FIN


def test_lookup_returns_none_when_absent():
    index = server._index_collection_results([SOL_RING_LTC])
    entry = server.DecklistEntry(1, "Black Lotus", None, None, None)
    assert server._lookup_entry(index, entry) is None


def test_lookup_does_not_substitute_a_different_printing_from_the_same_set():
    # A named collector number is a request for one exact printing. Degrading
    # to another printing of the same card in the same set would report
    # someone else's price as the user's.
    other_sld_sol_ring = {
        "name": "Sol Ring", "set": "sld", "collector_number": "2417",
        "finishes": ["foil"], "prices": {"usd_foil": "48.21"},
    }
    index = server._index_collection_results([other_sld_sol_ring])
    entry = server.DecklistEntry(1, "Sol Ring", "sld", "9999", None)
    assert server._lookup_entry(index, entry) is None


def test_lookup_still_degrades_when_only_a_set_was_named():
    # Contrast with the above: no collector number means the caller did not ask
    # for a specific printing, so falling back to the bare-name key is correct.
    index = server._index_collection_results([SOL_RING_LTC])
    entry = server.DecklistEntry(1, "Sol Ring", "c21", None, None)
    assert server._lookup_entry(index, entry) is SOL_RING_LTC


# ── _identifier_label ─────────────────────────────────────────────────────────

def test_identifier_label_renders_each_identifier_shape_readably():
    # not_found echoes back the identifier object we sent, so the old
    # `item.get("name", str(item))` printed a raw dict for printing lookups.
    assert server._identifier_label(
        {"set": "ltc", "collector_number": "284"}) == "LTC #284"
    assert server._identifier_label(
        {"name": "Arcane Signet", "set": "otc"}) == "Arcane Signet (OTC)"
    assert server._identifier_label({"name": "Rhystic Study"}) == "Rhystic Study"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_printing_prices.py -k "lookup or identifier_label" -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_index_collection_results'`

- [ ] **Step 3: Write the implementation**

In `server.py`, add immediately after `_price_for_finish`:

```python
def _card_name_aliases(card: dict) -> list[str]:
    """Lowercased names a decklist line might use for this card.

    Includes each face of a double-faced card, since a list may write only the
    front face while Scryfall returns 'Front // Back'.
    """
    full = (card.get("name") or "").lower().strip()
    if not full:
        return []
    aliases = [full]
    if "//" in full:
        aliases.extend(part.strip() for part in full.split("//") if part.strip())
    return aliases


def _index_collection_results(cards: list[dict]) -> dict:
    """Index collection results for lookup by printing, name+set, or name.

    Scryfall does not guarantee that response order matches request order, and
    omits not-found entries, so results must be matched explicitly rather than
    by position. First card wins on any given key.
    """
    index: dict = {}
    for card in cards:
        set_code = (card.get("set") or "").lower()
        collector = card.get("collector_number") or ""
        if set_code and collector:
            index.setdefault(("printing", set_code, collector), card)
        for alias in _card_name_aliases(card):
            if set_code:
                index.setdefault(("name_set", alias, set_code), card)
            index.setdefault(("name", alias), card)
    return index


def _lookup_entry(index: dict, entry: DecklistEntry) -> Optional[dict]:
    """Find the card matching this entry, most specific key first.

    An entry that names a collector number is asking for one exact printing, so
    a miss there is a miss — degrading to another printing of the same card
    would report someone else's price as the user's. Entries that named only a
    set, or only a name, do degrade.
    """
    name = entry.name.lower().strip()
    set_code = (entry.set_code or "").lower()

    if set_code and entry.collector_number:
        return index.get(("printing", set_code, entry.collector_number))
    if set_code:
        hit = index.get(("name_set", name, set_code))
        if hit is not None:
            return hit
    return index.get(("name", name))


def _identifier_label(identifier: dict) -> str:
    """Human-readable rendering of an identifier we sent to Scryfall.

    /cards/collection echoes unmatched identifiers back verbatim, so this is
    what the 'not found' section prints.
    """
    name = identifier.get("name")
    set_code = identifier.get("set")
    collector = identifier.get("collector_number")
    if set_code and collector:
        return f"{set_code.upper()} #{collector}"
    if name and set_code:
        return f"{name} ({set_code.upper()})"
    return name or str(identifier)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_printing_prices.py -k "lookup or identifier_label" -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_printing_prices.py
git commit -m "scryfall: Match collection results to lines without relying on order"
```

---

## Task 6: Display helpers for priced lines

Small formatting helpers, split out so Task 7's tool body stays readable and so the output text is testable without network access.

**Files:**
- Modify: `server.py` — add below `_identifier_label`
- Test: `tests/test_printing_prices.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_printing_prices.py`:

```python
# ── display helpers ───────────────────────────────────────────────────────────

def test_printing_label_names_the_exact_printing_and_finish():
    assert server._printing_label(COUNTERSPELL_DMR, "foil") == \
        "Counterspell (DMR #281, foil)"
    assert server._printing_label(COUNTERSPELL_DMR, None) == \
        "Counterspell (DMR #281, nonfoil)"


def test_available_finishes_lists_prices_the_printing_actually_has():
    assert server._available_finishes(ALL_FINISHES) == \
        "nonfoil $29.04, foil (no price), etched $26.31"


def test_available_finishes_omits_finishes_the_printing_lacks():
    assert server._available_finishes(FOIL_ONLY) == "foil $48.21"


def test_available_finishes_handles_a_printing_with_no_finish_data():
    assert server._available_finishes({"prices": {}}) == "none listed"


def test_entry_suffix_describes_what_the_line_asked_for():
    assert server._entry_suffix(
        server.DecklistEntry(1, "Sol Ring", "ltc", "284", "foil")) == " (LTC #284) foil"
    assert server._entry_suffix(
        server.DecklistEntry(1, "Sol Ring", "otc", None, None)) == " (OTC)"
    assert server._entry_suffix(
        server.DecklistEntry(1, "Sol Ring", None, None, None)) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_printing_prices.py -k "printing_label or available_finishes or entry_suffix" -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_printing_label'`

- [ ] **Step 3: Write the implementation**

In `server.py`, add immediately after `_identifier_label`:

```python
def _printing_label(card: dict, finish: Optional[str]) -> str:
    """'Counterspell (DMR #281, foil)' — the printing a price refers to."""
    name = card.get("name", "?")
    set_code = (card.get("set") or "?").upper()
    collector = card.get("collector_number", "?")
    return f"{name} ({set_code} #{collector}, {finish or 'nonfoil'})"


def _available_finishes(card: dict) -> str:
    """What this printing does exist in, and what those cost.

    Shown when the requested finish has no price, so the user learns why rather
    than just that the number is missing.
    """
    bits: list[str] = []
    for finish in ("nonfoil", "foil", "etched"):
        if finish not in (card.get("finishes") or []):
            continue
        price = _price_for_finish(card, finish)
        bits.append(f"{finish} ${price:.2f}" if price is not None else f"{finish} (no price)")
    return ", ".join(bits) if bits else "none listed"


def _entry_suffix(entry: DecklistEntry) -> str:
    """What the decklist line asked for, for lines with no matching card."""
    bits: list[str] = []
    if entry.set_code:
        printing = entry.set_code.upper()
        if entry.collector_number:
            printing += f" #{entry.collector_number}"
        bits.append(f"({printing})")
    if entry.finish:
        bits.append(entry.finish)
    return (" " + " ".join(bits)) if bits else ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_printing_prices.py -k "printing_label or available_finishes or entry_suffix" -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_printing_prices.py
git commit -m "scryfall: Add display helpers for printing-aware price output"
```

---

## Task 7: Rewrite `scryfall_price_list`

Swaps the input from a name list to decklist text, batches at Scryfall's 75-identifier limit, honors quantities, and groups output into sections so the total's coverage is explicit.

This is a deliberate breaking schema change — see spec Decision D1. Nothing in the repo calls this tool and no tests cover it.

**Files:**
- Modify: `server.py:247-253` (`PriceListInput`), `server.py:378-432` (the tool)
- Test: `tests/test_printing_prices.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_printing_prices.py`:

```python
# ── section assembly ──────────────────────────────────────────────────────────

def test_price_sections_group_lines_and_total_only_what_it_priced():
    entries = [
        server.DecklistEntry(2, "Counterspell", "dmr", "281", "foil"),   # 2 x 2.17
        server.DecklistEntry(1, "Sol Ring", "ltc", "284", None),          # 2.51
        server.DecklistEntry(1, "Arcane Signet", "sld", "589", "foil"),   # no foil price
        server.DecklistEntry(3, "Rhystic Study", None, None, None),       # default printing
        server.DecklistEntry(1, "Black Lotus", None, None, None),         # not found
    ]
    rhystic = {
        "name": "Rhystic Study", "set": "j22", "collector_number": "114",
        "finishes": ["nonfoil"], "prices": {"usd": "69.53"},
    }
    index = server._index_collection_results(
        [COUNTERSPELL_DMR, SOL_RING_LTC, ALL_FINISHES, rhystic])

    result = server._build_price_sections(entries, index, [{"name": "Black Lotus"}])

    assert result["total"] == Decimal("215.44")   # 4.34 + 2.51 + 208.59
    assert result["priced_cards"] == 6            # 2 + 1 + 3
    assert len(result["priced"]) == 2             # the two lines naming a printing
    assert len(result["defaulted"]) == 1          # Rhystic Study
    assert len(result["no_price"]) == 1           # Arcane Signet foil
    assert len(result["missing"]) == 1            # Black Lotus


def test_price_sections_exclude_unpriced_lines_from_the_total():
    entries = [server.DecklistEntry(1, "Arcane Signet", "sld", "589", "foil")]
    index = server._index_collection_results([ALL_FINISHES])
    result = server._build_price_sections(entries, index, [])
    assert result["total"] == Decimal("0")
    assert result["priced_cards"] == 0
    assert "foil" in result["no_price"][0]
    assert "nonfoil $29.04" in result["no_price"][0]


def test_price_sections_use_exact_decimal_arithmetic():
    # 100 lines at $0.07 is exactly $7.00; float accumulation drifts.
    card = {"name": "Island", "set": "unf", "collector_number": "240",
            "finishes": ["nonfoil"], "prices": {"usd": "0.07"}}
    entries = [server.DecklistEntry(1, "Island", "unf", "240", None)] * 100
    index = server._index_collection_results([card])
    result = server._build_price_sections(entries, index, [])
    assert result["total"] == Decimal("7.00")


def test_price_sections_multiply_by_quantity():
    entries = [server.DecklistEntry(10, "Sol Ring", "ltc", "284", None)]
    index = server._index_collection_results([SOL_RING_LTC])
    result = server._build_price_sections(entries, index, [])
    assert result["total"] == Decimal("25.10")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_printing_prices.py -k price_sections -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_build_price_sections'`

- [ ] **Step 3: Add the section builder**

In `server.py`, add immediately after `_entry_suffix`:

```python
def _build_price_sections(
    entries: list[DecklistEntry],
    index: dict,
    not_found: list[dict],
) -> dict:
    """Sort priced lines into sections and total only what was actually priced.

    Returns lists of preformatted strings plus the total, so the tool body is
    assembly only and this logic stays testable without network access.
    """
    priced: list[tuple[Decimal, str]] = []
    defaulted: list[tuple[Decimal, str]] = []
    no_price: list[str] = []
    missing: list[str] = []
    missing_identifiers: set[str] = set()

    total = Decimal("0")
    priced_cards = 0

    for entry in entries:
        card = _lookup_entry(index, entry)
        if card is None:
            missing.append(f"{entry.quantity}x {entry.name}{_entry_suffix(entry)}")
            missing_identifiers.add(repr(sorted(_entry_identifier(entry).items())))
            continue

        unit = _price_for_finish(card, entry.finish)
        if unit is None:
            no_price.append(
                f"{entry.quantity}x {_printing_label(card, entry.finish)} — "
                f"no {entry.finish or 'nonfoil'} price. "
                f"This printing has: {_available_finishes(card)}"
            )
            continue

        line_total = unit * entry.quantity
        total += line_total
        priced_cards += entry.quantity
        text = (f"{entry.quantity}x {_printing_label(card, entry.finish)} — "
                f"${unit:.2f} ea → ${line_total:.2f}")
        # A line that named no set got whichever printing Scryfall chose.
        (priced if entry.set_code else defaulted).append((line_total, text))

    priced.sort(key=lambda item: item[0], reverse=True)
    defaulted.sort(key=lambda item: item[0], reverse=True)

    # Every not_found identifier corresponds to some entry above whose lookup
    # already failed (that's the identifier we sent for it), so it is already
    # represented in `missing` with quantity and printing detail. Only surface
    # a not_found identifier here if it somehow has no matching line above —
    # defensive, but avoids reporting the same missing card twice.
    for identifier in not_found:
        key = repr(sorted(identifier.items()))
        if key not in missing_identifiers:
            missing.append(_identifier_label(identifier))

    return {
        "priced": [text for _, text in priced],
        "defaulted": [text for _, text in defaulted],
        "no_price": no_price,
        "missing": missing,
        "total": total,
        "priced_cards": priced_cards,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_printing_prices.py -k price_sections -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Replace the input model**

In `server.py`, replace `PriceListInput` (currently lines 247-253):

```python
class PriceListInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    cards: list[str] = Field(
        ...,
        description="List of card names to price (max 75 per request).",
        min_length=1, max_length=75,
    )
```

with:

```python
class PriceListInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    decklist: str = Field(
        ...,
        description=(
            "Decklist text, one card per line. Plain names work ('1 Sol Ring'), "
            "and so do full Archidekt/Moxfield lines naming a printing and "
            "finish ('1x Sol Ring (ltc) 284 *F* [Ramp]'). Lines with a set code "
            "are priced at that exact printing; lines without one use Scryfall's "
            "default printing and are flagged separately in the output. "
            "Max 500 lines."
        ),
        min_length=1, max_length=50000,
    )
```

- [ ] **Step 6: Replace the tool body**

In `server.py`, replace the whole `scryfall_price_list` function (from `@mcp.tool(name="scryfall_price_list")` through `return "\n".join(parts)`, currently lines 378-432) with:

```python
@mcp.tool(name="scryfall_price_list")
async def scryfall_price_list(params: PriceListInput) -> str:
    """Price a decklist and total it, honoring the printing on each line.

    '1x Sol Ring (ltc) 284 *F*' is priced as that exact printing in that exact
    finish. Lines naming no set use Scryfall's default printing and are listed
    separately so the total is honest about what it guessed.

    Never substitutes a different finish: if the requested finish has no price,
    the line is reported on its own with the finishes that printing does have,
    rather than being quietly totalled at another finish's price.
    """
    entries = _parse_decklist_entries(params.decklist)
    if not entries:
        return "No cards found in the decklist."
    if len(entries) > 500:
        return f"Too many lines ({len(entries)}). Maximum is 500."

    identifiers = _dedupe_identifiers(entries)

    found_cards: list[dict] = []
    not_found: list[dict] = []
    unchecked: list[dict] = []
    errors: list[str] = []

    for batch in _chunk(identifiers, 75):   # Scryfall's hard per-request cap
        try:
            data = await _scryfall_post("/cards/collection", {"identifiers": batch})
        except Exception as e:
            # Keep what other batches returned. These identifiers are unchecked,
            # NOT missing — reporting them as "not found" would tell the user a
            # real card does not exist because of a transient API failure.
            unchecked.extend(batch)
            errors.append(_scryfall_error(e))
            continue
        found_cards.extend(data.get("data", []))
        not_found.extend(data.get("not_found", []))

    if unchecked and not found_cards:
        return errors[0]

    sections = _build_price_sections(
        entries, _index_collection_results(found_cards), not_found, unchecked)

    total_cards = sum(e.quantity for e in entries)
    parts: list[str] = []
    parts.append(f"# Price List ({total_cards} cards)")
    parts.append("")

    if sections["priced"]:
        parts.append("**Priced at the printing you named:**")
        parts.extend(f"- {line}" for line in sections["priced"])
        parts.append("")

    if sections["defaulted"]:
        parts.append("**No printing specified — Scryfall's default printing used:**")
        parts.extend(f"- {line}" for line in sections["defaulted"])
        parts.append("")

    if sections["no_price"]:
        parts.append("**No price in the requested finish (excluded from total):**")
        parts.extend(f"- {line}" for line in sections["no_price"])
        parts.append("")

    if sections["unchecked"]:
        parts.append("**Could not be checked — Scryfall request failed (excluded from total):**")
        parts.extend(f"- {line}" for line in sections["unchecked"])
        parts.append(f"  ({errors[0]})")
        parts.append("")

    if sections["missing"]:
        parts.append("**Not found on Scryfall:**")
        parts.extend(f"- {line}" for line in sections["missing"])
        parts.append("")

    parts.append(
        f"**Total: ${sections['total']:.2f}** "
        f"({sections['priced_cards']} of {total_cards} cards priced)"
    )
    uncovered = total_cards - sections["priced_cards"]
    if uncovered:
        parts.append(f"{uncovered} card(s) are not included in this total — see the sections above.")

    return "\n".join(parts)
```

- [ ] **Step 7: Run the full suite**

Run: `pytest`
Expected: PASS

- [ ] **Step 8: Verify against the live API**

Run:

```bash
python -c "
import asyncio, server
print(asyncio.run(server.scryfall_price_list(server.PriceListInput(decklist='''
1x Counterspell (dmr) 281 *F*
2x Sol Ring (ltc) 284
1x Arcane Signet (sld) 589 *E*
1 Rhystic Study
1x Notarealcardxyz
'''))))
"
```

Expected: a "Priced at the printing you named" section containing Counterspell at its DMR #281 **foil** price and Sol Ring at 2x its LTC #284 price; Rhystic Study under the default-printing section; Notarealcardxyz under not-found rendered as a name, not a dict; and a total whose card count excludes the not-found line.

- [ ] **Step 9: Commit**

```bash
git add server.py tests/test_printing_prices.py
git commit -m "scryfall: Price decklists at the printing and finish on each line"
```

---

## Task 8: Printing filters on `scryfall_price`

Adds set/collector/finish filters, drops digital-only printings by default, and fixes the null-sort defect that currently makes this tool return ten unpriced rows for cards like Counterspell.

**Files:**
- Modify: `server.py:241-245` (`PriceInput`), `server.py:316-375` (the tool), plus a new enum near `ScryfallSearchOrder` at `server.py:195`
- Test: `tests/test_printing_prices.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_printing_prices.py`:

```python
# ── scryfall_price query building and sorting ─────────────────────────────────

import pytest
from pydantic import ValidationError


def test_price_query_is_just_the_name_and_a_digital_filter_by_default():
    params = server.PriceInput(name="Sol Ring")
    assert server._price_query(params) == '!"Sol Ring" -is:digital'


def test_price_query_includes_set_collector_and_finish():
    params = server.PriceInput(
        name="Counterspell", set_code="dmr", collector_number="281", finish="foil")
    assert server._price_query(params) == \
        '!"Counterspell" set:dmr cn:281 is:foil -is:digital'


def test_price_query_can_include_digital_printings():
    params = server.PriceInput(name="Counterspell", include_digital=True)
    assert server._price_query(params) == '!"Counterspell"'


def test_collector_number_without_set_code_is_rejected():
    # Collector numbers are only unique within a set, so this is a real error
    # rather than something to silently ignore.
    with pytest.raises(ValidationError):
        server.PriceInput(name="Sol Ring", collector_number="284")


def test_sort_puts_priced_printings_first_and_unpriced_last():
    # The live defect: Scryfall's order=usd&dir=asc sorts nulls FIRST, so the
    # ten rows this tool displayed for Counterspell were all unpriced.
    unpriced = {"name": "C", "set": "tpr", "collector_number": "1",
                "finishes": ["nonfoil"], "prices": {"usd": None}}
    cheap = {"name": "C", "set": "dmr", "collector_number": "281",
             "finishes": ["nonfoil"], "prices": {"usd": "2.15"}}
    dear = {"name": "C", "set": "6ed", "collector_number": "77",
            "finishes": ["nonfoil"], "prices": {"usd": "2.27"}}

    ordered = server._sort_by_price([unpriced, dear, cheap], None)
    assert [c["set"] for c in ordered] == ["dmr", "6ed", "tpr"]


def test_sort_uses_the_requested_finish_column():
    a = {"name": "C", "set": "a", "collector_number": "1",
         "finishes": ["nonfoil", "foil"], "prices": {"usd": "10.00", "usd_foil": "1.00"}}
    b = {"name": "C", "set": "b", "collector_number": "2",
         "finishes": ["nonfoil", "foil"], "prices": {"usd": "1.00", "usd_foil": "10.00"}}
    assert [c["set"] for c in server._sort_by_price([a, b], "foil")] == ["a", "b"]
    assert [c["set"] for c in server._sort_by_price([a, b], "nonfoil")] == ["b", "a"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_printing_prices.py -k "price_query or collector_number_without or sort_" -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_price_query'`

- [ ] **Step 3: Add the enums**

In `server.py`, immediately after the `ScryfallSearchOrder` enum (which ends at line 195 with `REVIEW = "review"`), add:

```python
class CardFinish(str, Enum):
    NONFOIL = "nonfoil"
    FOIL = "foil"
    ETCHED = "etched"


class PriceOrder(str, Enum):
    USD = "usd"
    RELEASED = "released"
    SET = "set"
    NAME = "name"
```

- [ ] **Step 4: Replace `PriceInput`**

In `server.py`, replace `PriceInput` (currently lines 241-245):

```python
class PriceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Card name to look up prices for.", min_length=1, max_length=200)
    limit: int = Field(default=10, description="Max printings to show.", ge=1, le=50)
```

with:

```python
class PriceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Card name to look up prices for.", min_length=1, max_length=200)
    set_code: Optional[str] = Field(
        default=None,
        description="Restrict to one set, e.g. 'dmr'. Use with collector_number to pin one printing.",
        min_length=2, max_length=6,
    )
    collector_number: Optional[str] = Field(
        default=None,
        description=(
            "Collector number within the set, e.g. '281' or 'IFIYW-10'. "
            "Requires set_code. With both set, prices exactly one printing."
        ),
        max_length=20,
    )
    finish: Optional[CardFinish] = Field(
        default=None,
        description="Restrict to printings available in this finish, and lead with its price.",
    )
    include_digital: bool = Field(
        default=False,
        description="Include Arena/MTGO-only printings. They never have paper prices, so off by default.",
    )
    order: PriceOrder = Field(default=PriceOrder.USD, description="Sort order requested from Scryfall.")
    limit: int = Field(default=10, description="Max printings to show.", ge=1, le=50)

    @model_validator(mode="after")
    def _collector_number_requires_set(self):
        if self.collector_number and not self.set_code:
            raise ValueError(
                "collector_number requires set_code — collector numbers are only "
                "unique within a set."
            )
        return self
```

- [ ] **Step 5: Add the query and sort helpers**

In `server.py`, immediately above `@mcp.tool(name="scryfall_price")` (currently line 316), add:

```python
def _price_query(params: "PriceInput") -> str:
    """Scryfall search query for a price lookup, including printing filters."""
    bits = [f'!"{params.name}"']
    if params.set_code:
        bits.append(f"set:{params.set_code}")
    if params.collector_number:
        bits.append(f"cn:{params.collector_number}")
    if params.finish:
        bits.append(f"is:{params.finish.value}")
    if not params.include_digital:
        bits.append("-is:digital")
    return " ".join(bits)


def _sort_by_price(cards: list[dict], finish: Optional[str]) -> list[dict]:
    """Cheapest priced printing first, unpriced printings last.

    Scryfall's own order=usd&dir=asc sorts null prices FIRST, which meant this
    tool's top rows were routinely printings with no price at all.
    """
    def sort_key(card: dict):
        price = _price_for_finish(card, finish)
        return (price is None, price if price is not None else Decimal("0"))
    return sorted(cards, key=sort_key)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_printing_prices.py -k "price_query or collector_number_without or sort_" -v`
Expected: PASS, 6 tests

- [ ] **Step 7: Replace the tool body**

In `server.py`, replace the whole `scryfall_price` function (from `@mcp.tool(name="scryfall_price")` through `return "\n".join(parts)`, currently lines 316-375) with:

```python
@mcp.tool(name="scryfall_price")
async def scryfall_price(params: PriceInput) -> str:
    """Get current market prices for a card, optionally for one specific printing.

    With no filters, lists printings cheapest first (printings with no price
    sort last, not first). Pass set_code to restrict to one set, set_code plus
    collector_number to price exactly one printing, or finish to restrict to
    printings that come in foil or etched.

    Digital-only (Arena/MTGO) printings are excluded by default because they
    never carry paper prices. Prices are updated daily by Scryfall.
    """
    try:
        data = await _scryfall_get(
            "/cards/search",
            params={
                "q": _price_query(params),
                "unique": "prints",
                "order": params.order.value,
                "dir": "asc",
            },
        )
    except Exception as e:
        return _scryfall_error(e)

    cards = data.get("data", [])
    if not cards:
        return f"No printings found for '{params.name}' with those filters."

    finish = params.finish.value if params.finish else None
    total = data.get("total_cards", len(cards))

    # One printing pinned exactly — show it in detail instead of as a list row.
    if params.set_code and params.collector_number and len(cards) == 1:
        return _format_single_printing(cards[0], finish)

    ordered = _sort_by_price(cards, finish)[:params.limit]

    parts: list[str] = []
    parts.append(f"# Prices for {cards[0].get('name', params.name)}")
    shown = f"Showing {len(ordered)} of {total} printings"
    filters = _describe_price_filters(params)
    parts.append(f"{shown}{filters}")
    parts.append("")

    for card in ordered:
        parts.append(f"**{card.get('set_name', '?')}** "
                     f"({(card.get('set') or '?').upper()} "
                     f"#{card.get('collector_number', '?')}, "
                     f"{card.get('rarity', '?')}) — {_format_price_columns(card)}")

    cheapest = next(
        (c for c in ordered if _price_for_finish(c, finish) is not None), None)
    if cheapest is not None:
        price = _price_for_finish(cheapest, finish)
        parts.append("")
        parts.append(f"Cheapest {finish or 'nonfoil'}: ${price:.2f} "
                     f"({cheapest.get('set_name', '?')}, "
                     f"{(cheapest.get('set') or '?').upper()} "
                     f"#{cheapest.get('collector_number', '?')})")

    if total > len(ordered):
        parts.append("")
        parts.append("Narrow with set_code, or raise limit, to see other printings.")

    return "\n".join(parts)
```

- [ ] **Step 8: Add the two formatting helpers it calls**

In `server.py`, immediately above `def _price_query(`, add:

```python
def _format_price_columns(card: dict) -> str:
    """Every price this printing has, as a single display string."""
    prices = card.get("prices") or {}
    bits: list[str] = []
    if prices.get("usd"):
        bits.append(f"${prices['usd']}")
    if prices.get("usd_foil"):
        bits.append(f"Foil: ${prices['usd_foil']}")
    if prices.get("usd_etched"):
        bits.append(f"Etched: ${prices['usd_etched']}")
    if prices.get("eur"):
        bits.append(f"EUR: €{prices['eur']}")
    if prices.get("tix"):
        bits.append(f"MTGO: {prices['tix']} tix")
    return " | ".join(bits) if bits else "No price data"


def _describe_price_filters(params: "PriceInput") -> str:
    """' (set:dmr, foil)' — states which filters produced this result set."""
    bits: list[str] = []
    if params.set_code:
        bits.append(f"set:{params.set_code}")
    if params.collector_number:
        bits.append(f"cn:{params.collector_number}")
    if params.finish:
        bits.append(params.finish.value)
    if params.include_digital:
        bits.append("including digital")
    return f" ({', '.join(bits)})" if bits else ""


def _format_single_printing(card: dict, finish: Optional[str]) -> str:
    """Detail view for one pinned printing — enough to identify a physical copy."""
    parts: list[str] = []
    parts.append(f"# {card.get('name', '?')}")
    parts.append(f"**{card.get('set_name', '?')}** "
                 f"({(card.get('set') or '?').upper()} "
                 f"#{card.get('collector_number', '?')}, "
                 f"{card.get('rarity', '?')})")
    parts.append("")
    parts.append(f"Prices: {_format_price_columns(card)}")
    parts.append(f"Available finishes: {_available_finishes(card)}")

    requested = _price_for_finish(card, finish)
    if finish:
        if requested is not None:
            parts.append(f"Requested finish ({finish}): ${requested:.2f}")
        else:
            parts.append(f"Requested finish ({finish}): no price for this printing")

    parts.append("")
    if card.get("artist"):
        parts.append(f"Artist: {card['artist']}")
    frame_bits = [card.get("frame", ""), card.get("border_color", "")]
    frame = ", ".join(b for b in frame_bits if b)
    if frame:
        parts.append(f"Frame/border: {frame}")
    if card.get("promo_types"):
        parts.append(f"Promo types: {', '.join(card['promo_types'])}")
    if card.get("released_at"):
        parts.append(f"Released: {card['released_at']}")
    if card.get("scryfall_uri"):
        parts.append(f"Link: {card['scryfall_uri']}")

    return "\n".join(parts)
```

- [ ] **Step 9: Run the full suite**

Run: `pytest`
Expected: PASS

- [ ] **Step 10: Verify the Counterspell defect is fixed against the live API**

Run:

```bash
python -c "
import asyncio, server
print(asyncio.run(server.scryfall_price(server.PriceInput(name='Counterspell'))))
print('=' * 60)
print(asyncio.run(server.scryfall_price(server.PriceInput(
    name='Counterspell', set_code='dmr', collector_number='281', finish='foil'))))
"
```

Expected: the first block lists printings that **have** prices (Dominaria Remastered around \$2.15 near the top) and ends with a `Cheapest nonfoil:` line — not the ten unpriced MTGO rows it returns today. The second block is a single-printing detail view showing DMR #281 with its foil price and available finishes.

- [ ] **Step 11: Commit**

```bash
git add server.py tests/test_printing_prices.py
git commit -m "scryfall: Filter prices by printing and sort unpriced printings last"
```

---

## Task 9: Emit finish markers from `format_archidekt`

Adds an optional `finish` to each card entry. No feature flag: an entry that does not set `finish` emits exactly what it emits today, so the field's absence is the opt-in (spec Part 3).

**Files:**
- Modify: `server.py:1267-1276` (`DeckCardEntry`), `server.py:1342-1352` (line building), `server.py:1295-1311` (docstring)
- Test: `tests/test_finish_markers.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_finish_markers.py`:

```python
def test_deck_card_entry_accepts_a_finish():
    entry = server.DeckCardEntry(name="Sol Ring", finish="foil")
    assert entry.finish.value == "foil"


def test_deck_card_entry_finish_defaults_to_none():
    assert server.DeckCardEntry(name="Sol Ring").finish is None


def test_archidekt_line_places_the_marker_after_collector_before_category():
    # Matches Archidekt's own export grammar, verified 2026-08-08:
    #   1x Name (set) 382 *F* [Commander{top}]
    line = server._archidekt_line(
        quantity=1, name="Counterspell", set_code="dmr", collector="281",
        finish="foil", category=" [Draw]", labels=" ^Test,#2ccce4^")
    assert line == "1x Counterspell (dmr) 281 *F* [Draw] ^Test,#2ccce4^"


def test_archidekt_line_omits_the_marker_for_nonfoil_and_none():
    for finish in (None, "nonfoil"):
        line = server._archidekt_line(
            quantity=1, name="Counterspell", set_code="dmr", collector="281",
            finish=finish, category=" [Draw]", labels="")
        assert line == "1x Counterspell (dmr) 281 [Draw]"


def test_archidekt_line_without_a_printing():
    assert server._archidekt_line(
        quantity=2, name="Sol Ring", set_code="", collector="",
        finish=None, category="", labels="") == "2x Sol Ring"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_finish_markers.py -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_archidekt_line'`

- [ ] **Step 3: Add the shared line builder**

Both `format_archidekt` and `archidekt_export` build the same grammar, so it goes in one place. In `server.py`, add immediately above `class DeckCardEntry(BaseModel):` (currently line 1267):

```python
def _archidekt_line(
    quantity: int,
    name: str,
    set_code: str,
    collector: str,
    finish: Optional[str],
    category: str,
    labels: str,
) -> str:
    """Build one Archidekt import line.

    Grammar, verified against Archidekt's own text export 2026-08-08:
      {qty}x {name} ({set}) {collector} *F* [{Category}{flags}] ^{Label},{#hex}^

    `category` and `labels` arrive already formatted with their leading space.
    """
    line = f"{quantity}x {name}"
    if set_code:
        line += f" ({set_code})"
    if collector:
        line += f" {collector}"
    marker = _finish_marker(finish)
    if marker:
        line += f" {marker}"
    return line + category + labels
```

- [ ] **Step 4: Add `finish` to `DeckCardEntry`**

In `server.py`, in `DeckCardEntry` (currently lines 1267-1276), add after the `quantity` field:

```python
    finish: Optional[CardFinish] = Field(
        default=None,
        description=(
            "Card finish. 'foil' emits *F* and 'etched' emits *E* on the line, "
            "which Archidekt imports as that finish. Omit for a normal card."
        ),
    )
```

- [ ] **Step 5: Use the builder in `format_archidekt`**

In `server.py`, in `format_archidekt`, replace the line-building block (currently lines 1342-1366, from `line = f"{entry.quantity}x {card_name}"` through the label `if`/`else` and ending just before `lines.append(line)`) with:

```python
        set_code = ""
        collector = ""
        if params.include_set_codes and scryfall_card:
            set_code = scryfall_card.get("set", "")
            collector = scryfall_card.get("collector_number", "")

        # Category annotation
        cat_annotation = ""
        if entry.commander:
            cat_annotation = " [Commander{top}]"
        elif entry.maybeboard:
            cat_annotation = " [Maybeboard{noDeck}{noPrice}]"
        elif entry.category:
            cat_annotation = f" [{entry.category}]"

        # Labels
        label_text = ""
        if entry.label:
            if entry.label_color:
                label_text = f" ^{entry.label},{entry.label_color}^"
            else:
                label_text = f" ^{entry.label}^"

        line = _archidekt_line(
            quantity=entry.quantity,
            name=card_name,
            set_code=set_code,
            collector=collector,
            finish=entry.finish.value if entry.finish else None,
            category=cat_annotation,
            labels=label_text,
        )
```

- [ ] **Step 6: Update the docstring**

In `server.py`, in `format_archidekt`'s docstring, replace this block:

```
    Output format (Archidekt native):
      1x Card Name [Category]
      1x Commander Name [Commander{top}]
      1x Maybe Card [Maybeboard{noDeck}{noPrice}]
      1x Labeled Card [Draw] ^To Buy,#2ccce4^
```

with:

```
    Output format (Archidekt native):
      1x Card Name [Category]
      1x Commander Name [Commander{top}]
      1x Maybe Card [Maybeboard{noDeck}{noPrice}]
      1x Labeled Card [Draw] ^To Buy,#2ccce4^
      1x Foil Card (dmr) 281 *F* [Ramp]

    Set finish='foil' or finish='etched' on a card to mark it as such (*F* / *E*).
    Pair it with include_set_codes so the marked line names a printing.
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_finish_markers.py -v`
Expected: PASS, 7 tests

- [ ] **Step 8: Verify unchanged output for callers that set no finish**

Run:

```bash
python -c "
import asyncio, server
out = asyncio.run(server.format_archidekt(server.FormatDeckInput(cards=[
    server.DeckCardEntry(name='Sol Ring', category='Ramp'),
    server.DeckCardEntry(name='Atraxa, Praetors\\' Voice', commander=True),
    server.DeckCardEntry(name='Rhystic Study', maybeboard=True),
    server.DeckCardEntry(name='Cultivate', category='Ramp', label='To Buy', label_color='#2ccce4'),
])))
print(out)
"
```

Expected: identical to current `main` output — `1x Sol Ring [Ramp]`, `1x Atraxa, Praetors' Voice [Commander{top}]`, `1x Rhystic Study [Maybeboard{noDeck}{noPrice}]`, `1x Cultivate [Ramp] ^To Buy,#2ccce4^`, sorted, with the total line. No `*F*` anywhere. If it differs, the refactor in Step 5 changed behavior it should not have.

- [ ] **Step 9: Commit**

```bash
git add server.py tests/test_finish_markers.py
git commit -m "archidekt: Emit finish markers from format_archidekt"
```

---

## Task 10: Preserve `modifier` in `archidekt_export`

Archidekt's deck API reports each card's finish as `modifier` (`Normal` / `Foil` / `Etched`) and this tool currently drops it, so a foil deck exported through the server comes back all-nonfoil. Defaults on, no flag — the syntax is Archidekt's own (spec Decision D7).

**Files:**
- Modify: `server.py:1205-1253` (the card loop), `server.py:1186-1192` (docstring)
- Test: `tests/test_finish_markers.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_finish_markers.py`:

```python
def test_modifier_finishes_maps_archidekt_values():
    assert server.MODIFIER_FINISHES["Foil"] == "foil"
    assert server.MODIFIER_FINISHES["Etched"] == "etched"
    assert server.MODIFIER_FINISHES["Normal"] == "nonfoil"


def test_export_line_carries_the_modifier_as_a_marker():
    assert server._archidekt_line(
        quantity=1, name="Sol Ring", set_code="ltc", collector="284",
        finish=server.MODIFIER_FINISHES.get("Foil"),
        category=" [Ramp]", labels="") == "1x Sol Ring (ltc) 284 *F* [Ramp]"


def test_export_line_has_no_marker_for_normal_or_unknown_modifiers():
    for modifier in ("Normal", "", "Something Else"):
        line = server._archidekt_line(
            quantity=1, name="Sol Ring", set_code="ltc", collector="284",
            finish=server.MODIFIER_FINISHES.get(modifier),
            category=" [Ramp]", labels="")
        assert line == "1x Sol Ring (ltc) 284 [Ramp]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_finish_markers.py -k "modifier or export_line" -v`
Expected: FAIL — `KeyError` or `AttributeError` on `MODIFIER_FINISHES` if Task 1 was skipped; otherwise these pass immediately, since Tasks 1 and 9 supply both pieces. If they pass, that is fine — go straight to Step 3, which is the change that actually matters.

- [ ] **Step 3: Read the modifier and use the shared builder**

In `server.py`, in `archidekt_export`, replace the block from `line = f"{qty}x {card_name}"` (currently line 1217) through `line += cat_annotation` (currently line 1241) with:

```python
        modifier = entry.get("modifier", "")

        # Determine category annotation and deck inclusion
        cat_annotation = ""
        is_in_deck = True
        for cat_name in entry_cats:
            cat_def = categories.get(cat_name, {})
            if cat_def.get("isPremier"):
                cat_annotation = f" [{cat_name}{{top}}]"
            elif not cat_def.get("includedInDeck", True):
                cat_annotation = f" [{cat_name}{{noDeck}}{{noPrice}}]"
                is_in_deck = False
            else:
                cat_annotation = f" [{cat_name}]"

        if is_in_deck:
            total_in_deck += qty

        line = _archidekt_line(
            quantity=qty,
            name=card_name,
            set_code=set_code,
            collector=collector,
            finish=MODIFIER_FINISHES.get(modifier),
            category=cat_annotation,
            labels="",
        )
```

Note the labels loop that follows (currently lines 1243-1251) appends to `line` afterwards and is unchanged — leave it exactly as it is.

- [ ] **Step 4: Update the docstring**

In `server.py`, in `archidekt_export`'s docstring, replace:

```
    Uses the full Archidekt import syntax with set codes, categories, and labels:
      1x Card Name (set) [Category{flags}] ^Label,#hex^
```

with:

```
    Uses the full Archidekt import syntax with set codes, finishes, categories,
    and labels:
      1x Card Name (set) 123 *F* [Category{flags}] ^Label,#hex^

    Foil and etched cards keep their finish (*F* / *E*), so the exported list
    re-imports as the same cards and prices correctly via scryfall_price_list.
```

- [ ] **Step 5: Run the full suite**

Run: `pytest`
Expected: PASS

- [ ] **Step 6: Verify against a real deck with foils**

Deck `1585124` was confirmed on 2026-08-08 to contain 6 foil and 1 etched card.

Run:

```bash
python -c "
import asyncio, server
out = asyncio.run(server.archidekt_export(server.ArchidektDeckInput(deck='1585124')))
print('\n'.join(l for l in out.splitlines() if '*' in l) or 'NO MARKERS FOUND')
"
```

Expected: roughly 7 lines, each shaped `1x Name (set) 123 *F* [Category]` with the marker between the collector number and the category, and at least one `*E*`. `NO MARKERS FOUND` means the modifier is not being read.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_finish_markers.py
git commit -m "archidekt: Keep foil and etched finishes when exporting a deck"
```

---

## Task 11: Round-trip test and documentation

Locks the emitters and the parser together, and updates the docs the model reads.

**Files:**
- Modify: `README.md:13-14`
- Test: `tests/test_finish_markers.py`

- [ ] **Step 1: Write the failing round-trip test**

Append to `tests/test_finish_markers.py`:

```python
def test_emitted_lines_parse_back_to_the_same_entries():
    # The emitters (Task 9/10) and the parser (Task 2) are two halves of one
    # format. This is what stops them drifting apart.
    cases = [
        (1, "Counterspell", "dmr", "281", "foil", " [Draw]", ""),
        (1, "Arcane Signet", "sld", "589", "etched", "", ""),
        (2, "Sol Ring", "ltc", "284", None, " [Ramp]", " ^Have,#2ccce4^"),
        (1, "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel",
         "fin", "382", "foil", " [Commander{top}]", ""),
    ]
    for qty, name, set_code, collector, finish, category, labels in cases:
        line = server._archidekt_line(
            quantity=qty, name=name, set_code=set_code, collector=collector,
            finish=finish, category=category, labels=labels)
        (entry,) = server._parse_decklist_entries(line)
        assert entry.quantity == qty, line
        assert entry.name == name, line
        assert entry.set_code == set_code, line
        assert entry.collector_number == collector, line
        assert entry.finish == finish, line


def test_round_trip_survives_a_multi_line_decklist():
    decklist = "\n".join([
        server._archidekt_line(1, "Counterspell", "dmr", "281", "foil", " [Draw]", ""),
        server._archidekt_line(10, "Island", "unf", "240", None, " [Lands]", ""),
        server._archidekt_line(1, "Arcane Signet", "sld", "589", "etched", "", ""),
    ])
    entries = server._parse_decklist_entries(decklist)
    assert [e.finish for e in entries] == ["foil", None, "etched"]
    assert [e.quantity for e in entries] == [1, 10, 1]
    assert [e.set_code for e in entries] == ["dmr", "unf", "sld"]
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_finish_markers.py -k round_trip -v`
Expected: PASS. These should pass immediately — Tasks 2, 9, and 10 built both halves. If either fails, the emitter and parser disagree; fix whichever deviates from the grammar in Task 9's `_archidekt_line` docstring.

- [ ] **Step 3: Update the README tool table**

In `README.md`, replace lines 13-14:

```markdown
| `scryfall_price` | Current prices across all printings |
| `scryfall_price_list` | Batch-price up to 75 cards at once |
```

with:

```markdown
| `scryfall_price` | Prices per printing; pin one with set code + collector number, filter by finish |
| `scryfall_price_list` | Price a decklist at the printing and finish on each line |
```

- [ ] **Step 4: Run the full suite**

Run: `pytest`
Expected: PASS, all tests across all four test files.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_finish_markers.py
git commit -m "docs: Document printing-aware pricing and finish round trip"
```

---

## Verification checklist

Before calling this done, confirm each with actual command output rather than by inspection:

- [ ] `pytest` passes with no failures or errors.
- [ ] `scryfall_price(name="Counterspell")` returns printings that have prices and a `Cheapest nonfoil:` line — the defect in Task 8 Step 10 is gone.
- [ ] `scryfall_price(name="Counterspell", set_code="dmr", collector_number="281", finish="foil")` returns the single-printing detail view.
- [ ] `scryfall_price(name="Sol Ring", collector_number="284")` raises a validation error.
- [ ] `scryfall_price_list` prices `1x Counterspell (dmr) 281 *F*` at the **foil** price, not the nonfoil price.
- [ ] `scryfall_price_list` puts `1x Arcane Signet (sld) 589 *F*` in the no-price section and excludes it from the total — it does not fall back to the nonfoil price.
- [ ] `format_archidekt` output is unchanged when no entry sets `finish` (Task 9 Step 8).
- [ ] `archidekt_export` on deck `1585124` emits `*F*` and `*E*` markers.
- [ ] `validate_decklist` and `precon_diff` still work on a decklist carrying set codes and finish markers.

## Changes made during execution

The plan was edited in place where its own code turned out to be wrong; those
sections above now show the corrected version. Recorded here so the reasoning is not
lost:

1. **Task 5 — `_lookup_entry` degraded too far.** As originally written, an entry
   naming both a set and a collector number would, on a miss, fall through to looser
   keys and return a *different real printing* of the same card — which Task 7 would
   then price and file under "priced at the printing you named." Reachable whenever a
   list holds a second resolvable line for the same card. A named collector number is
   now terminal. Two regression tests added.

2. **Task 7 — `missing` double-counted.** `missing + [_identifier_label(i) for i in
   not_found]` reported every genuinely absent card twice: once from the per-entry
   lookup failure, once from Scryfall's echoed identifier. Now deduped via
   `missing_identifiers`, keeping the richer per-line format.

3. **Task 7 — a failed batch discarded everything.** Introduced by the batching this
   task added. Worse, naive degradation would have filed those entries under "Not
   found on Scryfall," claiming a real card does not exist because a request timed
   out. Batches now fail individually into a distinct **unchecked** bucket.
   `_identifier_key`, `_dedupe_identifiers`, and `_chunk` were extracted so the
   dedupe and batching logic is unit-testable; five tests added.

4. **Task 8 — the `order` parameter was inert.** The mandatory client-side price
   re-sort made every non-default value indistinguishable from the default, so its
   schema description misled the calling model. Removed outright (spec D8), and
   `_printing_header` extracted to kill a verbatim duplicated format string. Five
   tests added covering the single-printing detail view.

5. **Tasks 9 and 10 — no committed guard on the emitters.** Both tasks refactored
   working code paths whose only verification was a one-off shell command. Three
   offline end-to-end tests were added (two for `format_archidekt`, one for
   `archidekt_export`), stubbing the single network helper with `monkeypatch` so they
   run in the suite like everything else.

Final suite: **89 tests**, up from the 18 that existed before this plan.

## Known deferrals

Recorded in the spec, deliberately not implemented here:

- **No validation that a requested finish exists on the chosen printing** (spec D6). Archidekt accepts `*F*` on a nonfoil-only printing, shows no price, and will not let the user toggle it off. The hook is cheap to add later: `format_archidekt` already holds the Scryfall card from its batch lookup, so the check costs no extra requests.
- **No "all foil" bulk mode.** It is the change that would most widen the D6 risk and should not ship before the validation does.
