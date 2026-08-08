import json

from goldfish.cards import SimCard, parse_cost
from goldfish.engine import Game, derive_seed, new_game
from tests.goldfish.test_cards import make_data  # reuse the factory


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


def test_seed_derivation_stable_and_distinct():
    assert derive_seed(42, 0) == derive_seed(42, 0)
    assert derive_seed(42, 0) != derive_seed(42, 1) != derive_seed(43, 0)


def test_new_game_shuffles_and_draws_seven():
    cards = mini_cards()
    deck = ["Plains"] * 20 + ["Mountain"] * 20 + ["Bear"] * 30 + ["Hammer"] * 29
    g1 = new_game(cards, deck, commander="Boss", seed=7)
    g2 = new_game(cards, deck, commander="Boss", seed=7)
    assert g1.hand == g2.hand and g1.library == g2.library     # deterministic
    assert len(g1.hand) == 7 and len(g1.library) == 92          # 99 - 7
    assert g1.command == ["Boss"] and g1.phase == "mulligan"


def test_state_roundtrip():
    cards = mini_cards()
    combos_arg = [{"cards": ["Plains", "Boss"], "wins": True}]
    g = new_game(cards, ["Plains"] * 40, commander="Boss", seed=3, combos=combos_arg)
    g.rng.random()                                              # advance rng
    g.turn = 3
    p = g.new_perm("Hammer", attached=["b1"], pump_eot=[1, 2], pump_perm=[3, 4],
                   is_token=True, token_power=5, token_toughness=6,
                   token_keywords=("trample",))
    g.emit("equipped Hammer")

    raw_blob = g.to_dict()                                      # pre-mutation snapshot
    blob = json.loads(json.dumps(raw_blob))                     # actual JSON round trip
    g2 = Game.from_dict(blob, cards)

    # (a) field-by-field equality, including the new permanent's tracked state
    assert g2.turn == g.turn == 3
    assert g2.phase == g.phase
    assert g2.hand == g.hand
    assert g2.library == g.library
    assert g2.log == g.log
    assert g2.combos == g.combos == [["Plains", "Boss"]]
    assert g2.combo_wins == g.combo_wins == [True]
    assert g2.combo_assembled_turn == g.combo_assembled_turn == [None]
    assert g2.combo_castable_turn == g.combo_castable_turn == [None]
    assert len(g2.battlefield) == len(g.battlefield) == 1
    p2 = g2.battlefield[0]
    assert p2 == p
    assert p2.attached == p.attached == ["b1"]
    assert p2.pump_eot == p.pump_eot == [1, 2]
    assert p2.pump_perm == p.pump_perm == [3, 4]
    assert p2.is_token is p.is_token is True
    assert p2.token_power == p.token_power == 5
    assert p2.token_toughness == p.token_toughness == 6
    assert p2.token_keywords == p.token_keywords == ("trample",)
    assert g2.rng.getstate() == g.rng.getstate()                # rng state travels

    # (b) aliasing regression guard: mutating the original game's combo
    # tracking list, and the caller's combos argument, after to_dict() was
    # called must NOT retroactively change the already-produced blob.
    g.combo_assembled_turn[0] = 3
    combos_arg[0]["cards"].append("Mountain")
    assert raw_blob["combo_assembled_turn"] == [None]
    assert raw_blob["combos"] == [["Plains", "Boss"]]
