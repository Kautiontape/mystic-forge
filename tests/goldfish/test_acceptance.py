"""Task 22 acceptance sweep — spec §Acceptance criteria, bullet by bullet.

Every spec bullet maps to a passing test. Bullets already proven elsewhere
name their test here; bullets with no prior home get their test in THIS file.

 1. Same seed + deck + annotations + policy -> byte-identical game log,
    including mulligan bottoming decisions:
      tests/goldfish/test_golden.py::test_golden_log_byte_identical
      tests/goldfish/test_mulligan.py::test_london_bottoming_deterministic
      tests/goldfish/test_runner.py::test_batch_deterministic_and_seed_paired
 2. goldfish_run n=10000 on a 99-card deck completes < 30s:
      THIS FILE::test_10k_games_under_30s
 3. Hypergeometric sanity — sim matches goldfish_odds closed form within 1%:
      tests/goldfish/test_odds.py::test_sim_matches_hypergeometric_within_1pct
    ...multi-group closed form validates the assembled-by-turn metric on a
    tutor-free deck:
      THIS FILE::test_multigroup_closed_form_validates_assembled_metric
 4. Colored-payment correctness (lands that cannot make {R} never cast an
    {R} spell):
      tests/goldfish/test_engine.py::test_cannot_pay_red_with_only_plains
      tests/goldfish/test_engine.py::test_pay_impossible_cost_raises_and_leaves_game_unmutated
 5. Summoning sickness (no haste -> never attacks or {T}-activates the turn
    it arrives):
      tests/goldfish/test_engine.py::test_summoning_sick_attacker_rejected_haste_allowed
      tests/goldfish/test_engine.py::test_tap_activation_respects_summoning_sickness
 6. Policy: a {T} producer the policy activates never also attacks that turn:
      tests/goldfish/test_policy.py::test_tap_producer_withheld_from_attack
    ...a combo piece on the battlefield still counts as assembled:
      tests/goldfish/test_engine.py::test_assembled_counts_battlefield
 7. goldfish_ab with identical decks reports ~zero deltas:
      tests/goldfish/test_runner.py::test_ab_identical_decks_zero_delta
      tests/goldfish/test_server_tools.py::test_goldfish_ab_identical_decks_zero_deltas
    ...1-card-swap A/B pair correlation above a stated floor:
      tests/goldfish/test_runner.py::test_ab_one_swap_high_correlation
 8. Every proportion metric in goldfish_run output carries a CI:
      tests/goldfish/test_metrics.py::test_aggregate_shapes   (aggregate level)
      THIS FILE::test_run_output_every_proportion_carries_ci  (tool level)
    ...every goldfish_ab output opens with the scope banner:
      tests/goldfish/test_server_tools.py::test_goldfish_ab_scope_banner
      tests/goldfish/test_report.py::test_ab_report_banner_verbatim_at_top
 9. Interactive: illegal actions rejected with reason, state unmutated on
    rejection:
      tests/goldfish/test_interactive.py::test_illegal_action_rejected_unmutated
    ...a game resumed from a state blob replays identically:
      tests/goldfish/test_interactive.py::test_until_fast_forward_and_resume
    ...ambiguous name references rejected with candidate IDs:
      tests/goldfish/test_engine.py::test_attach_ambiguous_name_rejected_with_candidate_ids
10. Unmodeled cards reported by name, class, and drawn-% in every run:
      tests/goldfish/test_metrics.py::test_honesty_three_way_split
      tests/goldfish/test_server_tools.py::test_goldfish_run_output_shape
      tests/goldfish/test_server_tools.py::test_goldfish_run_annotation_models_needs_annotation_card
11. Annotation validation rejects unknown verbs/events/conditions with the
    allowed list in the message:
      tests/goldfish/test_cards.py::test_unknown_verb_rejected_with_allowed_list
      THIS FILE::test_unknown_event_and_condition_rejected_with_allowed_list
12. Concurrency — an unrelated tool call answers while a batch run is in
    flight:
      tests/goldfish/test_server_tools.py::test_goldfish_runs_do_not_block_other_tools
      THIS FILE::test_concurrent_tool_call_not_blocked
13. A game evicted from the in-memory store is fully restorable from its
    echoed state blob:
      tests/goldfish/test_interactive.py::test_ttl_eviction
      tests/goldfish/test_interactive.py::test_lru_cap_eviction
      tests/goldfish/test_interactive.py::test_until_fast_forward_and_resume
14. Reports — the HTML makes no external requests:
      tests/goldfish/test_report.py::test_report_self_contained
    ...identical run inputs produce the same run code:
      tests/goldfish/test_report.py::test_run_code_shape_and_generated_at_excluded
      tests/goldfish/test_server_tools.py::test_run_code_stable_and_input_sensitive
    ...every number in the report matches the run's metrics JSON:
      tests/goldfish/test_report.py::test_report_numbers_match_metrics
    ...the hosted route 404s unknown codes and is absent when unconfigured:
      tests/goldfish/test_report.py::test_route_handler_404_and_200
      tests/goldfish/test_report.py::test_unconfigured_no_persist_no_url
"""
import asyncio
import json
import time

import pytest

import server as srv
from mystic_forge.goldfish.cards import AnnotationError, validate_annotations
from mystic_forge.goldfish.engine import MulliganRules
from mystic_forge.goldfish.odds import odds_groups
from mystic_forge.goldfish.runner import run_batch
from tests.goldfish.helpers import (
    MINI_DECK_TEXT,
    _patch_fetch_minideck,
    mini_cards,
)


@pytest.mark.slow
def test_10k_games_under_30s():
    """Acceptance bullet 2: n=10000 on a 99-card deck completes < 30s."""
    cards = mini_cards()
    deck = (["Plains"] * 20 + ["Mountain"] * 17 + ["Bear"] * 30 +
            ["Runner"] * 20 + ["Hammer"] * 12)
    assert len(deck) == 99
    t0 = time.perf_counter()
    run_batch(cards, deck, "Boss", n=10_000, seed=1, until_turn=8)
    assert time.perf_counter() - t0 < 30


async def test_concurrent_tool_call_not_blocked(monkeypatch):
    """Acceptance bullet 12: a quick tool answers while a sim churns."""
    _patch_fetch_minideck(monkeypatch)
    sim = asyncio.create_task(srv.goldfish_run(
        srv.GoldfishRunInput(deck=MINI_DECK_TEXT, n=5000, seed=1)))
    await asyncio.sleep(0.05)              # the sim now holds the semaphore
    t0 = time.perf_counter()
    odds_out = await srv.goldfish_odds(
        srv.GoldfishOddsInput(deck_size=99, draws=7, copies=8))
    quick_elapsed = time.perf_counter() - t0
    assert "exact hypergeometric" in odds_out
    sim_out = await sim
    assert "```json" in sim_out            # the sim itself completed fine
    assert quick_elapsed < 1.0


def test_multigroup_closed_form_validates_assembled_metric():
    """Acceptance bullet 3, second half: on a tutor-free deck with no draw
    effects, "combo assembled by end of turn t" is exactly "every piece among
    the first 7 + t cards seen" — the multi-group hypergeometric closed form
    (assembled counts hand + command zone + battlefield, so casting decisions
    cannot move the number). Mulligans are disabled via a wide-open keep
    window so the kept 7 is an unbiased sample."""
    deck = ["Plains"] * 30 + ["Bear"] * 5 + ["Runner"] * 4    # 39, tutor-free
    r = run_batch(mini_cards(), deck, "Boss", n=4000, seed=2, until_turn=3,
                  combos=[{"cards": ["Bear", "Runner"], "wins": False}],
                  rules=MulliganRules(min_sources=0, max_sources=7,
                                      min_real_lands=0))
    assembled = r["metrics"]["combos"][0]["assembled"]["reached_pct"]["value"]
    expected = odds_groups(39, 7 + 3, [{"copies": 5, "min_successes": 1},
                                       {"copies": 4, "min_successes": 1}])
    assert abs(assembled - expected) < 0.01


async def test_run_output_every_proportion_carries_ci(monkeypatch):
    """Acceptance bullet 8, tool level: walk goldfish_run's embedded metrics
    JSON — every proportion-shaped {"value": v} dict must carry a "ci"."""
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=50, seed=7, until_turn=6))
    metrics = json.loads(out.split("```json")[1].split("```")[0])["metrics"]
    found = 0

    def walk(node):
        nonlocal found
        if isinstance(node, dict):
            v = node.get("value")
            if isinstance(v, (int, float)) and 0.0 <= v <= 1.0:
                found += 1
                assert "ci" in node, f"proportion without ci: {node}"
                lo, hi = node["ci"]
                assert 0.0 <= lo <= hi <= 1.0
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(metrics)
    assert found >= 5                      # the walk really saw proportions


def test_unknown_event_and_condition_rejected_with_allowed_list():
    """Acceptance bullet 11 completion: unknown events and conditions (not
    just verbs) are rejected with the allowed list in the message."""
    with pytest.raises(AnnotationError) as ei:
        validate_annotations([{"name": "X", "triggers": [
            {"on": "upkeep_of_doom", "do": "draw"}]}])
    assert "upkeep_of_doom" in str(ei.value)
    assert "etb" in str(ei.value)                    # allowed events listed
    with pytest.raises(AnnotationError) as ei:
        validate_annotations([{"name": "X", "triggers": [
            {"on": "etb", "do": "draw", "if": "not_a_condition"}]}])
    assert "not_a_condition" in str(ei.value)
    assert "metalcraft" in str(ei.value)             # allowed conditions listed
