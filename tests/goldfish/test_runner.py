"""Task 14: batch runner — determinism, timeline records, per-turn action cap."""
import json
from collections import Counter

from goldfish.engine import MulliganRules, auto_mulligan, new_game
from goldfish.runner import play_one_game, run_batch
from tests.goldfish.test_engine import annotated, mini_cards


def small_deck():
    return ["Plains"] * 15 + ["Mountain"] * 15 + ["Bear"] * 5 + ["Runner"] * 4


# -- plan tests ---------------------------------------------------------------


def test_batch_deterministic_and_seed_paired():
    cards = mini_cards()
    r1 = run_batch(cards, small_deck(), "Boss", n=20, seed=42, until_turn=6)
    r2 = run_batch(cards, small_deck(), "Boss", n=20, seed=42, until_turn=6)
    assert r1["metrics"] == r2["metrics"]
    r3 = run_batch(cards, small_deck(), "Boss", n=20, seed=43, until_turn=6)
    assert r1["metrics"] != r3["metrics"]


def test_run_plays_games_to_until_turn():
    cards = mini_cards()
    r = run_batch(cards, small_deck(), "Boss", n=5, seed=1, until_turn=6)
    assert r["n"] == 5
    assert r["metrics"]["commander_cast"]["reached_pct"]["value"] > 0


# -- per-turn action cap (pinned): degenerate-but-legal loops degrade, not hang


def livelock_cards():
    """mini_cards plus the Task 12 review livelock pair: a creature_etb ->
    treasure listener and a non-tap "{1}: create_token" ability. Each
    activation taps one producer and mints a fresh untapped Treasure, so the
    multi-pass policy loop would re-fire it forever."""
    cards = mini_cards()
    from goldfish.cards import SimCard, parse_cost
    from tests.goldfish.test_cards import make_data

    cards["Mint"] = SimCard(data=make_data(
        "Mint", cost=parse_cost("{1}"), types=frozenset({"artifact"})),
        ann=None, scope_class=None)
    annotated(cards, "Mint", {"name": "Mint", "activated": [
        {"cost": "{1}", "do": "create_token", "power": 1, "toughness": 1}]})
    cards["Nursery"] = SimCard(data=make_data(
        "Nursery", cost=parse_cost("{1}"), types=frozenset({"enchantment"})),
        ann=None, scope_class=None)
    annotated(cards, "Nursery", {"name": "Nursery", "triggers": [
        {"on": "creature_etb", "do": "treasure"}]})
    return cards


def test_turn_action_cap_forces_pass_on_livelock():
    cards = livelock_cards()
    deck = ["Plains"] * 20 + ["Mint"] * 5 + ["Nursery"] * 5
    rec, log = play_one_game(cards, deck, "Boss", game_seed=5, until_turn=3)
    cap_lines = [ln for ln in log if ln.endswith("action cap reached — forcing pass")]
    assert cap_lines, "per-turn action cap never engaged on a livelocking board"
    # logged at most ONCE per turn (pinned)
    per_turn = Counter(ln.split(":", 1)[0] for ln in cap_lines)
    assert all(count == 1 for count in per_turn.values())
    # graceful degradation: the game still completed with a full record
    assert len(rec.casts_by_turn) == 3
    assert sum(rec.tokens_created_by_turn) > 0


# -- record contents ----------------------------------------------------------


def test_opening_hand_is_kept_hand_before_turn_one_draw():
    cards = mini_cards()
    rec, _log = play_one_game(cards, small_deck(), "Boss", game_seed=3,
                              until_turn=2)
    assert len(rec.opening_hand) == rec.kept_hand_size
    # Independent replay through the engine primitives: after auto_mulligan
    # the hand is the kept hand plus the turn-1 draw appended at the end.
    g = new_game(mini_cards(), small_deck(), "Boss", seed=3)
    auto_mulligan(g, MulliganRules())
    assert rec.opening_hand == g.hand[: rec.kept_hand_size]
    assert rec.opening_hand != g.hand              # turn-1 draw is excluded


def test_synthetic_combo_casts_excluded_from_cast_metrics():
    cards = mini_cards()
    rec, log = play_one_game(
        cards, small_deck(), "Boss", game_seed=7, until_turn=6,
        combos=[{"cards": ["Plains", "Boss"], "wins": True}])
    assert rec.won_turn is not None
    w = rec.won_turn
    combo_lines = [ln for ln in log
                   if ln.startswith(f"T{w}: cast ") and ln.endswith(" (combo)")]
    assert combo_lines, "expected synthetic combo casts on the win turn"
    real = sum(1 for ln in log
               if ln.startswith(f"T{w}: cast ") and not ln.endswith(" (combo)"))
    assert rec.casts_by_turn[w - 1] == real


def test_won_game_has_short_arrays_no_padding():
    cards = mini_cards()
    rec, _log = play_one_game(
        cards, small_deck(), "Boss", game_seed=7, until_turn=6,
        combos=[{"cards": ["Plains", "Boss"], "wins": True}])
    assert rec.won_turn is not None and rec.won_turn < 6
    for arr in (rec.damage_by_turn, rec.cmdr_damage_by_turn,
                rec.noncombat_damage_by_turn, rec.damage_per_opponent_by_turn,
                rec.board_width_by_turn, rec.total_power_by_turn,
                rec.casts_by_turn, rec.lands_played_by_turn,
                rec.mana_available_by_turn, rec.tokens_created_by_turn):
        assert len(arr) == rec.won_turn            # real values only, no padding


def test_full_horizon_arrays_and_monotone_board():
    cards = mini_cards()
    rec, _log = play_one_game(cards, small_deck(), "Boss", game_seed=11,
                              until_turn=6)
    assert rec.won_turn is None
    assert len(rec.board_width_by_turn) == 6
    # nothing dies in this pool: width and mana development never shrink
    assert rec.board_width_by_turn == sorted(rec.board_width_by_turn)
    assert rec.mana_available_by_turn == sorted(rec.mana_available_by_turn)
    assert all(len(row) == 1 for row in rec.damage_per_opponent_by_turn)
    assert [sum(row) for row in rec.damage_per_opponent_by_turn] == rec.damage_by_turn


def test_equipment_at_arrival_captured():
    cards = mini_cards()
    deck = ["Plains"] * 12 + ["Mountain"] * 12 + ["Hammer"] * 8 + ["Bear"] * 4
    rec, _log = play_one_game(cards, deck, "Boss", game_seed=2, until_turn=6)
    assert rec.commander_cast_turn is not None
    assert rec.equipment_at_arrival is not None
    assert rec.equipment_at_arrival >= 1           # cheap Hammers land first
    assert rec.equipped_on_arrival is not None     # commander arrived


# -- aggregate output hygiene (pinned) -----------------------------------------


def test_metrics_json_round_trip():
    cards = mini_cards()
    r = run_batch(cards, small_deck(), "Boss", n=8, seed=9, until_turn=6)
    m = r["metrics"]
    assert json.loads(json.dumps(m)) == m
