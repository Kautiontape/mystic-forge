"""Exact closed-form hypergeometric odds for goldfish_odds.

All probability mass is computed with `math.comb` integer arithmetic; the
only floating-point operation is the final division, so results are exact
to double precision (no accumulated rounding from repeated float ops).
"""
from __future__ import annotations

import math
from itertools import product


def odds_at_least(deck_size: int, draws: int, copies: int, min_successes: int = 1) -> float:
    """P(>= min_successes copies among `copies` in the deck appear in a
    `draws`-card sample of a `deck_size`-card deck)."""
    total = math.comb(deck_size, draws)
    p = 0
    for k in range(min_successes, min(copies, draws) + 1):
        p += math.comb(copies, k) * math.comb(deck_size - copies, draws - k)
    return p / total


def odds_groups(deck_size: int, draws: int, groups: list[dict]) -> float:
    """Exact joint P(every group meets its min) by direct enumeration of the
    per-group draw counts (multivariate hypergeometric)."""
    copies = [g["copies"] for g in groups]
    mins = [g.get("min_successes", 1) for g in groups]
    rest = deck_size - sum(copies)
    if rest < 0:
        raise ValueError("group copies exceed deck size")
    total = math.comb(deck_size, draws)
    hit = 0
    ranges = [range(m, min(c, draws) + 1) for c, m in zip(copies, mins)]
    for counts in product(*ranges):
        used = sum(counts)
        if used > draws:
            continue
        ways = math.prod(math.comb(c, k) for c, k in zip(copies, counts))
        ways *= math.comb(rest, draws - used)
        hit += ways
    return hit / total
