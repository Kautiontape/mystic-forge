"""Exact closed-form hypergeometric odds for goldfish_odds.

All probability mass is computed with `math.comb` integer arithmetic; the
only floating-point operation is the final division, so results are exact
to double precision (no accumulated rounding from repeated float ops).
"""
from __future__ import annotations

import math
from itertools import product

# odds_groups runs on the MCP event loop with no to_thread offload, so its
# itertools.product enumeration must be bounded up front — an unbounded
# query (many groups, or wide per-group ranges) can hang the loop for
# seconds and stall every other MCP session.
_MAX_GROUPS = 8              # bounds group count directly
_MAX_COMBINATIONS = 200_000  # bounds the product() search space (group ranges multiply)


def odds_at_least(deck_size: int, draws: int, copies: int, min_successes: int = 1) -> float:
    """P(>= min_successes copies among `copies` in the deck appear in a
    `draws`-card sample of a `deck_size`-card deck)."""
    if copies > deck_size:
        raise ValueError("copies exceed deck size")
    total = math.comb(deck_size, draws)
    p = 0
    for k in range(min_successes, min(copies, draws) + 1):
        p += math.comb(copies, k) * math.comb(deck_size - copies, draws - k)
    return p / total


def odds_groups(deck_size: int, draws: int, groups: list[dict]) -> float:
    """Exact joint P(every group meets its min) by direct enumeration of the
    per-group draw counts (multivariate hypergeometric). An empty groups
    list is vacuously certain -> 1.0 by design (no constraints to fail)."""
    copies = [g["copies"] for g in groups]
    mins = [g.get("min_successes", 1) for g in groups]
    rest = deck_size - sum(copies)
    if rest < 0:
        raise ValueError("group copies exceed deck size")
    ranges = [range(m, min(c, draws) + 1) for c, m in zip(copies, mins)]
    search = math.prod(len(r) for r in ranges)
    if len(groups) > _MAX_GROUPS or search > _MAX_COMBINATIONS:
        raise ValueError(
            f"odds_groups query too large: {len(groups)} groups / {search} "
            f"combinations; cap is {_MAX_GROUPS} groups or "
            f"{_MAX_COMBINATIONS // 1000}k combinations")
    total = math.comb(deck_size, draws)
    hit = 0
    for counts in product(*ranges):
        used = sum(counts)
        if used > draws:
            continue
        ways = math.prod(math.comb(c, k) for c, k in zip(copies, counts))
        ways *= math.comb(rest, draws - used)
        hit += ways
    return hit / total
