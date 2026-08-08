# Accurate Card Text Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Claude authoritative bulk card text — a `scryfall_card_text` tool, an `include_text` flag on `archidekt_deck`, and instruction steering — so it verifies rules text instead of hallucinating it.

**Architecture:** Reuse the existing `scryfall_price_list` bulk pipeline (`_parse_decklist_entries` → `_dedupe_identifiers` → `_chunk(75)` → `_scryfall_post("/cards/collection")` → `_index_collection_results` → `_lookup_entry`) with a text formatter instead of a price formatter. Archidekt enrichment renders `oracleCard` fields already present in the deck response — zero extra HTTP calls.

**Tech Stack:** Python, FastMCP, httpx, pydantic, pytest (+pytest-asyncio, `asyncio_mode = auto`). Everything lives in `server.py` (single-module server, established pattern); tests in `tests/`.

**Spec:** `docs/superpowers/specs/2026-08-08-accurate-card-text-design.md`

**Verified data shapes** (live Archidekt API, 2026-08-08):
- `oracleCard` carries `manaCost`, `superTypes`/`types`/`subTypes`, `power`/`toughness` (empty string `''` when absent; real values for Vehicles), `loyalty` (None for non-walkers), `text`, `faces`.
- Multi-faced cards: top-level `text` is `''`, `manaCost` is combined (`'{2}{B} // {B}'`), and `faces` is a list of dicts with `name`, `manaCost`, `text`, `superTypes`/`types`/`subTypes`, `power`/`toughness`, `loyalty`.

---

### Task 1: `scryfall_card_text` bulk tool

**Files:**
- Modify: `server.py` (input model after `PriceListInput` ~line 433; tool after `scryfall_price_list` ~line 745)
- Test: `tests/test_card_text.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_card_text.py`:

```python
import server


ERIETTE = {
    "name": "Eriette of the Charmed Apple",
    "mana_cost": "{1}{W}{B}",
    "type_line": "Legendary Creature — Human Warlock",
    "oracle_text": (
        "At the beginning of your end step, each opponent loses 1 life for "
        "each Aura you control attached to a permanent that player controls."
    ),
    "power": "1", "toughness": "4",
    "color_identity": ["B", "W"],
    "set": "woe", "collector_number": "197",
}

SOL_RING = {
    "name": "Sol Ring",
    "mana_cost": "{1}",
    "type_line": "Artifact",
    "oracle_text": "{T}: Add {C}{C}.",
    "color_identity": [],
    "set": "ltc", "collector_number": "284",
}

DFC = {
    "name": "Gumdrop Poisoner // Tempt with Treats",
    "color_identity": ["B"],
    "set": "woe", "collector_number": "96",
    "card_faces": [
        {"name": "Gumdrop Poisoner", "mana_cost": "{2}{B}",
         "type_line": "Creature — Human Warlock",
         "oracle_text": "Lifelink", "power": "2", "toughness": "2"},
        {"name": "Tempt with Treats", "mana_cost": "{B}",
         "type_line": "Sorcery — Adventure",
         "oracle_text": "Create a Food token."},
    ],
}


def _install_collection(monkeypatch, cards, not_found=None, fail=False):
    """Fake _scryfall_post; returns captured request bodies for inspection."""
    calls: list[dict] = []

    async def fake(endpoint, body):
        assert endpoint == "/cards/collection"
        calls.append(body)
        if fail:
            raise RuntimeError("boom")
        return {"data": cards, "not_found": not_found or []}

    monkeypatch.setattr(server, "_scryfall_post", fake)
    return calls


async def test_returns_text_blocks_in_request_order(monkeypatch):
    _install_collection(monkeypatch, [SOL_RING, ERIETTE])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="Eriette of the Charmed Apple\nSol Ring"))
    assert "each opponent loses 1 life for each Aura" in out
    assert "{T}: Add {C}{C}." in out
    assert "P/T: 1/4" in out
    # Request order, not response order: Eriette was asked for first.
    assert out.index("Eriette") < out.index("Sol Ring")


async def test_duplicate_lines_collapse_to_one_block(monkeypatch):
    _install_collection(monkeypatch, [SOL_RING])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="1 Sol Ring\n1x Sol Ring"))
    assert out.count("{T}: Add {C}{C}.") == 1


async def test_decklist_lines_with_printings_work(monkeypatch):
    _install_collection(monkeypatch, [SOL_RING])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="1x Sol Ring (ltc) 284 *F* [Ramp]"))
    assert "{T}: Add {C}{C}." in out


async def test_dfc_renders_both_faces(monkeypatch):
    _install_collection(monkeypatch, [DFC])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="Gumdrop Poisoner // Tempt with Treats"))
    assert "Lifelink" in out
    assert "Create a Food token." in out


async def test_not_found_reported_with_fuzzy_hint(monkeypatch):
    _install_collection(monkeypatch, [SOL_RING],
                        not_found=[{"name": "Erriette of the Charmed Aple"}])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="Sol Ring\nErriette of the Charmed Aple"))
    assert "## Not found (1)" in out
    assert "Erriette of the Charmed Aple" in out
    assert "scryfall_named" in out


async def test_chunks_at_75_identifiers(monkeypatch):
    names = [f"Card Number {i}" for i in range(100)]
    cards = [{"name": n, "oracle_text": f"Text {n}", "color_identity": []}
             for n in names]
    calls = _install_collection(monkeypatch, cards)
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="\n".join(names)))
    assert len(calls) == 2
    assert len(calls[0]["identifiers"]) == 75
    assert len(calls[1]["identifiers"]) == 25
    assert "Text Card Number 99" in out


async def test_total_failure_returns_error(monkeypatch):
    _install_collection(monkeypatch, [], fail=True)
    out = await server.scryfall_card_text(server.CardTextInput(cards="Sol Ring"))
    assert "Unexpected error" in out


async def test_empty_input_message(monkeypatch):
    # Whitespace-only input is rejected by pydantic (str_strip_whitespace +
    # min_length), so the no-entries path needs a comment-only decklist.
    _install_collection(monkeypatch, [])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="# just a comment"))
    assert out == "No card names found in input."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_card_text.py -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'CardTextInput'` (or `scryfall_card_text`)

- [ ] **Step 3: Write the implementation**

In `server.py`, after `PriceListInput` (~line 433):

```python
class CardTextInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    cards: str = Field(
        ...,
        description=(
            "Cards to fetch exact oracle text for, one per line. Plain names "
            "work ('Sol Ring'), and so do full Archidekt/Moxfield lines "
            "('1x Sol Ring (ltc) 284 *F* [Ramp]') — quantities, finishes, and "
            "categories are ignored. Max 500 lines."
        ),
        min_length=1, max_length=50000,
    )
```

After `scryfall_price_list` (~line 745):

```python
@mcp.tool(name="scryfall_card_text")
async def scryfall_card_text(params: CardTextInput) -> str:
    """Exact oracle text for a whole list of cards in one call.

    Use this BEFORE discussing what specific cards do — never state rules
    text from memory: distinct cards share similar names, and text gets
    errata'd. Returns name, mana cost, type line, full rules text, and P/T
    for every card. Cards Scryfall cannot match are listed explicitly under
    'Not found', never silently dropped.
    """
    entries = _parse_decklist_entries(params.cards)
    if not entries:
        return "No card names found in input."
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
            # Keep what other batches returned. These identifiers are
            # unchecked, NOT missing — a transient API failure must not read
            # as "this card does not exist".
            unchecked.extend(batch)
            errors.append(_scryfall_error(e))
            continue
        found_cards.extend(data.get("data", []))
        not_found.extend(data.get("not_found", []))

    if unchecked and not found_cards:
        return errors[0]

    index = _index_collection_results(found_cards)

    # One block per unique card, in the order the input asked for them.
    blocks: list[str] = []
    seen_cards: set[int] = set()
    for entry in entries:
        card = _lookup_entry(index, entry)
        if card is None or id(card) in seen_cards:
            continue
        seen_cards.add(id(card))
        blocks.append(_format_card(card, verbose=False))

    parts: list[str] = [f"# Card Text ({len(blocks)} card(s))", ""]
    for i, block in enumerate(blocks, 1):
        parts.append(f"--- {i} ---")
        parts.append(block)
        parts.append("")

    if not_found:
        parts.append(f"## Not found ({len(not_found)})")
        parts.extend(f"- {_identifier_label(ident)}" for ident in not_found)
        parts.append(
            "(Bulk lookup needs exact names — retry these one at a time with "
            "scryfall_named, which fuzzy-matches.)"
        )
        parts.append("")

    if unchecked:
        parts.append(
            f"## Could not be checked — Scryfall request failed ({len(unchecked)})")
        parts.extend(f"- {_identifier_label(ident)}" for ident in unchecked)
        parts.append(f"({errors[0]})")
        parts.append("")

    return "\n".join(parts).rstrip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_card_text.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_card_text.py
git commit -m "scryfall: Add scryfall_card_text bulk oracle text tool"
```

---

### Task 2: `archidekt_deck` `include_text` flag

**Files:**
- Modify: `server.py` — `ArchidektDeckInput` (~line 1371), helpers near `_archidekt_in_deck_cards` (~line 1326), `archidekt_deck` body (~line 1417)
- Test: `tests/test_archidekt_text.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_archidekt_text.py`:

```python
import server


DECK = {
    "name": "Test Deck",
    "deckFormat": 3,
    "owner": {"username": "shawn"},
    "categories": [
        {"name": "Commander", "isPremier": True, "includedInDeck": True},
        {"name": "Enchantments", "isPremier": False, "includedInDeck": True},
    ],
    "cards": [
        {"quantity": 1, "categories": ["Commander"],
         "card": {"oracleCard": {
             "name": "Eriette of the Charmed Apple",
             "manaCost": "{1}{W}{B}",
             "superTypes": ["Legendary"], "types": ["Creature"],
             "subTypes": ["Human", "Warlock"],
             "power": "1", "toughness": "4", "loyalty": None,
             "text": ("At the beginning of your end step, each opponent "
                      "loses 1 life for each Aura you control attached to a "
                      "permanent that player controls."),
             "faces": [],
         }}},
        {"quantity": 1, "categories": ["Enchantments"],
         "card": {"oracleCard": {
             "name": "Gumdrop Poisoner // Tempt with Treats",
             "manaCost": "{2}{B} // {B}",
             "superTypes": [], "types": [], "subTypes": [],
             "power": "", "toughness": "", "loyalty": None,
             "text": "",
             "faces": [
                 {"name": "Gumdrop Poisoner", "manaCost": "{2}{B}",
                  "superTypes": [], "types": ["Creature"],
                  "subTypes": ["Human", "Warlock"],
                  "power": "2", "toughness": "2", "loyalty": None,
                  "text": "Lifelink"},
                 {"name": "Tempt with Treats", "manaCost": "{B}",
                  "superTypes": [], "types": ["Sorcery"],
                  "subTypes": ["Adventure"],
                  "power": "", "toughness": "", "loyalty": None,
                  "text": "Create a Food token."},
             ],
         }}},
    ],
}


def _install_deck(monkeypatch):
    async def fake(path, params=None):
        return DECK
    monkeypatch.setattr(server, "_archidekt_get", fake)


async def test_default_output_has_no_text(monkeypatch):
    _install_deck(monkeypatch)
    out = await server.archidekt_deck(server.ArchidektDeckInput(deck="123"))
    assert "1 [CMDR] Eriette of the Charmed Apple" in out
    assert "loses 1 life" not in out
    assert "{1}{W}{B}" not in out


async def test_include_text_renders_mana_type_pt_text(monkeypatch):
    _install_deck(monkeypatch)
    out = await server.archidekt_deck(
        server.ArchidektDeckInput(deck="123", include_text=True))
    assert "1 [CMDR] Eriette of the Charmed Apple {1}{W}{B}" in out
    assert "Legendary Creature — Human Warlock 1/4" in out
    assert "loses 1 life for each Aura" in out


async def test_include_text_renders_faces(monkeypatch):
    _install_deck(monkeypatch)
    out = await server.archidekt_deck(
        server.ArchidektDeckInput(deck="123", include_text=True))
    assert "Gumdrop Poisoner {2}{B}" in out
    assert "Lifelink" in out
    assert "Sorcery — Adventure" in out
    assert "Create a Food token." in out


async def test_missing_fields_degrade_to_bare_line(monkeypatch):
    bare = {
        "name": "Bare Deck", "deckFormat": 3, "owner": {"username": "s"},
        "categories": [{"name": "Lands", "isPremier": False,
                        "includedInDeck": True}],
        "cards": [{"quantity": 7, "categories": ["Lands"],
                   "card": {"oracleCard": {"name": "Wastes"}}}],
    }

    async def fake(path, params=None):
        return bare
    monkeypatch.setattr(server, "_archidekt_get", fake)
    out = await server.archidekt_deck(
        server.ArchidektDeckInput(deck="123", include_text=True))
    assert "7 Wastes" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_archidekt_text.py -v`
Expected: `test_default_output_has_no_text` PASSES (regression guard); the other three FAIL (`include_text` is not a valid field)

- [ ] **Step 3: Write the implementation**

In `server.py`, add to `ArchidektDeckInput` (~line 1371) after the `deck` field:

```python
    include_text: bool = Field(
        default=False,
        description=(
            "Include mana cost, type line, and full oracle text for every "
            "card. Set true whenever you will discuss what cards do — never "
            "rely on memory for card text."
        ),
    )
```

Add helpers before `archidekt_deck` (near `_archidekt_in_deck_cards`, ~line 1326):

```python
def _archidekt_type_line(oracle: dict) -> str:
    """'Legendary Creature — Human Warlock' from Archidekt's split type fields."""
    left = " ".join([*(oracle.get("superTypes") or []), *(oracle.get("types") or [])])
    subs = " ".join(oracle.get("subTypes") or [])
    return f"{left} — {subs}" if subs else left


def _archidekt_face_lines(face: dict) -> list[str]:
    """Type/PT/loyalty line, then rules text, for one oracleCard or face dict.

    Archidekt reports absent power/toughness as '' (Vehicles do carry real
    values despite not being creatures), and absent loyalty as None.
    """
    lines: list[str] = []
    header = _archidekt_type_line(face)
    power, toughness = face.get("power"), face.get("toughness")
    if power not in (None, "") and toughness not in (None, ""):
        header = f"{header} {power}/{toughness}".strip()
    if face.get("loyalty"):
        header = f"{header} [Loyalty {face['loyalty']}]".strip()
    if header:
        lines.append(header)
    text = (face.get("text") or "").strip()
    if text:
        lines.extend(text.split("\n"))
    return lines


def _archidekt_card_detail(oracle: dict) -> list[str]:
    """Detail lines for include_text; face-by-face for multi-faced cards.

    Multi-faced cards carry empty top-level text and a combined manaCost —
    the real data lives in `faces`.
    """
    faces = oracle.get("faces") or []
    if not faces:
        return _archidekt_face_lines(oracle)
    lines: list[str] = []
    for i, face in enumerate(faces):
        if i:
            lines.append("//")
        lines.append(f"{face.get('name', '?')} {face.get('manaCost') or ''}".strip())
        lines.extend(_archidekt_face_lines(face))
    return lines
```

In `archidekt_deck`, replace the card-line construction inside the
`for cat_name in entry_cats:` loop (currently
`cards_by_cat[cat_name].append(f"{qty} {prefix}{card_name}")`):

```python
            line = f"{qty} {prefix}{card_name}"
            if params.include_text:
                if oracle.get("manaCost"):
                    line += f" {oracle['manaCost']}"
                detail = _archidekt_card_detail(oracle)
                if detail:
                    line += "\n" + "\n".join(f"   {d}" for d in detail)
            cards_by_cat[cat_name].append(line)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_archidekt_text.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_archidekt_text.py
git commit -m "archidekt: Add include_text flag for full oracle text"
```

---

### Task 3: Instruction steering + README

**Files:**
- Modify: `server.py` — `FastMCP(instructions=...)` block (~line 96)
- Modify: `README.md` — tool table

- [ ] **Step 1: Update server instructions**

In the `instructions` string, after the line
`"Use scryfall_search/scryfall_named for card lookups instead of web search. "`,
insert:

```python
        "NEVER state or reason about a card's rules text from memory — many "
        "distinct cards have similar names, and text gets errata'd. Before "
        "discussing what specific cards do, fetch the exact text: "
        "archidekt_deck with include_text=true when the deck is on Archidekt, "
        "scryfall_card_text for any list of card names, scryfall_named for a "
        "single card. "
```

- [ ] **Step 2: Update README tool table**

Add after the `scryfall_price_list` row:

```markdown
| `scryfall_card_text` | Exact oracle text for a whole list of cards in one call |
```

Change the `archidekt_deck` row description to:

```markdown
| `archidekt_deck` | Fetch a public deck by ID or URL (`include_text` adds full oracle text) |
```

- [ ] **Step 3: Sanity-check the server module loads**

Run: `python -c "import server"`
Expected: no output, exit 0

- [ ] **Step 4: Commit**

```bash
git add server.py README.md
git commit -m "server: Steer card text verification to the new tools"
```

---

### Task 4: Full suite, merge, push

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest`
Expected: all tests pass (existing suite + 12 new)

- [ ] **Step 2: Merge into main and push**

The primary checkout holds `main`; this worktree holds `accurate-card-text`.

```bash
git worktree list                       # find the primary checkout path
cd <primary-checkout>
git merge accurate-card-text
git push
```

Expected: fast-forward or clean merge, push succeeds.
