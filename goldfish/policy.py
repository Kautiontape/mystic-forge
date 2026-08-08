"""Greedy policy (spec §Policy): one legal action + a reason naming its rule.

`choose_action(g)` returns a single `(action, reason)` pair; the caller drives
the multi-pass loop by re-invoking it after every `step()` until it returns
`pass` (spec: the rule list is re-evaluated from the top after each action).

Legality contract: candidates are drawn from `engine.legal_actions(g)`
wherever the engine offers them (casts, land plays, activations), so the
engine's own castability/payability predicates guarantee the returned action
is legal at return time. The policy only narrows those candidates by rule
priority and adds its own payability check where legal_actions deliberately
doesn't filter (attach pairs). The one action built outside legal_actions is
the combat attack: the policy ALWAYS issues an attack while the combat's
attack is unconsumed — possibly with an empty attacker list, which the engine
pins as legal — so combat_begin listeners fire deterministically once per
combat instance.

Determinism: no randomness anywhere; every candidate list is either consumed
in legal_actions' pinned deterministic order or re-sorted with stable keys.
"""
from __future__ import annotations

from .cards import GLOBAL_EVENTS, Activated, Cost, SimCard
from .engine import (
    _KEEP_FLOOR,
    Game,
    MulliganRules,
    Permanent,
    _ability_payable,
    _activated_abilities,
    _attacker_eligible,
    _cast_cost,
    _count_sources,
    _equip_free_active,
    _is_creature_perm,
    _target_hand_size,
    bottom_order,
    can_pay,
    effective_power,
    keep_decision,
    legal_actions,
    parse_cost,
    pay,
    untapped_producers,
)

# Rule 8 termination guard: a mana-producing activation that doesn't tap its
# source can be net-neutral or net-positive ("{1}: create a Treasure" pays for
# its own next activation), so the multi-pass loop would re-fire it forever.
# Tap-costed versions self-limit (one activation per untap); non-tap versions
# are skipped entirely.
_MANA_VERBS = frozenset({"add_mana", "treasure", "ramp_mana"})
_TOKEN_VERBS = frozenset({"create_token", "token_copy"})


def choose_action(g: Game) -> tuple[dict, str]:
    """Greedy policy per spec §Policy. Returns (action, reason). The action is
    always legal at call time. Falls through to {"type": "pass"}."""
    if g.won_turn is not None:
        return {"type": "pass"}, "game won: pass"
    if g.phase == "mulligan":
        return _mulligan_action(g)
    if g.phase == "combat":
        return _combat_action(g)
    if g.phase in ("main1", "main2"):
        return _main_action(g)
    return {"type": "pass"}, "no rule fired"     # "end" is transient; be safe


# -- mulligan phase -----------------------------------------------------------

def _mulligan_action(g: Game) -> tuple[dict, str]:
    """Apply the engine's batch keep rule (default MulliganRules) as actions:
    keep (with the pinned bottom order) when the rule keeps or the forced-keep
    floor is reached, else mulligan."""
    rules = MulliganRules()
    hand_size = _target_hand_size(g, rules)
    sources, _lands = _count_sources(g.hand, g.cards, rules)
    if keep_decision(g.hand, g.cards, rules, hand_size) or hand_size <= _KEEP_FLOOR:
        bottoms = bottom_order(g.hand, g.cards, rules,
                               max(0, len(g.hand) - hand_size))
        return ({"type": "keep", "bottom": bottoms},
                f"mulligan: keep {hand_size} ({sources} sources)")
    return {"type": "mulligan"}, f"mulligan: {sources} sources"


# -- combat phase (rule 9) ------------------------------------------------------

def _withheld(g: Game, p: Permanent) -> bool:
    """Rule 9 withhold set: a creature with a {T} activated ability the policy
    can pay for is kept back to activate in main2 (a tapped Krenko makes no
    tokens; the activation is valued over the attack)."""
    return any(a.tap and _ability_payable(g, p, a)
               for a in _activated_abilities(g.card(p.name)))


def _combat_action(g: Game) -> tuple[dict, str]:
    if g.attacked_this_combat:
        return {"type": "pass"}, "combat done: pass"
    eligible = sorted((p for p in g.battlefield if _attacker_eligible(g, p)),
                      key=lambda p: int(p.id[1:]))
    attackers = [p.id for p in eligible if not _withheld(g, p)]
    held = len(eligible) - len(attackers)
    if not eligible:
        reason = "rule 9: attack (no eligible attackers)"
    elif held:
        reason = (f"rule 9: attack with {len(attackers)} "
                  f"(withheld {held} tap producer(s))")
    else:
        reason = f"rule 9: attack with {len(attackers)}"
    # Always issue the attack while unconsumed — empty is legal and fires
    # combat_begin deterministically for this combat instance (engine pin).
    return {"type": "attack", "attackers": attackers}, reason


# -- main phases (rules 1-8) ------------------------------------------------------

def _main_action(g: Game) -> tuple[dict, str]:
    legal = legal_actions(g)
    land_names = [a["card"] for a in legal if a["type"] == "play_land"]
    cast_names = [a["card"] for a in legal if a["type"] == "cast"]

    # Rule 1: play land(s) while drops remain.
    if land_names:
        name, enables = _pick_land(g, land_names)
        reason = ("rule 1: play untapped land enabling a cast" if enables
                  else "rule 1: play land")
        return {"type": "play_land", "card": name}, reason

    # Combo-piece hold: never cast an instant/sorcery piece of a declared
    # combo as a normal spell — in v1's zone model it dies to the graveyard
    # and the combo is permanently unassemblable. Permanent pieces are fine
    # (deploying reduces the joint cost). Silent skip: no reason emitted.
    castable = [n for n in cast_names if not _held_combo_piece(g, n)]
    others = [n for n in castable if n != g.commander_name]

    # Rule 2: cast static enablers / engine permanents (not the commander).
    for name in others:                          # legal_actions: (mv, name) order
        card = g.card(name)
        if card.data.types & {"instant", "sorcery"}:
            continue                             # statics/listeners need a body
        if card.statics() or _global_triggers(card):
            return ({"type": "cast", "card": name},
                    f"rule 2: cast engine permanent {name}")

    # Rule 3: cast mana rocks, then land-ramp spells.
    for name in others:
        card = g.card(name)
        if not card.is_land and card.data.produces:
            return {"type": "cast", "card": name}, f"rule 3: cast mana rock {name}"
    for name in others:
        if any(t.do == "ramp_land" for t in _all_triggers(g.card(name))):
            return {"type": "cast", "card": name}, f"rule 3: cast ramp spell {name}"

    # Rule 4: equipment-before-commander guard.
    commander_castable = g.commander_name in castable
    if commander_castable:
        for name in others:
            if g.card(name).is_equipment and _commander_payable_after(g, name):
                return ({"type": "cast", "card": name},
                        ("rule 4: equipment before commander so the ETB "
                         "attach sees it"))

    # Rule 5: cast the commander at affordable cost (with tax — already
    # priced into legal_actions' castability).
    if commander_castable:
        return ({"type": "cast", "card": g.commander_name},
                "rule 5: cast commander")

    # Rule 6: remaining spells cheapest-first; rituals early only when the
    # hand's spell cost exceeds the mana available this turn, else last.
    if others:
        rituals = [n for n in others if _is_ritual(g.card(n))]
        normal = [n for n in others if n not in rituals]
        hand_cost = sum(
            _cast_cost(g, g.card(n), tax=0).mv for n in g.hand
            if not g.card(n).is_land and not g.card(n).inert_reason
            and not _is_ritual(g.card(n)))
        if rituals and hand_cost > _available_mana(g):
            return ({"type": "cast", "card": rituals[0]},
                    "rule 6: cast ritual early (hand cost exceeds pool)")
        if normal:
            return ({"type": "cast", "card": normal[0]},
                    "rule 6: cast cheapest spell")
        if rituals:
            return ({"type": "cast", "card": rituals[0]},
                    "rule 6: cast ritual last")

    # Rule 7: attach equipment (free first is moot per-attach — the free
    # static is global — so: any unattached equipment, payable when not free).
    attach = _attach_candidate(g)
    if attach is not None:
        eq, tgt, free = attach
        reason = "rule 7: attach equipment" + (" (free)" if free else "")
        return {"type": "attach", "card": eq.id, "target": tgt.id}, reason

    # Rule 8: activate abilities.
    for act in (a for a in legal if a["type"] == "activate"):
        perm = g.perm(act["card"])
        ability = _activated_abilities(g.card(perm.name))[act["ability"]]
        if _activation_allowed(g, ability):
            return act, f"rule 8: activate {perm.name} ability {act['ability']}"

    return {"type": "pass"}, "no rule fired"


# -- rule helpers -----------------------------------------------------------------

def _global_triggers(card: SimCard) -> list:
    if card.ann is None or card.inert_reason:
        return []
    return [t for t in card.ann.triggers if t.on in GLOBAL_EVENTS]


def _all_triggers(card: SimCard) -> list:
    if card.ann is None or card.inert_reason:
        return []
    return list(card.ann.triggers)


def _is_ritual(card: SimCard) -> bool:
    """Rule 6's `add_mana` rituals: instants/sorceries whose triggers add mana."""
    return (bool(card.data.types & {"instant", "sorcery"})
            and any(t.do == "add_mana" for t in _all_triggers(card)))


def _held_combo_piece(g: Game, name: str) -> bool:
    if not any(name in pieces for pieces in g.combos):
        return False
    return bool(g.card(name).data.types & {"instant", "sorcery"})


def _unaffordable_costs(g: Game) -> list[Cost]:
    """Cast costs (reductions/tax applied) currently NOT payable: the spells a
    rule-1 land choice could enable this turn."""
    costs = []
    for name in sorted(set(g.hand)):
        card = g.card(name)
        if card.is_land or card.inert_reason:
            continue
        cost = _cast_cost(g, card, tax=0)
        if not can_pay(g, cost):
            costs.append(cost)
    cname = g.commander_name
    if cname in g.command and cname not in g.hand:
        card = g.card(cname)
        if not card.is_land and not card.inert_reason:
            cost = _cast_cost(g, card, tax=2 * g.commander_casts)
            if not can_pay(g, cost):
                costs.append(cost)
    return costs


def _enables_cast(g: Game, land_name: str, costs: list[Cost]) -> bool:
    """Would playing `land_name` untapped make any currently-unaffordable cost
    payable this turn? Probes with a scratch Permanent appended to the
    battlefield and removed in `finally` — can_pay never mutates, `_next_id`
    is only read (the probe id can't collide), and the append/remove pair
    leaves the game byte-identical."""
    probe = Permanent(id=f"b{g._next_id}", name=land_name, arrived_turn=g.turn)
    g.battlefield.append(probe)
    try:
        return any(can_pay(g, c) for c in costs)
    finally:
        g.battlefield.remove(probe)


def _pick_land(g: Game, land_names: list) -> tuple[str, bool]:
    """Rule 1 choice among name-sorted playable lands: any land enabling a
    currently unaffordable cast this turn (enters-tapped lands can't — their
    mana isn't available until next turn), else the first untapped, else the
    first."""
    unaffordable = _unaffordable_costs(g)
    if unaffordable:
        for name in land_names:
            if g.card(name).data.enters_tapped:
                continue
            if _enables_cast(g, name, unaffordable):
                return name, True
    for name in land_names:
        if not g.card(name).data.enters_tapped:
            return name, False
    return land_names[0], False


def _commander_payable_after(g: Game, eq_name: str) -> bool:
    """Rule 4 guard: is the commander still payable after buying `eq_name`?
    Probed on a to_dict/from_dict scratch copy — heavyweight but exactly the
    engine's own payment semantics, and only hit when the commander and an
    equipment are simultaneously affordable. Deliberately unmemoized; Task
    14's perf test owns any optimization here."""
    copy = Game.from_dict(g.to_dict(), g.cards)
    pay(copy, _cast_cost(copy, copy.card(eq_name), tax=0))
    cname = copy.commander_name
    from_command = cname in copy.command and cname not in copy.hand
    tax = 2 * copy.commander_casts if from_command else 0
    return can_pay(copy, _cast_cost(copy, copy.card(cname), tax))


def _attach_candidate(g: Game) -> tuple[Permanent, Permanent, bool] | None:
    """Rule 7: first unattached battlefield equipment (id order) the policy
    can attach right now, targeting the commander's creature if fielded, else
    the highest-effective-power creature (first maximum — deterministic)."""
    target = None
    for p in g.battlefield:
        if (p.name == g.commander_name and not p.is_token
                and _is_creature_perm(g, p)):
            target = p
            break
    if target is None:
        creatures = [p for p in g.battlefield if _is_creature_perm(g, p)]
        if not creatures:
            return None
        target = max(creatures, key=lambda p: effective_power(g, p))
    free = _equip_free_active(g)
    equipment = sorted(
        (p for p in g.battlefield
         if g.card(p.name).is_equipment and p.attached_to is None
         and p.id != target.id),
        key=lambda p: int(p.id[1:]))
    for eq in equipment:
        cost = g.card(eq.name).data.equip_cost or parse_cost(None)
        if free or can_pay(g, cost):
            return eq, target, free
    return None


def _haste_available(g: Game, ability: Activated) -> bool:
    """Could this ability's token output attack immediately? True when a
    battlefield anthem static grants haste, or the tokens are created with
    haste themselves."""
    if "haste" in ability.keywords:
        return True
    return any("haste" in s.keywords
               for p in g.battlefield for s in g.card(p.name).statics()
               if s.kind == "anthem")


def _activation_allowed(g: Game, ability: Activated) -> bool:
    """Rule 8 gating on an already-payable ability (legal_actions filtered):
    - token producers: post-combat (main2) by default; pre-combat only when
      their output could attack immediately (haste available);
    - extra_combat: main1 only, and only when an eligible attacker exists
      (reaching rule 8 means rule 6's casts are exhausted, so remaining mana
      is genuinely spare);
    - mana-producing verbs: tap-costed only (termination guard, see module
      constant);
    - everything else: whenever payable, except free non-tap abilities, which
      the multi-pass loop would re-fire forever."""
    if ability.do in _TOKEN_VERBS:
        return g.phase == "main2" or _haste_available(g, ability)
    if ability.do == "extra_combat":
        return (g.phase == "main1"
                and any(_attacker_eligible(g, p) for p in g.battlefield))
    if ability.do in _MANA_VERBS:
        return ability.tap
    return ability.tap or ability.mana.mv > 0


def _available_mana(g: Game) -> int:
    """Total mana reachable this turn: untapped producers plus the pool."""
    return (sum(qty for _, _, qty in untapped_producers(g))
            + sum(g.mana_pool.values()))
