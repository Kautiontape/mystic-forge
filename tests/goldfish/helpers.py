"""Shared goldfish test helpers (Task 15 ride-along consolidation).

Definitions MOVED here from their original homes — ``make_data`` from
test_cards, ``mini_cards``/``annotated``/``started`` from test_engine,
``small_deck`` from test_runner — so every test module imports one place.
"""
from goldfish.cards import CardData, SimCard, parse_cost, validate_annotations
from goldfish.engine import new_game


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
