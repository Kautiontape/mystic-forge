# Goldfish Simulator — Design

- **Date:** 2026-08-08
- **Status:** Approved (design); amended same day after multi-user review and a
  five-persona stress test; amendments pending user review
- **Component:** new `goldfish/` package + tool registrations in `server.py`
- **Branch:** `goldfish`

## Problem

Deck changes are argued from vibes. We want quantitative answers ("how often does
Cloud swing equipped on arrival, before vs after these swaps") and an interactive
mode where the sim holds ground-truth state while Claude narrates decisions. Claude
simulating games purely conversationally drifts state; the tool must own state.

## Goals

1. Batch Monte Carlo: N seeded solitaire games of a decklist → structured stats.
2. A/B: run two lists under identical per-game seeds, diff the metrics — and
   distinguish signal from noise (confidence intervals, paired significance).
3. Interactive mode: seeded single game, stepped turn-by-turn, tool returns full
   state each call; state is recoverable if the server restarts.
4. Cards described by a small effect DSL that **Claude annotates in-conversation**
   — the server auto-derives the boring majority of a deck from Scryfall data.
5. Colored mana modeled from v1 (cast-turn numbers are gated by pips, not just
   total mana, in any 2–3 color deck).
6. Serve the common goldfishable archetypes — equipment/voltron, spellslinger,
   go-wide tokens, lands/landfall, tutor-combo — not just the Cloud deck.

## Non-goals (the tarpit fence)

- **No full rules engine.** No stack, no priority, no targeting rules, no opponent
  decks. Goldfish only: static 40-life opponents, no blockers in v1.
- No death/sacrifice semantics in v1 (sac outlets, dies triggers, state-based
  deaths) — v2, behind an explicit `sac_token`-style cost design.
- No spell copying, X-costs, or graveyard recursion loops in v1 — v2 candidates.
- No targeting-dependent effects ever (e.g. Zada-style copy-per-target) — they
  require targeting semantics, which is the rules-engine slope.
- No GUI. MCP tools + JSON/text out; Claude renders and narrates.
- Not a Forge replacement — Forge covers "real games vs AI," this covers statistics.
- No server-side persistent annotation store in v1 (annotations travel with the
  tool call; revisit if re-sending becomes painful).
- Complex mana (conditional producers, filter lands, fetch color logic) —
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

## Persona stress test (2026-08-08)

Five persona reviews of the draft design, each attempting to annotate a real deck
with the then-current registries and to answer their archetype's questions:

| Persona | Verdict on draft | Load-bearing findings |
|---|---|---|
| Competitive equipment tuner | partially served | No CI/variance anywhere; paired seeds silently lose power unless the shuffle is position-stable across A/B lists; keep rule mismeasures land↔rock swaps; greedy order casts commander before same-turn equipment, corrupting "% equipped on arrival" |
| Spellslinger/burn | not served | No damage verb, no "you cast a spell" event; 0/8 defining cards expressible; policy never casts generic spells |
| Go-wide tokens | not served | No token-creation verb, no `{T}` activation costs, no anthem static; **summoning sickness absent from the spec entirely** (a correctness bug for every archetype) |
| Lands/landfall | not served | No `land_etb` event, one land drop hardcoded (expensive to retrofit past golden tests), no `per_land` count; fetches mis-count landfall |
| Tutor-combo | partially served | Tutor filter can't name cards; no combo-assembly metric; combat-only kill turn is misleading (not just missing) for spell combos; `goldfish_odds` can't do "A AND B" |

Every fix adopted below is counters-and-arithmetic on existing state — none
require the stack, priority, targeting, or death semantics, so the tarpit fence
holds. The registry roughly doubles; that is the price of goal 6.

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
- **D6 — Registry generalized beyond the target deck.** The persona stress test
  showed the draft registry was equipment-shaped. v1 adds the fence-safe events,
  verbs, counts, and statics needed by spellslinger, tokens, lands, and combo
  archetypes (see DSL section). Rejected: shipping Cloud-only and generalizing
  later — three of five common archetypes would get honest-but-useless output.
- **D7 — Statistics are first-class.** Every proportion metric carries a 95%
  Wilson CI. `goldfish_ab` computes per-game paired differences and reports mean
  delta ± 1.96·sd/√n with a significance flag (exact McNemar for binary
  metrics). Deck arrays are aligned so shared cards occupy identical indices
  before shuffling — a k-card swap perturbs exactly k positions — and the
  achieved per-pair correlation is reported so degraded pairing is visible.
- **D8 — Summoning sickness is modeled.** Creatures cannot attack (or use `{T}`
  activations) the turn they arrive unless they have haste. Silently omitting
  it would inflate every combat metric; haste is already auto-derived.

## Architecture

```
goldfish/
  __init__.py
  cards.py       # DSL pydantic models, verb/condition/static registries, validation
  autoderive.py  # Scryfall card data → annotations (lands, rocks, draw, ramp, vanilla)
  engine.py      # pure game core: step(state, action, rng), legal-action enumeration
  policy.py      # greedy heuristic policy; pluggable choose_action(state) -> action
  metrics.py     # per-game trace → aggregate metrics + CIs
  runner.py      # batch runner, per-game seed derivation, paired A/B stats
server.py        # thin @mcp.tool wrappers; deck fetch reuses existing
                 # _parse_decklist / Archidekt / Scryfall helpers
```

- Engine is pure and deterministic: same seed + deck + annotations + policy →
  byte-identical log. Per-game RNG derived from `(master_seed, game_index)` so
  A/B pairs game-for-game and batches are order-independent.
- Card data fetched via the existing Scryfall collection endpoint (as
  `scryfall_price_list` does), cached in-process per server lifetime.

### Concurrency & multi-user

The server serves multiple MCP sessions over streamable HTTP; goldfish is its
first CPU-bound feature, so:

- **Batch runs execute off the event loop** via `asyncio.to_thread`, behind a
  global semaphore (2 concurrent runs; further requests queue). `n` is capped
  (20,000) to bound worst-case latency. A sim in flight must never block other
  users' tool calls.
- **Game IDs are `uuid4`** — collision-free and unguessable in practice; the ID
  is the only capability token for a game.
- **Game store eviction:** LRU cap (100 games) plus idle TTL (24h). Eviction is
  non-destructive because every response echoes the full resumable state blob —
  an evicted game restores via `goldfish_start(resume_state=...)`.
- **No per-user identity or isolation** — consistent with the rest of Mystic
  Forge (trusted personal deployment). The spec states this rather than
  implying isolation that doesn't exist.

## Card annotation DSL

Auto-derived from Scryfall for every card (never client-supplied): name, mana
cost pips, mana value, types, power/toughness, keywords, equip cost, produced
colors **and quantity** (a Karoo makes 2), enters-tapped. The auto-classifier
additionally recognizes from oracle text: mana rocks (`{T}: Add …`), plain draw
(`draw N cards`), ramp-to-battlefield land search, fetch lands (sacrifice-to-
search: modeled as sac-self + searched land entering, so land counts and
`land_etb` fires stay correct; color logic stays approximate and flagged), and
simple equipment grants (`equipped creature gets +X/+Y`). Everything
unrecognized defaults to `inert` (occupies a slot, does nothing) and is named
in the honesty report.

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

- **Self events** (fire for the annotated card itself): `cast`, `etb`.
- **Global events** (fire for any qualifying event on your side): `spell_cast`
  (optional `filter: instant_or_sorcery | noncreature | any`), `creature_etb`,
  `equipment_etb`, `land_etb`, `attack`, `combat_begin`, `upkeep`.
- **Verbs** (`do`):
  - `draw`, `gain_life`, `treasure`
  - `damage` (`target: one_opponent | each_opponent`) — opponents are static
    life totals; no targeting rules involved
  - `create_token` (`power`, `toughness`, `count`, `keywords?`)
  - `ramp_land` (search land onto battlefield), `ramp_mana` (create a permanent
    producer), `add_mana` (one-shot pool add, pip string e.g. `"{R}{R}{R}"`)
  - `tutor` (to hand; `filter: equipment | land | creature | instant | sorcery |
    artifact | name:<CardName> | any`)
  - `attach`, `attach_from_board`, `pump` (temp or permanent P/T),
    `extra_combat`, `token_copy`
- **Conditions** (`if`): `power_gte:N`, `metalcraft`, `equipped`.
- **Counts**: integer or symbolic `per_equipped_attacker`, `per_artifact`,
  `per_creature`, `per_attacker`, `per_land`, `per_spell_cast_this_turn`.
- **Statics**: `equip_free`, `equip_free_if_metalcraft`,
  `anthem {power, toughness}` (your creatures, applied at power-calculation
  time, fixed order, no layers), `token_doubling` (`count *= 2^n` inside
  `create_token`), `cost_reduction {filter, amount}` (shaves generic before the
  pip solver runs), `extra_land_drops: N`.
- **Activated**: `cost` = mana pip string, `{T}`, or both. `{T}` activations
  respect summoning sickness on creatures.
- **Equipment grants**: `{"power": N, "toughness": N, "keywords": [...]}`.

Deliberately excluded in v1 (annotate as `inert`; reported): scry/surveil,
death/sacrifice triggers and sac costs, spell copying, X-costs, graveyard
recursion, targeting-dependent effects, protection/removal interaction,
instant-speed decisions, opponent-dependent effects.

## Engine

- **Turn structure:** untap → upkeep triggers → draw → main1 (policy loop) →
  combat (repeatable via `extra_combat`) → main2 (policy loop) → end. Per-turn
  state counters: `land_drops_remaining` (1 + `extra_land_drops` statics),
  `spells_cast_this_turn`.
- **Summoning sickness (D8):** creatures track arrival turn; no attacks or
  `{T}` activations on arrival turn without haste (own keyword, granted by
  equipment, or anthem-style grant).
- **Mana:** pips `{W,U,B,R,G,C,generic}`; producers are color sets **with
  quantity**; payment is greedy, spending most-constrained producers first.
  `cost_reduction` statics shave generic cost before the pip solver runs.
  Enters-tapped respected; conditionally-tapped lands approximated by their
  common case and flagged.
- **Combat:** attack with everything legal (goldfish); commander damage tracked
  per-opponent; opponents deal no damage in v1. Multi-opponent damage
  assignment: all attackers hit the same opponent until lethal, then next
  (focus-fire), so sequential-kill and table-lethal metrics are well-defined.
- **Commander:** command zone, commander tax on recast (no death in v1, but tax
  plumbing exists for v2 removal events).
- **Combo detection (passive):** `goldfish_run`/`goldfish_ab` accept
  `combos: [["Kiki-Jiki, Mirror Breaker", "Zealous Conscripts"], ...]`. At the
  end of each main phase the engine records: **assembled** (all pieces in
  hand/command zone) and **assembled+castable** (assembled AND the joint pip
  cost of all pieces is payable with currently available producers). Optional
  `wins: true` per combo ends the game there and feeds the kill-turn histogram
  — without it, spell-combo decks get a misleading combat-only kill turn. The
  joint same-turn payment check is a flagged simplification (ignores casting a
  piece the turn before).
- **Mulligan:** London; optional EDH free-first. Keep rule counts **mana
  sources** (lands + auto-classified rocks of MV ≤ 2) in a configurable
  `[min, max]` (default 3–5 for a 99-card deck; lands-only counting available
  as an opt-in). Hands of ≤ 5 cards are always kept. Bottoming rule is pinned
  and deterministic: bottom excess lands above `max`, then highest-MV spells —
  required for byte-identical golden logs.

## Policy (greedy, v1)

Within a main phase, in order:

1. Play land(s) while `land_drops_remaining > 0` (prefer untapped when it
   enables a cast this turn).
2. Cast static enablers and engine permanents.
3. Cast mana rocks, then land-ramp spells.
4. Compute the turn's affordable purchase set. **If both the commander and
   equipment are affordable this turn, cast equipment first** so ETB
   attach effects see them (protects "% equipped on arrival" — the draft order
   provably misplayed the target archetype).
5. Cast the commander at affordable cost (with tax).
6. Cast remaining annotated spells: `add_mana` rituals only when remaining hand
   cost exceeds the pool, then cheapest-first (maximizes cast count; greedy
   slightly undercounts optimal storm chains — noted in the honesty report).
7. Attach equipment (free first, then paid).
8. Activate abilities: token producers activate post-combat by default,
   pre-combat when their output could attack immediately (haste available);
   `extra_combat` activations fire when combat damage is live.
9. Attack with everything legal.

Tutor targeting: when a declared combo is missing exactly one accessible piece
and the tutor's filter admits it, fetch that piece; otherwise a filter-specific
default (cheapest unattached equipment, first missing land type, etc.). The
policy is pluggable (`choose_action(state) -> action`) for v3.

## Statistics (batch + A/B)

- Every proportion metric reports a 95% Wilson CI; medians report IQR.
- `goldfish_ab` runs both lists under identical per-game seeds with
  **position-stable deck alignment**: shared cards occupy identical deck-array
  indices before shuffling, so a k-card swap perturbs exactly k positions and
  per-pair correlation survives. The achieved per-pair correlation is reported;
  per-metric output is mean paired delta ± 1.96·sd/√n plus a significance flag
  (exact McNemar for binary metrics — stdlib per D5).
- `goldfish_ab` output carries a standing caveat: shared-policy bias cancels
  for most swaps but **not** for swaps whose value is sequencing (e.g.
  Sigarda's Aid-class cards) — those are systematically undervalued.
- `goldfish_ab` refuses decks with different commanders unless explicitly
  overridden.

## MCP tools

| Tool | Input | Output |
|---|---|---|
| `goldfish_annotate` | deck (Archidekt URL or decklist text) | auto-derived annotations + oracle text for unrecognized cards, so Claude fills the gaps in one pass |
| `goldfish_run` | deck, annotations?, combos?, n=1000, seed=42, until_turn=10, opponents=1, mulligan opts | text summary + metrics JSON (with CIs) + honesty report |
| `goldfish_ab` | deck_a, deck_b, shared/per-deck annotations, combos?, n, seed | paired-seed per-metric deltas ± CI, significance flags, achieved pairing correlation |
| `goldfish_start` | deck, annotations?, seed, resume_state? | game_id + full state + legal actions |
| `goldfish_step` | game_id, action? (omit = policy chooses) | new state + chosen/legal actions + log lines |
| `goldfish_state` | game_id | current full state |
| `goldfish_odds` | deck_size, draws, then either flat (copies, min_successes=1) or `groups: [{copies, min_successes}, ...]` | exact hypergeometric probability; multi-group via inclusion–exclusion (answers "≥1 piece A AND ≥1 piece B in top m") |

Illegal `goldfish_step` actions are rejected with a reason; state is never
mutated on rejection.

## v1 metrics

- Mulligan/keep stats; lands and mana sources in kept hand.
- Commander cast turn: distribution, median, % by T4/T5/T6.
- % of games commander is equipped the turn it arrives; avg equipment on board
  at that point.
- Damage: per-opponent and table-total, combat and noncombat split, by turn N;
  median turn to 21 commander damage; kill-turn histogram (single opponent) and
  **table-lethal turn** (first turn total attacking power ≥ combined remaining
  life; sequential-kill turn under focus-fire).
- Board state per turn: creature count (width), total power (post-anthem),
  tokens created (aggregate and by source).
- Casts per turn: distribution, max single-turn chain, % of games with an
  N+-spell turn by T6.
- On-curve health: % of games with all land drops made each turn through T5;
  avg mana available per turn.
- Combo (when declared): assembled-by-turn and assembled+castable-by-turn
  distributions, % by T4/T5/T6.
- **Generic trigger-fire table:** avg fires per game for every annotated
  trigger, per card. Subsumes bespoke metrics ("treasure rate", "Tremors
  damage") with no new metric code per deck.
- **Static/condition activation:** first turn each static or condition
  (metalcraft, equip_free) becomes active — "% of games online by T4".
- Honesty report: every `inert`/approximated card with % of games it was drawn;
  standing notes for known approximations (greedy chain undercount, joint
  combo payment, fetch color logic).

## Interactive state schema

```json
{
  "turn": 4, "phase": "main1",
  "mana_pool": {"W": 2, "R": 1, "C": 2},
  "land_drops_remaining": 1, "spells_cast_this_turn": 2,
  "zones": {"library": 84, "hand": ["..."],
            "battlefield": [{"name": "...", "attached": ["..."], "tapped": false,
                             "arrived_turn": 3}],
            "graveyard": [], "command": []},
  "commander": {"cast_count": 1, "damage_dealt": {"opp1": 9}},
  "opponents": [{"life": 31, "cmdr_dmg": 9}],
  "rng_state": [...], "log": ["T3: cast Puresteel Paladin"],
  "resume": {"deck": "...", "annotations": [...], "combos": [...]}
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
      (golden-seed tests in CI), including mulligan bottoming decisions.
- [ ] `goldfish_run` n=10000 on a 99-card deck completes < 30s.
- [ ] Hypergeometric sanity: "≥1 of k copies in first m cards" from sim matches
      `goldfish_odds` closed form within 1%; multi-group closed form validates
      the assembled-by-turn metric on a tutor-free deck.
- [ ] Colored-payment correctness: a deck whose lands cannot produce `{R}` never
      casts an `{R}` spell (unit tests).
- [ ] Summoning sickness: a creature cast without haste never attacks or `{T}`
      activates the turn it arrives (unit tests).
- [ ] `goldfish_ab` with identical decks reports ~zero deltas; a 1-card-swap
      A/B reports per-pair correlation above a stated floor (pairing actually
      pairs).
- [ ] Every proportion metric in `goldfish_run` output carries a CI.
- [ ] Interactive: illegal actions rejected with reason; state unmutated on
      rejection; a game resumed from a state blob replays identically.
- [ ] Unmodeled cards reported by name with drawn-% in every run.
- [ ] Annotation validation rejects unknown verbs/events/conditions with the
      allowed list in the message.
- [ ] Concurrency: while a batch run is in flight, an unrelated tool call on
      another session answers without waiting for the run to finish.
- [ ] A game evicted from the in-memory store is fully restorable from its
      echoed state blob.

## Phasing

- **v1:** everything above.
- **v2:** death/sacrifice semantics (`sac_token`-style activation costs, dies
  triggers — unlocks Skullclamp/aristocrats), simple blocker model where a
  chump block **kills an attacker** (so width-resilience is visible, not just
  damage absorption), removal-pressure events (random removal on a timer),
  `copy_spell` verb, X-cost casting (X = remaining pool), graveyard land
  recursion, per-card leave-one-out contribution, BFS minimum-kill-turn solver,
  keep-rule extensions (mull toward tutors/combo pieces), protection-overlap
  metric, subtype count symbols (`per_goblin`), persistent annotation store if
  re-sending annotations proves painful.
- **v3:** Claude-in-the-loop policy via `goldfish_step` as a first-class batch
  mode; policy comparisons (greedy vs Claude decisions).
