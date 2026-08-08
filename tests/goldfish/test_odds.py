import math

import pytest

from goldfish.odds import odds_at_least, odds_groups


def test_single_group_matches_closed_form():
    # >=1 of 4 copies in top 10 of 99: 1 - C(95,10)/C(99,10)
    expected = 1 - math.comb(95, 10) / math.comb(99, 10)
    assert abs(odds_at_least(99, 10, copies=4, min_successes=1) - expected) < 1e-12


def test_groups_joint_probability():
    # P(>=1 A and >=1 B), 3 As and 2 Bs in 40, draw 7 -- brute-force cross-check
    from itertools import combinations

    deck = ["A"] * 3 + ["B"] * 2 + ["x"] * 35
    hits = sum(
        1
        for hand in combinations(range(40), 7)
        if any(deck[i] == "A" for i in hand) and any(deck[i] == "B" for i in hand)
    )
    expected = hits / math.comb(40, 7)
    got = odds_groups(
        40, 7, [{"copies": 3, "min_successes": 1}, {"copies": 2, "min_successes": 1}]
    )
    assert abs(got - expected) < 1e-12


def test_min_successes_exceeds_copies_is_impossible():
    assert odds_at_least(99, 10, copies=4, min_successes=5) == 0.0


def test_draws_covers_whole_deck_when_copies_meet_minimum():
    # Drawing the entire deck guarantees every copy is seen.
    assert odds_at_least(20, 20, copies=5, min_successes=3) == 1.0


def test_groups_copies_exceeding_deck_size_raises():
    with pytest.raises(ValueError):
        odds_groups(10, 5, [{"copies": 6, "min_successes": 1}, {"copies": 6, "min_successes": 1}])


def test_empty_groups_is_vacuously_certain():
    assert odds_groups(40, 7, []) == 1.0


def test_single_group_via_odds_groups_matches_odds_at_least():
    flat = odds_at_least(99, 10, copies=4, min_successes=2)
    grouped = odds_groups(99, 10, [{"copies": 4, "min_successes": 2}])
    assert grouped == pytest.approx(flat, abs=1e-12)


def test_odds_at_least_is_exact_float_division_of_exact_integers():
    # math.comb is exact integer arithmetic; the only float op is the final
    # division, so results should be reproducible bit-for-bit across calls
    # and agree with a manually computed ratio to machine precision.
    total = math.comb(99, 10)
    hit = sum(
        math.comb(4, k) * math.comb(95, 10 - k) for k in range(1, min(4, 10) + 1)
    )
    expected = hit / total
    got = odds_at_least(99, 10, copies=4, min_successes=1)
    assert got == expected
    # deterministic across repeated calls
    assert odds_at_least(99, 10, copies=4, min_successes=1) == got
