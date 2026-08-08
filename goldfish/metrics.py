"""Statistics primitives and per-game metrics aggregation.

Engine-agnostic on purpose: the runner (Task 14) builds ``GameRecord``s from
finished games; everything here is pure stdlib so it stays unit-testable.

Statistical contract (spec §Statistics):
- every proportion carries a 95% Wilson CI;
- turn-to-event metrics report %-reached-by-until_turn plus the median turn
  among games that reached the event (no imputation for censored games);
- medians are lower-middle for even counts (deterministic), IQR alongside.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Statistics primitives
# --------------------------------------------------------------------------


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% (by default) Wilson score interval for a binomial proportion.

    Endpoints are snapped exactly: 0 successes pins the lower bound to 0.0
    and n successes pins the upper bound to 1.0 (mathematically true for the
    Wilson interval; snapped explicitly so no float residue leaks out)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    lo = 0.0 if successes == 0 else max(0.0, center - half)
    hi = 1.0 if successes == n else min(1.0, center + half)
    return (lo, hi)


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact binomial test on discordant pairs."""
    n, k = b + c, min(b, c)
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def median_low(values: Sequence[float]) -> float:
    """Median of ``values``; the lower-middle element for even counts.

    Pinned to lower-middle (never an interpolated half) so aggregate output
    stays deterministic and integer turns stay integers.
    """
    if not values:
        raise ValueError("median_low of empty sequence")
    xs = sorted(values)
    return xs[(len(xs) - 1) // 2]


def median_iqr(values: Sequence[float]) -> tuple[float, float]:
    """(median, IQR) with lower-middle medians throughout.

    Quartiles use the exclusive method: the halves omit the median element
    for odd counts, and each quartile is the lower-middle median of its half.
    """
    if not values:
        raise ValueError("median_iqr of empty sequence")
    xs = sorted(values)
    n = len(xs)
    med = xs[(n - 1) // 2]
    if n < 2:
        return med, 0
    lower = xs[: n // 2]
    upper = xs[(n + 1) // 2:]
    return med, median_low(upper) - median_low(lower)


@dataclass(frozen=True)
class PairedDelta:
    """Per-metric paired A/B difference (A minus B)."""

    mean: float
    sd: float
    n: int
    ci_low: float
    ci_high: float
    significant: bool
    mcnemar_p: float | None = None


def paired_delta(
    a_values: Sequence[float], b_values: Sequence[float]
) -> PairedDelta:
    """Mean paired delta ± 1.96·sd/√n; exact McNemar when both are 0/1."""
    if len(a_values) != len(b_values):
        raise ValueError("paired_delta requires equal-length value lists")
    n = len(a_values)
    if n == 0:
        raise ValueError("paired_delta requires at least one pair")
    deltas = [a - b for a, b in zip(a_values, b_values)]
    mean = sum(deltas) / n
    sd = (
        math.sqrt(sum((d - mean) ** 2 for d in deltas) / (n - 1))
        if n > 1
        else 0.0
    )
    half = 1.96 * sd / math.sqrt(n)
    ci_low, ci_high = mean - half, mean + half
    significant = not (ci_low <= 0.0 <= ci_high)
    mcnemar_p: float | None = None
    if all(v in (0, 1) for v in a_values) and all(v in (0, 1) for v in b_values):
        b_disc = sum(1 for a, b in zip(a_values, b_values) if a == 1 and b == 0)
        c_disc = sum(1 for a, b in zip(a_values, b_values) if a == 0 and b == 1)
        mcnemar_p = mcnemar_exact_p(b_disc, c_disc)
    return PairedDelta(mean, sd, n, ci_low, ci_high, significant, mcnemar_p)


# --------------------------------------------------------------------------
# Per-game record
# --------------------------------------------------------------------------


@dataclass
class GameRecord:
    """Every per-game observable the runner extracts from a finished game.

    All fields default so partial records are valid (censored games simply
    leave event turns as ``None``). Per-turn lists are indexed from 0 =
    turn 1 and may be shorter than the horizon for games that ended early.
    Dict keys for triggers/statics are ``"card|event|verb"`` strings.
    """

    mulligans_taken: int = 0
    kept_hand_size: int = 7
    kept_hand_lands: int = 0
    kept_hand_sources: int = 0
    opening_hand: list[str] = field(default_factory=list)
    commander_cast_turn: int | None = None
    equipped_on_arrival: bool | None = None  # None until the commander arrives
    equipment_at_arrival: int | None = None  # equipment on the battlefield at
                                             # the end of the commander's
                                             # arrival turn; None if it never
                                             # arrived
    kill_turn: int | None = None
    cmdr21_turn: int | None = None
    table_lethal_turn: int | None = None
    won_turn: int | None = None
    damage_by_turn: list[int] = field(default_factory=list)
    cmdr_damage_by_turn: list[int] = field(default_factory=list)
    noncombat_damage_by_turn: list[int] = field(default_factory=list)
    damage_per_opponent_by_turn: list[list[int]] = field(default_factory=list)
    board_width_by_turn: list[int] = field(default_factory=list)
    total_power_by_turn: list[int] = field(default_factory=list)
    casts_by_turn: list[int] = field(default_factory=list)
    lands_played_by_turn: list[int] = field(default_factory=list)
    mana_available_by_turn: list[int] = field(default_factory=list)
    tokens_created_by_turn: list[int] = field(default_factory=list)
    tokens_by_source: dict[str, int] = field(default_factory=dict)
    trigger_fires: dict[str, int] = field(default_factory=dict)
    static_active_turn: dict[str, int] = field(default_factory=dict)
    combo_assembled_turn: list[int | None] = field(default_factory=list)
    combo_castable_turn: list[int | None] = field(default_factory=list)
    drawn_names: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _prop(successes: int, n: int) -> dict[str, Any]:
    lo, hi = wilson_ci(successes, n)
    return {"value": successes / n if n else 0.0, "ci": [lo, hi]}


def _turn_metric(
    turns: Sequence[int | None], n: int, until_turn: int
) -> dict[str, Any]:
    """Censoring-aware turn-to-event metric: %-reached-by-T + median among
    reached (spec §Statistics — no imputation). Histogram keys are strings
    so the whole aggregate dict survives a JSON round trip unchanged."""
    reached = sorted(t for t in turns if t is not None and t <= until_turn)
    histogram: dict[str, int] = {}
    for t in reached:
        histogram[str(t)] = histogram.get(str(t), 0) + 1
    if reached:
        med, iqr = median_iqr(reached)
    else:
        med, iqr = None, None
    return {
        "reached_pct": _prop(len(reached), n),
        "median_among_reached": med,
        "iqr_among_reached": iqr,
        "histogram": histogram,
    }


def _avg_by_turn(
    lists: Sequence[Sequence[float]], until_turn: int
) -> list[float | None]:
    """Per-turn mean among games that have data for that turn (games that
    ended early drop out of later turns — no imputation). Turns where NO
    game has data emit None, never a fake 0.0."""
    out: list[float | None] = []
    for i in range(until_turn):
        vals = [xs[i] for xs in lists if i < len(xs)]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def _honesty(
    records: Sequence[GameRecord],
    n: int,
    card_scopes: dict[str, str | None],
) -> dict[str, Any]:
    """Three-way honesty split (spec §v1 metrics, D9).

    ``card_scopes`` maps card name -> D9 scope class, ``"unrecognized"``, or
    ``None`` for modeled cards. Modeled-low-impact = modeled cards drawn in
    at least one game whose trigger fires sum to zero across all records.
    """

    def drawn_entry(name: str) -> dict[str, Any]:
        drawn = sum(1 for r in records if name in r.drawn_names)
        return {"name": name, "drawn_pct": _prop(drawn, n)}

    out_of_scope: dict[str, list[dict[str, Any]]] = {}
    unrecognized: list[dict[str, Any]] = []
    for name in sorted(card_scopes):
        cls = card_scopes[name]
        if cls is None:
            continue
        if cls == "unrecognized":
            unrecognized.append(drawn_entry(name))
        else:
            out_of_scope.setdefault(cls, []).append(drawn_entry(name))

    fired = {
        key.split("|", 1)[0]
        for r in records
        for key, count in r.trigger_fires.items()
        if count > 0
    }
    drawn_any = {name for r in records for name in r.drawn_names}
    modeled_low_impact = sorted(
        name
        for name, cls in card_scopes.items()
        if cls is None and name not in fired and name in drawn_any
    )
    return {
        "out_of_scope": {k: out_of_scope[k] for k in sorted(out_of_scope)},
        "unrecognized": unrecognized,
        "modeled_low_impact": modeled_low_impact,
    }


def aggregate(
    records: Sequence[GameRecord],
    until_turn: int,
    card_scopes: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Build the v1 metrics dict from per-game records (spec §v1 metrics).

    Every proportion is ``{"value": v, "ci": [lo, hi]}``; every turn-to-event
    metric is ``{"reached_pct", "median_among_reached", "iqr_among_reached",
    "histogram"}``. The honesty report is included only when ``card_scopes``
    is provided.
    """
    if not records:
        raise ValueError("aggregate requires at least one record")
    n = len(records)

    def avg(vals: Sequence[float]) -> float:
        return sum(vals) / len(vals)

    metrics: dict[str, Any] = {"n": n, "until_turn": until_turn}

    # Mulligan / keep stats.
    metrics["mulligans"] = {
        "avg_taken": avg([r.mulligans_taken for r in records]),
        "pct_mulliganed": _prop(
            sum(1 for r in records if r.mulligans_taken > 0), n
        ),
        "avg_kept_hand_size": avg([r.kept_hand_size for r in records]),
        "avg_kept_lands": avg([r.kept_hand_lands for r in records]),
        "avg_kept_sources": avg([r.kept_hand_sources for r in records]),
    }

    # Commander cast: turn-to-event plus % by T4/T5/T6.
    commander_cast = _turn_metric(
        [r.commander_cast_turn for r in records], n, until_turn
    )
    commander_cast["pct_by_turn"] = {
        str(t): _prop(
            sum(
                1
                for r in records
                if r.commander_cast_turn is not None
                and r.commander_cast_turn <= t
            ),
            n,
        )
        for t in (4, 5, 6)
    }
    metrics["commander_cast"] = commander_cast

    # % equipped the turn the commander arrives, among games it arrived, plus
    # the average equipment count on the battlefield at that point (spec §v1
    # metrics); None when the commander never arrived in any game.
    arrived = [
        r.equipped_on_arrival
        for r in records
        if r.equipped_on_arrival is not None
    ]
    equipped = _prop(sum(1 for v in arrived if v), len(arrived))
    equipped["n_arrived"] = len(arrived)
    eq_counts = [
        r.equipment_at_arrival
        for r in records
        if r.equipment_at_arrival is not None
    ]
    equipped["avg_equipment_at_arrival"] = (
        sum(eq_counts) / len(eq_counts) if eq_counts else None
    )
    metrics["equipped_on_arrival"] = equipped

    # Turn-to-event metrics (censoring-aware).
    metrics["kill"] = _turn_metric([r.kill_turn for r in records], n, until_turn)
    metrics["cmdr21"] = _turn_metric(
        [r.cmdr21_turn for r in records], n, until_turn
    )
    metrics["table_lethal"] = _turn_metric(
        [r.table_lethal_turn for r in records], n, until_turn
    )
    metrics["won"] = _turn_metric([r.won_turn for r in records], n, until_turn)

    # Damage per turn, combat/commander/noncombat split. The explicit
    # "combat"/"noncombat" sections carry the split; combat is derived
    # per-record as total minus noncombat (records built by the runner keep
    # the two arrays the same length; a missing noncombat entry counts 0).
    def combat_row(r: GameRecord) -> list[int]:
        return [
            d
            - (
                r.noncombat_damage_by_turn[i]
                if i < len(r.noncombat_damage_by_turn)
                else 0
            )
            for i, d in enumerate(r.damage_by_turn)
        ]

    n_opp = max(
        (
            len(row)
            for r in records
            for row in r.damage_per_opponent_by_turn
        ),
        default=0,
    )
    per_opponent: list[list[float | None]] = []
    for i in range(until_turn):
        row: list[float | None] = []
        for o in range(n_opp):
            vals = [
                r.damage_per_opponent_by_turn[i][o]
                for r in records
                if i < len(r.damage_per_opponent_by_turn)
                and o < len(r.damage_per_opponent_by_turn[i])
            ]
            row.append(sum(vals) / len(vals) if vals else None)
        per_opponent.append(row)
    metrics["damage"] = {
        "avg_by_turn": _avg_by_turn(
            [r.damage_by_turn for r in records], until_turn
        ),
        "avg_cmdr_by_turn": _avg_by_turn(
            [r.cmdr_damage_by_turn for r in records], until_turn
        ),
        "avg_noncombat_by_turn": _avg_by_turn(
            [r.noncombat_damage_by_turn for r in records], until_turn
        ),
        "avg_total": avg(
            [sum(r.damage_by_turn[:until_turn]) for r in records]
        ),
        "avg_per_opponent_by_turn": per_opponent,
        "combat": {
            "avg_by_turn": _avg_by_turn(
                [combat_row(r) for r in records], until_turn
            ),
            "avg_total": avg(
                [sum(combat_row(r)[:until_turn]) for r in records]
            ),
        },
        "noncombat": {
            "avg_by_turn": _avg_by_turn(
                [r.noncombat_damage_by_turn for r in records], until_turn
            ),
            "avg_total": avg(
                [
                    sum(r.noncombat_damage_by_turn[:until_turn])
                    for r in records
                ]
            ),
        },
    }

    # Board state per turn.
    metrics["board"] = {
        "avg_width_by_turn": _avg_by_turn(
            [r.board_width_by_turn for r in records], until_turn
        ),
        "avg_power_by_turn": _avg_by_turn(
            [r.total_power_by_turn for r in records], until_turn
        ),
    }
    source_totals: dict[str, int] = {}
    for r in records:
        for name, count in r.tokens_by_source.items():
            source_totals[name] = source_totals.get(name, 0) + count
    metrics["tokens"] = {
        "avg_by_turn": _avg_by_turn(
            [r.tokens_created_by_turn for r in records], until_turn
        ),
        "avg_total": avg(
            [sum(r.tokens_created_by_turn[:until_turn]) for r in records]
        ),
        # avg per game per source card (runner attribution; approximate —
        # see GameRecord.tokens_by_source's producer in the runner)
        "by_source": {
            name: source_totals[name] / n for name in sorted(source_totals)
        },
    }

    # Casts per turn: distribution, max chain, % games with a 4+ turn by T6.
    distribution: dict[int, int] = {}
    for r in records:
        for casts in r.casts_by_turn[:until_turn]:
            distribution[casts] = distribution.get(casts, 0) + 1
    per_game_max = [
        max(r.casts_by_turn[:until_turn], default=0) for r in records
    ]
    metrics["casts"] = {
        # str keys (like every histogram here) for JSON round-trip fidelity
        "distribution": {str(k): distribution[k] for k in sorted(distribution)},
        "avg_by_turn": _avg_by_turn(
            [r.casts_by_turn for r in records], until_turn
        ),
        "max_chain": {"max": max(per_game_max), "avg": avg(per_game_max)},
        "pct_4plus_turn_by_t6": _prop(
            sum(
                1
                for r in records
                if any(c >= 4 for c in r.casts_by_turn[:6])
            ),
            n,
        ),
    }

    # On-curve health: all land drops through T5, mana available per turn.
    # Short-list semantics: a game that ended before T5 is judged only on
    # the turns it actually played — all() over the truncated list is
    # vacuously true for the missing turns, so an early win with perfect
    # drops counts as on-curve rather than as missed drops.
    horizon = min(5, until_turn)
    on_curve = sum(
        1
        for r in records
        if all(v >= 1 for v in r.lands_played_by_turn[:horizon])
    )
    metrics["on_curve"] = {
        "pct_all_drops_through_t5": _prop(on_curve, n),
        "avg_lands_by_turn": _avg_by_turn(
            [r.lands_played_by_turn for r in records], until_turn
        ),
        "avg_mana_by_turn": _avg_by_turn(
            [r.mana_available_by_turn for r in records], until_turn
        ),
    }

    # Generic trigger-fire table: avg fires per game per "card|event|verb".
    fire_totals: dict[str, int] = {}
    for r in records:
        for key, count in r.trigger_fires.items():
            fire_totals[key] = fire_totals.get(key, 0) + count
    metrics["trigger_fires"] = {
        key: fire_totals[key] / n for key in sorted(fire_totals)
    }

    # Static/condition activation: % of games online by T4 per key.
    static_keys = sorted({k for r in records for k in r.static_active_turn})
    metrics["statics"] = {
        key: {
            "pct_online_by_t4": _prop(
                sum(
                    1
                    for r in records
                    if key in r.static_active_turn
                    and r.static_active_turn[key] <= 4
                ),
                n,
            )
        }
        for key in static_keys
    }

    # Combos (when declared): assembled and assembled+castable per combo.
    n_combos = max(
        max((len(r.combo_assembled_turn) for r in records), default=0),
        max((len(r.combo_castable_turn) for r in records), default=0),
    )
    combos: list[dict[str, Any]] = []
    for i in range(n_combos):
        assembled = [
            r.combo_assembled_turn[i]
            if i < len(r.combo_assembled_turn)
            else None
            for r in records
        ]
        castable = [
            r.combo_castable_turn[i]
            if i < len(r.combo_castable_turn)
            else None
            for r in records
        ]
        combos.append(
            {
                "assembled": _turn_metric(assembled, n, until_turn),
                "castable": _turn_metric(castable, n, until_turn),
            }
        )
    metrics["combos"] = combos

    if card_scopes is not None:
        metrics["honesty"] = _honesty(records, n, card_scopes)
    return metrics
