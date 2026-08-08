"""Server-tool tests for the goldfish MCP surface (Task 18).

The shared server-tool helpers (``MINI_DECK_TEXT``, ``_patch_fetch_minideck``,
``_fake_fetch_for``) live in tests/goldfish/helpers.py — the pinned
consolidation home — for reuse by later server-tool test modules
(test_interactive.py, test_acceptance.py).
"""
import pydantic
import pytest

import server as srv
from goldfish.odds import odds_at_least
from tests.goldfish.helpers import (
    MINI_DECK_TEXT,
    _fake_fetch_for,
    _patch_fetch_minideck,
)
from tests.goldfish.test_autoderive import PLAINS, SOL_RING

# ── goldfish_odds ────────────────────────────────────────────────────────────


async def test_goldfish_odds_flat():
    out = await srv.goldfish_odds(srv.GoldfishOddsInput(
        deck_size=99, draws=7, copies=10, min_successes=1))
    expected = odds_at_least(99, 7, 10, 1) * 100
    assert "P(≥1 of 10 copies in top 7 of 99)" in out
    assert f"{expected:.2f}%" in out
    assert "exact hypergeometric" in out


async def test_goldfish_odds_groups():
    out = await srv.goldfish_odds(srv.GoldfishOddsInput.model_validate(
        {"deck_size": 99, "draws": 12,
         "groups": [{"copies": 1, "min_successes": 1},
                    {"copies": 1, "min_successes": 1}]}))
    assert out.count("%") >= 3          # one line per group + the joint result
    assert "Joint" in out
    assert "exact multivariate hypergeometric" in out


async def test_goldfish_odds_both_and_neither_rejected():
    neither = await srv.goldfish_odds(
        srv.GoldfishOddsInput(deck_size=99, draws=7))
    both = await srv.goldfish_odds(srv.GoldfishOddsInput.model_validate(
        {"deck_size": 99, "draws": 7, "copies": 4,
         "groups": [{"copies": 1}]}))
    assert "exactly one" in neither.lower()
    assert "exactly one" in both.lower()


async def test_goldfish_odds_cap_error_passthrough():
    # 8 groups x 60-wide ranges blows odds.py's combination cap; the tool
    # must return the ValueError message, not raise.
    out = await srv.goldfish_odds(srv.GoldfishOddsInput.model_validate(
        {"deck_size": 500, "draws": 100,
         "groups": [{"copies": 60} for _ in range(8)]}))
    assert "too large" in out


async def test_goldfish_odds_draws_exceeding_deck_size():
    out = await srv.goldfish_odds(srv.GoldfishOddsInput(
        deck_size=40, draws=60, copies=4))
    assert "draws" in out.lower() and "deck" in out.lower()


def test_goldfish_odds_nine_groups_rejected_by_model():
    with pytest.raises(pydantic.ValidationError):
        srv.GoldfishOddsInput.model_validate(
            {"deck_size": 99, "draws": 7, "groups": [{"copies": 1}] * 9})


def test_goldfish_odds_group_shape_validated():
    with pytest.raises(pydantic.ValidationError):        # copies required
        srv.GoldfishOddsInput.model_validate(
            {"deck_size": 99, "draws": 7, "groups": [{"min_successes": 1}]})
    with pytest.raises(pydantic.ValidationError):        # copies >= 0
        srv.GoldfishOddsInput.model_validate(
            {"deck_size": 99, "draws": 7, "groups": [{"copies": -1}]})


# ── goldfish_annotate ────────────────────────────────────────────────────────

SWORDS = {
    "name": "Swords to Plowshares", "type_line": "Instant", "mana_cost": "{W}",
    "oracle_text": "Exile target creature. Its controller gains life equal "
                   "to its power.",
    "keywords": []}
PURESTEEL = {
    "name": "Puresteel Paladin", "type_line": "Creature — Human Knight",
    "mana_cost": "{W}{W}", "power": "2", "toughness": "2",
    "oracle_text": "Metalcraft — Equip abilities you activate cost {0} to "
                   "activate as long as you control three or more artifacts.",
    "keywords": []}
BOROS_SIGNET = {
    "name": "Boros Signet", "type_line": "Artifact", "mana_cost": "{2}",
    "oracle_text": "{1}, {T}: Add {R}{W}.", "produced_mana": ["R", "W"],
    "keywords": []}


async def test_goldfish_annotate_three_sections(monkeypatch):
    monkeypatch.setattr(
        srv, "_goldfish_fetch_cards",
        _fake_fetch_for([PLAINS, SOL_RING, SWORDS, PURESTEEL, BOROS_SIGNET]))
    out = await srv.goldfish_annotate(srv.GoldfishAnnotateInput(
        deck="1 Puresteel Paladin\n1 Plains\n1 Sol Ring\n"
             "1 Swords to Plowshares\n1 Boros Signet\n1 Fake Card"))

    # Three sections, correctly populated (singular header for one card).
    assert "## Auto-derived (2 cards)" in out
    assert "## Out of scope (1 card)" in out
    assert "## Needs annotation (2 cards)" in out
    needs = out.split("## Needs annotation")[1]
    assert "Puresteel Paladin" in needs and "Boros Signet" in needs
    assert "Metalcraft — Equip abilities" in needs      # full oracle shown
    # Auto-derived cards don't leak into the gap list (the Signet worked
    # example is the only place its name may reappear).
    assert "Sol Ring" not in needs
    assert "Plains" not in needs

    # Out-of-scope class explanation (D9).
    assert "interaction_removal" in out
    assert "interaction — counted, not simulated" in out

    # Registry cheat-sheet and BOTH worked examples.
    assert "add_mana" in needs and "metalcraft" in needs
    assert "Cloud, Ex-SOLDIER" in needs
    assert '"Boros Signet"' in needs and '"{1}{T}"' in needs
    # Filter/target vocabularies are part of the cheat-sheet.
    assert "instant_or_sorcery" in needs        # spell_cast/cost_reduction filters
    assert "name:<CardName>" in needs           # tutor filter escape hatch
    assert "color:<W|U|B|R|G>" in needs         # cost_reduction color filter
    assert "each_opponent" in needs and "one_opponent" in needs

    # Unrecognized names surface at the top.
    assert "Not recognized by Scryfall" in out
    assert "Fake Card" in out


async def test_goldfish_annotate_minideck_all_auto(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_annotate(
        srv.GoldfishAnnotateInput(deck=MINI_DECK_TEXT))
    assert "Boss" in out.split("\n")[1]                 # commander named up top
    assert "## Auto-derived (6 cards)" in out
    assert "## Needs annotation (0 cards)" in out
    assert "100 cards" in out
    assert "Warning" not in out                         # exactly 100: no size warning


async def test_goldfish_annotate_warns_on_non_100(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_annotate(
        srv.GoldfishAnnotateInput(deck="1 Boss\n2 Plains"))
    assert "Warning" in out and "100" in out


# ── _goldfish_load_deck ──────────────────────────────────────────────────────


async def test_load_deck_first_line_commander():
    names, commander, note = await srv._goldfish_load_deck(
        "1 Boss\n2 Plains\n1 Bear")
    assert commander == "Boss"
    assert names == ["Boss", "Plains", "Plains", "Bear"]
    assert note is None


async def test_load_deck_cmdr_marker_overrides_first_line():
    names, commander, _ = await srv._goldfish_load_deck(
        "2 Plains\n1 Boss *CMDR*\n1 Bear")
    assert commander == "Boss"
    assert names.count("Boss") == 1
    assert all("*CMDR*" not in n for n in names)


async def test_load_deck_single_line_count_pattern():
    names, commander, _ = await srv._goldfish_load_deck("2 Plains")
    assert names == ["Plains", "Plains"]
    assert commander == "Plains"


async def test_load_deck_zero_quantity_deck_rejected():
    with pytest.raises(ValueError, match="no cards"):
        await srv._goldfish_load_deck("0 Sol Ring\n0 Plains")


async def test_load_deck_non_archidekt_url_rejected():
    with pytest.raises(ValueError, match="Only Archidekt URLs"):
        await srv._goldfish_load_deck("https://moxfield.com/decks/abc123")


async def test_annotate_non_archidekt_url_message():
    out = await srv.goldfish_annotate(srv.GoldfishAnnotateInput(
        deck="https://moxfield.com/decks/abc123"))
    assert "Only Archidekt URLs" in out


ARCHIDEKT_PARTNER_DATA = {
    "categories": [
        {"name": "Commander", "isPremier": True, "includedInDeck": True},
    ],
    "cards": [
        {"quantity": 1, "categories": ["Commander"],
         "card": {"oracleCard": {"name": "Partner A"}}},
        {"quantity": 1, "categories": ["Commander"],
         "card": {"oracleCard": {"name": "Partner B"}}},
        {"quantity": 2, "categories": [],
         "card": {"oracleCard": {"name": "Plains"}}},
    ],
}


async def test_load_deck_partner_precon_note(monkeypatch):
    async def fake_get(path, params=None):
        return ARCHIDEKT_PARTNER_DATA

    monkeypatch.setattr(srv, "_archidekt_get", fake_get)
    names, commander, note = await srv._goldfish_load_deck("12345")
    assert commander == "Partner A"
    assert note is not None and "first of 2 premier cards" in note
    assert names.count("Plains") == 2


async def test_annotate_partner_note_in_header(monkeypatch):
    async def fake_get(path, params=None):
        return ARCHIDEKT_PARTNER_DATA

    monkeypatch.setattr(srv, "_archidekt_get", fake_get)
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_annotate(srv.GoldfishAnnotateInput(deck="12345"))
    assert "Commander: Partner A (first of 2 premier cards" in out
    assert "v1 simulates a single commander" in out


# ── _goldfish_fetch_cards cache ──────────────────────────────────────────────


async def test_fetch_cards_second_call_hits_cache(monkeypatch):
    calls = {"n": 0}

    async def fake_post(endpoint, body):
        calls["n"] += 1
        return {"data": [{"name": i["name"]} for i in body["identifiers"]],
                "not_found": []}

    monkeypatch.setattr(srv, "_scryfall_post", fake_post)
    monkeypatch.setattr(srv, "_GOLDFISH_CARD_CACHE", {})
    cards1, nf1 = await srv._goldfish_fetch_cards(["Plains", "Sol Ring"])
    cards2, nf2 = await srv._goldfish_fetch_cards(["Plains", "Sol Ring"])
    assert calls["n"] == 1                              # second call: no HTTP
    assert [c["name"] for c in cards1] == ["Plains", "Sol Ring"]
    assert cards1 == cards2
    assert nf1 == [] and nf2 == []


async def test_fetch_cards_chunks_of_75_and_not_found(monkeypatch):
    batches = []

    async def fake_post(endpoint, body):
        idents = body["identifiers"]
        batches.append(len(idents))
        return {"data": [{"name": i["name"]} for i in idents
                         if i["name"] != "Fake Card"],
                "not_found": [i for i in idents if i["name"] == "Fake Card"]}

    monkeypatch.setattr(srv, "_scryfall_post", fake_post)
    monkeypatch.setattr(srv, "_GOLDFISH_CARD_CACHE", {})
    names = [f"Card {i}" for i in range(79)] + ["Fake Card"]
    cards, not_found = await srv._goldfish_fetch_cards(names)
    assert batches == [75, 5]
    assert len(cards) == 79
    assert not_found == ["Fake Card"]
