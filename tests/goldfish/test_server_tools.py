"""Server-tool tests for the goldfish MCP surface (Task 18).

The shared helpers ``MINI_DECK_TEXT`` and ``_patch_fetch_minideck`` are
defined here per the plan; later server-tool test modules
(test_interactive.py, test_acceptance.py) import them from this file.
"""
import pydantic
import pytest

import server as srv
from goldfish.odds import odds_at_least

# ── Shared helpers ───────────────────────────────────────────────────────────

# First line's card is the commander (goldfish's text-decklist pin), so Boss
# leads. Total is 100 including commander.
MINI_DECK_TEXT = "1 Boss\n20 Plains\n17 Mountain\n30 Bear\n20 Runner\n12 Hammer"

# Scryfall-shaped fixtures for the six mini-pool names (Task 17 fixture style).
MINI_SCRYFALL = [
    {"name": "Boss", "type_line": "Legendary Creature — Human Warrior",
     "mana_cost": "{2}{R}", "power": "3", "toughness": "3",
     "oracle_text": "", "keywords": []},
    {"name": "Plains", "type_line": "Basic Land — Plains",
     "oracle_text": "({T}: Add {W}.)", "produced_mana": ["W"], "keywords": []},
    {"name": "Mountain", "type_line": "Basic Land — Mountain",
     "oracle_text": "({T}: Add {R}.)", "produced_mana": ["R"], "keywords": []},
    {"name": "Bear", "type_line": "Creature — Bear", "mana_cost": "{1}{G}",
     "power": "2", "toughness": "2", "oracle_text": "", "keywords": []},
    {"name": "Runner", "type_line": "Creature — Goblin", "mana_cost": "{R}",
     "power": "1", "toughness": "1", "oracle_text": "Haste",
     "keywords": ["Haste"]},
    {"name": "Hammer", "type_line": "Artifact — Equipment", "mana_cost": "{1}",
     "oracle_text": "Equipped creature gets +10/+10.\nEquip {8}",
     "keywords": ["Equip"]},
]


def _fake_fetch_for(fixtures):
    by_name = {c["name"]: c for c in fixtures}

    async def fake_fetch(names):
        unique = list(dict.fromkeys(names))
        return ([by_name[n] for n in unique if n in by_name],
                [n for n in unique if n not in by_name])

    return fake_fetch


def _patch_fetch_minideck(monkeypatch):
    monkeypatch.setattr(srv, "_goldfish_fetch_cards",
                        _fake_fetch_for(MINI_SCRYFALL))


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
    from tests.goldfish.test_autoderive import PLAINS, SOL_RING
    monkeypatch.setattr(
        srv, "_goldfish_fetch_cards",
        _fake_fetch_for([PLAINS, SOL_RING, SWORDS, PURESTEEL, BOROS_SIGNET]))
    out = await srv.goldfish_annotate(srv.GoldfishAnnotateInput(
        deck="1 Puresteel Paladin\n1 Plains\n1 Sol Ring\n"
             "1 Swords to Plowshares\n1 Boros Signet\n1 Fake Card"))

    # Three sections, correctly populated.
    assert "## Auto-derived (2 cards)" in out
    assert "## Out of scope (1" in out
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
    names, commander = await srv._goldfish_load_deck("1 Boss\n2 Plains\n1 Bear")
    assert commander == "Boss"
    assert names == ["Boss", "Plains", "Plains", "Bear"]


async def test_load_deck_cmdr_marker_overrides_first_line():
    names, commander = await srv._goldfish_load_deck(
        "2 Plains\n1 Boss *CMDR*\n1 Bear")
    assert commander == "Boss"
    assert names.count("Boss") == 1
    assert all("*CMDR*" not in n for n in names)


async def test_load_deck_single_line_count_pattern():
    names, commander = await srv._goldfish_load_deck("2 Plains")
    assert names == ["Plains", "Plains"]
    assert commander == "Plains"


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
