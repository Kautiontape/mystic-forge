"""Shared goldfish test helpers (Task 15 ride-along consolidation).

Definitions MOVED here from their original homes — ``make_data`` from
test_cards, ``mini_cards``/``annotated``/``started`` from test_engine,
``small_deck`` from test_runner — so every test module imports one place.
"""
import server as srv
from mystic_forge.goldfish.cards import CardData, SimCard, parse_cost, validate_annotations
from mystic_forge.goldfish.engine import new_game


def make_data(name, **kw):
    base = {"name": name, "cost": None, "types": frozenset(), "power": None,
            "toughness": None, "keywords": frozenset(), "produces": None,
            "enters_tapped": False, "equip_cost": None, "oracle": ""}
    base.update(kw)
    return CardData(**base)


def started(cards, deck, seed=1, hand=None):
    """A game skipped past the mulligan into turn 1 main1. Land drops derive
    from _land_drops_used (0 by default), so one drop is available without
    further setup."""
    g = new_game(cards, deck, "Boss", seed=seed)
    g.phase = "main1"
    g.turn = 1
    if hand is not None:
        g.hand[:] = hand
    return g


def annotated(cards, name, ann_dict):
    """Replace cards[name] with an annotated copy."""
    anns = validate_annotations([ann_dict])
    c = cards[name]
    cards[name] = SimCard(data=c.data, ann=anns[name], scope_class=None)


def mini_cards():
    """Six-name pool used across engine tests."""
    defs = {
        "Plains":   make_data("Plains", types=frozenset({"land"}), produces={"W": 1}),
        "Mountain": make_data("Mountain", types=frozenset({"land"}), produces={"R": 1}),
        "Bear":     make_data("Bear", cost=parse_cost("{1}{G}"),
                              types=frozenset({"creature"}), power=2, toughness=2),
        "Runner":   make_data("Runner", cost=parse_cost("{R}"),
                              types=frozenset({"creature"}), power=1, toughness=1,
                              keywords=frozenset({"haste"})),
        "Hammer":   make_data("Hammer", cost=parse_cost("{1}"),
                              types=frozenset({"artifact", "equipment"}),
                              equip_cost=parse_cost("{8}")),
        "Boss":     make_data("Boss", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature", "legendary", "commander"}),
                              power=3, toughness=3),
    }
    return {n: SimCard(data=d, ann=None, scope_class=None) for n, d in defs.items()}


def small_deck():
    return ["Plains"] * 15 + ["Mountain"] * 15 + ["Bear"] * 5 + ["Runner"] * 4


# ── Server-tool helpers (Task 18) ────────────────────────────────────────────

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
    """A stand-in for srv._goldfish_fetch_cards serving the given Scryfall-
    shaped dicts; unknown names come back in the not_found list."""
    by_name = {c["name"]: c for c in fixtures}

    async def fake_fetch(names):
        unique = list(dict.fromkeys(names))
        return ([by_name[n] for n in unique if n in by_name],
                [n for n in unique if n not in by_name])

    return fake_fetch


def _patch_fetch_minideck(monkeypatch):
    monkeypatch.setattr(srv, "_goldfish_fetch_cards",
                        _fake_fetch_for(MINI_SCRYFALL))
