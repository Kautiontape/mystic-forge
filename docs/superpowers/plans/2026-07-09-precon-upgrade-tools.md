# Precon Upgrade Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two MCP tools to Mystic Forge — `edhrec_precon_upgrade` (EDHREC community cut/add stats for a precon) and `precon_diff` (exact cut/added between an MTGJSON precon and a specific deck) — so precon upgrade data is real and citable instead of hallucinated.

**Architecture:** Both tools are plain `async def` functions decorated with `@mcp.tool` in the single-file `server.py`, following the existing Scryfall/EDHREC/MTGJSON patterns. `edhrec_precon_upgrade` resolves a precon name to an EDHREC slug through a cached index with a difflib confidence gate, then reads `json.edhrec.com/pages/precon/<slug>[/<commander>].json`. `precon_diff` pulls the precon baseline from MTGJSON and the upgraded side from Archidekt or a pasted list, then set-diffs by normalized name with basic lands separated.

**Tech Stack:** Python 3, `mcp` (FastMCP), `httpx`, `pydantic` v2, stdlib `difflib`; tests use `pytest` + `pytest-asyncio` with monkeypatched fetchers (no live network).

**Spec:** `docs/superpowers/specs/2026-07-09-precon-upgrade-tools-design.md`

---

## File Structure

- **Modify `server.py`:**
  - Imports: add `from difflib import SequenceMatcher`.
  - Constants: add `MATCH_THRESHOLD`, `MATCH_MARGIN`.
  - EDHREC section (after `edhrec_salt`, before the `ARCHIDEKT` banner ~line 750): add `_precon_index_cache`, `_get_precon_index`, `_match_score`, `_resolve_precon_slug`, `_precon_candidates_message`, `PreconUpgradeInput`, and the `edhrec_precon_upgrade` tool.
  - Archidekt section (after `_archidekt_error` ~line 778): add `_archidekt_in_deck_cards`.
  - PRECON section (after `precon_export`, before the `ENTRYPOINT` banner ~line 1826): add `BASIC_LANDS`, `_diff_key`, `_canonical_names`, `PreconDiffInput`, and the `precon_diff` tool.
  - `FastMCP(instructions=...)` string (~line 37): add a steering line.
- **Create `conftest.py`** (repo root, empty) — makes `import server` work from tests.
- **Create `pytest.ini`** — `asyncio_mode = auto`.
- **Create `requirements-dev.txt`** — pytest + pytest-asyncio.
- **Create `tests/test_precon_upgrade.py`** — fixture-based tests for `edhrec_precon_upgrade`.
- **Create `tests/test_precon_diff.py`** — fixture-based tests for `precon_diff`.
- **Modify `README.md`** — document the two tools.

Anchor edits by the section banner comments and function names shown above; line numbers are relative to the base commit and will drift as you edit.

---

## Task 1: Test scaffolding

**Files:**
- Create: `pytest.ini`
- Create: `conftest.py`
- Create: `requirements-dev.txt`

- [ ] **Step 1: Create `pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 2: Create `conftest.py`** (repo root)

Explicitly puts the repo root on `sys.path` so tests can `import server` regardless of pytest import mode.

```python
import os
import sys

# Make the repo-root modules (server.py) importable from tests/.
sys.path.insert(0, os.path.dirname(__file__))
```

- [ ] **Step 3: Create `requirements-dev.txt`**

```
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

- [ ] **Step 4: Verify pytest collects nothing yet (no error)**

Run: `python3 -m pytest -q`
Expected: exits 0 (or "no tests ran"), no import/config errors.

- [ ] **Step 5: Commit**

```bash
git add pytest.ini conftest.py requirements-dev.txt
git commit -m "precon: Add pytest scaffolding for precon tools"
```

---

## Task 2: Imports and constants

**Files:**
- Modify: `server.py` (imports block ~lines 12-20; constants block ~lines 24-31)

- [ ] **Step 1: Add the `difflib` import**

Find:

```python
from collections import Counter

import httpx
```

Replace with:

```python
from collections import Counter
from difflib import SequenceMatcher

import httpx
```

- [ ] **Step 2: Add match-gate constants**

Find:

```python
USER_AGENT = "MysticForge/1.0"
REQUEST_TIMEOUT = 15.0
```

Replace with:

```python
USER_AGENT = "MysticForge/1.0"
REQUEST_TIMEOUT = 15.0

# Precon name → slug fuzzy-match gate (Decision D3). Tunable.
MATCH_THRESHOLD = 0.72   # min difflib ratio to accept a match
MATCH_MARGIN = 0.08      # min lead over the runner-up to accept without ambiguity
```

- [ ] **Step 3: Verify the module still imports**

Run: `python3 -c "import server; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "precon: Add difflib import and match-gate constants"
```

---

## Task 3: Precon slug resolution helpers

Confidence-gated resolution of a precon name/slug against EDHREC's cached index.

**Files:**
- Modify: `server.py` (EDHREC section, after `edhrec_salt`, before the `ARCHIDEKT` banner)
- Create: `tests/test_precon_upgrade.py`

- [ ] **Step 1: Write the failing tests for resolution**

Create `tests/test_precon_upgrade.py`:

```python
import httpx
import pytest
import server

# ── Fixtures: real-shaped EDHREC precon JSON (trimmed) ────────────────────────

INDEX = {
    "header": "Precon Upgrade Guide",
    "container": {"json_dict": {"cardlists": [
        {"tag": "setA", "cardviews": [
            {"name": "World Shaper", "url": "/precon/world-shaper"},
            {"name": "Avengers Assemble", "url": "/precon/avengers-assemble"},
            {"name": "Doom Prevails", "url": "/precon/doom-prevails"},
        ]},
    ]}},
}

BASE = {
    "header": "World Shaper Precon",
    "deck": {"commander": ["Szarel, Genesis Shepherd"], "cards": {}},
    "precon_commander_counts": [
        {"value": "Hearthhull, the Worldseed", "count": 12322,
         "href": "/precon/world-shaper/hearthhull-the-worldseed"},
        {"value": "Szarel, Genesis Shepherd", "count": 2764,
         "href": "/precon/world-shaper"},
    ],
    "container": {"json_dict": {"cardlists": [
        {"tag": "cardstoadd", "header": "Cards to Add", "cardviews": [
            {"name": "Icetill Explorer", "inclusion": 1143, "num_decks": 1143,
             "potential_decks": 2737, "synergy": 0.2346},
        ]},
        {"tag": "landstoadd", "header": "Lands to Add", "cardviews": [
            {"name": "Stomping Ground", "inclusion": 593, "num_decks": 593,
             "potential_decks": 2764, "synergy": -0.281},
        ]},
        {"tag": "cardstocut", "header": "Cards to Cut", "cardviews": [
            {"name": "World Breaker", "inclusion": 0, "num_decks": 0,
             "potential_decks": 0, "unpopularity": 0.7152},
        ]},
        {"tag": "landstocut", "header": "Lands to Cut", "cardviews": [
            {"name": "Ancient Tomb", "inclusion": 0, "num_decks": 0,
             "potential_decks": 0, "unpopularity": 0.51},
        ]},
    ]}},
}

SUB = {
    "header": "World Shaper Precon Upgrades for Hearthhull, the Worldseed",
    "deck": {"commander": ["Szarel, Genesis Shepherd"], "cards": {}},
    "precon_commander_counts": BASE["precon_commander_counts"],
    "container": {"json_dict": {"cardlists": [
        {"tag": "cardstoadd", "header": "Cards to Add", "cardviews": [
            {"name": "Cultivate", "inclusion": 900, "num_decks": 900,
             "potential_decks": 1200, "synergy": 0.1},
        ]},
        {"tag": "cardstocut", "header": "Cards to Cut", "cardviews": [
            {"name": "World Breaker", "inclusion": 0, "num_decks": 0,
             "potential_decks": 0, "unpopularity": 0.6},
        ]},
    ]}},
}


def _fake_edhrec_get(monkeypatch):
    async def fake(path):
        if path == "/pages/precon.json":
            return INDEX
        if path == "/pages/precon/world-shaper.json":
            return BASE
        if path == "/pages/precon/world-shaper/hearthhull-the-worldseed.json":
            return SUB
        req = httpx.Request("GET", "http://test")
        raise httpx.HTTPStatusError("not found", request=req,
                                    response=httpx.Response(403, request=req))
    monkeypatch.setattr(server, "_edhrec_get", fake)


# ── Resolution tests ──────────────────────────────────────────────────────────

async def test_resolve_exact_name(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    kind, val = await server._resolve_precon_slug("World Shaper")
    assert kind == "slug"
    assert val == "world-shaper"


async def test_resolve_low_confidence_returns_candidates(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    kind, val = await server._resolve_precon_slug("Zzqx Nonsense Foo")
    assert kind == "candidates"
    assert isinstance(val, list) and len(val) >= 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_precon_upgrade.py -q`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_resolve_precon_slug'`.

- [ ] **Step 3: Implement the resolution helpers**

In `server.py`, immediately after the `edhrec_salt` function (before the `# ═══ ARCHIDEKT` banner), add:

```python
# ── Precon index (EDHREC) ─────────────────────────────────────────────────────

_precon_index_cache: dict[str, Any] = {"data": None, "expires_at": 0.0}


async def _get_precon_index() -> list[dict]:
    """Fetch and cache EDHREC's precon index as a list of {name, slug} (24h TTL)."""
    if _precon_index_cache["data"] and time.time() < _precon_index_cache["expires_at"]:
        return _precon_index_cache["data"]
    data = await _edhrec_get("/pages/precon.json")
    entries: list[dict] = []
    for cl in data.get("container", {}).get("json_dict", {}).get("cardlists", []):
        for cv in cl.get("cardviews", []):
            name = cv.get("name", "")
            url = cv.get("url", "")
            slug = url.rsplit("/", 1)[-1] if url else _sanitize(name)
            if name and slug:
                entries.append({"name": name, "slug": slug})
    _precon_index_cache["data"] = entries
    _precon_index_cache["expires_at"] = time.time() + 86400
    return entries


def _match_score(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


async def _resolve_precon_slug(query: str) -> tuple[str, Any]:
    """Resolve a precon name/slug to an EDHREC slug (Decision D3).

    Returns ("slug", slug) on a confident match, or ("candidates", [names])
    when the match is low-confidence or ambiguous.
    """
    index = await _get_precon_index()
    q = _sanitize(query)

    # Exact slug or exact name match → confident
    for e in index:
        if _sanitize(e["slug"]) == q or _sanitize(e["name"]) == q:
            return ("slug", e["slug"])

    # Unique substring containment → confident
    if q:
        contains = [e for e in index if q in _sanitize(e["name"])]
        if len(contains) == 1:
            return ("slug", contains[0]["slug"])

    # Fuzzy score with a threshold + margin gate
    scored = sorted(
        ((_match_score(q, _sanitize(e["name"])), e) for e in index),
        key=lambda t: t[0], reverse=True,
    )
    if not scored:
        return ("candidates", [])
    best_score, best = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= MATCH_THRESHOLD and (best_score - runner) >= MATCH_MARGIN:
        return ("slug", best["slug"])
    return ("candidates", [e["name"] for _, e in scored[:5]])


def _precon_candidates_message(query: str, names: list[str]) -> str:
    if not names:
        return (f"No precon matched '{query}'. Try precon_search for the MTGJSON "
                f"name, or check the precon's name on EDHREC.")
    lines = [f"No confident precon match for '{query}'. Closest matches — "
             f"call again with one of these:", ""]
    lines += [f'- {name}  →  precon="{name}"' for name in names]
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_precon_upgrade.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_precon_upgrade.py
git commit -m "precon: Add confidence-gated EDHREC slug resolution"
```

---

## Task 4: `edhrec_precon_upgrade` tool

**Files:**
- Modify: `server.py` (EDHREC section, after the resolution helpers from Task 3)
- Modify: `tests/test_precon_upgrade.py` (append tool tests)

- [ ] **Step 1: Write the failing tool tests**

Append to `tests/test_precon_upgrade.py`:

```python
# ── Tool tests ────────────────────────────────────────────────────────────────

async def test_adds_show_real_percentage(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    out = await server.edhrec_precon_upgrade(
        server.PreconUpgradeInput(precon="World Shaper"))
    assert "Cards to Add" in out
    assert "Icetill Explorer" in out
    assert "42%" in out                 # 1143 / 2737
    assert "1143 of 2737 decks" in out


async def test_cuts_have_no_percentage(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    out = await server.edhrec_precon_upgrade(
        server.PreconUpgradeInput(precon="World Shaper"))
    assert "Cards to Cut" in out
    # The cut card appears, but its line carries no "%".
    cut_line = next(ln for ln in out.splitlines() if "World Breaker" in ln)
    assert "%" not in cut_line
    assert "unpopularity" in out.lower()          # the honest-labeling note


async def test_lists_both_commanders(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    out = await server.edhrec_precon_upgrade(
        server.PreconUpgradeInput(precon="World Shaper"))
    assert "Hearthhull, the Worldseed" in out
    assert "Szarel, Genesis Shepherd" in out
    assert "12322 decks" in out


async def test_commander_param_fetches_subpage(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    out = await server.edhrec_precon_upgrade(
        server.PreconUpgradeInput(precon="World Shaper",
                                  commander="Hearthhull, the Worldseed"))
    assert "Hearthhull" in out
    assert "Cultivate" in out           # SUB-page's add card
    assert "Icetill Explorer" not in out


async def test_slug_fast_path(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    out = await server.edhrec_precon_upgrade(
        server.PreconUpgradeInput(precon="world-shaper"))
    assert "Icetill Explorer" in out


async def test_low_confidence_query_returns_candidates(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    out = await server.edhrec_precon_upgrade(
        server.PreconUpgradeInput(precon="Zzqx Nonsense Foo"))
    assert "No confident precon match" in out


async def test_provenance_footer(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    out = await server.edhrec_precon_upgrade(
        server.PreconUpgradeInput(precon="World Shaper"))
    assert "edhrec.com/precon/world-shaper" in out
    assert "Based on 2764 decks" in out            # default commander = Szarel
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_precon_upgrade.py -q`
Expected: FAIL — `AttributeError: module 'server' has no attribute 'PreconUpgradeInput'`.

- [ ] **Step 3: Implement the input model and tool**

In `server.py`, directly after `_precon_candidates_message` (still in the EDHREC section), add:

```python
class PreconUpgradeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    precon: str = Field(
        ...,
        description="Precon name or slug (e.g. 'World Shaper' or 'world-shaper').",
        min_length=1, max_length=200,
    )
    commander: Optional[str] = Field(
        default=None,
        description="Face commander to view when the precon builds as several "
                    "(e.g. 'Hearthhull, the Worldseed').",
    )
    limit: int = Field(default=20, ge=1, le=50, description="Max cards per section.")


@mcp.tool(name="edhrec_precon_upgrade")
async def edhrec_precon_upgrade(params: PreconUpgradeInput) -> str:
    """Get how a preconstructed deck is commonly upgraded, from EDHRec's Precon
    Upgrade Guide. Use this INSTEAD of web search or memory for "what cards are
    cut/added" questions.

    Returns the most-added cards/lands with real inclusion percentages, and the
    most-cut cards/lands as a ranked list (EDHRec's unpopularity score, not a
    percentage). Precons that build as multiple commanders are handled explicitly;
    pass `commander` to switch faces. For an exact cut percentage against a
    specific deck, use precon_diff.
    """
    query = params.precon.strip()

    # 1. Resolve to a base slug (fast path for literal slugs, else gated resolve).
    slug: Optional[str] = None
    if re.fullmatch(r"[a-z0-9-]+", query):
        slug = query  # optimistic; validated by the fetch below
    else:
        try:
            kind, val = await _resolve_precon_slug(query)
        except Exception as e:
            return _edhrec_error(e)
        if kind == "candidates":
            return _precon_candidates_message(params.precon, val)
        slug = val

    # 2. Fetch the base page; if an optimistic slug 403/404s, fall back to resolve.
    try:
        page = await _edhrec_get(f"/pages/precon/{slug}.json")
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (403, 404):
            try:
                kind, val = await _resolve_precon_slug(query)
            except Exception as e2:
                return _edhrec_error(e2)
            if kind == "candidates":
                return _precon_candidates_message(params.precon, val)
            slug = val
            try:
                page = await _edhrec_get(f"/pages/precon/{slug}.json")
            except Exception as e2:
                return _edhrec_error(e2)
        else:
            return _edhrec_error(e)
    except Exception as e:
        return _edhrec_error(e)

    commander_counts = page.get("precon_commander_counts", []) or []
    base_href = f"/precon/{slug}"
    active_href = base_href
    selected = page

    # 3. Optional face-commander switch → fetch the sub-page.
    if params.commander and commander_counts:
        cq = _sanitize(params.commander)
        best_c, best_s = None, 0.0
        for c in commander_counts:
            s = _match_score(cq, _sanitize(c.get("value", "")))
            if s > best_s:
                best_s, best_c = s, c
        if best_c and best_s >= MATCH_THRESHOLD:
            href = best_c.get("href", "")
            if href and href != base_href and href.startswith("/precon/"):
                sub = href[len("/precon/"):]
                try:
                    selected = await _edhrec_get(f"/pages/precon/{sub}.json")
                    active_href = href
                except Exception as e:
                    return _edhrec_error(e)

    # Which commander do the shown tables describe?
    active_commander = next(
        (c for c in commander_counts if c.get("href") == active_href), None)

    # 4. Render.
    parts: list[str] = []
    parts.append(f"# {selected.get('header', 'Precon Upgrade')}")
    if commander_counts:
        parts.append("")
        if len(commander_counts) > 1:
            parts.append("**Commanders in this precon:**")
            for c in commander_counts:
                mark = "  ← shown below" if c.get("href") == active_href else ""
                parts.append(f"- {c.get('value', '?')} "
                             f"({c.get('count', '?')} decks){mark}")
            if not params.commander:
                parts.append("")
                parts.append('_Pass `commander="<name>"` to see another '
                             'commander\'s upgrades._')
    parts.append("")

    cardlists = {cl.get("tag"): cl for cl in
                 selected.get("container", {}).get("json_dict", {}).get("cardlists", [])}

    for tag, title in (("cardstoadd", "Cards to Add"), ("landstoadd", "Lands to Add")):
        cl = cardlists.get(tag)
        if cl and cl.get("cardviews"):
            parts.append(f"## {title}")
            for cv in cl["cardviews"][:params.limit]:
                inc, pot = cv.get("inclusion"), cv.get("potential_decks")
                pct = _pct(inc, pot) if inc is not None and pot else "?%"
                line = f"- **{cv.get('name', '?')}** — {pct}"
                if inc is not None and pot:
                    line += f" ({inc} of {pot} decks)"
                syn = cv.get("synergy")
                if syn is not None:
                    line += f" | synergy {syn:+.0%}"
                parts.append(line)
            parts.append("")

    any_cuts = False
    for tag, title in (("cardstocut", "Cards to Cut"), ("landstocut", "Lands to Cut")):
        cl = cardlists.get(tag)
        if cl and cl.get("cardviews"):
            any_cuts = True
            parts.append(f"## {title} (ranked by how often they're cut)")
            for cv in cl["cardviews"][:params.limit]:
                score = cv.get("unpopularity")
                suffix = (f" — cut-frequency score {score:.2f}"
                          if isinstance(score, (int, float)) else "")
                parts.append(f"- **{cv.get('name', '?')}**{suffix}")
            parts.append("")

    if any_cuts:
        parts.append("_Cut ranking is EDHRec's unpopularity score (higher = cut "
                     "more often), not a percentage. For an exact cut percentage "
                     "against a specific deck, use precon_diff._")
        parts.append("")

    parts.append("---")
    footer = f"Source: https://edhrec.com{active_href}"
    if active_commander and active_commander.get("count") is not None:
        footer += f" | Based on {active_commander['count']} decks"
    parts.append(footer)

    return "\n".join(parts)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_precon_upgrade.py -q`
Expected: all tests pass (2 from Task 3 + 7 here = 9 passed).

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_precon_upgrade.py
git commit -m "precon: Add edhrec_precon_upgrade tool"
```

---

## Task 5: Diff helpers

**Files:**
- Modify: `server.py` (Archidekt section for `_archidekt_in_deck_cards`; PRECON section for `BASIC_LANDS`, `_diff_key`, `_canonical_names`)
- Create: `tests/test_precon_diff.py`

- [ ] **Step 1: Write failing helper tests**

Create `tests/test_precon_diff.py`:

```python
import pytest
import server


def test_diff_key_normalizes_case_and_space():
    assert server._diff_key("  Sol   Ring ") == "sol ring"
    assert server._diff_key("SOL RING") == server._diff_key("sol ring")


def test_archidekt_in_deck_cards_excludes_maybeboard():
    data = {
        "categories": [
            {"name": "Commander", "isPremier": True, "includedInDeck": True},
            {"name": "Ramp", "isPremier": False, "includedInDeck": True},
            {"name": "Maybeboard", "isPremier": False, "includedInDeck": False},
        ],
        "cards": [
            {"quantity": 1, "categories": ["Commander"],
             "card": {"oracleCard": {"name": "Test Commander"}}},
            {"quantity": 1, "categories": ["Ramp"],
             "card": {"oracleCard": {"name": "Sol Ring"}}},
            {"quantity": 1, "categories": ["Maybeboard"],
             "card": {"oracleCard": {"name": "Mana Crypt"}}},
            {"quantity": 7, "categories": [],
             "card": {"oracleCard": {"name": "Forest"}}},
        ],
    }
    out = server._archidekt_in_deck_cards(data)
    assert out == {"Test Commander": 1, "Sol Ring": 1, "Forest": 7}
    assert "Mana Crypt" not in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_precon_diff.py -q`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_diff_key'`.

- [ ] **Step 3: Implement `_archidekt_in_deck_cards`**

In `server.py`, in the ARCHIDEKT section, directly after `_archidekt_error`, add:

```python
def _archidekt_in_deck_cards(data: dict) -> dict[str, int]:
    """Return {card_name: quantity} for cards in the deck (commander included,
    maybeboard excluded). Mirrors the inclusion logic used by archidekt_deck."""
    categories = {c["name"]: c for c in data.get("categories", [])}
    counts: dict[str, int] = {}
    for entry in data.get("cards", []):
        qty = entry.get("quantity", 1)
        name = entry.get("card", {}).get("oracleCard", {}).get("name", "?")
        entry_cats = entry.get("categories", [])
        if entry_cats:
            in_deck = any(
                categories.get(cn, {}).get("isPremier", False)
                or categories.get(cn, {}).get("includedInDeck", True)
                for cn in entry_cats
            )
        else:
            in_deck = True  # uncategorized cards default into the deck
        if in_deck:
            counts[name] = counts.get(name, 0) + qty
    return counts
```

- [ ] **Step 4: Implement `BASIC_LANDS`, `_diff_key`, `_canonical_names`**

In `server.py`, in the PRECON section, directly after `_get_deck_list` (before `# ── Precon Input Models`), add:

```python
BASIC_LANDS = {
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest", "Snow-Covered Wastes",
}


def _diff_key(name: str) -> str:
    """Normalized comparison key for card names (case/whitespace-insensitive)."""
    return re.sub(r"\s+", " ", name).strip().lower()


async def _canonical_names(names: list[str]) -> Optional[dict[str, str]]:
    """Map _diff_key(name) → Scryfall canonical name for recognized cards.

    Returns None if the Scryfall lookup fails entirely (caller falls back to raw
    names). Names absent from the returned map are unrecognized by Scryfall.
    """
    mapping: dict[str, str] = {}
    try:
        for i in range(0, len(names), 75):
            batch = names[i:i + 75]
            data = await _scryfall_post(
                "/cards/collection",
                {"identifiers": [{"name": n} for n in batch]},
            )
            for card in data.get("data", []):
                cname = card.get("name", "")
                if cname:
                    mapping[_diff_key(cname)] = cname
    except Exception:
        return None
    return mapping
```

- [ ] **Step 5: Run to verify helper tests pass**

Run: `python3 -m pytest tests/test_precon_diff.py -q`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_precon_diff.py
git commit -m "precon: Add diff helpers (name normalization, in-deck extraction)"
```

---

## Task 6: `precon_diff` tool

**Files:**
- Modify: `server.py` (PRECON section, after `precon_export`, before the `ENTRYPOINT` banner)
- Modify: `tests/test_precon_diff.py` (append tool tests)

- [ ] **Step 1: Write the failing tool tests**

Append to `tests/test_precon_diff.py`:

```python
# ── precon_diff tool ──────────────────────────────────────────────────────────

PRECON = {"data": {
    "name": "Test Precon", "code": "TST", "type": "Commander Deck",
    "commander": [{"name": "Test Commander", "count": 1}],
    "mainBoard": [
        {"name": "Sol Ring", "count": 1},
        {"name": "Arcane Signet", "count": 1},
        {"name": "Cultivate", "count": 1},
        {"name": "Forest", "count": 10},
        {"name": "Mountain", "count": 8},
    ],
    "sideBoard": [],
}}

UPGRADED_LIST = """1 Test Commander
1 Sol Ring
1 Arcane Signet
1 Rampant Growth
12 Forest
6 Mountain"""


def _fake_mtgjson(monkeypatch):
    async def fake(path):
        assert path == "/decks/TestPrecon.json"
        return PRECON
    monkeypatch.setattr(server, "_mtgjson_get", fake)


async def test_diff_added_cut_kept(monkeypatch):
    _fake_mtgjson(monkeypatch)
    out = await server.precon_diff(server.PreconDiffInput(
        file_name="TestPrecon", decklist=UPGRADED_LIST, canonicalize=False))
    assert "## Added (1)" in out
    assert "Rampant Growth" in out
    assert "## Cut (1)" in out
    assert "Cultivate" in out
    assert "Kept: 3" in out
    # Non-basic cut of 1 out of a 22-card precon = 5%.
    assert "Cut: 1 (5% of precon)" in out


async def test_basics_reported_separately(monkeypatch):
    _fake_mtgjson(monkeypatch)
    out = await server.precon_diff(server.PreconDiffInput(
        file_name="TestPrecon", decklist=UPGRADED_LIST, canonicalize=False))
    assert "Basic land changes" in out
    assert "Forest: 10 → 12" in out
    assert "Mountain: 8 → 6" in out
    # Basics must not appear in the meaningful add/cut lists.
    added_cut = out.split("Basic land changes")[0]
    assert "Forest" not in added_cut
    assert "Mountain" not in added_cut


async def test_requires_exactly_one_source(monkeypatch):
    _fake_mtgjson(monkeypatch)
    both = await server.precon_diff(server.PreconDiffInput(
        file_name="TestPrecon", deck="123", decklist=UPGRADED_LIST))
    assert "exactly one" in both.lower()
    neither = await server.precon_diff(server.PreconDiffInput(file_name="TestPrecon"))
    assert "exactly one" in neither.lower()


async def test_canonicalize_flags_unknown(monkeypatch):
    _fake_mtgjson(monkeypatch)

    async def fake_scryfall_post(endpoint, body):
        # Recognize every real card; leave the bogus one unmatched.
        known = {"Test Commander", "Sol Ring", "Arcane Signet", "Cultivate",
                 "Rampant Growth", "Forest", "Mountain"}
        wanted = {i["name"] for i in body["identifiers"]}
        return {"data": [{"name": n} for n in wanted & known],
                "not_found": [{"name": n} for n in wanted - known]}

    monkeypatch.setattr(server, "_scryfall_post", fake_scryfall_post)
    listing = UPGRADED_LIST + "\n1 Notarealcard Xyz"
    out = await server.precon_diff(server.PreconDiffInput(
        file_name="TestPrecon", decklist=listing, canonicalize=True))
    assert "Notarealcard Xyz" in out
    assert "not recognized" in out.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_precon_diff.py -q`
Expected: FAIL — `AttributeError: module 'server' has no attribute 'PreconDiffInput'`.

- [ ] **Step 3: Implement the input model and tool**

In `server.py`, directly after `precon_export` (before the `# ═══ ENTRYPOINT` banner), add:

```python
class PreconDiffInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    file_name: str = Field(
        ...,
        description="MTGJSON precon fileName from precon_search (e.g. 'WorldShaper_TDC').",
        min_length=1,
    )
    deck: Optional[str] = Field(
        default=None,
        description="Upgraded deck as an Archidekt deck ID or URL.",
    )
    decklist: Optional[str] = Field(
        default=None,
        description="OR the upgraded deck as a pasted list ('1 Card Name' per line).",
    )
    canonicalize: bool = Field(
        default=True,
        description="Normalize display names via Scryfall and flag unrecognized "
                    "cards. Matching is always case-insensitive.",
    )


@mcp.tool(name="precon_diff")
async def precon_diff(params: PreconDiffInput) -> str:
    """Compute the exact cards cut and added between a preconstructed deck and a
    specific upgraded deck. Use this INSTEAD of estimating from memory when a user
    wants a real cut/added percentage for one deck.

    The precon baseline comes from MTGJSON (use precon_search for the fileName).
    Provide the upgraded deck as either an Archidekt deck (`deck`) or a pasted
    `decklist`. Basic lands are reported separately so routine mana-base churn
    doesn't distort the numbers.
    """
    if bool(params.deck) == bool(params.decklist):
        return ("Provide exactly one of `deck` (Archidekt ID/URL) or `decklist` "
                "(pasted list).")

    # Precon baseline (MTGJSON).
    try:
        pdata = await _mtgjson_get(f"/decks/{params.file_name}.json")
    except Exception as e:
        return _mtgjson_error(e)
    deck_obj = pdata.get("data", {})
    precon: dict[str, int] = {}
    for card in deck_obj.get("commander", []) + deck_obj.get("mainBoard", []):
        name = card.get("name", "?")
        precon[name] = precon.get(name, 0) + card.get("count", 1)

    # Upgraded side.
    if params.deck:
        deck_id = _parse_deck_id(params.deck)
        try:
            adata = await _archidekt_get(f"/decks/{deck_id}/")
        except Exception as e:
            return _archidekt_error(e)
        upgraded = _archidekt_in_deck_cards(adata)
        upgraded_src = f"Archidekt deck {deck_id}"
    else:
        upgraded = {}
        for qty, name in _parse_decklist(params.decklist):
            upgraded[name] = upgraded.get(name, 0) + qty
        upgraded_src = "pasted decklist"

    # Optional Scryfall canonicalization (display + unknown-card flagging).
    canon: Optional[dict[str, str]] = None
    unknown: list[str] = []
    canon_note = ""
    if params.canonicalize:
        canon = await _canonical_names(list({*precon, *upgraded}))
        if canon is None:
            canon_note = ("_Name canonicalization unavailable — matched on raw "
                          "names._")
        else:
            for n in {*precon, *upgraded}:
                if _diff_key(n) not in canon:
                    unknown.append(n)

    def display(name: str) -> str:
        if canon:
            return canon.get(_diff_key(name), name)
        return name

    # Split basics out, then diff non-basics by normalized key.
    def split(counts: dict[str, int]):
        main, basics = {}, {}
        for n, c in counts.items():
            (basics if n in BASIC_LANDS else main)[n] = c
        return main, basics

    precon_main, precon_basics = split(precon)
    upgraded_main, upgraded_basics = split(upgraded)

    precon_keys = {_diff_key(n): n for n in precon_main}
    upgraded_keys = {_diff_key(n): n for n in upgraded_main}
    added = sorted(display(upgraded_keys[k])
                   for k in set(upgraded_keys) - set(precon_keys))
    cut = sorted(display(precon_keys[k])
                 for k in set(precon_keys) - set(upgraded_keys))
    kept = set(precon_keys) & set(upgraded_keys)

    precon_size = sum(precon.values())
    pct = lambda n: (round(n / precon_size * 100) if precon_size else 0)

    parts: list[str] = []
    parts.append(f"# Precon Diff: {deck_obj.get('name', params.file_name)}")
    parts.append(f"Baseline: {deck_obj.get('name', '?')} "
                 f"({deck_obj.get('code', '?')})  vs  {upgraded_src}")
    parts.append("")

    parts.append(f"## Added ({len(added)})")
    parts.extend(f"- {n}" for n in added) if added else parts.append("- (none)")
    parts.append("")

    parts.append(f"## Cut ({len(cut)})")
    parts.extend(f"- {n}" for n in cut) if cut else parts.append("- (none)")
    parts.append("")

    parts.append("## Summary")
    parts.append(f"- Precon size: {precon_size} cards")
    parts.append(f"- Cut: {len(cut)} ({pct(len(cut))}% of precon)")
    parts.append(f"- Added: {len(added)} ({pct(len(added))}% of precon)")
    parts.append(f"- Kept: {len(kept)}")
    parts.append("- (Basic lands excluded above and listed separately.)")

    basic_names = sorted(set(precon_basics) | set(upgraded_basics))
    basic_changes = [(n, precon_basics.get(n, 0), upgraded_basics.get(n, 0))
                     for n in basic_names
                     if precon_basics.get(n, 0) != upgraded_basics.get(n, 0)]
    if basic_changes:
        parts.append("")
        parts.append("## Basic land changes")
        for n, b, a in basic_changes:
            parts.append(f"- {n}: {b} → {a}")

    if unknown:
        parts.append("")
        parts.append("## Not recognized by Scryfall")
        parts.append("These names were diffed as-is and may be typos:")
        parts.extend(f"- {n}" for n in sorted(unknown))

    parts.append("")
    parts.append("---")
    parts.append(f"Precon source: MTGJSON /decks/{params.file_name}.json | "
                 f"Upgraded: {upgraded_src}")
    if canon_note:
        parts.append(canon_note)

    return "\n".join(parts)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_precon_diff.py -q`
Expected: all pass (2 helper tests + 4 tool tests = 6 passed).

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_precon_diff.py
git commit -m "precon: Add precon_diff tool"
```

---

## Task 7: Wire-up — server instructions and README

**Files:**
- Modify: `server.py` (`FastMCP(instructions=...)` ~lines 37-52)
- Modify: `README.md`

- [ ] **Step 1: Update the server instructions string**

Find:

```python
        "Use spellbook_combos/spellbook_card_combos for combo lookups instead of web search or memory. "
        "Use precon_search + precon_decklist for precon decklists instead of web search. "
```

Replace with:

```python
        "Use spellbook_combos/spellbook_card_combos for combo lookups instead of web search or memory. "
        "Use precon_search + precon_decklist for precon decklists instead of web search. "
        "Use edhrec_precon_upgrade for how a precon is commonly upgraded (which cards "
        "the community cuts/adds and how often), and precon_diff for the exact cut/added "
        "cards between a precon and a specific deck. Never estimate cut/added percentages from memory. "
```

- [ ] **Step 2: Update the README tools table**

Find the EDHRec table row:

```markdown
| `edhrec_average_deck` | Average decklist for a commander |
```

Insert after it:

```markdown
| `edhrec_precon_upgrade` | Community cut/added cards for a precon (real add %, ranked cuts) |
```

Then find the precon-related tools. If the README has no precon section yet, add one after the Validation section:

```markdown
### Precon Decks (MTGJSON + EDHRec)
| Tool | Description |
|---|---|
| `precon_search` | Search official precons by name or set code (MTGJSON) |
| `precon_decklist` | Full official contents of a precon (MTGJSON) |
| `precon_export` | Export a precon in Archidekt import format |
| `precon_diff` | Exact cut/added cards between a precon and a specific deck |
```

- [ ] **Step 3: Verify module imports and full suite passes**

Run: `python3 -c "import server; print('ok')" && python3 -m pytest -q`
Expected: `ok`, then all tests pass (9 + 6 = 15 passed).

- [ ] **Step 4: Commit**

```bash
git add server.py README.md
git commit -m "precon: Document upgrade/diff tools and steer model to them"
```

---

## Task 8: Final verification

- [ ] **Step 1: Full suite, verbose**

Run: `python3 -m pytest -v`
Expected: 15 passed, 0 failed.

- [ ] **Step 2: Smoke-test the module loads and tools are registered**

Run:

```bash
python3 -c "import server; import asyncio; \
print('edhrec_precon_upgrade' , asyncio.iscoroutinefunction(server.edhrec_precon_upgrade)); \
print('precon_diff', asyncio.iscoroutinefunction(server.precon_diff))"
```

Expected: both print `True`.

- [ ] **Step 3 (optional): Live sanity check against EDHREC**

Only if a network check is desired; not part of the automated suite.

```bash
python3 -c "import server, asyncio; \
print(asyncio.run(server.edhrec_precon_upgrade(server.PreconUpgradeInput(precon='World Shaper')))[:400])"
```

Expected: real World Shaper add cards with percentages; cuts without percentages.

---

## Self-Review Notes

- **Spec coverage:** `edhrec_precon_upgrade` (Task 4) covers the EDHREC upgrade tool incl. add %, ranked cuts (D1), alt-commander awareness, provenance footer; confidence gate (D3) in Task 3. `precon_diff` (Task 6) covers the two-list diff, basic-land separation, provenance. Instructions steering + README in Task 7. All spec sections map to a task.
- **Cuts never carry a `%`** (D1) — enforced and asserted by `test_cuts_have_no_percentage`.
- **Identifier systems stay separate** (D2) — `edhrec_precon_upgrade` uses EDHREC slugs; `precon_diff` takes an MTGJSON `file_name`. No unification.
- **Type/name consistency:** `PreconUpgradeInput`, `PreconDiffInput`, `_resolve_precon_slug`, `_get_precon_index`, `_precon_candidates_message`, `_archidekt_in_deck_cards`, `_diff_key`, `_canonical_names`, `BASIC_LANDS`, `MATCH_THRESHOLD`, `MATCH_MARGIN` are defined once and referenced consistently across tasks.
</content>
