"""Pure deterministic goldfish game core. No network, no clock."""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

from .cards import COLORS, DAMAGE_TARGETS, VERBS, CardData, Cost, SimCard, parse_cost


def derive_seed(master_seed: int, game_index: int) -> int:
    h = hashlib.sha256(f"{master_seed}:{game_index}".encode()).digest()
    return int.from_bytes(h[:8], "big")


class IllegalAction(ValueError):
    """Raised by step(); the game is guaranteed unmutated."""


@dataclass
class Permanent:
    id: str
    name: str
    tapped: bool = False
    arrived_turn: int = 0
    attached: list = field(default_factory=list)      # equipment perm ids on me
    attached_to: str | None = None                    # perm id I'm attached to
    is_token: bool = False
    token_power: int | None = None
    token_toughness: int | None = None
    token_keywords: tuple = ()
    pump_eot: list = field(default_factory=lambda: [0, 0])
    pump_perm: list = field(default_factory=lambda: [0, 0])

    def to_dict(self):
        return {"id": self.id, "name": self.name, "tapped": self.tapped,
                "arrived_turn": self.arrived_turn, "attached": list(self.attached),
                "attached_to": self.attached_to, "is_token": self.is_token,
                "token_power": self.token_power, "token_toughness": self.token_toughness,
                "token_keywords": list(self.token_keywords),
                "pump_eot": list(self.pump_eot), "pump_perm": list(self.pump_perm)}

    @classmethod
    def from_dict(cls, d):
        p = cls(id=d["id"], name=d["name"], tapped=d["tapped"],
                arrived_turn=d["arrived_turn"], attached=list(d["attached"]),
                attached_to=d["attached_to"], is_token=d["is_token"],
                token_power=d["token_power"], token_toughness=d["token_toughness"],
                token_keywords=tuple(d["token_keywords"]))
        p.pump_eot, p.pump_perm = list(d["pump_eot"]), list(d["pump_perm"])
        return p


PHASES = ("mulligan", "main1", "combat", "main2", "end")


@dataclass
class Game:
    cards: dict                       # name -> SimCard (not serialized; rebuilt by caller)
    library: list                     # card names, index 0 = top
    hand: list
    battlefield: list                 # [Permanent]
    graveyard: list
    command: list
    commander_name: str
    opponents: list                   # life totals
    cmdr_damage: list
    turn: int = 0
    phase: str = "mulligan"
    commander_casts: int = 0
    mana_pool: dict = field(default_factory=lambda: {c: 0 for c in COLORS} | {"C": 0, "any": 0})
    _land_drops_used: int = 0         # serialized as "land_drops_used";
                                      # land_drops_remaining is a computed property
    spells_cast_this_turn: int = 0
    combats_done: int = 0
    extra_combats: int = 0
    mulligans_taken: int = 0
    free_mulligan_used: bool = False
    won_turn: int | None = None
    life_gained: int = 0              # opponents never attack in goldfish games, so
                                      # gained life has no total to raise; it
                                      # accumulates here for metrics
    trigger_fires: dict = field(default_factory=dict)  # "card|event|verb" -> fire
                                      # count (string keys so it JSON-serializes)
    static_active_turn: dict = field(default_factory=dict)  # "card|static_kind" or
                                      # "condition|metalcraft" -> first turn active
    combos: list = field(default_factory=list)          # [[names], ...]
    combo_wins: list = field(default_factory=list)      # [bool per combo]
    combo_assembled_turn: list = field(default_factory=list)   # [int|None per combo]
    combo_castable_turn: list = field(default_factory=list)
    rng: random.Random = field(default_factory=random.Random)
    log: list = field(default_factory=list)
    _next_id: int = 1
    # Not serialized: trigger dispatch fully unwinds within one action, so
    # none of these cross a step boundary.
    _fire_depth: int = 0
    _cascade_fires: int = 0           # total fires this cascade (fan-out budget)
    _depth_warned: bool = False       # one suppression line per cascade

    # -- identity helpers -------------------------------------------------
    @property
    def land_drops_remaining(self) -> int:
        """Dynamic (spec §Engine): allowed drops are recomputed from the
        battlefield on every read, so an extra_land_drops static arriving
        mid-turn raises the remaining count immediately (Azusa grants drops
        the turn she lands)."""
        return _land_drops_allowed(self) - self._land_drops_used

    def card(self, name: str) -> SimCard:
        try:
            return self.cards[name]
        except KeyError:
            raise IllegalAction(f"unknown card {name!r}")

    def perm(self, pid: str) -> Permanent:
        for p in self.battlefield:
            if p.id == pid:
                return p
        raise IllegalAction(f"no permanent with id {pid!r}")

    def new_perm(self, name: str, **kw) -> Permanent:
        p = Permanent(id=f"b{self._next_id}", name=name,
                      arrived_turn=self.turn, **kw)
        self._next_id += 1
        self.battlefield.append(p)
        return p

    def emit(self, line: str):
        self.log.append(f"T{self.turn}: {line}")

    # -- serialization (D3) ------------------------------------------------
    def to_dict(self) -> dict:
        """"land_drops_remaining" is display-only; from_dict reads only
        "land_drops_used"."""
        return {
            "turn": self.turn, "phase": self.phase,
            "mana_pool": dict(self.mana_pool),
            "land_drops_remaining": self.land_drops_remaining,  # computed, display only
            "land_drops_used": self._land_drops_used,           # state; from_dict reads this
            "spells_cast_this_turn": self.spells_cast_this_turn,
            "combats_done": self.combats_done, "extra_combats": self.extra_combats,
            "mulligans_taken": self.mulligans_taken,
            "free_mulligan_used": self.free_mulligan_used,
            "life_gained": self.life_gained,
            "trigger_fires": dict(self.trigger_fires),
            "static_active_turn": dict(self.static_active_turn),
            "zones": {"library": list(self.library), "hand": list(self.hand),
                      "battlefield": [p.to_dict() for p in self.battlefield],
                      "graveyard": list(self.graveyard), "command": list(self.command)},
            "commander": {"name": self.commander_name, "cast_count": self.commander_casts},
            "opponents": [{"life": l, "cmdr_dmg": d}
                          for l, d in zip(self.opponents, self.cmdr_damage)],
            "won_turn": self.won_turn,
            "combos": [list(c) for c in self.combos],
            "combo_wins": list(self.combo_wins),
            "combo_assembled_turn": list(self.combo_assembled_turn),
            "combo_castable_turn": list(self.combo_castable_turn),
            "rng_state": _rng_state_to_json(self.rng.getstate()),
            "log": list(self.log), "next_id": self._next_id,
        }

    @classmethod
    def from_dict(cls, d: dict, cards: dict) -> Game:
        g = cls(cards=cards,
                library=list(d["zones"]["library"]), hand=list(d["zones"]["hand"]),
                battlefield=[Permanent.from_dict(p) for p in d["zones"]["battlefield"]],
                graveyard=list(d["zones"]["graveyard"]),
                command=list(d["zones"]["command"]),
                commander_name=d["commander"]["name"],
                opponents=[o["life"] for o in d["opponents"]],
                cmdr_damage=[o["cmdr_dmg"] for o in d["opponents"]],
                turn=d["turn"], phase=d["phase"],
                commander_casts=d["commander"]["cast_count"],
                mana_pool=dict(d["mana_pool"]),
                _land_drops_used=d["land_drops_used"],
                spells_cast_this_turn=d["spells_cast_this_turn"],
                combats_done=d["combats_done"], extra_combats=d["extra_combats"],
                mulligans_taken=d["mulligans_taken"],
                free_mulligan_used=d["free_mulligan_used"],
                life_gained=d["life_gained"],
                trigger_fires=dict(d["trigger_fires"]),
                static_active_turn=dict(d["static_active_turn"]),
                won_turn=d["won_turn"], combos=[list(c) for c in d["combos"]],
                combo_wins=list(d["combo_wins"]),
                combo_assembled_turn=list(d["combo_assembled_turn"]),
                combo_castable_turn=list(d["combo_castable_turn"]))
        g.log = list(d["log"])
        g._next_id = d["next_id"]
        g.rng.setstate(_rng_state_from_json(d["rng_state"]))
        return g


def _rng_state_to_json(state):
    version, internal, gauss = state
    return [version, list(internal), gauss]


def _rng_state_from_json(js):
    version, internal, gauss = js
    return (version, tuple(internal), gauss)


def _synth_card(name: str, types: frozenset, produces: dict | None) -> SimCard:
    return SimCard(data=CardData(name=name, cost=None, types=types, power=None,
                                 toughness=None, keywords=frozenset(),
                                 produces=produces, enters_tapped=False,
                                 equip_cost=None, oracle=""),
                   ann=None, scope_class=None)


# Engine-created permanents the card pool must always resolve (g.card() would
# KeyError otherwise). "Treasure" is a reusable-rock approximation: v1 never
# sacrifices it, so it taps for any one color every turn — flagged in the
# honesty report. "Token" is the typeless placeholder for create_token output;
# its stats live on the Permanent (token_power/token_toughness). "Rock" is the
# ramp_mana output: a generic colorless mana rock (Powerstone-like artifact).
_SYNTHETIC_CARDS = (
    ("Treasure", frozenset({"artifact"}), {c: 1 for c in COLORS}),
    ("Token", frozenset(), None),
    ("Rock", frozenset({"artifact"}), {"C": 1}),
)


def new_game(cards: dict, deck: list, commander: str, seed: int,
             opponents: int = 1, combos: list | None = None) -> Game:
    for name, types, produces in _SYNTHETIC_CARDS:
        cards.setdefault(name, _synth_card(name, types, produces))
    rng = random.Random(seed)
    library = list(deck)
    rng.shuffle(library)
    hand, library = library[:7], library[7:]
    g = Game(cards=cards, library=library, hand=hand, battlefield=[],
             graveyard=[], command=[commander], commander_name=commander,
             opponents=[40] * opponents, cmdr_damage=[0] * opponents,
             combos=list(combos or []))
    g.combo_wins = [bool(c.get("wins")) if isinstance(c, dict) else False
                    for c in g.combos]
    g.combos = [list(c["cards"]) if isinstance(c, dict) else list(c) for c in g.combos]
    g.combo_assembled_turn = [None] * len(g.combos)
    g.combo_castable_turn = [None] * len(g.combos)
    g.rng = rng
    return g


# -- Mana payment solver (D5-ish; see spec Engine (Mana)) -------------------

def untapped_producers(g: Game):
    """[(perm, colors: frozenset|{'C'}, qty)] for untapped mana permanents,
    deterministic order by (fewest colors, perm id) — strictest first. No
    sickness check: v1 producers are rocks/lands only (creature producers
    would need an arrived_turn guard)."""
    out = []
    for p in g.battlefield:
        if p.tapped:
            continue
        card = g.card(p.name)
        if card.data.produces:
            colors = frozenset(card.data.produces.keys())
            qty = max(card.data.produces.values())
            out.append((p, colors, qty))
    out.sort(key=lambda t: (len(t[1]), int(t[0].id[1:])))   # strictest first
    return out


def _payment_plan(g: Game, cost: Cost):
    """Greedy, most-constrained-first: colored pips are paid one at a time,
    always choosing the still-needed color with the fewest remaining
    candidate sources (untapped matching producers + matching pool, plus the
    pool wildcard for WUBRG but never for {C}) — recomputed after every pip so
    a dual land isn't claimed by a color that had other options (MRV). Within
    a color the existing strictest-producer (fewest colors, then id) choice
    is kept via `avail`'s pre-sort. Generic is paid from the rest (largest
    quantity first); any producer overshoot — colored or generic — is banked
    into the pool using the same routing rule (multicolor producer -> "any",
    monocolor -> its one color) so it can fund a later payment this turn.
    Returns ([perm ids], pool) or None."""
    producers = untapped_producers(g)
    used, avail = [], list(producers)
    pool = dict(g.mana_pool)
    need = dict(cost.pips)

    def candidates(color):
        n = sum(1 for t in avail if color in t[1]) + pool.get(color, 0)
        if color != "C":                       # "any" can't pay a {C} pip
            n += pool.get("any", 0)
        return n

    remaining = {c: need[c] for c in ("W", "U", "B", "R", "G", "C") if need.get(c)}
    while any(remaining.values()):
        color = min((c for c in remaining if remaining[c]),
                    key=lambda c: (candidates(c), -remaining[c], "WUBRGC".index(c)))
        remaining[color] -= 1
        if pool.get(color, 0) > 0:
            pool[color] -= 1
            continue
        hit = next((t for t in avail if color in t[1]), None)
        if hit:
            avail.remove(hit)
            used.append(hit)
            surplus = hit[2] - 1
            if surplus:
                k = "any" if len(hit[1]) > 1 else next(iter(hit[1]))
                pool[k] = pool.get(k, 0) + surplus
        elif color != "C" and pool.get("any", 0) > 0:
            pool["any"] -= 1
        else:
            return None

    generic = need.get("generic", 0)
    avail.sort(key=lambda t: (-t[2], len(t[1])))    # big colorless rocks first
    for t in list(avail):
        if generic <= 0:
            break
        avail.remove(t); used.append(t)
        take = min(generic, t[2])
        generic -= take
        surplus = t[2] - take
        if surplus:
            k = "any" if len(t[1]) > 1 else next(iter(t[1]))
            pool[k] = pool.get(k, 0) + surplus
    while generic > 0:
        for k in ("C", "W", "U", "B", "R", "G", "any"):
            if pool.get(k, 0) > 0:
                pool[k] -= 1; generic -= 1
                break
        else:
            return None
    return [t[0].id for t in used], pool


def can_pay(g: Game, cost: Cost) -> bool:
    return _payment_plan(g, cost) is not None


def pay(g: Game, cost: Cost):
    plan = _payment_plan(g, cost)
    if plan is None:
        raise IllegalAction(f"cannot pay {cost.pips}")
    ids, pool = plan
    for pid in ids:
        g.perm(pid).tapped = True
    g.mana_pool = {k: pool.get(k, 0) for k in list("WUBRG") + ["C", "any"]}


# -- Effective stats (statics: anthem, equipment grants) --------------------

def _equipment_grants(g: Game, eq: Permanent) -> dict:
    """The equipment's `grants` annotation when present, else {} (auto-derived
    grants from oracle text arrive with the Scryfall classifier, Task 17)."""
    ann = g.card(eq.name).ann
    return (ann.grants or {}) if ann is not None else {}


def _is_creature_perm(g: Game, p: Permanent) -> bool:
    # Not every engine token is a creature (Treasure/Rock are artifacts), but
    # creature tokens always carry token stats — so key off token_power.
    return g.card(p.name).is_creature or (p.is_token and p.token_power is not None)


def _anthem_totals(g: Game) -> tuple:
    """Summed battlefield anthem statics: (power, toughness, keywords).
    Callers gate on _is_creature_perm — anthems buff creatures only."""
    power = toughness = 0
    kws: set = set()
    for src in g.battlefield:
        for s in g.card(src.name).statics():
            if s.kind == "anthem":
                power += s.power
                toughness += s.toughness
                kws |= set(s.keywords)
    return power, toughness, kws


def effective_power(g: Game, p: Permanent) -> int:
    card = g.card(p.name)
    base = (p.token_power or 0) if p.is_token else (card.data.power or 0)
    base += p.pump_perm[0] + p.pump_eot[0]
    for eq_id in p.attached:
        base += _equipment_grants(g, g.perm(eq_id)).get("power", 0)
    if _is_creature_perm(g, p):
        base += _anthem_totals(g)[0]
    return base


def effective_keywords(g: Game, p: Permanent) -> set:
    card = g.card(p.name)
    kws = set(p.token_keywords if p.is_token else card.data.keywords)
    for eq_id in p.attached:
        kws |= set(_equipment_grants(g, g.perm(eq_id)).get("keywords", ()))
    if _is_creature_perm(g, p):       # same gate as effective_power: anthem
        kws |= _anthem_totals(g)[2]   # keywords must not leak onto Treasures
    return kws                        # or equipment


# -- Counts and conditions --------------------------------------------------

def resolve_count(g: Game, count, ctx: dict) -> int:
    if isinstance(count, int):
        return count
    if count == "per_artifact":
        return sum(1 for p in g.battlefield if g.card(p.name).is_artifact)
    if count == "per_creature":
        return sum(1 for p in g.battlefield if _is_creature_perm(g, p))
    if count == "per_land":
        return sum(1 for p in g.battlefield if g.card(p.name).is_land)
    if count == "per_spell_cast_this_turn":
        return g.spells_cast_this_turn
    if count == "per_attacker":
        return len(ctx.get("attackers", ()))
    if count == "per_equipped_attacker":
        return sum(1 for pid in ctx.get("attackers", ()) if g.perm(pid).attached)
    raise IllegalAction(f"unknown count {count!r}")


def check_condition(g: Game, cond, source_perm, ctx) -> bool:
    if cond is None:
        return True
    if cond[0] == "power_gte":
        return source_perm is not None and effective_power(g, source_perm) >= cond[1]
    if cond[0] == "metalcraft":
        return sum(1 for p in g.battlefield if g.card(p.name).is_artifact) >= 3
    if cond[0] == "equipped":
        return source_perm is not None and bool(source_perm.attached)
    return False


# -- Verb execution ---------------------------------------------------------

def _focus_target(g: Game) -> int:
    """Focus-fire: first opponent still above 0, else 0."""
    for i, life in enumerate(g.opponents):
        if life > 0:
            return i
    return 0


def _tutor_match(g: Game, f: str, name: str) -> bool:
    if f == "any":
        return True
    if f.startswith("name:"):
        return name == f[5:]
    return f in g.card(name).data.types


def _attach_target(g: Game) -> Permanent | None:
    """Commander's permanent if fielded, else the highest-effective-power
    creature (max keeps the first maximum — deterministic battlefield order)."""
    for p in g.battlefield:
        if p.name == g.commander_name and not p.is_token:
            return p
    creatures = [p for p in g.battlefield if _is_creature_perm(g, p)]
    if not creatures:
        return None
    return max(creatures, key=lambda p: effective_power(g, p))


def _do_attach(g: Game, eq: Permanent, tgt: Permanent):
    eq.attached_to = tgt.id
    tgt.attached.append(eq.id)
    g.emit(f"attached {eq.name} to {tgt.name}")


def execute_verb(g: Game, source, verb: str, ctx: dict | None = None, **params):
    ctx = ctx or {}
    if verb not in VERBS:     # checked before the zero-count return so typo'd
                              # verbs can't silently no-op at count 0
        raise IllegalAction(f"verb {verb!r} not implemented")
    n = resolve_count(g, params.get("count", 1), ctx)
    if n <= 0:
        return    # zero-count convention: no mutation AND no log line for the
                  # verb (fire() still records the trigger_fires entry);
                  # negative counts are rejected at annotation level and
                  # no-op defensively here
    if verb == "draw":
        for _ in range(n):
            if g.library:
                card = g.library.pop(0)
                g.hand.append(card)
                g.emit(f"drew {card}")
    elif verb == "damage":
        target = params.get("target")
        if target not in DAMAGE_TARGETS:
            raise IllegalAction(
                f"damage requires target in {sorted(DAMAGE_TARGETS)}, got {target!r}")
        if target == "each_opponent":
            g.opponents = [life - n for life in g.opponents]
        else:
            i = _focus_target(g)
            g.opponents[i] -= n
        g.emit(f"{n} damage ({target})")
    elif verb == "create_token":
        power, toughness = params.get("power"), params.get("toughness")
        if power is None or toughness is None:
            raise IllegalAction("create_token requires power and toughness")
        doublers = sum(1 for p in g.battlefield
                       for s in g.card(p.name).statics() if s.kind == "token_doubling")
        total = n * (2 ** doublers)
        g.emit(f"created {total} token(s)")      # cause precedes the ETB effects
        for _ in range(total):
            tok = g.new_perm(params.get("token_name", "Token"), is_token=True,
                             token_power=power, token_toughness=toughness,
                             token_keywords=tuple(params.get("keywords", ())))
            fire(g, "creature_etb", entering=tok)
    elif verb == "treasure":
        for _ in range(n):
            g.new_perm("Treasure", is_token=True)   # Treasure SimCard added in new_game
        g.emit(f"created {n} treasure(s)")
    elif verb == "gain_life":
        g.life_gained += n
        g.emit(f"gained {n} life")
    elif verb == "add_mana":
        pips_str = params.get("pips")
        if not pips_str and not params.get("any_mana"):
            raise IllegalAction("add_mana requires pips or colors:any + count")
        if pips_str:
            for color, qty in parse_cost(pips_str).pips.items():
                if qty:
                    # generic braces in an add_mana string mean colorless mana
                    key = "C" if color == "generic" else color
                    g.mana_pool[key] = g.mana_pool.get(key, 0) + qty * n
            g.emit(f"added {pips_str} to pool")
        else:
            g.mana_pool["any"] += n
            g.emit(f"added {n} mana of any color to pool")
    elif verb == "ramp_land":
        # Cultivate-class common-case approximation: the first n lands in
        # library order (deterministic) enter TAPPED.
        for _ in range(n):
            idx = next((i for i, name in enumerate(g.library)
                        if g.card(name).is_land), None)
            if idx is None:
                g.emit("ramp found no land")
                break
            name = g.library.pop(idx)
            perm = g.new_perm(name, tapped=True)
            g.emit(f"ramped {name} onto battlefield (tapped)")
            fire(g, "land_etb", entering=perm)
    elif verb == "ramp_mana":
        for _ in range(n):
            g.new_perm("Rock", is_token=True)       # not a creature: no creature_etb
        g.emit(f"created {n} mana rock(s)")
    elif verb == "tutor":
        f = params.get("tutor_filter") or "any"
        for _ in range(n):
            idx = next((i for i, name in enumerate(g.library)
                        if _tutor_match(g, f, name)), None)
            if idx is None:
                g.emit("tutor whiffed")
            else:
                name = g.library.pop(idx)
                g.hand.append(name)
                g.emit(f"tutored {name}")
    elif verb == "pump":
        power, toughness = params.get("power"), params.get("toughness")
        if power is None or toughness is None:
            raise IllegalAction("pump requires power and toughness")
        if source is None:
            g.emit("pump with no source permanent — skipped")
        else:
            duration = params.get("duration", "eot")
            dest = source.pump_eot if duration == "eot" else source.pump_perm
            dest[0] += power * n
            dest[1] += toughness * n
            # log the applied delta, not the cumulative bucket
            g.emit(f"{source.name} pumped {power * n:+d}/{toughness * n:+d} "
                   f"({duration})")
    elif verb == "attach":
        for _ in range(n):
            eq = next((p for p in g.battlefield
                       if g.card(p.name).is_equipment and p.attached_to is None), None)
            tgt = _attach_target(g)
            if eq is None or tgt is None or tgt.id == eq.id:
                g.emit("attach: nothing to attach")
                break
            _do_attach(g, eq, tgt)
    elif verb == "attach_from_board":
        if source is None:
            g.emit("attach_from_board with no source permanent — skipped")
        else:
            eqs = [p for p in g.battlefield
                   if g.card(p.name).is_equipment and p.attached_to is None
                   and p.id != source.id][:n]
            for eq in eqs:
                _do_attach(g, eq, source)
            if not eqs:
                g.emit("attach_from_board: no unattached equipment")
    elif verb == "extra_combat":
        g.extra_combats += n
        g.emit(f"+{n} extra combat(s)")
    elif verb == "token_copy":
        subject = None
        if source is not None:
            s_card = g.card(source.name)
            if s_card.is_equipment and source.attached_to:
                subject = g.perm(source.attached_to)
            else:
                subject = source
        if subject is None:
            g.emit("token_copy with no source permanent — skipped")
        else:
            card = g.card(subject.name)
            # A copy takes printed stats plus permanent pumps; end-of-turn
            # pumps and attached-equipment grants are not copied.
            base_p = ((subject.token_power or 0) if subject.is_token
                      else (card.data.power or 0)) + subject.pump_perm[0]
            base_t = ((subject.token_toughness or 0) if subject.is_token
                      else (card.data.toughness or 0)) + subject.pump_perm[1]
            kws = tuple(subject.token_keywords if subject.is_token
                        else sorted(card.data.keywords))
            for _ in range(n):
                tok = g.new_perm(subject.name, is_token=True, token_power=base_p,
                                 token_toughness=base_t, token_keywords=kws)
                g.emit(f"created token copy of {subject.name}")
                fire(g, "creature_etb", entering=tok)
    else:
        raise IllegalAction(f"verb {verb!r} not implemented")


# -- Event dispatch ---------------------------------------------------------

_MAX_FIRE_DEPTH = 20        # bounds chain depth
_MAX_CASCADE_FIRES = 200    # bounds total fires per cascade (branching fan-out:
                            # two etb->create_token listeners give 2^depth fires,
                            # so a depth cap alone is not enough)


def fire(g: Game, event: str, source_perm=None, entering=None, spell=None, ctx=None):
    """Run all battlefield listeners for a global event. `entering` is skipped:
    a permanent doesn't hear its own arrival as a global event. Self events
    (cast/etb) are executed at cast time, not via fire(). Listeners are
    snapshotted before execution so verb side effects (new tokens) don't feed
    the same dispatch.

    Dispatch is bounded two ways — chain depth AND total fires per cascade:
    a validation-legal annotation loop (e.g. creature_etb -> create_token)
    would otherwise recurse without bound, and two such listeners branch into
    2^depth fires. Past either limit, further triggers are suppressed —
    deterministically, with one log line per cascade — and the game
    continues."""
    if (g._fire_depth >= _MAX_FIRE_DEPTH
            or g._cascade_fires >= _MAX_CASCADE_FIRES):
        if not g._depth_warned:
            g._depth_warned = True
            g.emit("trigger limit reached — further triggers suppressed")
        return
    g._fire_depth += 1
    g._cascade_fires += 1
    try:
        _dispatch(g, event, entering, spell, ctx)
    finally:
        g._fire_depth -= 1
        if g._fire_depth == 0:        # cascade fully unwound: next one starts
            g._depth_warned = False   # fresh with a full budget and may warn
            g._cascade_fires = 0      # again


def _dispatch(g: Game, event: str, entering, spell, ctx):
    listeners = []
    for p in g.battlefield:
        if entering is not None and p.id == entering.id:
            continue          # a permanent doesn't hear its own arrival as a global event
        for t in g.card(p.name).triggers_for(event):
            if (event == "spell_cast" and spell is not None
                    and not _spell_filter_ok(g, t.event_filter, spell)):
                continue
            listeners.append((p, t))
    for p, t in listeners:
        _run_trigger(g, p.name, p, t, ctx)


def _run_trigger(g: Game, name: str, source_perm, t, ctx):
    """Shared per-trigger execution — condition gate, log line, fire count,
    verb — used by global dispatch and by cast-time self (cast/etb) triggers."""
    if not check_condition(g, t.condition, source_perm, ctx or {}):
        return
    g.emit(f"{name} trigger — {t.do}")
    key = f"{name}|{t.on}|{t.do}"
    g.trigger_fires[key] = g.trigger_fires.get(key, 0) + 1
    execute_verb(g, source_perm, t.do, ctx=ctx, count=t.count, target=t.target,
                 power=t.power, toughness=t.toughness,
                 keywords=t.keywords, tutor_filter=t.tutor_filter,
                 pips=t.pips, any_mana=t.any_mana, duration=t.duration)


def _spell_filter_ok(g: Game, f, spell_card: SimCard) -> bool:
    if f in (None, "any"):
        return True
    if f == "instant_or_sorcery":
        return bool(spell_card.data.types & {"instant", "sorcery"})
    if f == "noncreature":
        return not spell_card.is_creature
    return False


# -- Actions: play_land / cast / pass + turn engine --------------------------

def check_combos(g: Game) -> None:
    """Combo detection stub — Task 11 fills it. Called when a main phase ends."""


def _land_drops_allowed(g: Game) -> int:
    extra = sum(s.count for p in g.battlefield
                for s in g.card(p.name).statics() if s.kind == "extra_land_drops")
    return 1 + extra


def _record_activations(g: Game):
    """Static/condition activation metric: record the first turn each
    battlefield static (key "card|kind") and the metalcraft condition
    (key "condition|metalcraft") is seen active. Cheap: setdefault only.
    Runs after every successful step mutation and at each turn start."""
    for p in g.battlefield:
        for s in g.card(p.name).statics():
            g.static_active_turn.setdefault(f"{p.name}|{s.kind}", g.turn)
    if ("condition|metalcraft" not in g.static_active_turn
            and check_condition(g, ("metalcraft",), None, {})):
        g.static_active_turn["condition|metalcraft"] = g.turn


def _cost_reduction_applies(g: Game, f: str, card: SimCard) -> bool:
    """cost_reduction filter vocabulary (spec §Card annotation DSL)."""
    if f == "any":
        return True
    if f == "instant_or_sorcery":
        return bool(card.data.types & {"instant", "sorcery"})
    if f == "noncreature":
        return not card.is_creature
    if f == "creature":
        return card.is_creature
    if f == "artifact":
        return card.is_artifact
    if f == "equipment":
        return card.is_equipment
    if f.startswith("color:"):
        # the card's cost carries that colored pip; generic never counts
        return bool(card.data.cost and card.data.cost.pips.get(f[6:], 0) > 0)
    return False


def _cast_cost(g: Game, card: SimCard, tax: int) -> Cost:
    """Card cost + commander tax on generic, then cost_reduction statics shave
    generic only — after the tax, floored at 0; colored pips are never touched
    (spec §Engine: reductions run before the pip solver)."""
    pips = dict(card.data.cost.pips) if card.data.cost else dict(parse_cost(None).pips)
    reduction = sum(
        s.amount for p in g.battlefield for s in g.card(p.name).statics()
        if s.kind == "cost_reduction" and _cost_reduction_applies(g, s.filter, card))
    pips["generic"] = max(0, pips.get("generic", 0) + tax - reduction)
    return Cost(pips)


def _resolve_perm(g: Game, ref) -> Permanent:
    """Resolve an attach/activate `card`/`target` reference: battlefield
    instance id first, else a unique permanent name (spec §Interactive mode).
    Ambiguous names raise IllegalAction listing every candidate id; a ref
    matching neither an id nor exactly one name raises IllegalAction."""
    if not isinstance(ref, str):
        raise IllegalAction(f"permanent reference must be a string, got {ref!r}")
    for p in g.battlefield:
        if p.id == ref:
            return p
    matches = sorted((p for p in g.battlefield if p.name == ref),
                     key=lambda p: int(p.id[1:]))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(p.id for p in matches)
        raise IllegalAction(f"ambiguous name {ref!r}; candidates: {ids}")
    raise IllegalAction(f"no permanent matching {ref!r}")


def _equip_free_active(g: Game) -> bool:
    """True when a battlefield static grants free equipping: `equip_free`
    unconditionally, or `equip_free_if_metalcraft` while metalcraft (spec
    §Card annotation DSL) holds."""
    for p in g.battlefield:
        for s in g.card(p.name).statics():
            if s.kind == "equip_free":
                return True
            if (s.kind == "equip_free_if_metalcraft"
                    and check_condition(g, ("metalcraft",), None, {})):
                return True
    return False


def _activated_abilities(card: SimCard) -> list:
    """Same inert-gating as triggers_for/statics(): an inert card exposes no
    activated abilities (defensive — annotations are never written for
    out-of-scope cards in practice)."""
    if card.ann is None or card.inert_reason:
        return []
    return card.ann.activated


def _activation_tap_ok(g: Game, perm: Permanent) -> bool:
    """{T} legality independent of the specific ability: untapped AND (not a
    creature OR summoning sickness has passed OR haste) — spec §Engine, D8."""
    if perm.tapped:
        return False
    if _is_creature_perm(g, perm) and perm.arrived_turn >= g.turn:
        return "haste" in effective_keywords(g, perm)
    return True


def _ability_payable(g: Game, perm: Permanent, ability) -> bool:
    """Combined legality for `legal_actions`: {T} part (if any) plus mana."""
    if ability.tap and not _activation_tap_ok(g, perm):
        return False
    return can_pay(g, ability.mana)


def step(g: Game, action: dict) -> None:
    """Apply one atomic action (D10). Validates completely first; on
    IllegalAction the game is guaranteed unmutated."""
    kind = action.get("type") if isinstance(action, dict) else None
    if kind == "play_land":
        _step_play_land(g, action)
    elif kind == "cast":
        _step_cast(g, action)
    elif kind == "attach":
        _step_attach(g, action)
    elif kind == "activate":
        _step_activate(g, action)
    elif kind == "pass":
        _step_pass(g)
    else:
        # attack arrives in Task 9, mulligan/keep in Task 10.
        raise IllegalAction(f"unknown or unsupported action type {kind!r}")
    # A turn-advancing pass already recorded inside _begin_new_turn; this
    # second call is deliberate and idempotent (setdefault) — do not "fix" it.
    _record_activations(g)


def _step_play_land(g: Game, action: dict):
    name = action.get("card")
    if not isinstance(name, str):
        raise IllegalAction(f"action needs a card name, got {name!r}")
    if g.phase not in ("main1", "main2"):
        raise IllegalAction(f"cannot play a land during {g.phase}")
    if name not in g.hand:
        raise IllegalAction(f"{name!r} is not in hand")
    card = g.card(name)
    if not card.is_land:
        raise IllegalAction(f"{name!r} is not a land")
    if g.land_drops_remaining <= 0:
        raise IllegalAction("no land drops remaining this turn")
    # --- all checks passed; mutation begins ---
    g.hand.remove(name)
    perm = g.new_perm(name, tapped=card.data.enters_tapped)
    g._land_drops_used += 1
    g.emit(f"played {name}")                    # cause precedes the ETB effects
    fire(g, "land_etb", entering=perm)


def _step_cast(g: Game, action: dict):
    name = action.get("card")
    if not isinstance(name, str):
        raise IllegalAction(f"action needs a card name, got {name!r}")
    if g.phase not in ("main1", "main2"):
        raise IllegalAction(f"cannot cast during {g.phase}")
    in_hand = name in g.hand
    from_command = (not in_hand and name == g.commander_name
                    and name in g.command)
    if not in_hand and not from_command:
        raise IllegalAction(f"{name!r} is not in hand or the command zone")
    card = g.card(name)
    if card.is_land:
        raise IllegalAction(f"{name!r} is a land — use play_land")
    if card.inert_reason:
        raise IllegalAction(f"{name!r} is not simulable ({card.inert_reason})")
    tax = 2 * g.commander_casts if from_command else 0
    cost = _cast_cost(g, card, tax)
    if not can_pay(g, cost):
        raise IllegalAction(f"cannot pay for {name!r}")
    # --- all checks passed; mutation begins ---
    pay(g, cost)
    if in_hand:
        g.hand.remove(name)
    else:
        # Commander bookkeeping happens with zone departure so the commander
        # never exists in two zones during the trigger cascade (Task 11's
        # check_combos inspects zones mid-cascade).
        g.command.remove(name)
        g.commander_casts += 1
    g.spells_cast_this_turn += 1      # BEFORE the spell's own cast triggers:
                                      # Grapeshot-style counts include the spell
    if from_command and tax > 0:
        g.emit(f"cast {name} (commander, tax {tax})")
    else:
        g.emit(f"cast {name}")        # cause precedes trigger effects in the log
    # Battlefield listeners only: the spell is in no battlefield zone yet, so
    # it never hears its own cast (spec §Card annotation DSL).
    fire(g, "spell_cast", spell=card)
    if card.data.types & {"instant", "sorcery"}:
        for t in card.triggers_for("cast"):
            _run_trigger(g, name, None, t, {})
        g.graveyard.append(name)
    else:
        # Permanents enter untapped unless the card data says enters_tapped
        # (lands are rejected above — they are played, never cast).
        perm = g.new_perm(name, tapped=card.data.enters_tapped)
        for t in card.triggers_for("cast"):
            _run_trigger(g, name, perm, t, {})
        for t in card.triggers_for("etb"):
            _run_trigger(g, name, perm, t, {})
        if card.is_creature:
            fire(g, "creature_etb", entering=perm)
        if card.is_equipment:
            fire(g, "equipment_etb", entering=perm)


def _step_attach(g: Game, action: dict):
    if g.phase not in ("main1", "main2"):
        raise IllegalAction(f"cannot attach during {g.phase}")
    eq = _resolve_perm(g, action.get("card"))
    tgt = _resolve_perm(g, action.get("target"))
    eq_card = g.card(eq.name)
    if not eq_card.is_equipment:
        raise IllegalAction(f"{eq.name!r} is not equipment")
    if not _is_creature_perm(g, tgt):
        raise IllegalAction(f"{tgt.name!r} is not a creature")
    free = _equip_free_active(g)
    cost = parse_cost(None) if free else (eq_card.data.equip_cost or parse_cost(None))
    if not free and not can_pay(g, cost):
        raise IllegalAction(f"cannot pay equip cost for {eq.name!r}")
    # --- all checks passed; mutation begins ---
    if not free:
        pay(g, cost)
    if eq.attached_to is not None:
        g.perm(eq.attached_to).attached.remove(eq.id)
    _do_attach(g, eq, tgt)


def _step_activate(g: Game, action: dict):
    # v1 pin: activations are main-phase only. Real activated abilities (token
    # producers, {T}: draw a card, ...) are frequently used mid-combat too,
    # but that interacts with the extra_combat policy Task 9 introduces — so
    # combat-phase activation is deferred there rather than half-modeled here.
    if g.phase not in ("main1", "main2"):
        raise IllegalAction(f"cannot activate during {g.phase}")
    perm = _resolve_perm(g, action.get("card"))
    idx = action.get("ability")
    if not isinstance(idx, int) or isinstance(idx, bool):
        raise IllegalAction(f"activate needs an integer ability index, got {idx!r}")
    abilities = _activated_abilities(g.card(perm.name))
    if not (0 <= idx < len(abilities)):
        raise IllegalAction(f"{perm.name!r} has no activated ability {idx!r}")
    ability = abilities[idx]
    if ability.tap and not _activation_tap_ok(g, perm):
        raise IllegalAction(
            f"{perm.name!r} cannot activate a {{T}} ability now "
            f"(tapped or summoning sick)")
    if not can_pay(g, ability.mana):
        raise IllegalAction(f"cannot pay activation cost for {perm.name!r}")
    # --- all checks passed; mutation begins ---
    pay(g, ability.mana)
    if ability.tap:
        perm.tapped = True
    g.emit(f"activated {perm.name} ability {idx}")   # cause precedes the verb's effects
    execute_verb(g, perm, ability.do, count=ability.count, target=ability.target,
                power=ability.power, toughness=ability.toughness,
                keywords=ability.keywords, tutor_filter=ability.tutor_filter,
                pips=ability.pips, any_mana=ability.any_mana, duration=ability.duration)


def _step_pass(g: Game):
    if g.phase not in ("main1", "combat", "main2", "end"):
        raise IllegalAction(f"cannot pass during {g.phase}")
    # --- all checks passed; mutation begins ---
    if g.phase in ("main1", "main2"):
        check_combos(g)               # end-of-main-phase combo snapshot (Task 11)
    if g.phase == "main1":
        g.phase = "combat"
        g.emit("phase: combat")
    elif g.phase == "combat":
        # Task 9 adds extra-combat replays; for now combat always ends.
        g.phase = "main2"
        g.emit("phase: main2")
    else:
        # main2 (or a resumed "end") rolls through end straight into the next
        # turn — "end" is a transient phase, never a resting state.
        _begin_new_turn(g)


def _begin_new_turn(g: Game):
    """End the current turn and start the next: untap, clear end-of-turn
    pumps, reset per-turn counters and the mana pool (spec §Engine: the pool
    persists for the whole turn — not across turns), upkeep, draw."""
    g.turn += 1
    g.phase = "main1"
    for p in g.battlefield:
        p.tapped = False
        p.pump_eot[:] = [0, 0]
    g.mana_pool = {c: 0 for c in COLORS} | {"C": 0, "any": 0}
    g._land_drops_used = 0
    g.spells_cast_this_turn = 0
    g.combats_done = 0
    g.extra_combats = 0
    g.emit(f"turn {g.turn} begins")
    fire(g, "upkeep")
    # This solitaire format always draws — turn 1 included: the hypergeometric
    # acceptance tests count 7 + N cards seen by turn N.
    execute_verb(g, None, "draw", count=1)
    _record_activations(g)            # turn start, after upkeep/draw (Task 10's
                                      # turn-one entry reuses this path); step()
                                      # records again post-action — idempotent


def legal_actions(g: Game) -> list:
    """Factored legal-action list (spec §Interactive mode), deterministic
    order: playable land names (deduped, name-sorted), castable spells sorted
    by (mv, name) — including the commander with tax and reductions applied —
    then attach pairs (every equipment x creature combo on the battlefield,
    sorted by (eq id, target id) numerically — not payability-filtered per
    spec: "list pairs, it's fine"), then payable + untapped-eligible
    activated-ability entries (sorted by (perm id, ability index)), then
    pass. Attack (Task 9) entries arrive in a later task; mulligan-phase
    actions in Task 10."""
    if g.phase == "mulligan":
        return []
    actions = []
    if g.phase in ("main1", "main2"):
        if g.land_drops_remaining > 0:
            actions += [{"type": "play_land", "card": n}
                        for n in sorted({n for n in g.hand if g.card(n).is_land})]
        castable = set()
        for n in set(g.hand):
            card = g.card(n)
            if (not card.is_land and not card.inert_reason
                    and can_pay(g, _cast_cost(g, card, tax=0))):
                castable.add(n)
        cname = g.commander_name
        if cname in g.command and cname not in g.hand:
            card = g.card(cname)
            if (not card.is_land and not card.inert_reason
                    and can_pay(g, _cast_cost(g, card, tax=2 * g.commander_casts))):
                castable.add(cname)
        actions += [{"type": "cast", "card": n}
                    for n in sorted(castable, key=lambda n: (g.card(n).mv, n))]
        equipment = [p for p in g.battlefield if g.card(p.name).is_equipment]
        creatures = [p for p in g.battlefield if _is_creature_perm(g, p)]
        pairs = sorted(((eq, tgt) for eq in equipment for tgt in creatures),
                       key=lambda pair: (int(pair[0].id[1:]), int(pair[1].id[1:])))
        actions += [{"type": "attach", "card": eq.id, "target": tgt.id}
                    for eq, tgt in pairs]
        activations = sorted(
            ((p, i) for p in g.battlefield
             for i, ability in enumerate(_activated_abilities(g.card(p.name)))
             if _ability_payable(g, p, ability)),
            key=lambda t: (int(t[0].id[1:]), t[1]))
        actions += [{"type": "activate", "card": p.id, "ability": i}
                    for p, i in activations]
    actions.append({"type": "pass"})
    return actions
