"""Batch runner (Task 14): policy-driven games -> GameRecords -> aggregate.

Timeline design (pinned): ``build_record`` never parses damage out of the
log. ``play_one_game``'s loop observes the Game directly —

- per-step opponent-life / commander-damage deltas, classified combat vs
  noncombat by the acting step: an ``attack`` action's losses are combat
  (including the life zeroing of a 21-commander-damage elimination and any
  attack-trigger burn fired during the swing); every other step's losses are
  damage-verb output, i.e. noncombat;
- a board snapshot at each turn boundary, taken immediately before the
  turn-advancing pass so it sees the turn's final battlefield (land-drop
  counters and tapped state reset the moment the next turn begins):
  creature width, post-anthem total power, mana development (every
  battlefield producer's max output — the pool is excluded to avoid double
  counting tapped producers' banked mana), equipment count, token count,
  land drops used, commander presence/equipped flags, opponent life.

Per-turn arrays cover turns 1..min(last played turn, until_turn) with REAL
values only — a game won early has short arrays; metrics'
mean-among-games-with-data handles the truncation (no padding, no
imputation).
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict
from typing import Any

from .engine import (
    Game,
    MulliganRules,
    _is_creature_perm,
    auto_mulligan,
    derive_seed,
    effective_power,
    new_game,
    step,
)
from .metrics import GameRecord, aggregate, paired_delta
from .policy import choose_action

# Per-turn action cap (graceful degradation): a degenerate-but-legal
# annotation pair — e.g. a creature_etb -> treasure listener plus a non-tap
# "{1}: create_token" ability (the Task 12 review livelock) — is a
# mana-neutral perpetual loop the multi-pass policy re-fires forever. Past
# this many actions in one turn the runner forces "pass" until the turn
# advances, logging "action cap reached — forcing pass" once per turn,
# instead of aborting the whole run.
_TURN_ACTION_CAP = 300

# Game-level livelock backstop (plan): with the per-turn cap forcing at most
# ~cap+3 actions per turn this is unreachable for any policy behavior; it
# guards runner bugs (e.g. a pass that stops advancing phases).
_GUARD_PER_TURN = 500

_TOKEN_MAKER_VERBS = ("create_token", "token_copy")


def _end_state(g: Game) -> dict[str, Any]:
    """One battlefield sweep -> the per-turn snapshot fields (see module
    docstring). ``total_power`` counts every creature, tapped or summoning
    sick included — the same series as the board metric, and the v1 stand-in
    for "total attacking power" in the table-lethal check."""
    width = power = mana = equipment = tokens = 0
    cmdr = None
    for p in g.battlefield:
        card = g.card(p.name)
        if _is_creature_perm(g, p):
            width += 1
            power += effective_power(g, p)
        if card.data.produces:
            mana += max(card.data.produces.values())
        if card.is_equipment:
            equipment += 1
        if p.is_token:
            tokens += 1
        if cmdr is None and p.name == g.commander_name and not p.is_token:
            cmdr = p
    return {
        "board_width": width,
        "total_power": power,
        "mana_available": mana,
        "equipment": equipment,
        "tokens_total": tokens,
        "lands_played": g._land_drops_used,
        "commander_on_bf": cmdr is not None,
        "commander_equipped": bool(cmdr.attached) if cmdr is not None else False,
        "opp_life": list(g.opponents),
    }


class _Timeline:
    """Per-turn observers ``play_one_game`` feeds and ``build_record`` reads.

    ``turns[i]`` is turn i+1's finalized entry: the boundary snapshot merged
    with that turn's damage accumulators. Turn-to-event turns (kill, cmdr21,
    commander cast) are detected per step, so mid-turn events carry the
    exact turn; table-lethal is evaluated at each boundary."""

    def __init__(self, g: Game):
        self.turns: list[dict[str, Any]] = []
        self.kill_turn: int | None = None
        self.cmdr21_turn: int | None = None
        self.table_lethal_turn: int | None = None
        self.commander_cast_turn: int | None = None
        self.drawn: set[str] = set(g.hand)     # kept hand + the turn-1 draw
        self._prev_tokens = 0
        self._n_opp = len(g.opponents)
        self._reset()

    def _reset(self) -> None:
        self._damage = 0
        self._noncombat = 0
        self._cmdr = 0
        self._per_opp = [0] * self._n_opp

    def observe_step(
        self, g: Game, action: dict, pre_life: list, pre_cmdr: list
    ) -> None:
        combat = action.get("type") == "attack"
        for i, (before, after) in enumerate(zip(pre_life, g.opponents)):
            lost = before - after              # opponent life never rises
            if lost > 0:
                self._damage += lost
                self._per_opp[i] += lost
                if not combat:
                    self._noncombat += lost
        self._cmdr += sum(
            now - before for before, now in zip(pre_cmdr, g.cmdr_damage)
        )
        if self.kill_turn is None and any(life <= 0 for life in g.opponents):
            self.kill_turn = g.turn
        if self.cmdr21_turn is None and any(d >= 21 for d in g.cmdr_damage):
            self.cmdr21_turn = g.turn
        # commander_casts counts real command-zone casts only — synthetic
        # combo casts never bump it, matching the cast-metric exclusion
        if self.commander_cast_turn is None and g.commander_casts > 0:
            self.commander_cast_turn = g.turn
        self.drawn |= set(g.hand)

    def finalize_turn(self, end_state: dict[str, Any]) -> None:
        entry = dict(end_state)
        entry["damage"] = self._damage
        entry["noncombat"] = self._noncombat
        entry["cmdr_damage"] = self._cmdr
        entry["per_opponent"] = list(self._per_opp)
        # v1 tokens normally never leave the battlefield, but a sac_self
        # activation (fetch-land modelling) removes its own token, so the
        # per-turn created count is net of removals, floored at 0.
        entry["tokens_created"] = max(
            0, end_state["tokens_total"] - self._prev_tokens
        )
        self._prev_tokens = end_state["tokens_total"]
        self.turns.append(entry)
        # Table-lethal (spec §v1 metrics, v1 stand-in): first turn the
        # board's total power covers the combined remaining life. A dead
        # table (0 remaining) is trivially lethal.
        if (
            self.table_lethal_turn is None
            and entry["total_power"] >= sum(entry["opp_life"])
        ):
            self.table_lethal_turn = len(self.turns)
        self._reset()


def play_one_game(
    cards: dict,
    deck: list,
    commander: str,
    game_seed: int,
    until_turn: int,
    opponents: int = 1,
    combos: list | None = None,
    rules: MulliganRules | None = None,
) -> tuple[GameRecord, list]:
    """Play one policy-driven game to ``until_turn`` (or a win) and return
    (record, log)."""
    g = new_game(
        cards, deck, commander, seed=game_seed,
        opponents=opponents, combos=combos,
    )
    auto_mulligan(g, rules or MulliganRules())
    # Kept-hand snapshot (the Game doesn't store it): auto_mulligan ends by
    # entering turn 1, whose draw APPENDS to the hand — and a fresh game's
    # turn-1 upkeep fires over an empty battlefield, so nothing else touched
    # the hand. Slicing to kept_hand_size recovers the kept hand exactly
    # (an empty library skips the draw; the slice is then the whole hand).
    opening_hand = list(g.hand[: g.kept_hand_size or 0])
    timeline = _Timeline(g)
    guard = 0
    actions_this_turn = 0
    capped_this_turn = False
    prev_turn = g.turn
    boundary: dict[str, Any] | None = None
    while g.turn <= until_turn and g.won_turn is None:
        guard += 1
        if guard > _GUARD_PER_TURN * until_turn:
            raise RuntimeError("policy livelock — bug")
        if actions_this_turn >= _TURN_ACTION_CAP:
            if not capped_this_turn:
                capped_this_turn = True
                g.emit("action cap reached — forcing pass")
            action: dict[str, Any] = {"type": "pass"}
        else:
            action, _reason = choose_action(g)
        pre_life = list(g.opponents)
        pre_cmdr = list(g.cmdr_damage)
        if action["type"] == "pass" and g.phase in ("main2", "end"):
            boundary = _end_state(g)   # last look at this turn's battlefield
        step(g, action)
        actions_this_turn += 1
        if g.turn != prev_turn:
            # Turn boundary: the advancing pass came from main2/end, so
            # `boundary` was just captured; the fallback covers only a
            # hypothetical non-pass advance and reads post-reset state.
            timeline.finalize_turn(
                boundary if boundary is not None else _end_state(g)
            )
            prev_turn = g.turn
            actions_this_turn = 0
            capped_this_turn = False
            boundary = None
        # After a boundary the step's own deltas (next turn's upkeep burn)
        # belong to the NEW turn — observe after finalizing.
        timeline.observe_step(g, action, pre_life, pre_cmdr)
    if (
        g.won_turn is not None
        and g.won_turn == g.turn
        and len(timeline.turns) < g.turn
    ):
        # Won mid-turn: the partial turn is real data (short arrays are the
        # documented contract, but a played turn is never dropped).
        timeline.finalize_turn(_end_state(g))
    return build_record(g, until_turn, timeline, opening_hand), g.log


def _casts_by_turn(log: list, horizon: int) -> list[int]:
    """Real casts per turn from the pinned log format ``"T{n}: cast X"``.
    Synthetic combo casts carry the ``" (combo)"`` suffix and are excluded —
    they count in spells_cast_this_turn (spec) but not in cast metrics; this
    log parse IS the exclusion mechanism (pinned)."""
    counts = [0] * horizon
    for line in log:
        if line.endswith(" (combo)"):
            continue
        head, sep, rest = line.partition(": ")
        if sep and rest.startswith("cast ") and head.startswith("T"):
            try:
                t = int(head[1:])
            except ValueError:
                continue
            if 1 <= t <= horizon:
                counts[t - 1] += 1
    return counts


def _tokens_by_source(trigger_fires: dict) -> dict[str, int]:
    """Token attribution (pinned approximation): the log's "created N
    token(s)" lines carry no source, so this sums TRIGGER FIRES whose verb
    is create_token/token_copy per source card. It counts fires, not tokens
    (a count-2 trigger or a token doubler mints more tokens than fires),
    and activated-ability tokens — no trigger_fires entry — are not
    attributed at all."""
    out: dict[str, int] = {}
    for key, count in trigger_fires.items():
        card, _event, verb = key.rsplit("|", 2)   # keys are "card|event|verb"
        if verb in _TOKEN_MAKER_VERBS:
            out[card] = out.get(card, 0) + count
    return out


def build_record(
    g: Game,
    until_turn: int,
    timeline: _Timeline,
    opening_hand: list,
) -> GameRecord:
    """Extract the GameRecord from a finished game, its log, and the
    timeline ``play_one_game`` observed (see module docstring)."""
    turns = timeline.turns[: min(len(timeline.turns), until_turn)]
    horizon = len(turns)
    arrival = next((e for e in turns if e["commander_on_bf"]), None)
    return GameRecord(
        mulligans_taken=g.mulligans_taken,
        kept_hand_size=g.kept_hand_size or 0,
        kept_hand_lands=g.kept_hand_lands or 0,
        kept_hand_sources=g.kept_hand_sources or 0,
        opening_hand=list(opening_hand),
        commander_cast_turn=timeline.commander_cast_turn,
        equipped_on_arrival=(
            None if arrival is None else bool(arrival["commander_equipped"])
        ),
        equipment_at_arrival=(
            None if arrival is None else arrival["equipment"]
        ),
        kill_turn=timeline.kill_turn,
        cmdr21_turn=timeline.cmdr21_turn,
        table_lethal_turn=timeline.table_lethal_turn,
        won_turn=g.won_turn,
        damage_by_turn=[e["damage"] for e in turns],
        cmdr_damage_by_turn=[e["cmdr_damage"] for e in turns],
        noncombat_damage_by_turn=[e["noncombat"] for e in turns],
        damage_per_opponent_by_turn=[list(e["per_opponent"]) for e in turns],
        board_width_by_turn=[e["board_width"] for e in turns],
        total_power_by_turn=[e["total_power"] for e in turns],
        casts_by_turn=_casts_by_turn(g.log, horizon),
        lands_played_by_turn=[e["lands_played"] for e in turns],
        mana_available_by_turn=[e["mana_available"] for e in turns],
        tokens_created_by_turn=[e["tokens_created"] for e in turns],
        tokens_by_source=_tokens_by_source(g.trigger_fires),
        trigger_fires=dict(g.trigger_fires),
        static_active_turn=dict(g.static_active_turn),
        combo_assembled_turn=list(g.combo_assembled_turn),
        combo_castable_turn=list(g.combo_castable_turn),
        drawn_names=set(timeline.drawn),
    )


def run_batch(
    cards: dict,
    deck: list,
    commander: str,
    n: int,
    seed: int,
    until_turn: int,
    opponents: int = 1,
    combos: list | None = None,
    rules: MulliganRules | None = None,
) -> dict:
    """Run ``n`` policy-driven games with per-game seeds derived from
    ``(seed, index)`` (spec §Architecture: batches are order-independent and
    A/B pairs game-for-game) and aggregate the records."""
    records = []
    for i in range(n):
        rec, _log = play_one_game(
            cards, deck, commander,
            game_seed=derive_seed(seed, i),
            until_turn=until_turn, opponents=opponents,
            combos=combos, rules=rules,
        )
        records.append(rec)
    return {
        "n": n,
        "seed": seed,
        "until_turn": until_turn,
        "metrics": aggregate(records, until_turn),
        "records": records,
    }


# --------------------------------------------------------------------------
# Task 15: A/B — position-stable alignment + paired statistics
# --------------------------------------------------------------------------

# Standing caveat (spec §Statistics), carried in every run_ab output: the
# shared policy plays both decks, so its bias cancels for most swaps — but
# NOT for swaps whose value is sequencing (Sigarda's Aid-class cards); those
# are systematically undervalued by the paired deltas.
_AB_SEQUENCING_CAVEAT = (
    "Both decks are played by the same policy, so policy bias cancels for "
    "most swaps — but not for cards whose value is sequencing (e.g. "
    "Sigarda's Aid-class flash enablers); those are systematically "
    "undervalued."
)


def align_decks(deck_a: list, deck_b: list) -> tuple[list, list]:
    """Position-stable A/B alignment (spec §Statistics): shared cards occupy
    identical indices; swapped cards fill the leftover tail slots. Both the
    shared base and the unique tails are laid out in sorted multiset order,
    so the result is deterministic regardless of input ordering and a k-card
    swap perturbs exactly k positions."""
    if len(deck_a) != len(deck_b):
        raise ValueError(
            f"align_decks requires equal-size decks (got {len(deck_a)} vs "
            f"{len(deck_b)}); deck-size validation belongs upstream, but the "
            "runner refuses to mis-align"
        )
    ca, cb = Counter(deck_a), Counter(deck_b)
    shared = ca & cb
    only_a = sorted((ca - shared).elements())
    only_b = sorted((cb - shared).elements())
    base = sorted(shared.elements())
    return base + only_a, base + only_b


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson r of two equal-length series. Degenerate-variance pin: if
    either series has zero variance, r is 1.0 when the series are identical
    (an A/A run) and 0.0 otherwise. Rounding is clamped to [-1, 1]."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0.0 or vy == 0.0:
        return 1.0 if xs == ys else 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return max(-1.0, min(1.0, cov / math.sqrt(vx * vy)))


def _ab_scalars(
    records: list[GameRecord], until_turn: int
) -> dict[str, list[float]]:
    """The pinned per-game scalar per A/B metric, one list entry per game."""

    # Turn-to-event fields may carry until_turn+1 (upkeep kills fire after
    # the horizon check); the <=T censoring here handles it, same as aggregate.
    def by(turn: int | None, t: int) -> int:
        return 1 if turn is not None and turn <= t else 0

    out: dict[str, list[float]] = {}
    for t in (4, 5, 6):
        out[f"commander_cast_by_t{t}"] = [
            by(r.commander_cast_turn, t) for r in records
        ]
    out["kill_by_until_turn"] = [by(r.kill_turn, until_turn) for r in records]
    out["cmdr21_by_until_turn"] = [
        by(r.cmdr21_turn, until_turn) for r in records
    ]
    out["total_damage"] = [
        float(sum(r.damage_by_turn[:until_turn])) for r in records
    ]
    out["kept_hand_lands"] = [r.kept_hand_lands for r in records]
    out["mulligans_taken"] = [r.mulligans_taken for r in records]
    out["casts_total"] = [
        sum(r.casts_by_turn[:until_turn]) for r in records
    ]
    out["max_cast_chain"] = [
        max(r.casts_by_turn[:until_turn], default=0) for r in records
    ]
    horizon = min(5, until_turn)
    out["on_curve_through_t5"] = [
        1 if all(v >= 1 for v in r.lands_played_by_turn[:horizon]) else 0
        for r in records
    ]
    n_combos = max((len(r.combo_assembled_turn) for r in records), default=0)
    for i in range(n_combos):
        out[f"combo{i}_assembled_by_t6"] = [
            by(
                r.combo_assembled_turn[i]
                if i < len(r.combo_assembled_turn)
                else None,
                6,
            )
            for r in records
        ]
        out[f"combo{i}_castable_by_t6"] = [
            by(
                r.combo_castable_turn[i]
                if i < len(r.combo_castable_turn)
                else None,
                6,
            )
            for r in records
        ]
    return out


def run_ab(
    cards_a: dict,
    deck_a: list,
    commander_a: str,
    cards_b: dict,
    deck_b: list,
    commander_b: str,
    n: int,
    seed: int,
    until_turn: int,
    opponents: int = 1,
    combos: list | None = None,
    rules: MulliganRules | None = None,
    allow_different_commanders: bool = False,
) -> dict:
    """Paired A/B run (spec §Statistics): align the decks position-stably,
    run both arms game-for-game under identical ``derive_seed(seed, i)``
    seeds, and report per-metric paired deltas (± 1.96·sd/√n, exact McNemar
    for binaries) plus the achieved pair correlation — the Pearson r of the
    per-game total-damage-by-until_turn series between arms."""
    if commander_a != commander_b and not allow_different_commanders:
        raise ValueError(
            f"A/B decks have different commanders ({commander_a!r} vs "
            f"{commander_b!r}); cross-commander deltas confound every metric "
            "— pass allow_different_commanders=True to compare anyway"
        )
    aligned_a, aligned_b = align_decks(deck_a, deck_b)
    batch_a = run_batch(
        cards_a, aligned_a, commander_a, n, seed, until_turn,
        opponents=opponents, combos=combos, rules=rules,
    )
    batch_b = run_batch(
        cards_b, aligned_b, commander_b, n, seed, until_turn,
        opponents=opponents, combos=combos, rules=rules,
    )
    scalars_a = _ab_scalars(batch_a["records"], until_turn)
    scalars_b = _ab_scalars(batch_b["records"], until_turn)
    deltas = {
        name: asdict(paired_delta(scalars_a[name], scalars_b[name]))
        for name in scalars_a
        if name in scalars_b
    }
    return {
        "n": n,
        "seed": seed,
        "deltas": deltas,
        "pair_correlation": _pearson(
            scalars_a["total_damage"], scalars_b["total_damage"]
        ),
        "metrics_a": batch_a["metrics"],
        "metrics_b": batch_b["metrics"],
        "records_a": batch_a["records"],
        "records_b": batch_b["records"],
        "caveat": _AB_SEQUENCING_CAVEAT,
    }
