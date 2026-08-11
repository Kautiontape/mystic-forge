"""Task 14 golden seal: the seed-1234 log prefix is frozen as a literal.

Any engine/policy change that alters these lines must update this list
CONSCIOUSLY — that is the point of the test (spec §Architecture: same seed +
deck + annotations + policy -> byte-identical log).
"""
from mystic_forge.goldfish.runner import play_one_game
from tests.goldfish.helpers import mini_cards, small_deck

# Frozen at implementation time from a real run (game_seed=1234, until_turn=6).
GOLDEN_PREFIX = [
    "T0: kept 7",
    "T1: turn 1 begins",
    "T1: drew Mountain",
    "T1: played Mountain",
    "T1: phase: combat",
    "T1: no attack",
    "T1: phase: main2",
    "T2: turn 2 begins",
    "T2: drew Plains",
    "T2: played Mountain",
    "T2: phase: combat",
    "T2: no attack",
]


def test_golden_log_byte_identical():
    rec1, log1 = play_one_game(mini_cards(), small_deck(), "Boss",
                               game_seed=1234, until_turn=6)
    rec2, log2 = play_one_game(mini_cards(), small_deck(), "Boss",
                               game_seed=1234, until_turn=6)
    assert log1 == log2                    # full determinism, byte-identical
    assert rec1 == rec2
    assert log1[: len(GOLDEN_PREFIX)] == GOLDEN_PREFIX, (
        "seed-1234 golden log drifted — if the engine/policy change is "
        "intentional, update GOLDEN_PREFIX consciously"
    )
