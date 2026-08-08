import json

import pytest

from goldfish.cards import AnnotationError, SimCard, parse_cost, validate_annotations
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
    legal_actions,
    new_game,
    pay,
    resolve_count,
    step,
    untapped_producers,
)
from tests.goldfish.test_cards import make_data  # reuse the factory


def bf_land(g, name):
    p = g.new_perm(name)
    return p


def started(cards, deck, seed=1, hand=None):
    """A game skipped past the mulligan into turn 1 main1 (module-level for
    reuse by later task tests). Land drops derive from _land_drops_used (0 by
    default), so one drop is available without further setup."""
    g = new_game(cards, deck, "Boss", seed=seed)
    g.phase = "main1"
    g.turn = 1
    if hand is not None:
        g.hand[:] = hand
    return g


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
    g._land_drops_used = 1
    g.static_active_turn["Doubler|token_doubling"] = 2
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
    # land drops serialize as the used counter; remaining is computed display
    assert g2._land_drops_used == g._land_drops_used == 1
    assert g2.land_drops_remaining == g.land_drops_remaining == 0   # 1 allowed - 1 used
    assert blob["land_drops_used"] == 1
    assert blob["land_drops_remaining"] == 0
    assert (g2.static_active_turn == g.static_active_turn
            == {"Doubler|token_doubling": 2})

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
    with pytest.raises(IllegalAction):
        execute_verb(g, None, "scry", count=0)   # even at count 0: the verb
        # check runs before the zero-count early return


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
    assert sum("trigger limit reached" in line for line in g.log) == 1
    assert g._fire_depth == 0                   # fully unwound
    execute_verb(g, None, "create_token", count=1, power=1, toughness=1)
    assert sum("trigger limit reached" in line for line in g.log) == 2   # next
    # cascade warns afresh


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
    assert not any("trigger limit" in line for line in g.log)
    # cause precedes effects: "created" is logged before the ETB draw
    created = next(i for i, line in enumerate(g.log) if "created 1 token(s)" in line)
    drew = next(i for i, line in enumerate(g.log) if "drew" in line)
    assert created < drew


def test_branching_trigger_cascade_bounded_by_fire_budget():
    # Reviewer's breadth repro: TWO creature_etb -> create_token listeners
    # branch into 2^depth fires, so the depth cap alone never terminates in
    # useful time (65s at depth 14, ~days at 20). The per-cascade fire budget
    # must cut the cascade off: every executed fire spawns at most 2 tokens,
    # so the battlefield stays within 2 * budget + the direct token.
    from goldfish.engine import _MAX_CASCADE_FIRES
    cards = mini_cards()
    for name in ("BroodA", "BroodB"):
        cards[name] = SimCard(data=make_data(name, cost=parse_cost("{3}{G}"),
                              types=frozenset({"enchantment"})), ann=None, scope_class=None)
        annotated(cards, name, {"name": name, "triggers": [
            {"on": "creature_etb", "do": "create_token", "power": 1, "toughness": 1}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("BroodA"); g.new_perm("BroodB")
    execute_verb(g, None, "create_token", count=1, power=1, toughness=1)
    tokens = sum(1 for p in g.battlefield if p.is_token)
    assert _MAX_CASCADE_FIRES <= tokens <= 2 * _MAX_CASCADE_FIRES + 1
    assert sum("trigger limit reached" in line for line in g.log) == 1
    assert g._fire_depth == 0 and g._cascade_fires == 0        # budget reset
    execute_verb(g, None, "create_token", count=1, power=1, toughness=1)
    assert sum("trigger limit reached" in line for line in g.log) == 2   # fresh
    # budget and warning per cascade


def test_negative_count_noops_defensively():
    # annotation-level validation rejects negative counts; a direct engine
    # call with one must still no-op (no heal-by-negative-damage), unlogged
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    log_len = len(g.log)
    execute_verb(g, None, "damage", count=-3, target="each_opponent")
    assert g.opponents == [40]
    assert len(g.log) == log_len


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


# -- Task 7: play_land / cast / pass actions + turn engine -----------------

def test_play_land_and_drop_counter():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=["Plains", "Plains"])
    step(g, {"type": "play_land", "card": "Plains"})
    assert g.land_drops_remaining == 0
    with pytest.raises(IllegalAction):
        step(g, {"type": "play_land", "card": "Plains"})
    assert len(g.hand) == 1                      # unmutated on rejection


def test_play_land_enters_tapped_and_fires_land_etb():
    cards = mini_cards()
    cards["Gate"] = SimCard(data=make_data("Gate", types=frozenset({"land"}),
                            produces={"W": 1}, enters_tapped=True),
                            ann=None, scope_class=None)
    cards["Landfall"] = SimCard(data=make_data("Landfall", cost=parse_cost("{1}{G}"),
                                types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Landfall", {"name": "Landfall", "triggers": [
        {"on": "land_etb", "do": "gain_life", "count": 1}]})
    g = started(cards, ["Plains"] * 40, hand=["Gate"])
    g.new_perm("Landfall")
    step(g, {"type": "play_land", "card": "Gate"})
    gate = next(p for p in g.battlefield if p.name == "Gate")
    assert gate.tapped is True                   # enters_tapped respected
    assert g.life_gained == 1                    # land_etb fired for the drop
    played = next(i for i, line in enumerate(g.log) if "played Gate" in line)
    trig = next(i for i, line in enumerate(g.log) if "Landfall trigger" in line)
    assert played < trig                         # cause precedes effects


def test_cast_commander_with_tax():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=[])
    for _ in range(3):
        g.new_perm("Mountain")
    step(g, {"type": "cast", "card": "Boss"})            # {2}{R}, exact
    assert g.commander_casts == 1 and g.command == []
    # return to command zone by hand for tax test:
    g.battlefield = [p for p in g.battlefield if p.name != "Boss"]
    g.command = ["Boss"]
    for p in g.battlefield:
        p.tapped = False
    assert not any(a["type"] == "cast" and a["card"] == "Boss"
                   for a in legal_actions(g))            # 3 lands can't pay {4}{R}


def test_commander_recast_logs_tax():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=[])
    g.commander_casts = 1                                # simulate a prior cast
    for _ in range(5):
        g.new_perm("Mountain")
    step(g, {"type": "cast", "card": "Boss"})            # {2}{R} + {2} tax, exact
    assert g.commander_casts == 2 and g.command == []
    assert any("cast Boss (commander, tax 2)" in line for line in g.log)


def test_pass_advances_and_new_turn_untaps_draws():
    cards = mini_cards()
    g = started(cards, ["Bear"] * 40, hand=[])
    p = g.new_perm("Plains"); p.tapped = True
    for ph in ("combat", "main2", "end"):
        step(g, {"type": "pass"})
        if ph != "end":
            assert g.phase == ph
    assert g.turn == 2 and g.phase == "main1"
    assert p.tapped is False and len(g.hand) == 1        # untap + draw
    assert g.land_drops_remaining == 1 and g.spells_cast_this_turn == 0


def test_new_turn_clears_pump_eot_pool_and_counters():
    cards = mini_cards()
    g = started(cards, ["Bear"] * 40, hand=[])
    bear = g.new_perm("Bear")
    bear.pump_eot[:] = [3, 3]
    bear.pump_perm[:] = [1, 1]
    g.spells_cast_this_turn = 2
    g._land_drops_used = 1
    g.combats_done = 1
    g.extra_combats = 1
    g.mana_pool["R"] = 2                        # pool persists for the turn only
    for _ in range(3):
        step(g, {"type": "pass"})
    assert g.turn == 2
    assert bear.pump_eot == [0, 0] and bear.pump_perm == [1, 1]
    assert g.spells_cast_this_turn == 0 and g.land_drops_remaining == 1
    assert g.combats_done == 0 and g.extra_combats == 0
    assert g.mana_pool == {c: 0 for c in "WUBRG"} | {"C": 0, "any": 0}


def test_dynamic_extra_land_drops():
    cards = mini_cards()
    cards["Azusa"] = SimCard(data=make_data("Azusa", cost=parse_cost("{2}{G}"),
                             types=frozenset({"creature"}), power=1, toughness=2),
                             ann=None, scope_class=None)
    annotated(cards, "Azusa", {"name": "Azusa",
                               "statics": [{"kind": "extra_land_drops", "count": 2}]})
    g = started(cards, ["Plains"] * 40, hand=["Plains", "Plains", "Plains"])
    step(g, {"type": "play_land", "card": "Plains"})
    g.new_perm("Azusa")                                   # arrives mid-turn
    assert g.land_drops_remaining == 2                    # dynamic: 1+2 total, 1 used
    step(g, {"type": "play_land", "card": "Plains"})
    step(g, {"type": "play_land", "card": "Plains"})
    assert g.land_drops_remaining == 0


def test_cast_counts_spell_before_own_triggers_and_feeds_listeners():
    # Pinned (spec §Card annotation DSL, Counts): a spell's own cast increments
    # spells_cast_this_turn BEFORE its triggers resolve — Grapeshot-style
    # counts include the spell itself — and battlefield spell_cast listeners
    # DO hear the cast (the spell itself is not a listener).
    cards = mini_cards()
    cards["Bolt"] = SimCard(data=make_data("Bolt", cost=parse_cost("{R}"),
                            types=frozenset({"instant"})), ann=None, scope_class=None)
    annotated(cards, "Bolt", {"name": "Bolt", "triggers": [
        {"on": "cast", "do": "damage", "target": "one_opponent",
         "count": "per_spell_cast_this_turn"}]})
    cards["Shrine"] = SimCard(data=make_data("Shrine", cost=parse_cost("{2}"),
                              types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Shrine", {"name": "Shrine", "triggers": [
        {"on": "spell_cast", "do": "gain_life", "count": 1}]})
    g = started(cards, ["Plains"] * 40, hand=["Bolt"])
    g.new_perm("Shrine")
    for _ in range(3):
        g.new_perm("Mountain")
    step(g, {"type": "cast", "card": "Bolt"})
    assert g.opponents == [39]                  # exactly 1 damage, not 0
    assert g.life_gained == 1                   # battlefield listener fired
    assert g.graveyard == ["Bolt"]              # instant resolved to graveyard
    assert g.spells_cast_this_turn == 1
    assert g.trigger_fires["Bolt|cast|damage"] == 1


def test_cast_permanent_self_triggers_then_global_etb_excluding_self():
    cards = mini_cards()
    cards["Herald"] = SimCard(data=make_data("Herald", cost=parse_cost("{1}"),
                              types=frozenset({"creature"}), power=1, toughness=1),
                              ann=None, scope_class=None)
    annotated(cards, "Herald", {"name": "Herald", "triggers": [
        {"on": "cast", "do": "treasure", "count": 1},
        {"on": "etb", "do": "gain_life", "count": 1},
        {"on": "creature_etb", "do": "draw", "count": 1}]})
    cards["Watcher"] = SimCard(data=make_data("Watcher", cost=parse_cost("{2}"),
                               types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Watcher", {"name": "Watcher", "triggers": [
        {"on": "creature_etb", "do": "draw", "count": 1}]})
    g = started(cards, ["Plains"] * 40, hand=["Herald"])
    g.new_perm("Watcher")
    g.new_perm("Plains")
    step(g, {"type": "cast", "card": "Herald"})
    assert sum(1 for p in g.battlefield if p.name == "Treasure") == 1   # self cast
    assert g.life_gained == 1                                           # self etb
    assert len(g.hand) == 1                     # Watcher drew exactly 1; Herald's own
                                                # creature_etb listener was excluded
    assert "Herald|creature_etb|draw" not in g.trigger_fires
    cast_i = next(i for i, line in enumerate(g.log) if "cast Herald" in line)
    self_cast = next(i for i, line in enumerate(g.log)
                     if "Herald trigger — treasure" in line)
    self_etb = next(i for i, line in enumerate(g.log)
                    if "Herald trigger — gain_life" in line)
    global_etb = next(i for i, line in enumerate(g.log)
                      if "Watcher trigger — draw" in line)
    assert cast_i < self_cast < self_etb < global_etb


def test_cast_equipment_fires_equipment_etb():
    cards = mini_cards()
    cards["Smith"] = SimCard(data=make_data("Smith", cost=parse_cost("{2}"),
                             types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Smith", {"name": "Smith", "triggers": [
        {"on": "equipment_etb", "do": "gain_life", "count": 1}]})
    g = started(cards, ["Plains"] * 40, hand=["Hammer"])
    g.new_perm("Smith")
    g.new_perm("Plains")
    step(g, {"type": "cast", "card": "Hammer"})
    assert g.life_gained == 1
    hammer = next(p for p in g.battlefield if p.name == "Hammer")
    assert hammer.tapped is False               # permanents enter untapped


def test_illegal_actions_leave_game_unmutated():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=["Bear", "Plains"])
    snap = g.to_dict()
    for bad in (
        {"type": "cast", "card": "Bear"},           # {1}{G} with no producers
        {"type": "play_land", "card": "Mountain"},  # not in hand
        {"type": "cast", "card": "Plains"},         # lands aren't cast
        {"type": "cast", "card": "Missing"},        # not in hand or command
        {"type": "attack", "attackers": []},        # Task 9 — not implemented yet
        {"type": "attach", "card": "x", "target": "y"},   # Task 8
        {"type": "mulligan"},                       # Task 10
        {"type": "frobnicate"},                     # unknown type
    ):
        with pytest.raises(IllegalAction):
            step(g, bad)
    assert g.to_dict() == snap                      # byte-identical state
    g.phase = "combat"
    snap2 = g.to_dict()
    with pytest.raises(IllegalAction):
        step(g, {"type": "play_land", "card": "Plains"})   # wrong phase
    with pytest.raises(IllegalAction):
        step(g, {"type": "cast", "card": "Bear"})          # casts are main-phase
    assert g.to_dict() == snap2


def test_cost_reduction_color_filter_shaves_generic_never_pips():
    cards = mini_cards()
    cards["Medallion"] = SimCard(data=make_data("Medallion", cost=parse_cost("{2}"),
                                 types=frozenset({"artifact"})), ann=None, scope_class=None)
    cards["Ember"] = SimCard(data=make_data("Ember", cost=parse_cost("{2}{R}"),
                             types=frozenset({"creature"}), power=2, toughness=2),
                             ann=None, scope_class=None)
    cards["Forest"] = SimCard(data=make_data("Forest", types=frozenset({"land"}),
                              produces={"G": 1}), ann=None, scope_class=None)
    annotated(cards, "Medallion", {"name": "Medallion", "statics": [
        {"kind": "cost_reduction", "filter": "color:R", "amount": 1}]})
    # {2}{R} -> {1}{R}: Mountain + Plains is exactly enough
    g = started(cards, ["Plains"] * 40, hand=["Ember"])
    g.new_perm("Medallion")
    g.new_perm("Mountain"); g.new_perm("Plains")
    step(g, {"type": "cast", "card": "Ember"})
    assert all(p.tapped for p in g.battlefield if g.card(p.name).is_land)
    # never touches pips: a {R} spell still needs a real R source
    g2 = started(cards, ["Plains"] * 40, hand=["Runner"])
    g2.new_perm("Medallion")
    g2.new_perm("Plains")
    with pytest.raises(IllegalAction):
        step(g2, {"type": "cast", "card": "Runner"})
    # color filter keys off the card's pips: {1}{G} has no R pip, no discount
    g3 = started(cards, ["Plains"] * 40, hand=["Bear"])
    g3.new_perm("Medallion")
    g3.new_perm("Forest")
    with pytest.raises(IllegalAction):
        step(g3, {"type": "cast", "card": "Bear"})


def test_cost_reduction_equipment_filter_and_floor_at_zero():
    cards = mini_cards()
    cards["Forge"] = SimCard(data=make_data("Forge", cost=parse_cost("{2}"),
                             types=frozenset({"enchantment"})), ann=None, scope_class=None)
    cards["Sword"] = SimCard(data=make_data("Sword", cost=parse_cost("{3}"),
                             types=frozenset({"artifact", "equipment"}),
                             equip_cost=parse_cost("{1}")), ann=None, scope_class=None)
    cards["Golem"] = SimCard(data=make_data("Golem", cost=parse_cost("{3}"),
                             types=frozenset({"artifact", "creature"}),
                             power=3, toughness=3), ann=None, scope_class=None)
    annotated(cards, "Forge", {"name": "Forge", "statics": [
        {"kind": "cost_reduction", "filter": "equipment", "amount": 2}]})
    g = started(cards, ["Plains"] * 40, hand=["Sword", "Golem", "Hammer"])
    g.new_perm("Forge")
    g.new_perm("Plains")
    casts = [a["card"] for a in legal_actions(g) if a["type"] == "cast"]
    assert "Sword" in casts and "Golem" not in casts    # filter: equipment only
    # floor at 0 generic: Hammer {1} - 2 is {0} — castable with no mana at all
    g.battlefield = [p for p in g.battlefield if p.name == "Forge"]
    step(g, {"type": "cast", "card": "Hammer"})
    assert any(p.name == "Hammer" for p in g.battlefield)
    assert g.mana_pool == {c: 0 for c in "WUBRG"} | {"C": 0, "any": 0}


def test_cost_reduction_applies_to_commander_tax():
    cards = mini_cards()
    cards["Helm"] = SimCard(data=make_data("Helm", cost=parse_cost("{1}"),
                            types=frozenset({"artifact"})), ann=None, scope_class=None)
    annotated(cards, "Helm", {"name": "Helm", "statics": [
        {"kind": "cost_reduction", "filter": "creature", "amount": 2}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    g.commander_casts = 1                       # tax {2}: total {4}{R} before reduction
    g.new_perm("Helm")
    for _ in range(3):
        g.new_perm("Mountain")
    step(g, {"type": "cast", "card": "Boss"})   # ({2}+{2}-{2}){R} = {2}{R}: 3 lands pay
    assert g.commander_casts == 2 and g.command == []


def test_legal_actions_factored_and_deterministic():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40,
                hand=["Plains", "Plains", "Mountain", "Runner", "Bear", "Hammer"])
    for _ in range(2):
        g.new_perm("Mountain")
    g.new_perm("Plains")
    acts = legal_actions(g)
    assert acts == [
        {"type": "play_land", "card": "Mountain"},     # lands deduped, name-sorted
        {"type": "play_land", "card": "Plains"},
        {"type": "cast", "card": "Hammer"},            # casts by (mv, name)
        {"type": "cast", "card": "Runner"},
        {"type": "cast", "card": "Boss"},              # commander, tax 0, payable
        {"type": "pass"},
    ]                                                  # Bear {1}{G}: no G source
    g._land_drops_used = 1
    assert not any(a["type"] == "play_land" for a in legal_actions(g))
    g.phase = "combat"
    assert legal_actions(g) == [{"type": "pass"}]      # main-phase actions gone


def test_static_active_turn_records_first_activation():
    cards = mini_cards()
    cards["Doubler"] = SimCard(data=make_data("Doubler", cost=parse_cost("{2}"),
                               types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Doubler", {"name": "Doubler",
                                 "statics": [{"kind": "token_doubling"}]})
    g = started(cards, ["Plains"] * 40, hand=["Doubler"])
    g.new_perm("Plains"); g.new_perm("Plains")
    step(g, {"type": "cast", "card": "Doubler"})
    assert g.static_active_turn == {"Doubler|token_doubling": 1}
    for _ in range(3):
        g.new_perm("Hammer")                    # metalcraft flips on mid-turn
    step(g, {"type": "pass"})                   # recorded after the next mutation
    assert g.static_active_turn["condition|metalcraft"] == 1
    step(g, {"type": "pass"}); step(g, {"type": "pass"})       # into turn 2
    assert g.turn == 2
    assert g.static_active_turn["Doubler|token_doubling"] == 1  # first turn sticks


def test_malformed_add_mana_pips_rejected_before_any_cast():
    # Regression: "{Q}{R}" used to pass validate_annotations and blow up with
    # CostParseError mid-cast, AFTER pay() — tapped land, emptied hand,
    # incremented counters. The annotation is now rejected up front, so the
    # corrupting cast path is unreachable.
    cards = mini_cards()
    cards["Ritual"] = SimCard(data=make_data("Ritual", cost=parse_cost("{R}"),
                              types=frozenset({"instant"})), ann=None, scope_class=None)
    with pytest.raises(AnnotationError):
        annotated(cards, "Ritual", {"name": "Ritual", "triggers": [
            {"on": "cast", "do": "add_mana", "pips": "{Q}{R}"}]})
    assert cards["Ritual"].ann is None          # card pool untouched by the attempt


def test_unknown_card_name_raises_illegal_action_not_keyerror():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=["Ghost"])   # in hand, not in pool
    snap = g.to_dict()
    with pytest.raises(IllegalAction):
        step(g, {"type": "play_land", "card": "Ghost"})
    with pytest.raises(IllegalAction):
        step(g, {"type": "cast", "card": "Ghost"})
    with pytest.raises(IllegalAction):
        step(g, {"type": "cast", "card": None})           # name must be a string
    assert g.to_dict() == snap


# -- Task 8: attach / activate actions + equip statics ----------------------

def test_attach_pays_equip_cost_and_free_static():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=[])
    bear = g.new_perm("Bear"); ham = g.new_perm("Hammer")
    for _ in range(8):
        g.new_perm("Plains")
    step(g, {"type": "attach", "card": ham.id, "target": bear.id})
    assert ham.attached_to == bear.id and bear.attached == [ham.id]
    assert sum(1 for p in g.battlefield if p.tapped) == 8      # paid {8}
    # free-equip static:
    cards["Aid"] = SimCard(data=make_data("Aid", cost=parse_cost("{W}"),
                           types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Aid", {"name": "Aid", "statics": ["equip_free"]})
    g2 = started(cards, ["Plains"] * 40, hand=[])
    b2, h2 = g2.new_perm("Bear"), g2.new_perm("Hammer")
    g2.new_perm("Aid")
    step(g2, {"type": "attach", "card": h2.id, "target": b2.id})
    assert not any(p.tapped for p in g2.battlefield)           # free


def test_tap_activation_respects_summoning_sickness():
    cards = mini_cards()
    cards["Krenko"] = SimCard(data=make_data("Krenko", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature"}), power=3, toughness=3),
                              ann=None, scope_class=None)
    annotated(cards, "Krenko", {"name": "Krenko", "activated": [
        {"cost": "{T}", "do": "create_token", "power": 1, "toughness": 1,
         "count": "per_creature"}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    k = g.new_perm("Krenko")                     # arrived this turn, no haste
    with pytest.raises(IllegalAction):
        step(g, {"type": "activate", "card": k.id, "ability": 0})
    g.turn += 1                                  # next turn
    step(g, {"type": "activate", "card": k.id, "ability": 0})
    assert k.tapped and sum(1 for p in g.battlefield if p.is_token) == 1


def test_attach_ambiguous_name_rejected_with_candidate_ids():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=[])
    b1 = g.new_perm("Bear")
    b2 = g.new_perm("Bear")
    ham = g.new_perm("Hammer")
    for _ in range(8):
        g.new_perm("Plains")
    with pytest.raises(IllegalAction) as exc:
        step(g, {"type": "attach", "card": ham.id, "target": "Bear"})
    msg = str(exc.value)
    assert b1.id in msg and b2.id in msg
    # unresolvable ref is rejected too, distinctly from ambiguity
    with pytest.raises(IllegalAction):
        step(g, {"type": "attach", "card": ham.id, "target": "Ghost"})


def test_attach_reattach_moves_equipment_between_bearers():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=[])
    bear = g.new_perm("Bear")
    boss = g.new_perm("Boss")
    ham = g.new_perm("Hammer")
    for _ in range(16):
        g.new_perm("Plains")
    step(g, {"type": "attach", "card": ham.id, "target": bear.id})
    assert ham.attached_to == bear.id and bear.attached == [ham.id]
    step(g, {"type": "attach", "card": ham.id, "target": boss.id})
    assert ham.attached_to == boss.id
    assert boss.attached == [ham.id]
    assert bear.attached == []                    # old bearer's list emptied


def test_attach_equip_free_if_metalcraft_gates_on_three_artifacts():
    cards = mini_cards()
    cards["Manual"] = SimCard(data=make_data("Manual", cost=parse_cost("{1}"),
                              types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Manual", {"name": "Manual",
                                "statics": ["equip_free_if_metalcraft"]})
    # 3 artifacts on board (the equipment + 2 spares): metalcraft active, free
    g = started(cards, ["Plains"] * 40, hand=[])
    bear = g.new_perm("Bear")
    ham = g.new_perm("Hammer")
    g.new_perm("Hammer"); g.new_perm("Hammer")
    g.new_perm("Manual")
    step(g, {"type": "attach", "card": ham.id, "target": bear.id})
    assert not any(p.tapped for p in g.battlefield)

    # only 1 artifact on board: metalcraft inactive, cost is paid in full
    g2 = started(cards, ["Plains"] * 40, hand=[])
    bear2 = g2.new_perm("Bear")
    ham2 = g2.new_perm("Hammer")
    g2.new_perm("Manual")
    for _ in range(8):
        g2.new_perm("Plains")
    step(g2, {"type": "attach", "card": ham2.id, "target": bear2.id})
    assert sum(1 for p in g2.battlefield if p.tapped) == 8


def test_attach_and_activate_illegal_variants_leave_game_unmutated():
    cards = mini_cards()
    cards["Krenko"] = SimCard(data=make_data("Krenko", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature"}), power=3, toughness=3),
                              ann=None, scope_class=None)
    annotated(cards, "Krenko", {"name": "Krenko", "activated": [
        {"cost": "{T}", "do": "create_token", "power": 1, "toughness": 1,
         "count": "per_creature"}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    bear = g.new_perm("Bear")
    boss = g.new_perm("Boss")
    ham = g.new_perm("Hammer")
    krenko = g.new_perm("Krenko")                # arrives this turn: summoning sick
    snap = g.to_dict()
    for bad in (
        {"type": "attach", "card": bear.id, "target": ham.id},   # card not equipment
        {"type": "attach", "card": ham.id, "target": ham.id},    # target not creature
        {"type": "attach", "card": ham.id, "target": bear.id},   # can't pay {8}
        {"type": "attach", "card": "Ghost", "target": bear.id},  # unresolvable card
        {"type": "attach", "card": ham.id, "target": "Ghost"},   # unresolvable target
        {"type": "activate", "card": krenko.id, "ability": 5},   # bad ability index
        {"type": "activate", "card": krenko.id, "ability": 0},   # summoning sick
        {"type": "activate", "card": "Ghost", "ability": 0},     # unresolvable
        {"type": "activate", "card": boss.id, "ability": 0},     # no activated abilities
    ):
        with pytest.raises(IllegalAction):
            step(g, bad)
    assert g.to_dict() == snap


def test_attach_and_activate_rejected_outside_main_phases():
    cards = mini_cards()
    cards["Krenko"] = SimCard(data=make_data("Krenko", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature"}), power=3, toughness=3),
                              ann=None, scope_class=None)
    annotated(cards, "Krenko", {"name": "Krenko", "activated": [
        {"cost": "{T}", "do": "draw", "count": 1}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    bear = g.new_perm("Bear")
    ham = g.new_perm("Hammer")
    krenko = g.new_perm("Krenko")
    g.turn += 1
    g.phase = "combat"
    snap = g.to_dict()
    with pytest.raises(IllegalAction):
        step(g, {"type": "attach", "card": ham.id, "target": bear.id})
    with pytest.raises(IllegalAction):
        step(g, {"type": "activate", "card": krenko.id, "ability": 0})
    assert g.to_dict() == snap


def test_activate_by_unique_name():
    cards = mini_cards()
    cards["Krenko"] = SimCard(data=make_data("Krenko", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature"}), power=3, toughness=3),
                              ann=None, scope_class=None)
    annotated(cards, "Krenko", {"name": "Krenko", "activated": [
        {"cost": "{T}", "do": "create_token", "power": 1, "toughness": 1,
         "count": "per_creature"}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    k = g.new_perm("Krenko")
    g.turn += 1
    step(g, {"type": "activate", "card": "Krenko", "ability": 0})
    assert k.tapped and sum(1 for p in g.battlefield if p.is_token) == 1


def test_activate_haste_creature_can_tap_on_arrival_turn():
    cards = mini_cards()
    annotated(cards, "Runner", {"name": "Runner", "activated": [
        {"cost": "{T}", "do": "draw", "count": 1}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    runner = g.new_perm("Runner")                # arrives this turn, but has haste
    before = len(g.hand)
    step(g, {"type": "activate", "card": runner.id, "ability": 0})
    assert runner.tapped and len(g.hand) == before + 1


def test_activate_mana_cost_paid_and_untapped_ability_ignores_sickness():
    # A non-{T} activated ability on a summoning-sick creature is legal: only
    # {T} abilities are gated by D8.
    cards = mini_cards()
    annotated(cards, "Bear", {"name": "Bear", "activated": [
        {"cost": "{1}{G}", "do": "gain_life", "count": 3}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    bear = g.new_perm("Bear")                    # arrives this turn, no haste
    cards["Forest"] = SimCard(data=make_data("Forest", types=frozenset({"land"}),
                              produces={"G": 1}), ann=None, scope_class=None)
    g.new_perm("Forest"); g.new_perm("Plains")
    step(g, {"type": "activate", "card": bear.id, "ability": 0})
    assert bear.tapped is False                  # no {T} in the cost
    assert g.life_gained == 3
    assert all(p.tapped for p in g.battlefield if g.card(p.name).is_land)
    assert any("activated Bear ability 0" in line for line in g.log)


def test_legal_actions_includes_attach_and_activate_in_pinned_order():
    cards = mini_cards()
    annotated(cards, "Boss", {"name": "Boss", "activated": [
        {"cost": "{T}", "do": "draw", "count": 1}]})
    g = started(cards, ["Plains"] * 40, hand=["Plains", "Runner"])
    g.new_perm("Mountain")                        # untapped R source for Runner
    bear = g.new_perm("Bear")
    boss = g.new_perm("Boss")
    ham = g.new_perm("Hammer")
    g.turn += 1                                   # clears Boss's summoning sickness
    acts = legal_actions(g)
    assert acts == [
        {"type": "play_land", "card": "Plains"},
        {"type": "cast", "card": "Runner"},
        {"type": "attach", "card": ham.id, "target": bear.id},
        {"type": "attach", "card": ham.id, "target": boss.id},
        {"type": "activate", "card": boss.id, "ability": 0},
        {"type": "pass"},
    ]
