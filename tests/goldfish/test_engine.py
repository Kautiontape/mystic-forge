import json

import pytest

from goldfish.cards import SimCard, parse_cost
from goldfish.engine import (
    Game,
    IllegalAction,
    _payment_plan,
    can_pay,
    derive_seed,
    new_game,
    pay,
    untapped_producers,
)
from tests.goldfish.test_cards import make_data  # reuse the factory


def bf_land(g, name):
    p = g.new_perm(name)
    return p


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
    assert g.combos[0] is not combos_arg[0]["cards"]            # defensive copy in new_game
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


# -- Task 5: mana payment solver -----------------------------------------

def test_untapped_producers_strictest_first_ordering():
    cards = mini_cards()
    cards["Karoo"] = SimCard(data=make_data("Karoo", types=frozenset({"land"}),
                                            produces={"W": 2}), ann=None, scope_class=None)
    cards["Rainbow"] = SimCard(data=make_data(
        "Rainbow", types=frozenset({"land"}),
        produces={"W": 1, "U": 1, "B": 1, "R": 1, "G": 1}), ann=None, scope_class=None)
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "Rainbow")                      # 5 colors -> id b1, least strict
    bf_land(g, "Plains")                       # 1 color -> id b2
    bf_land(g, "Karoo")                        # 1 color, qty 2 -> id b3
    mtn = bf_land(g, "Mountain")
    mtn.tapped = True                           # tapped producers are excluded

    out = untapped_producers(g)
    assert [p.id for p, _, _ in out] == ["b2", "b3", "b1"]      # strictest (fewest
    # colors) first, ties broken by perm id
    assert out[0][1] == frozenset({"W"}) and out[0][2] == 1
    assert out[1][1] == frozenset({"W"}) and out[1][2] == 2
    assert out[2][1] == frozenset({"W", "U", "B", "R", "G"}) and out[2][2] == 1


def test_cannot_pay_red_with_only_plains():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    for _ in range(3):
        bf_land(g, "Plains")
    assert can_pay(g, parse_cost("{R}")) is False
    assert can_pay(g, parse_cost("{2}")) is True


def test_greedy_prefers_flexible_land_for_generic():
    # 1 Mountain + 1 Plains, cost {1}{R}: the Mountain must fund {R}, Plains the {1}
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "Mountain"); bf_land(g, "Plains")
    assert can_pay(g, parse_cost("{1}{R}")) is True
    pay(g, parse_cost("{1}{R}"))
    assert all(p.tapped for p in g.battlefield)


def test_quantity_producer_and_pool_wildcard():
    cards = mini_cards()
    cards["Karoo"] = SimCard(data=make_data("Karoo", types=frozenset({"land"}),
                                            produces={"W": 2}), ann=None, scope_class=None)
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "Karoo")
    g.mana_pool["any"] = 1
    assert can_pay(g, parse_cost("{2}{R}")) is True   # W W from Karoo + wildcard R


def test_karoo_pays_double_pip_with_single_tap_and_no_surplus():
    # {W}{W} against a single Karoo (produces W qty 2): one permanent taps,
    # and the surplus is fully consumed by the second pip (pool ends at 0).
    cards = mini_cards()
    cards["Karoo"] = SimCard(data=make_data("Karoo", types=frozenset({"land"}),
                                            produces={"W": 2}), ann=None, scope_class=None)
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "Karoo")
    assert can_pay(g, parse_cost("{W}{W}")) is True
    pay(g, parse_cost("{W}{W}"))
    tapped = [p for p in g.battlefield if p.tapped]
    assert len(tapped) == 1
    assert tapped[0].name == "Karoo"
    assert g.mana_pool["W"] == 0


def test_generic_paid_from_pool_with_empty_battlefield():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.mana_pool["any"] = 1
    assert can_pay(g, parse_cost("{1}")) is True
    pay(g, parse_cost("{1}"))
    assert g.battlefield == []                 # nothing to tap
    assert g.mana_pool["any"] == 0


def test_pay_impossible_cost_raises_and_leaves_game_unmutated():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    for _ in range(3):
        bf_land(g, "Plains")
    pool_before = dict(g.mana_pool)
    with pytest.raises(IllegalAction):
        pay(g, parse_cost("{R}"))
    assert all(not p.tapped for p in g.battlefield)
    assert g.mana_pool == pool_before


def test_payment_plan_is_deterministic():
    cards = mini_cards()
    g1 = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g2 = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    for g in (g1, g2):
        bf_land(g, "Mountain"); bf_land(g, "Plains"); bf_land(g, "Mountain")
    cost = parse_cost("{1}{R}{R}")
    plan1 = _payment_plan(g1, cost)
    plan2 = _payment_plan(g2, cost)
    assert plan1 is not None and plan2 is not None
    ids1, pool1 = plan1
    ids2, pool2 = plan2
    assert ids1 == ids2                          # same perms tapped, same order
    assert pool1 == pool2


# -- Regressions: MRV pip ordering + generic surplus banking --------------
# The plan's reference solver ordered colored pips by raw demand (-need[c]),
# a one-shot sort that strands duals shared between two needed colors, and
# discarded producer overshoot in the generic phase instead of banking it.
# Fixed to dynamic most-constrained-first (recomputed after every pip) and
# to route all overshoot — colored and generic — through the pool.

def _dual_cards():
    """mini_cards() plus a WU and a WB dual land, for pip-ordering regressions."""
    cards = mini_cards()
    cards["WU"] = SimCard(data=make_data("WU", types=frozenset({"land"}),
                                         produces={"W": 1, "U": 1}), ann=None, scope_class=None)
    cards["WB"] = SimCard(data=make_data("WB", types=frozenset({"land"}),
                                         produces={"W": 1, "B": 1}), ann=None, scope_class=None)
    return cards


def _rock_cards():
    """mini_cards() plus an all-color-agnostic colorless rock (Sol Ring-alike)."""
    cards = mini_cards()
    cards["SolRing"] = SimCard(data=make_data("SolRing", types=frozenset({"artifact"}),
                                              produces={"C": 2}), ann=None, scope_class=None)
    return cards


def test_mrv_orders_colored_pips_by_scarcity_not_demand():
    # Each of these is payable by an exact matcher; the demand-ordered solver
    # greedily grabbed the shared color first and stranded the dual.
    for names, cost_str in (
        (["WU", "WB"], "{W}{U}"),
        (["WB", "WU"], "{W}{U}"),
        (["WU", "WB", "WB"], "{W}{U}{B}"),
    ):
        cards = _dual_cards()
        g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
        for n in names:
            bf_land(g, n)
        cost = parse_cost(cost_str)
        assert can_pay(g, cost) is True, (names, cost_str)
        pay(g, cost)
        assert all(p.tapped for p in g.battlefield)


def test_mrv_lets_a_basic_rescue_a_double_pip_that_would_strand_a_dual():
    cards = _dual_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "Plains"); bf_land(g, "WU"); bf_land(g, "WB")
    cost = parse_cost("{W}{W}{U}")
    assert can_pay(g, cost) is True
    pay(g, cost)
    assert all(p.tapped for p in g.battlefield)


def test_generic_surplus_is_banked_for_a_second_payment_same_turn():
    cards = _rock_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "SolRing")
    pay(g, parse_cost("{1}"))
    assert g.mana_pool["C"] == 1                  # overshoot banked, not discarded
    assert can_pay(g, parse_cost("{1}")) is True
    pay(g, parse_cost("{1}"))
    assert g.mana_pool["C"] == 0


def test_generic_surplus_banked_even_when_split_with_a_land():
    cards = _rock_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "SolRing"); bf_land(g, "Plains")
    pay(g, parse_cost("{1}{W}"))                  # W from Plains, generic from SolRing
    assert g.mana_pool["C"] == 1
    assert can_pay(g, parse_cost("{1}")) is True   # banked C covers the follow-up


def test_colorless_pip_not_payable_from_any_wildcard():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.mana_pool["any"] = 1
    assert can_pay(g, parse_cost("{C}")) is False


def test_pool_any_untouched_when_a_producer_covers_the_pip():
    # wildcards spent last: with a Mountain able to pay {R} directly, the
    # pool's "any" wildcard must be left alone.
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "Mountain")
    g.mana_pool["any"] = 1
    assert can_pay(g, parse_cost("{R}")) is True
    pay(g, parse_cost("{R}"))
    assert g.mana_pool["any"] == 1
    assert g.battlefield[0].tapped is True
