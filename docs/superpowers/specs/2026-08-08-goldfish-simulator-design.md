# Goldfish Simulator — Design

- **Date:** 2026-08-08
- **Status:** Approved (design), pending implementation plan
- **Component:** new `goldfish/` package + tool registrations in `server.py`
- **Branch:** `goldfish`

## Problem

Deck changes are argued from vibes. We want quantitative answers ("how often does
Cloud swing equipped on arrival, before vs after these swaps") and an interactive
mode where the sim holds ground-truth state while Claude narrates decisions. Claude
simulating games purely conversationally drifts state; the tool must own state.

## Goals

1. Batch Monte Carlo: N seeded solitaire games of a decklist → structured stats.
2. A/B: run two lists under identical per-game seeds, diff the metrics.
3. Interactive mode: seeded single game, stepped turn-by-turn, tool returns full
   state each call; state is recoverable if the server restarts.
4. Cards described by a small effect DSL that **Claude annotates in-conversation**
   — the server auto-derives the boring majority of a deck from Scryfall data.
5. Colored mana modeled from v1 (cast-turn numbers are gated by pips, not just
   total mana, in any 2–3 color deck).

## Non-goals (the tarpit fence)

- **No full rules engine.** No stack, no priority, no targeting rules, no opponent
  decks. Goldfish only: static 40-life opponents, no blockers in v1.
- No GUI. MCP tools + JSON/text out; Claude renders and narrates.
- Not a Forge replacement — Forge covers "real games vs AI," this covers statistics.
- No server-side persistent annotation store in v1 (annotations travel with the
  tool call; revisit if re-sending becomes painful).
- Complex mana (conditional producers, filter lands, fetch-land color logic) —
  approximated; noted per-card in the honesty report.

## Prior art (researched 2026-08-08)

Nothing occupies the seeded-batch-goldfish-stats-over-MCP niche. Full engines
(Forge, XMage, Magarena) are Java, GPL or process-boundary-only, and run
seconds-per-game where we need thousands-per-second. The only embeddable Python
library (`mtg-mana-simulator`, MIT, frozen 2022) models mana only. Design ideas
adopted from research:

- **Forge's card DSL shape** (proven against ~30k cards): trigger mode × effect
  verb × selector/condition, flattened here into JSON. Symbolic amounts replace
  bespoke verbs (`{on: attack, do: draw, count: per_equipped_attacker}` rather
  than a one-off `draw_per_equipped_attacker` verb).
- **mtgoncurve/landlord**: auto-tap payment solving, pluggable London-mulligan
  keep rules, per-turn on-curve probability as a headline metric.
- **Godrek/mtg, FasterFishing**: kill-turn histogram, commander-cast-turn
  distribution; per-card leave-one-out contribution deferred to v2.

## Decisions

- **D1 — Annotations are client-supplied.** The server auto-derives lands (with
  produced colors and enters-tapped), mana rocks, plain draw/ramp spells, and
  vanilla bodies from Scryfall; Claude supplies DSL for the interesting cards as
  tool input. Works for any deck with zero server commits. Rejected: checked-in
  YAML per deck (repo commit per deck, awkward for Docker/remote), server-side
  store (most infrastructure, least immediate payoff).
- **D2 — Colored mana in v1.** Costs parsed to pips, producers tagged with color
  sets, greedy strictest-first payment. Rejected: scalar pool (systematically
  optimistic for multicolor; retrofit would touch cost parsing, state, policy,
  and every golden test).
- **D3 — Interactive state is in-memory + resumable.** `game_id → state` dict;
  every response echoes full JSON state; `goldfish_start` accepts a resume blob.
  Rejected: memory-only (restart orphans games), disk persistence (storage
  lifecycle the server doesn't have anywhere else).
- **D4 — Output format.** Batch tools return a compact text summary plus a fenced
  JSON block of raw metrics (consistent with the server's text style, still
  machine-diffable). Interactive tools return JSON state.
- **D5 — Pure stdlib.** No numpy/scipy. 10k games of a linear deck is well within
  pure-Python budget; `math.comb` covers closed-form hypergeometrics.

## Architecture

```
goldfish/
  __init__.py
  cards.py       # DSL pydantic models, verb/condition/static registries, validation
  autoderive.py  # Scryfall card data → annotations (lands, rocks, draw, ramp, vanilla)
  engine.py      # pure game core: step(state, action, rng), legal-action enumeration
  policy.py      # greedy heuristic policy; pluggable choose_action(state) -> action
  metrics.py     # per-game trace → aggregate metrics
  runner.py      # batch runner, per-game seed derivation, paired A/B diff
server.py        # thin @mcp.tool wrappers; deck fetch reuses existing
                 # _parse_decklist / Archidekt / Scryfall helpers
```

- Engine is pure and deterministic: same seed + deck + annotations + policy →
  byte-identical log. Per-game RNG derived from `(master_seed, game_index)` so
  A/B pairs game-for-game and batches are order-independent.
- Card data fetched via the existing Scryfall collection endpoint (as
  `scryfall_price_list` does), cached in-process per server lifetime.

## Card annotation DSL

Auto-derived from Scryfall for every card (never client-supplied): name, mana
cost pips, mana value, types, power/toughness, keywords, equip cost, produced
colors, enters-tapped. The auto-classifier additionally recognizes from oracle
text: mana rocks (`{T}: Add …`), plain draw (`draw N cards`), ramp-to-battlefield
land search, and simple equipment grants (`equipped creature gets +X/+Y`).
Everything unrecognized defaults to `inert` (occupies a slot, does nothing) and
is named in the honesty report.

Client annotations (JSON array, one entry per card name) add or override:

```json
{
  "name": "Cloud, Ex-SOLDIER",
  "triggers": [
    {"on": "etb", "do": "attach_from_board"},
    {"on": "attack", "do": "draw", "count": "per_equipped_attacker"},
    {"on": "attack", "if": "power_gte:7", "do": "treasure", "count": 2}
  ],
  "statics": [],
  "activated": [{"cost": "{3}{R}{W}", "do": "extra_combat"}]
}
```

v1 registries (validated; unknown names rejected with the allowed list so Claude
self-corrects):

- **Events** (`on`): `cast`, `etb`, `attack`, `combat_begin`, `equipment_etb`,
  `upkeep`.
- **Verbs** (`do`): `draw`, `ramp_land`, `ramp_mana`, `treasure`, `attach`,
  `attach_from_board`, `tutor` (with `filter: equipment|land|any`, to hand),
  `pump` (temp or permanent P/T), `extra_combat`, `token_copy`, `gain_life`.
- **Conditions** (`if`): `power_gte:N`, `metalcraft`, `equipped`.
- **Counts**: integer or symbolic `per_equipped_attacker`, `per_artifact`.
- **Statics**: `equip_free`, `equip_free_if_metalcraft`.
- **Equipment grants**: `{"power": N, "toughness": N, "keywords": [...]}`.

Deliberately excluded in v1 (annotate as `inert`; reported): scry/surveil,
protection/removal interaction, instant-speed decisions, opponent-dependent
effects.

## Engine

- **Turn structure:** untap → upkeep triggers → draw → main1 (policy loop) →
  combat (repeatable via `extra_combat`) → main2 (policy loop) → end.
- **Mana:** pips `{W,U,B,R,G,C,generic}`; producers are color sets; payment is
  greedy, spending most-constrained producers first. Enters-tapped respected;
  conditionally-tapped lands approximated by their common case and flagged.
- **Combat:** attack with everything (goldfish); commander damage tracked
  per-opponent; opponents deal no damage in v1.
- **Commander:** command zone, commander tax on recast (no death in v1, but tax
  plumbing exists for v2 removal events).
- **Mulligan:** London; optional EDH free-first; keep rule = land count in a
  configurable `[min, max]` (default 3–5 for a 99-card deck). Only actual lands
  count toward the keep rule — rocks and dorks do not.
- **Policy (greedy, v1):** play land (prefer untapped when it enables a cast) →
  mana rocks → land-ramp → static enablers → commander at affordable cost
  (with tax) → equipment → attach (free first, then paid) → attack with
  everything. Pluggable `choose_action(state) -> action` interface for v3.

## MCP tools

| Tool | Input | Output |
|---|---|---|
| `goldfish_annotate` | deck (Archidekt URL or decklist text) | auto-derived annotations + oracle text for unrecognized cards, so Claude fills the gaps in one pass |
| `goldfish_run` | deck, annotations?, n=1000, seed=42, until_turn=10, opponents=1, mulligan opts | text summary + metrics JSON + honesty report |
| `goldfish_ab` | deck_a, deck_b, shared/per-deck annotations, n, seed | paired-seed per-metric deltas |
| `goldfish_start` | deck, annotations?, seed, resume_state? | game_id + full state + legal actions |
| `goldfish_step` | game_id, action? (omit = policy chooses) | new state + chosen/legal actions + log lines |
| `goldfish_state` | game_id | current full state |
| `goldfish_odds` | deck_size, copies, draws, min_successes=1 | exact hypergeometric probability (no simulation) |

Illegal `goldfish_step` actions are rejected with a reason; state is never
mutated on rejection.

## v1 metrics

- Mulligan/keep stats; lands in kept hand.
- Commander cast turn: distribution, median, % by T4/T5/T6.
- % of games commander is equipped the turn it arrives; avg equipment on board
  at that point.
- Damage: total and commander damage to a single opponent by turn N; median turn
  to 21 commander damage; kill-turn histogram (40 damage).
- On-curve health: % of games with land drop made each turn through T5; avg mana
  available per turn.
- **Generic trigger-fire table:** avg fires per game for every annotated trigger,
  per card. This subsumes bespoke metrics ("treasure rate", "draws from
  triggers") and needs no new code for future decks.
- Honesty report: every `inert`/approximated card with % of games it was drawn.

## Interactive state schema

```json
{
  "turn": 4, "phase": "main1",
  "mana_pool": {"W": 2, "R": 1, "C": 2},
  "zones": {"library": 84, "hand": ["..."],
            "battlefield": [{"name": "...", "attached": ["..."], "tapped": false}],
            "graveyard": [], "command": []},
  "commander": {"cast_count": 1, "damage_dealt": {"opp1": 9}},
  "opponents": [{"life": 31, "cmdr_dmg": 9}],
  "rng_state": [...], "log": ["T3: cast Puresteel Paladin"],
  "resume": {"deck": "...", "annotations": [...]}
}
```

`rng_state` is `random.Random.getstate()` serialized, so a resumed game continues
its exact shuffle/draw sequence.

## Error handling

- Deck fetch/parse errors reuse the server's existing error-helper style.
- Annotation validation errors name the offending card, field, and the allowed
  registry values.
- `goldfish_ab` refuses decks with different commanders unless explicitly
  overridden (guards against accidental apples-to-oranges runs).

## Acceptance criteria

- [ ] Same seed + deck + annotations + policy → byte-identical game log
      (golden-seed tests in CI).
- [ ] `goldfish_run` n=10000 on a 99-card deck completes < 30s.
- [ ] Hypergeometric sanity: "≥1 of k copies in first m cards" from sim matches
      `goldfish_odds` closed form within 1%.
- [ ] Colored-payment correctness: a deck whose lands cannot produce `{R}` never
      casts an `{R}` spell (unit tests).
- [ ] `goldfish_ab` with identical decks reports ~zero deltas.
- [ ] Interactive: illegal actions rejected with reason; state unmutated on
      rejection; a game resumed from a state blob replays identically.
- [ ] Unmodeled cards reported by name with drawn-% in every run.
- [ ] Annotation validation rejects unknown verbs/events/conditions with the
      allowed list in the message.

## Phasing

- **v1:** everything above.
- **v2:** simple blocker model (opponent chumps X power/turn), removal-pressure
  events (random removal on a timer to test resilience), per-card leave-one-out
  contribution, BFS minimum-kill-turn solver, persistent annotation store if
  re-sending annotations proves painful.
- **v3:** Claude-in-the-loop policy via `goldfish_step` as a first-class batch
  mode; policy comparisons (greedy vs Claude decisions).
