import json

import pytest

from goldfish.cards import SimCard, parse_cost, validate_annotations
from goldfish.engine import (
    Game,
    IllegalAction,
    _payment_plan,
    can_pay,
    check_condition,
    derive_seed,
    effective_keywords,
    effective_power,
    execute_verb,
    fire,
    new_game,
    pay,
    resolve_count,
    untapped_producers,
)
from tests.goldfish.test_cards import make_data  # reuse the factory


def bf_land(g, name):
    p = g.new_perm(name)
    return p


def annotated(cards, name, ann_dict):
    """Replace cards[name] with an annotated copy (module-level for reuse)."""
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
    g.life_gained = 7
    g.trigger_fires["Tremors|creature_etb|damage"] = 4
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
    assert g2.life_gained == g.life_gained == 7
    assert g2.trigger_fires == g.trigger_fires == {"Tremors|creature_etb|damage": 4}
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


# -- Task 6: events, verbs, counts, conditions ----------------------------

def test_damage_each_opponent_and_one():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1, opponents=3)
    execute_verb(g, source=None, verb="damage", count=3, target="each_opponent")
    assert g.opponents == [37, 37, 37]
    execute_verb(g, source=None, verb="damage", count=2, target="one_opponent")
    assert g.opponents == [35, 37, 37]        # focus-fire: lowest index first


def test_create_token_fires_creature_etb_per_token_and_doubles():
    cards = mini_cards()
    cards["Tremors"] = SimCard(data=make_data("Tremors", cost=parse_cost("{1}{R}"),
                               types=frozenset({"enchantment"})), ann=None, scope_class=None)
    cards["Doubler"] = SimCard(data=make_data("Doubler", cost=parse_cost("{4}"),
                               types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Tremors", {"name": "Tremors", "triggers": [
        {"on": "creature_etb", "do": "damage", "target": "each_opponent", "count": 1}]})
    annotated(cards, "Doubler", {"name": "Doubler", "statics": [{"kind": "token_doubling"}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Tremors"); g.new_perm("Doubler")
    execute_verb(g, source=None, verb="create_token", count=2, power=1, toughness=1)
    tokens = [p for p in g.battlefield if p.is_token]
    assert len(tokens) == 4                     # doubled
    assert g.opponents == [36]                  # 4 Tremors pings


def test_spell_cast_listener_requires_battlefield():
    cards = mini_cards()
    cards["Snipe"] = SimCard(data=make_data("Snipe", cost=parse_cost("{2}{R}"),
                             types=frozenset({"creature"}), power=2, toughness=2),
                             ann=None, scope_class=None)
    annotated(cards, "Snipe", {"name": "Snipe", "triggers": [
        {"on": "spell_cast", "filter": "instant_or_sorcery",
         "do": "damage", "target": "each_opponent", "count": 2}]})
    cards["Bolt"] = SimCard(data=make_data("Bolt", cost=parse_cost("{R}"),
                            types=frozenset({"instant"})), ann=None, scope_class=None)
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    fire(g, "spell_cast", spell=cards["Bolt"])
    assert g.opponents == [40]              # global listeners only hear from battlefield
    g.new_perm("Snipe")
    fire(g, "spell_cast", spell=cards["Bolt"])
    assert g.opponents == [38]
    fire(g, "spell_cast", spell=cards["Bear"])
    assert g.opponents == [38]              # filter: creature spell ignored


def test_per_spell_cast_count_reads_counter():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.spells_cast_this_turn = 3
    before = len(g.hand)
    execute_verb(g, None, "draw", count="per_spell_cast_this_turn")
    assert len(g.hand) == before + 3


def test_new_game_injects_synthetic_cards():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    assert set(g.card("Treasure").data.produces) == set("WUBRG")
    assert g.card("Rock").data.produces == {"C": 1}
    assert g.card("Token").data.types == frozenset()
    assert g.card("Token").data.produces is None


def test_gain_life_accumulates():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    execute_verb(g, None, "gain_life", count=4)
    execute_verb(g, None, "gain_life", count=2)
    assert g.life_gained == 6
    assert any("gained 4 life" in line for line in g.log)


def test_add_mana_pips_and_wildcard():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    execute_verb(g, None, "add_mana", pips="{R}{R}{G}")
    assert g.mana_pool["R"] == 2 and g.mana_pool["G"] == 1
    execute_verb(g, None, "add_mana", any_mana=True, count=2)
    assert g.mana_pool["any"] == 2


def test_treasure_tokens_produce_any_color():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    execute_verb(g, None, "treasure", count=2)
    ts = [p for p in g.battlefield if p.name == "Treasure"]
    assert len(ts) == 2 and all(p.is_token for p in ts)
    assert can_pay(g, parse_cost("{U}{B}")) is True
    assert can_pay(g, parse_cost("{U}{B}{R}")) is False
    # treasures are artifacts, not creatures
    assert resolve_count(g, "per_artifact", {}) == 2
    assert resolve_count(g, "per_creature", {}) == 0


def test_ramp_land_moves_lands_tapped_and_fires_land_etb():
    cards = mini_cards()
    cards["Landfall"] = SimCard(data=make_data("Landfall", cost=parse_cost("{1}{G}"),
                                types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Landfall", {"name": "Landfall", "triggers": [
        {"on": "land_etb", "do": "gain_life", "count": 1}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Landfall")
    g.library[:] = ["Bear", "Plains", "Bear", "Mountain", "Plains"]
    execute_verb(g, None, "ramp_land", count=2)
    ramped = [p for p in g.battlefield if g.card(p.name).is_land]
    assert [p.name for p in ramped] == ["Plains", "Mountain"]   # library order
    assert all(p.tapped for p in ramped)                        # enter tapped
    assert g.library == ["Bear", "Bear", "Plains"]
    assert g.life_gained == 2                                   # land_etb fired per land


def test_ramp_mana_creates_rocks_without_creature_etb():
    cards = mini_cards()
    cards["Tremors"] = SimCard(data=make_data("Tremors", cost=parse_cost("{1}{R}"),
                               types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Tremors", {"name": "Tremors", "triggers": [
        {"on": "creature_etb", "do": "damage", "target": "each_opponent", "count": 1}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Tremors")
    execute_verb(g, None, "ramp_mana", count=2)
    rocks = [p for p in g.battlefield if p.name == "Rock"]
    assert len(rocks) == 2
    assert g.opponents == [40]                  # rocks are not creatures
    assert len(untapped_producers(g)) == 2


def test_tutor_by_type_name_any_and_whiff():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.library[:] = ["Bear", "Plains", "Hammer", "Plains"]
    execute_verb(g, None, "tutor", tutor_filter="equipment")
    assert g.hand[-1] == "Hammer" and "Hammer" not in g.library
    assert any("tutored Hammer" in line for line in g.log)
    execute_verb(g, None, "tutor", tutor_filter="name:Bear")
    assert g.hand[-1] == "Bear"
    execute_verb(g, None, "tutor", tutor_filter="planeswalker")
    assert any("tutor whiffed" in line for line in g.log)
    execute_verb(g, None, "tutor")                              # any = first card
    assert g.hand[-1] == "Plains" and g.library == ["Plains"]


def test_pump_durations_count_and_no_source():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bear = g.new_perm("Bear")
    execute_verb(g, bear, "pump", power=2, toughness=1)         # default eot
    assert bear.pump_eot == [2, 1]
    assert effective_power(g, bear) == 4
    assert any("Bear pumped +2/+1 (eot)" in line for line in g.log)
    execute_verb(g, bear, "pump", power=1, toughness=1, duration="permanent", count=3)
    assert bear.pump_perm == [3, 3]
    assert effective_power(g, bear) == 7
    # the log carries the applied delta, never the cumulative bucket
    assert any("Bear pumped +3/+3 (permanent)" in line for line in g.log)
    log_len = len(g.log)
    execute_verb(g, None, "pump", power=1, toughness=1)         # no source: logged no-op
    assert len(g.log) == log_len + 1


def test_attach_prefers_commander_then_biggest_creature():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    hammer = g.new_perm("Hammer")
    g.new_perm("Bear")
    boss = g.new_perm("Boss")
    execute_verb(g, None, "attach")
    assert hammer.attached_to == boss.id
    assert boss.attached == [hammer.id]

    g2 = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    h2 = g2.new_perm("Hammer")
    g2.new_perm("Runner")
    bear2 = g2.new_perm("Bear")
    execute_verb(g2, None, "attach")
    assert h2.attached_to == bear2.id           # highest effective power wins

    g3 = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g3.new_perm("Hammer")
    log_len = len(g3.log)
    execute_verb(g3, None, "attach")            # no creature: logged no-op
    assert len(g3.log) == log_len + 1


def test_attach_from_board_attaches_up_to_n_to_source():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bear = g.new_perm("Bear")
    h1 = g.new_perm("Hammer")
    h2 = g.new_perm("Hammer")
    h3 = g.new_perm("Hammer")
    execute_verb(g, bear, "attach_from_board", count=2)
    assert h1.attached_to == bear.id and h2.attached_to == bear.id
    assert h3.attached_to is None
    assert bear.attached == [h1.id, h2.id]


def test_extra_combat_increments():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    execute_verb(g, None, "extra_combat", count=2)
    assert g.extra_combats == 2


def test_token_copy_copies_perm_pumps_not_eot_or_equipment():
    cards = mini_cards()
    cards["Watcher"] = SimCard(data=make_data("Watcher", cost=parse_cost("{W}"),
                               types=frozenset({"creature"}), power=1, toughness=1),
                               ann=None, scope_class=None)
    annotated(cards, "Watcher", {"name": "Watcher", "triggers": [
        {"on": "creature_etb", "do": "gain_life", "count": 1}]})
    annotated(cards, "Hammer", {"name": "Hammer", "grants": {"power": 3}})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Watcher")
    runner = g.new_perm("Runner")
    runner.pump_perm[:] = [1, 1]
    runner.pump_eot[:] = [5, 5]
    hammer = g.new_perm("Hammer")
    hammer.attached_to = runner.id
    runner.attached.append(hammer.id)
    execute_verb(g, hammer, "token_copy")       # equipment source: copy what it's on
    copies = [p for p in g.battlefield if p.is_token]
    assert len(copies) == 1
    tok = copies[0]
    assert tok.name == "Runner" and tok.is_token
    assert tok.token_power == 2                 # 1 base + 1 pump_perm; no eot, no grant
    assert tok.token_toughness == 2
    assert tok.token_keywords == ("haste",)     # keywords come from the card
    assert g.life_gained == 1                   # copy fired creature_etb
    execute_verb(g, runner, "token_copy")       # creature source: copies itself
    assert sum(1 for p in g.battlefield if p.is_token) == 2
    assert g.life_gained == 2


def test_effective_power_and_keywords_anthem_and_grants():
    cards = mini_cards()
    cards["Anthem"] = SimCard(data=make_data("Anthem", cost=parse_cost("{2}"),
                              types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Anthem", {"name": "Anthem", "statics": [
        {"kind": "anthem", "power": 1, "toughness": 1, "keywords": ["haste"]}]})
    annotated(cards, "Hammer", {"name": "Hammer",
                                "grants": {"power": 3, "keywords": ["trample"]}})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bear = g.new_perm("Bear")
    g.new_perm("Anthem")
    hammer = g.new_perm("Hammer")
    hammer.attached_to = bear.id
    bear.attached.append(hammer.id)
    assert effective_power(g, bear) == 6        # 2 base + 1 anthem + 3 grant
    assert {"haste", "trample"} <= effective_keywords(g, bear)
    assert effective_power(g, hammer) == 0      # grants boost the bearer, not the
                                                # equipment; no base, no anthem
    assert "haste" not in effective_keywords(g, hammer)   # anthem keywords are
                                                          # creature-gated too
    tok = g.new_perm("Token", is_token=True, token_power=2, token_toughness=2)
    assert effective_power(g, tok) == 3         # token stats + anthem
    treasure = g.new_perm("Treasure", is_token=True)
    assert effective_keywords(g, treasure) == set()       # no leak onto Treasures


def test_resolve_count_symbolics_and_unknown():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bear = g.new_perm("Bear")
    runner = g.new_perm("Runner")
    hammer = g.new_perm("Hammer")
    g.new_perm("Hammer")
    bf_land(g, "Plains"); bf_land(g, "Mountain")
    hammer.attached_to = bear.id
    bear.attached.append(hammer.id)
    g.spells_cast_this_turn = 5
    assert resolve_count(g, 7, {}) == 7
    assert resolve_count(g, "per_artifact", {}) == 2
    assert resolve_count(g, "per_creature", {}) == 2
    assert resolve_count(g, "per_land", {}) == 2
    assert resolve_count(g, "per_spell_cast_this_turn", {}) == 5
    ctx = {"attackers": [bear.id, runner.id]}
    assert resolve_count(g, "per_attacker", ctx) == 2
    assert resolve_count(g, "per_equipped_attacker", ctx) == 1
    with pytest.raises(IllegalAction):
        resolve_count(g, "per_goblin", {})


def test_check_condition_variants():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bear = g.new_perm("Bear")
    assert check_condition(g, None, None, {}) is True
    assert check_condition(g, ("power_gte", 2), bear, {}) is True
    assert check_condition(g, ("power_gte", 3), bear, {}) is False
    assert check_condition(g, ("power_gte", 1), None, {}) is False
    assert check_condition(g, ("equipped",), bear, {}) is False
    assert check_condition(g, ("metalcraft",), None, {}) is False
    g.new_perm("Hammer"); g.new_perm("Hammer")
    assert check_condition(g, ("metalcraft",), None, {}) is False   # 2 artifacts
    h3 = g.new_perm("Hammer")
    assert check_condition(g, ("metalcraft",), None, {}) is True    # 3 artifacts
    h3.attached_to = bear.id
    bear.attached.append(h3.id)
    assert check_condition(g, ("equipped",), bear, {}) is True
    assert check_condition(g, ("equipped",), None, {}) is False


def test_fire_records_trigger_fires_and_respects_condition():
    cards = mini_cards()
    cards["Sage"] = SimCard(data=make_data("Sage", cost=parse_cost("{1}{U}"),
                            types=frozenset({"creature"}), power=1, toughness=1),
                            ann=None, scope_class=None)
    annotated(cards, "Sage", {"name": "Sage", "triggers": [
        {"on": "upkeep", "if": "metalcraft", "do": "draw", "count": 1}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Sage")
    before = len(g.hand)
    fire(g, "upkeep")
    assert len(g.hand) == before and g.trigger_fires == {}      # condition unmet
    for _ in range(3):
        g.new_perm("Hammer")
    fire(g, "upkeep")
    assert len(g.hand) == before + 1
    assert g.trigger_fires == {"Sage|upkeep|draw": 1}


def test_fire_excludes_entering_permanent():
    cards = mini_cards()
    cards["Watcher"] = SimCard(data=make_data("Watcher", cost=parse_cost("{W}"),
                               types=frozenset({"creature"}), power=1, toughness=1),
                               ann=None, scope_class=None)
    annotated(cards, "Watcher", {"name": "Watcher", "triggers": [
        {"on": "creature_etb", "do": "gain_life", "count": 1}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    w = g.new_perm("Watcher")
    fire(g, "creature_etb", entering=w)
    assert g.life_gained == 0                   # doesn't hear its own arrival
    b = g.new_perm("Bear")
    fire(g, "creature_etb", entering=b)
    assert g.life_gained == 1


def test_unknown_verb_raises():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    with pytest.raises(IllegalAction):
        execute_verb(g, None, "scry")


# -- Task 6 review fixes: recursion bound, zero counts, snapshot pins ------

def test_trigger_loop_bounded_by_depth_limit():
    # Reviewer's repro: "whenever a creature enters, create a 1/1" is a
    # validation-legal annotation that self-feeds. The depth bound must stop
    # it deterministically, log one suppression line per cascade, and leave
    # the game consistent.
    from goldfish.engine import _MAX_FIRE_DEPTH
    cards = mini_cards()
    cards["Broodmother"] = SimCard(data=make_data("Broodmother", cost=parse_cost("{3}{G}"),
                                   types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Broodmother", {"name": "Broodmother", "triggers": [
        {"on": "creature_etb", "do": "create_token", "power": 1, "toughness": 1}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Broodmother")
    execute_verb(g, None, "create_token", count=1, power=1, toughness=1)
    tokens = [p for p in g.battlefield if p.is_token]
    assert len(tokens) == _MAX_FIRE_DEPTH + 1   # 1 direct + 1 per dispatched level
    assert sum("depth limit" in line for line in g.log) == 1
    assert g._fire_depth == 0                   # fully unwound
    execute_verb(g, None, "create_token", count=1, power=1, toughness=1)
    assert sum("depth limit" in line for line in g.log) == 2   # next cascade warns afresh


def test_nested_triggers_within_depth_work_normally():
    # land_etb -> create_token -> creature_etb -> draw: a depth-2/3 chain is
    # untouched by the bound.
    cards = mini_cards()
    cards["Nest"] = SimCard(data=make_data("Nest", cost=parse_cost("{2}"),
                            types=frozenset({"enchantment"})), ann=None, scope_class=None)
    cards["Scribe"] = SimCard(data=make_data("Scribe", cost=parse_cost("{1}{U}"),
                              types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Nest", {"name": "Nest", "triggers": [
        {"on": "land_etb", "do": "create_token", "power": 1, "toughness": 1}]})
    annotated(cards, "Scribe", {"name": "Scribe", "triggers": [
        {"on": "creature_etb", "do": "draw", "count": 1}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Nest"); g.new_perm("Scribe")
    before = len(g.hand)
    fire(g, "land_etb")
    assert sum(1 for p in g.battlefield if p.is_token) == 1
    assert len(g.hand) == before + 1
    assert not any("depth limit" in line for line in g.log)
    # cause precedes effects: "created" is logged before the ETB draw
    created = next(i for i, line in enumerate(g.log) if "created 1 token(s)" in line)
    drew = next(i for i, line in enumerate(g.log) if "drew" in line)
    assert created < drew


def test_zero_count_skips_mutation_and_log_but_records_fire():
    cards = mini_cards()
    cards["Tactician"] = SimCard(data=make_data("Tactician", cost=parse_cost("{1}{W}"),
                                 types=frozenset({"creature"}), power=1, toughness=1),
                                 ann=None, scope_class=None)
    annotated(cards, "Tactician", {"name": "Tactician", "triggers": [
        {"on": "attack", "do": "draw", "count": "per_equipped_attacker"}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Tactician")
    bear = g.new_perm("Bear")
    before = len(g.hand)
    fire(g, "attack", ctx={"attackers": [bear.id]})     # bear unequipped -> n == 0
    assert len(g.hand) == before
    assert g.trigger_fires == {"Tactician|attack|draw": 1}   # fire still recorded
    assert not any("drew" in line for line in g.log)         # verb logged nothing
    log_len = len(g.log)
    execute_verb(g, None, "gain_life", count=0)          # uniform across verbs
    assert g.life_gained == 0 and len(g.log) == log_len


def test_snapshot_new_listener_misses_in_flight_event():
    # A listener's verb creates a permanent that itself listens for the SAME
    # event: the newcomer must not hear the dispatch that created it, but
    # participates in the next one.
    cards = mini_cards()
    cards["Summoner"] = SimCard(data=make_data("Summoner", cost=parse_cost("{2}"),
                                types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Summoner", {"name": "Summoner", "triggers": [
        {"on": "upkeep", "do": "token_copy"}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Summoner")
    fire(g, "upkeep")
    assert sum(1 for p in g.battlefield if p.is_token) == 1   # copy not in snapshot
    fire(g, "upkeep")                                         # both copy themselves now
    assert sum(1 for p in g.battlefield if p.is_token) == 3


def test_condition_evaluated_at_execution_time_within_dispatch():
    # An earlier listener's Treasure flips a later listener's metalcraft
    # inside one dispatch: conditions are checked at execution time, not
    # snapshot time.
    cards = mini_cards()
    cards["Smith"] = SimCard(data=make_data("Smith", cost=parse_cost("{2}"),
                             types=frozenset({"enchantment"})), ann=None, scope_class=None)
    cards["Auditor"] = SimCard(data=make_data("Auditor", cost=parse_cost("{2}"),
                               types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Smith", {"name": "Smith", "triggers": [
        {"on": "upkeep", "do": "treasure", "count": 1}]})
    annotated(cards, "Auditor", {"name": "Auditor", "triggers": [
        {"on": "upkeep", "if": "metalcraft", "do": "draw", "count": 1}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Smith")                          # listens first (battlefield order)
    g.new_perm("Auditor")
    g.new_perm("Hammer"); g.new_perm("Hammer")   # 2 artifacts: metalcraft off
    before = len(g.hand)
    fire(g, "upkeep")
    assert len(g.hand) == before + 1             # Smith's Treasure was the 3rd artifact
    assert g.trigger_fires["Auditor|upkeep|draw"] == 1


def test_missing_required_params_raise():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bear = g.new_perm("Bear")
    with pytest.raises(IllegalAction):
        execute_verb(g, None, "damage", count=1)                 # no target
    with pytest.raises(IllegalAction):
        execute_verb(g, None, "damage", count=1, target="everyone")
    with pytest.raises(IllegalAction):
        execute_verb(g, None, "create_token", count=1, power=1)  # no toughness
    with pytest.raises(IllegalAction):
        execute_verb(g, bear, "pump", count=1, power=1)          # no toughness
    with pytest.raises(IllegalAction):
        execute_verb(g, None, "add_mana", count=1)               # no pips / colors:any
    assert g.opponents == [40] and g.life_gained == 0            # nothing mutated


def test_spell_cast_noncreature_filter():
    cards = mini_cards()
    cards["Prowess"] = SimCard(data=make_data("Prowess", cost=parse_cost("{1}{R}"),
                               types=frozenset({"creature"}), power=1, toughness=1),
                               ann=None, scope_class=None)
    annotated(cards, "Prowess", {"name": "Prowess", "triggers": [
        {"on": "spell_cast", "filter": "noncreature",
         "do": "damage", "target": "each_opponent", "count": 1}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Prowess")
    fire(g, "spell_cast", spell=cards["Hammer"])    # artifact: noncreature
    assert g.opponents == [39]
    fire(g, "spell_cast", spell=cards["Bear"])      # creature: filtered out
    assert g.opponents == [39]


def test_token_doubling_stacks_multiplicatively():
    cards = mini_cards()
    cards["Doubler"] = SimCard(data=make_data("Doubler", cost=parse_cost("{4}"),
                               types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Doubler", {"name": "Doubler", "statics": [{"kind": "token_doubling"}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Doubler"); g.new_perm("Doubler")
    execute_verb(g, None, "create_token", count=1, power=1, toughness=1)
    assert sum(1 for p in g.battlefield if p.is_token) == 4     # 1 * 2^2
