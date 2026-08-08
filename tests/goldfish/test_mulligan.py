import json

import pytest

from goldfish.cards import SimCard, parse_cost
from goldfish.engine import (
    Game,
    IllegalAction,
    MulliganRules,
    auto_mulligan,
    bottom_order,
    keep_decision,
    legal_actions,
    new_game,
    step,
)
from tests.goldfish.helpers import make_data, mini_cards


def test_keep_decision_pins():
    cards = mini_cards()
    rules = MulliganRules()                     # defaults: sources 3..5, free_first True
    rocks0 = keep_decision(["Plains"] * 2 + ["Bear"] * 5, cards, rules, hand_size=7)
    assert rocks0 is False                      # 2 sources < min 3
    ok = keep_decision(["Plains"] * 3 + ["Bear"] * 4, cards, rules, hand_size=7)
    assert ok is True
    flood = keep_decision(["Plains"] * 6 + ["Bear"], cards, rules, hand_size=7)
    assert flood is False                       # 6 > max 5
    small = keep_decision(["Plains"] * 6, cards, rules, hand_size=5)
    assert small is True                        # <=5 cards always kept


def test_two_real_lands_floor():
    cards = mini_cards()
    cards["Signet"] = SimCard(data=make_data("Signet", cost=parse_cost("{2}"),
                              types=frozenset({"artifact"}), produces={"C": 1}),
                              ann=None, scope_class=None)
    rules = MulliganRules()
    hand = ["Plains", "Signet", "Signet", "Signet"] + ["Bear"] * 3
    assert keep_decision(hand, cards, rules, hand_size=7) is False  # 1 land < floor 2


def test_london_bottoming_deterministic():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=11)
    auto_mulligan(g, MulliganRules())
    assert g.phase == "main1" and g.turn == 1
    assert g.kept_hand_size is not None
    assert len(g.hand) == g.kept_hand_size + 1   # kept hand, plus turn 1's draw
    effective_mulls = g.mulligans_taken - (1 if g.free_mulligan_used else 0)
    assert g.kept_hand_size == 7 - effective_mulls
    g2 = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=11)
    auto_mulligan(g2, MulliganRules())
    assert g.hand == g2.hand and g.log == g2.log  # byte-identical decisions


def test_bottom_order_bottoms_excess_lands_before_spells():
    cards = mini_cards()
    rules = MulliganRules()
    hand = ["Plains"] * 6 + ["Bear"]               # 6 sources > max 5: 1 excess land
    order = bottom_order(hand, cards, rules, 1)
    assert order == ["Plains"]                     # excess land bottomed, not the spell


def test_bottom_order_ties_broken_by_name_among_equal_mv_spells():
    cards = mini_cards()
    cards["Signet"] = SimCard(data=make_data("Signet", cost=parse_cost("{2}"),
                              types=frozenset({"artifact"}), produces={"C": 1}),
                              ann=None, scope_class=None)
    rules = MulliganRules()
    # Signet appears before Bear in the hand, but Bear (mv 2) must sort ahead
    # of Signet (also mv 2) by name — proving the tie-break, not hand order.
    hand = ["Mountain", "Plains", "Signet", "Bear", "Runner"]
    order = bottom_order(hand, cards, rules, 2)
    assert order == ["Bear", "Signet"]             # both mv 2, name tie-break; Runner (mv 1) spared


def test_free_first_mulligan_bottoms_nothing():
    cards = mini_cards()
    rules = MulliganRules()                       # free_first=True default
    g = new_game(cards, ["Plains"] * 40 + ["Bear"] * 40, "Boss", seed=5)
    g.mulligans_taken = 1
    g.free_mulligan_used = True                    # simulate: the free mulligan already used
    g.hand[:] = ["Plains", "Plains", "Plains", "Bear", "Bear", "Bear", "Bear"]
    auto_mulligan(g, rules)
    assert g.mulligans_taken == 1                   # no additional mulligan taken
    assert g.phase == "main1" and g.turn == 1
    assert g.kept_hand_size == 7                    # the free mulligan doesn't shrink the kept hand
    assert "T0: kept 7" in g.log


def test_free_first_disabled_mulligan_bottoms_one():
    cards = mini_cards()
    rules = MulliganRules(free_first=False)
    g = new_game(cards, ["Plains"] * 40 + ["Bear"] * 40, "Boss", seed=6)
    g.mulligans_taken = 1                           # free_mulligan_used stays False: no free first
    g.hand[:] = ["Plains", "Plains", "Plains", "Bear", "Bear", "Bear", "Bear"]
    auto_mulligan(g, rules)
    assert g.mulligans_taken == 1
    assert g.phase == "main1" and g.turn == 1
    assert g.kept_hand_size == 6
    assert "T0: kept 6 (bottomed: Bear)" in g.log


def test_forced_keep_at_hand_size_floor():
    cards = mini_cards()
    rules = MulliganRules()
    g = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=9)
    g.mulligans_taken = 4
    g.free_mulligan_used = True                     # effective mulls = 3 -> hand_size 4
    g.hand[:] = ["Plains"] * 7                       # flood: would fail keep_decision above hand_size 5
    auto_mulligan(g, rules)
    assert g.mulligans_taken == 4                    # no further mulligan happened
    assert g.phase == "main1" and g.turn == 1
    assert g.kept_hand_size == 4
    assert len(g.hand) == 5                          # 4 kept, plus turn 1's draw


def test_kept_hand_stats_recorded_and_serialized():
    cards = mini_cards()
    rules = MulliganRules()
    g = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=10)
    assert g.kept_hand_size is None                 # nothing recorded before a keep
    assert g.kept_hand_lands is None
    assert g.kept_hand_sources is None
    auto_mulligan(g, rules)
    assert g.kept_hand_size == len(g.hand) - 1       # + turn 1's draw
    assert g.kept_hand_lands is not None
    assert g.kept_hand_sources is not None
    blob = json.loads(json.dumps(g.to_dict()))
    g2 = Game.from_dict(blob, cards)
    assert g2.kept_hand_size == g.kept_hand_size
    assert g2.kept_hand_lands == g.kept_hand_lands
    assert g2.kept_hand_sources == g.kept_hand_sources


def test_interactive_mulligan_then_keep_wrong_count_rejected():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=21)
    assert g.phase == "mulligan"
    step(g, {"type": "mulligan"})
    assert g.mulligans_taken == 1 and g.free_mulligan_used is True
    assert len(g.hand) == 7
    assert g.log[-1].endswith("mulligan to 7")       # free mulligan: still 7
    step(g, {"type": "mulligan"})
    assert g.mulligans_taken == 2
    assert g.log[-1].endswith("mulligan to 6")
    # Effective hand size is now 6: keeping requires bottoming exactly 1 card.
    snap = g.to_dict()
    with pytest.raises(IllegalAction):
        step(g, {"type": "keep", "bottom": []})       # wrong count: 0 != 1
    assert g.to_dict() == snap                          # unmutated on rejection
    with pytest.raises(IllegalAction):
        step(g, {"type": "keep", "bottom": ["NotInHand"]})  # not actually in hand
    assert g.to_dict() == snap
    name = g.hand[0]
    step(g, {"type": "keep", "bottom": [name]})
    assert g.phase == "main1" and g.turn == 1
    assert len(g.hand) == 7                          # 6 kept, plus turn 1's draw
    assert g.kept_hand_size == 6


def test_keep_bottoms_duplicate_basics():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=8)
    for _ in range(3):
        step(g, {"type": "mulligan"})
    assert g.mulligans_taken == 3                    # effective mulls = 2 -> hand_size 5
    g.hand[:] = ["Plains", "Plains", "Bear", "Bear", "Bear", "Bear", "Bear"]
    step(g, {"type": "keep", "bottom": ["Plains", "Plains"]})
    assert g.phase == "main1" and g.turn == 1
    assert g.hand.count("Plains") == 0
    assert len(g.hand) == 6                          # 5 kept, plus turn 1's draw
    assert g.kept_hand_size == 5
    assert "T0: kept 5 (bottomed: Plains, Plains)" in g.log


def test_mulligan_and_keep_illegal_outside_mulligan_phase():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=13)
    step(g, {"type": "keep", "bottom": []})           # keep the opening 7 untouched
    assert g.phase == "main1"
    with pytest.raises(IllegalAction):
        step(g, {"type": "mulligan"})
    with pytest.raises(IllegalAction):
        step(g, {"type": "keep", "bottom": []})


def test_mulligan_illegal_at_floor():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=14)
    g.mulligans_taken = 4
    g.free_mulligan_used = True                       # hand_size floor (4) reached
    with pytest.raises(IllegalAction):
        step(g, {"type": "mulligan"})


def test_legal_actions_mulligan_phase_shape():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=12)
    assert legal_actions(g) == [{"type": "mulligan"}, {"type": "keep", "bottom_count": 0}]
    step(g, {"type": "mulligan"})
    assert legal_actions(g) == [{"type": "mulligan"}, {"type": "keep", "bottom_count": 0}]
    step(g, {"type": "mulligan"})
    assert legal_actions(g) == [{"type": "mulligan"}, {"type": "keep", "bottom_count": 1}]
    g.mulligans_taken = 4
    g.free_mulligan_used = True
    assert legal_actions(g) == [{"type": "keep", "bottom_count": 3}]      # floor: no more mulligans
