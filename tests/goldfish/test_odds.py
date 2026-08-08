import math

import pytest

from goldfish.odds import odds_at_least, odds_groups


def test_single_group_matches_closed_form():
    # >=1 of 4 copies in top 10 of 99: 1 - C(95,10)/C(99,10)
    expected = 1 - math.comb(95, 10) / math.comb(99, 10)
    assert abs(odds_at_least(99, 10, copies=4, min_successes=1) - expected) < 1e-12


def test_groups_joint_probability():
    # P(>=1 A and >=1 B), 3 As and 2 Bs in 15, draw 5 -- brute-force cross-check.
    # Small enough (C(15,5)=3003) to run in milliseconds while exercising the
    # same code paths as a full-deck query.
    from itertools import combinations

    deck = ["A"] * 3 + ["B"] * 2 + ["x"] * 10
    hits = sum(
        1
        for hand in combinations(range(15), 5)
        if any(deck[i] == "A" for i in hand) and any(deck[i] == "B" for i in hand)
    )
    expected = hits / math.comb(15, 5)
    got = odds_groups(
        15, 5, [{"copies": 3, "min_successes": 1}, {"copies": 2, "min_successes": 1}]
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


def test_odds_at_least_copies_exceeding_deck_size_raises():
    with pytest.raises(ValueError, match="copies exceed deck size"):
        odds_at_least(10, 5, copies=11, min_successes=1)


def test_odds_groups_too_many_groups_raises():
    # 9 groups exceeds the _MAX_GROUPS=8 cap, regardless of how small each
    # group's own search range is.
    groups = [{"copies": 1, "min_successes": 1}] * 9
    with pytest.raises(ValueError, match="too large"):
        odds_groups(99, 7, groups)


def test_odds_groups_over_combination_cap_raises():
    # 5 groups (within the group-count cap) whose per-group ranges multiply
    # past the 200k combination cap: this is the shape of the reviewer's
    # 15-group/copies-6 case that hung the event loop for 20+ seconds --
    # the guard must reject it before itertools.product ever runs.
    groups = [{"copies": 12, "min_successes": 1}] * 5
    with pytest.raises(ValueError, match="too large"):
        odds_groups(100, 15, groups)


def test_odds_groups_eight_groups_small_case_still_computes():
    # At the group-count cap but with a tiny search space -- must not be
    # rejected, and must still return a valid probability.
    groups = [{"copies": 1, "min_successes": 1}] * 8
    got = odds_groups(99, 10, groups)
    assert 0.0 <= got <= 1.0


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
