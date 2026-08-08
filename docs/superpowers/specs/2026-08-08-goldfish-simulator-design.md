# Goldfish Simulator — Design

- **Date:** 2026-08-08
- **Status:** Approved (design); amended after multi-user review and two
  persona stress-test rounds (7 personas); pending user review
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
7. Never mislead the casual user: out-of-scope cards are classified and counted,
   and A/B output states what the sim can and cannot judge.

## Non-goals (the tarpit fence)

- **No full rules engine.** No stack, no priority, no targeting rules, no opponent
  decks. Goldfish only: static 40-life opponents, no blockers in v1.
- No death/sacrifice semantics in v1 (sac outlets, dies triggers, state-based
  deaths) — v2, behind an explicit `sac_token`-style cost design.
- No spell copying, X-costs, Sagas, coin flips, life-payment activation costs,
  or graveyard recursion loops in v1 — v2 candidates.
- No targeting-dependent effects ever (e.g. Zada-style copy-per-target) — they
  require targeting semantics, which is the rules-engine slope.
- Interaction (removal, counterspells, wipes, protection-in-response) is
  **structurally invisible** to a goldfish sim — permanently out of scope for
  *valuation*; v1's job is to classify and count it honestly (goal 7).
- No GUI. MCP tools + JSON/text out; Claude renders and narrates.
- Not a Forge replacement — Forge covers "real games vs AI," this covers statistics.
- No server-side persistent annotation store in v1 (annotations travel with the
  tool call; cached precon annotation packs are a v2 exception candidate).
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

## Persona stress tests (2026-08-08)

**Round 1** (five personas vs the draft): the registry was equipment-shaped —
spellslinger, tokens, and lands collapsed to `inert`; the Spike found the
missing CI/pairing rigor; the combo player found combat-only kill turns
misleading. All adopted fixes are counters-and-arithmetic; the registry roughly
doubled — the price of goal 6.

| Persona | Draft verdict | Re-review after amendments |
|---|---|---|
| Competitive equipment tuner | partially served | fixes confirmed; 3 pins (keep-rule land floor, equipment-first guard, censored metrics) — applied below |
| Spellslinger/burn | not served | **approve**; pins (cost_reduction filters, pump schema, cast-count timing, ritual order) — applied below |
| Go-wide tokens | not served | registry serves it; 2 pins (withhold tapped producers from combat, anthem keywords) — applied below |
| Lands/landfall | not served | fixes confirmed; 2 pins (dynamic drop counter, universal `land_etb`) — applied below |
| Tutor-combo | partially served | fixes confirmed; 1 correctness pin (assembled must count battlefield) — applied below |

**Round 2** (two new personas): the **interactive narrator** found the action
schema for `goldfish_step` entirely undefined and battlefield objects lacking
instance IDs — v1 blockers, fixed by the Interactive mode section below. The
**precon upgrader** (grounded against the real Limit Break precon: ~28% of the
friendliest possible precon is out of scope) showed `goldfish_ab` could
actively mislead casual users — zero deltas on interaction swaps, inflated
deltas from inert-count asymmetry, wrong-reason "confirmation" of community
cuts — driving goal 7 and the out-of-scope classification below.

Not run deliberately: aristocrats/reanimator (the v2 fence already answers
them), stax/politics (not goldfishable — permanent non-goal, stated in tool
descriptions).

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
  This doubles as **rewind**: restarting from any earlier echoed blob replays
  the same future draws (`rng_state` travels in the blob), which is exactly the
  right semantics for "what if I'd played the rock instead."
- **D4 — Output format.** Batch tools return a compact text summary plus a fenced
  JSON block of raw metrics (consistent with the server's text style, still
  machine-diffable). Interactive tools return JSON state.
- **D5 — Pure stdlib.** No numpy/scipy. 10k games of a linear deck is well within
  pure-Python budget; `math.comb` covers closed-form hypergeometrics. Side
  benefit: the engine is runnable in any agent sandbox from a single download —
  a PyPI CLI / Agent Skill packaging is a cheap v2 follow-on.
- **D6 — Registry generalized beyond the target deck.** (Round-1 finding.) v1
  includes the fence-safe events, verbs, counts, and statics needed by
  spellslinger, tokens, lands, and combo archetypes. Rejected: shipping
  Cloud-only — three of five common archetypes would get honest-but-useless
  output.
- **D7 — Statistics are first-class.** Every proportion metric carries a 95%
  Wilson CI. `goldfish_ab` computes per-game paired differences and reports mean
  delta ± 1.96·sd/√n with a significance flag (exact McNemar for binary
  metrics). Deck arrays are aligned so shared cards occupy identical indices
  before shuffling — a k-card swap perturbs exactly k positions — and the
  achieved per-pair correlation is reported so degraded pairing is visible.
- **D8 — Summoning sickness is modeled.** Creatures cannot attack (or use `{T}`
  activations) the turn they arrive unless they have haste. Silently omitting
  it would inflate every combat metric; haste is already auto-derived.
- **D9 — Out-of-scope cards are classified, not just inert.** (Precon-upgrader
  finding.) The auto-classifier labels unsimulable cards by class
  (`interaction_removal`, `interaction_wipe`, `interaction_counter`,
  `protection`, `political`, `unmodeled_other`). All behave as `inert` in the
  engine; the labels drive honest reporting (goal 7) and cut the annotation
  burden — `goldfish_annotate` requests annotations only for plausibly
  simulable cards.
- **D10 — The action schema is the interactive contract.** (Narrator finding.)
  One step = one atomic action; actions are a typed discriminated union;
  battlefield objects carry stable instance IDs. v3's Claude-in-the-loop batch
  mode is premised on this same interface.

## Architecture

```
goldfish/
  __init__.py
  cards.py       # DSL pydantic models, verb/condition/static registries, validation
  autoderive.py  # Scryfall card data → annotations + out-of-scope classification
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
search: modeled as sac-self + searched land entering; color logic stays
approximate and flagged), simple equipment grants (`equipped creature gets
+X/+Y`), and **out-of-scope classes per D9** (removal, wipes, counterspells,
protection, political, and unmodeled-other such as Sagas, X-costs, coin flips,
sac effects). Out-of-scope and unrecognized cards behave as `inert` (occupy a
slot, do nothing) and are named — with their class — in the honesty report.

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
  Global-event listeners are active **only while their source is on the
  battlefield**; a card's own cast never fires its own `spell_cast` listener.
  `land_etb` fires for **every** land entering — land drops, `ramp_land`
  searches, and fetch resolutions alike. Each token from `create_token` fires
  `creature_etb` individually.
- **Verbs** (`do`):
  - `draw`, `gain_life`, `treasure`
  - `damage` (`target: one_opponent | each_opponent`) — opponents are static
    life totals; no targeting rules involved
  - `create_token` (`power`, `toughness`, `count`, `keywords?`)
  - `ramp_land` (search land onto battlefield), `ramp_mana` (create a permanent
    producer), `add_mana` (one-shot pool add: pip string e.g. `"{R}{R}{R}"`, or
    `{count: N, colors: any}` — wildcard mana spent as whatever the payment
    solver needs)
  - `tutor` (to hand; `filter: equipment | land | creature | instant | sorcery |
    artifact | enchantment | planeswalker | name:<CardName> | any`)
  - `attach`, `attach_from_board`,
    `pump` (`{power, toughness, duration: eot | permanent}`, applies to the
    annotated card; team-wide bonuses are the `anthem` static),
    `extra_combat`, `token_copy`
- **Conditions** (`if`): `power_gte:N`, `metalcraft`, `equipped`.
- **Counts**: integer or symbolic `per_equipped_attacker`, `per_artifact`,
  `per_creature`, `per_attacker`, `per_land`, `per_spell_cast_this_turn`
  (a spell's own cast increments the counter **before** its triggers resolve —
  Grapeshot-style counts include the spell itself). Tribal stand-ins
  (`per_creature` for `per_goblin`) are legal but noted in the honesty report;
  true subtype counts are v2.
- **Statics**: `equip_free`, `equip_free_if_metalcraft`,
  `anthem {power, toughness, keywords?}` (your creatures, applied at
  power-calculation time, fixed order, no layers; `keywords` covers haste
  lords), `token_doubling` (`count *= 2^n` inside `create_token`),
  `cost_reduction {filter, amount}` (filter vocabulary: `instant_or_sorcery |
  noncreature | creature | artifact | equipment | color:<W|U|B|R|G> | any`;
  shaves generic before the pip solver runs), `extra_land_drops: N`.
- **Activated**: `cost` = mana pip string, `{T}`, or both; the effect side
  accepts the full verb parameter set (e.g. Krenko:
  `{cost: "{T}", do: create_token, power: 1, toughness: 1, count: per_creature}`).
  `{T}` activations respect summoning sickness on creatures.
- **Equipment grants**: `{"power": N, "toughness": N, "keywords": [...]}`.

Deliberately excluded in v1 (classified per D9, behave as `inert`, reported):
scry/surveil, death/sacrifice triggers and sac costs, spell copying, X-costs,
Sagas, coin flips, life-payment activation costs, graveyard recursion,
targeting-dependent effects, protection/removal interaction, instant-speed
decisions, opponent-dependent effects.

## Engine

- **Turn structure:** untap → upkeep triggers → draw → main1 (policy loop) →
  combat (repeatable via `extra_combat`) → main2 (policy loop) → end. Per-turn
  state counters: `land_drops_remaining` and `spells_cast_this_turn`.
  `land_drops_remaining` is **dynamic**: an `extra_land_drops` static arriving
  mid-turn raises it immediately, and the multi-pass policy loop revisits land
  plays (Azusa grants drops the turn she lands).
- **Summoning sickness (D8):** creatures track arrival turn; no attacks or
  `{T}` activations on arrival turn without haste (own keyword, granted by
  equipment or anthem).
- **Mana:** pips `{W,U,B,R,G,C,generic}` plus a wildcard slot from
  `colors: any` sources; producers are color sets **with quantity**; payment is
  greedy, most-constrained first, wildcards spent last. `cost_reduction`
  statics shave generic cost before the pip solver runs. The pool persists for
  the whole turn (a flagged simplification — real pools empty per phase).
  Enters-tapped respected; conditionally-tapped lands approximated by their
  common case and flagged.
- **Combat:** attack with all legal attackers the policy commits (see policy
  rule 9); commander damage tracked per-opponent; opponents deal no damage in
  v1. Multi-opponent assignment is focus-fire: all attackers hit one opponent
  until lethal, then the next — so sequential-kill and table-lethal metrics
  are well-defined.
- **Commander:** command zone, commander tax on recast (no death in v1, but tax
  plumbing exists for v2 removal events).
- **Combo detection (passive):** `goldfish_run`/`goldfish_ab` accept
  `combos: [["Kiki-Jiki, Mirror Breaker", "Zealous Conscripts"], ...]`. At the
  end of each main phase the engine records: **assembled** — every piece is in
  hand, command zone, **or on the battlefield** — and **assembled+castable** —
  assembled AND the joint pip cost of the not-yet-deployed pieces (battlefield
  pieces cost zero; command-zone pieces include accumulated commander tax) is
  payable with currently available producers. Optional `wins: true` per combo:
  the game ends at the moment assembled+castable is first true; the remaining
  pieces are logged as cast (feeding `spells_cast_this_turn` and cast metrics)
  and the turn feeds the kill-turn histogram. The joint same-turn payment check
  is a flagged simplification (ignores casting a piece the turn before).
- **Mulligan (batch rule):** London; optional EDH free-first. Keep = mana
  sources (lands + auto-classified rocks of MV ≤ 2) within configurable
  `[min, max]` **and at least 2 actual lands** (rocks need lands to get cast).
  Defaults 3–5 sources for a 99-card deck; lands-only counting is an opt-in.
  Hands of ≤ 5 cards are always kept. Bottoming is pinned and deterministic:
  bottom excess mana sources above `max` (lands first), then highest-MV
  spells — required for byte-identical golden logs.

## Policy (greedy, v1)

The main-phase loop is **multi-pass**: after every action the rule list is
re-evaluated from the top until no rule fires.

1. Play land(s) while `land_drops_remaining > 0` (prefer untapped when it
   enables a cast this turn).
2. Cast static enablers and engine permanents.
3. Cast mana rocks, then land-ramp spells.
4. **Equipment-before-commander guard:** if the commander and equipment are
   both castable this turn *and the commander remains payable after buying the
   equipment*, cast equipment first so ETB attach effects see it. Otherwise
   the commander takes priority.
5. Cast the commander at affordable cost (with tax).
6. Cast remaining annotated spells cheapest-first (maximizes cast count).
   `add_mana` rituals: cast early when hand cost exceeds the pool; otherwise
   cast at the end of this step — they still count toward casts-per-turn and
   feed `spell_cast` listeners. Greedy slightly undercounts optimal storm
   chains — honesty-report note.
7. Attach equipment (free first, then paid).
8. Activate abilities: token producers activate post-combat by default,
   pre-combat when their output could attack immediately (haste available);
   `extra_combat` fires when combat damage is live.
9. Attack with everything legal **except** creatures whose `{T}` activation
   the policy plans to use this turn (a tapped Krenko makes no tokens; the
   activation is valued over the attack).

Tutor targeting: when a declared combo is missing exactly one accessible piece
and the tutor's filter admits it, fetch that piece; otherwise a filter-specific
default (cheapest unattached equipment, first missing land type, etc.).
Policy-chosen actions carry a `reason` string naming the rule that fired
("rule 4: equipment before commander so the ETB attach sees it") so Claude
narrates real rationale instead of confabulating. The policy is pluggable
(`choose_action(state) -> action`) for v3.

## Interactive mode

**One step = one atomic action (D10).** `goldfish_step` with no action asks the
policy to choose one; with an action it validates and applies it. `pass`
advances to the next phase.

**Action schema** — discriminated union on `type`; permanents are referenced by
stable instance ID (`b3`), with unique card names accepted as a convenience
alias (ambiguous names are rejected with the candidate IDs):

```json
{"type": "play_land",  "card": "Plains"}
{"type": "cast",       "card": "Puresteel Paladin"}
{"type": "attach",     "card": "Colossus Hammer", "target": "b3"}
{"type": "activate",   "card": "Krenko, Mob Boss", "ability": 0}
{"type": "attack",     "attackers": ["b1", "b3"]}
{"type": "pass"}
{"type": "mulligan"}
{"type": "keep",       "bottom": ["Mountain", "Wastes"]}
```

- **Legal-action enumeration is factored, not combinatorial:** attach options
  list source→target pairs; combat returns the *eligible attacker set*, never
  2^C subsets. Attacker choice is mechanically live even in goldfish — tapped
  attackers can't fund post-combat `{T}` activations.
- **Fast-forward:** `goldfish_step(game_id, until: "phase:combat" | "turn:6" |
  "end")` runs the policy to the boundary and returns the aggregated log —
  "get me to turn 6, then I pilot."
- **Interactive mulligans:** `goldfish_start(..., interactive_mulligan: true)`
  returns state in `phase: "mulligan"` with legal actions `mulligan`/`keep`
  (bottom count enforced per London + EDH free-first). Default remains the
  automated batch keep rule.
- **Log completeness:** the log records every event — draws, trigger fires with
  their effects, mana payments, phase transitions — not just casts. (Golden-log
  byte-identity requires this anyway; narration depends on it.)
- **Rewind:** any earlier echoed state blob restarts via
  `goldfish_start(resume_state=...)` and replays the same future draws (D3) —
  counterfactuals hold the deck order constant. A `goldfish_undo` sugar tool is
  v2.

## Statistics (batch + A/B)

- Every proportion metric reports a 95% Wilson CI; medians report IQR.
- **Censored games are handled explicitly:** turn-to-event metrics (kill turn,
  21-commander-damage, combo assembly) are reported as %-reached-by-turn-T
  (McNemar-comparable, defined for every game) plus median turn *among games
  that reached the event* with the reach-% alongside. No imputation.
- `goldfish_ab` runs both lists under identical per-game seeds with
  **position-stable deck alignment**: shared cards occupy identical deck-array
  indices before shuffling, so a k-card swap perturbs exactly k positions and
  per-pair correlation survives. The achieved per-pair correlation is reported;
  per-metric output is mean paired delta ± 1.96·sd/√n plus a significance flag
  (exact McNemar for binary metrics — stdlib per D5).
- **Scope banner (goal 7):** `goldfish_ab` output opens with the honesty
  accounting: "Changed: 15 cards — 10 simulated, 5 out of scope (2 removal,
  1 protection, 1 flash-enabler [undervalued], 1 other). Deck A: 28/99 out of
  scope; Deck B: 23/99. Deltas measure speed and consistency only; the sim
  cannot value interaction, and converting out-of-scope cards to simulable
  ones inflates deltas — do not read results as 'cut your removal'."
  `goldfish_ab` includes the full honesty report, same as `goldfish_run`.
- Standing caveat: shared-policy bias cancels for most swaps but **not** for
  swaps whose value is sequencing (e.g. Sigarda's Aid-class cards) — those are
  systematically undervalued.
- `goldfish_ab` refuses decks with different commanders unless explicitly
  overridden.

## MCP tools

| Tool | Input | Output |
|---|---|---|
| `goldfish_annotate` | deck (Archidekt URL or decklist text) | auto-derived annotations, out-of-scope classification (D9), and oracle text **only for plausibly simulable gaps** — cuts a ~60-card annotation pass to ~15–20 |
| `goldfish_run` | deck, annotations?, combos?, n=1000, seed=42, until_turn=10, opponents=1, mulligan opts | text summary + metrics JSON (with CIs) + honesty report |
| `goldfish_ab` | deck_a, deck_b, shared/per-deck annotations, combos?, n, seed | scope banner, paired-seed per-metric deltas ± CI, significance flags, achieved pairing correlation, honesty report |
| `goldfish_start` | deck, annotations?, seed, interactive_mulligan?, resume_state? | game_id + full state + legal actions |
| `goldfish_step` | game_id, action? \| until? | new state + applied action (+ `reason` if policy-chosen) + legal actions + log lines |
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
  21-commander-damage and kill (40) as %-reached-by-T + median-among-reached;
  **table-lethal turn** (first turn total attacking power ≥ combined remaining
  life; sequential-kill turn under focus-fire).
- Board state per turn: creature count (width), total power (post-anthem),
  tokens created (aggregate and by source).
- Casts per turn: distribution, max single-turn chain, % of games with an
  N+-spell turn by T6.
- On-curve health: % of games with all land drops made each turn through T5;
  avg mana available per turn.
- Combo (when declared): assembled and assembled+castable as %-by-turn
  distributions.
- **Generic trigger-fire table:** avg fires per game for every annotated
  trigger, per card. Subsumes bespoke metrics with no new code per deck.
- **Static/condition activation:** first turn each static or condition
  (metalcraft, equip_free) becomes active — "% of games online by T4".
- Honesty report, split into: **out-of-scope** cards by class (D9) with
  drawn-%, **unrecognized** cards (candidates for annotation), and **modeled,
  low-impact** cards (cast/fired but bottom of the trigger table) — only the
  last group supports "the sim agrees this card underperforms." Standing notes
  for known approximations (greedy chain undercount, joint combo payment,
  Karoo bounce, fetch color logic, tribal count stand-ins, per-turn mana pool).

## Interactive state schema

```json
{
  "turn": 4, "phase": "main1",
  "mana_pool": {"W": 2, "R": 1, "C": 2, "any": 0},
  "land_drops_remaining": 1, "spells_cast_this_turn": 2,
  "zones": {"library": 84, "hand": ["..."],
            "battlefield": [{"id": "b3", "name": "...", "attached": ["b5"],
                             "tapped": false, "arrived_turn": 3}],
            "graveyard": [], "command": []},
  "commander": {"cast_count": 1, "damage_dealt": {"opp1": 9}},
  "opponents": [{"life": 31, "cmdr_dmg": 9}],
  "rng_state": [...],
  "log": ["T3: cast Puresteel Paladin", "T3: drew Colossus Hammer",
          "T3: Puresteel Paladin trigger — equip free"],
  "resume": {"deck": "...", "annotations": [...], "combos": [...]}
}
```

`rng_state` is `random.Random.getstate()` serialized, so a resumed game continues
its exact shuffle/draw sequence.

## Error handling

- Deck fetch/parse errors reuse the server's existing error-helper style.
- Annotation validation errors name the offending card, field, and the allowed
  registry values.
- Ambiguous name references in interactive actions are rejected with the
  candidate instance IDs.
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
- [ ] Policy: a `{T}` token producer the policy activates is never also an
      attacker that turn; a combo piece on the battlefield still counts as
      assembled (unit tests).
- [ ] `goldfish_ab` with identical decks reports ~zero deltas; a 1-card-swap
      A/B reports per-pair correlation above a stated floor (pairing actually
      pairs).
- [ ] Every proportion metric in `goldfish_run` output carries a CI; every
      `goldfish_ab` output opens with the scope banner.
- [ ] Interactive: illegal actions rejected with reason; state unmutated on
      rejection; a game resumed from a state blob replays identically;
      ambiguous name references rejected with candidate IDs.
- [ ] Unmodeled cards reported by name, class, and drawn-% in every run.
- [ ] Annotation validation rejects unknown verbs/events/conditions with the
      allowed list in the message.
- [ ] Concurrency: while a batch run is in flight, an unrelated tool call on
      another session answers without waiting for the run to finish.
- [ ] A game evicted from the in-memory store is fully restorable from its
      echoed state blob.

## Phasing

- **v1:** everything above.
- **v2:** death/sacrifice semantics (`sac_token`-style activation costs, dies
  triggers — unlocks Skullclamp/aristocrats), life-payment activation costs
  (Aetherflux), simple blocker model where a chump block **kills an attacker**
  (so width-resilience is visible), removal-pressure events, `copy_spell` verb,
  X-cost casting (X = remaining pool — unlocks Limit Break itself), Saga
  support, graveyard land recursion, subtype count symbols (`per_goblin`),
  per-card leave-one-out contribution, BFS minimum-kill-turn solver, keep-rule
  extensions (mull toward tutors/combo pieces), protection-overlap metric,
  `goldfish_undo` sugar tool, `deck: "precon:<name>"` input sugar, cached
  precon annotation packs (the natural exception to no-server-store), PyPI
  CLI / Agent Skill packaging of the stdlib-only engine for sandbox-side
  execution, persistent annotation store if re-sending proves painful.
- **v3:** Claude-in-the-loop policy via `goldfish_step` as a first-class batch
  mode (premised on the D10 action schema); policy comparisons (greedy vs
  Claude decisions).
