"""Pure deterministic goldfish game core. No network, no clock."""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

from .cards import COLORS, Cost, SimCard


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
    land_drops_remaining: int = 0
    spells_cast_this_turn: int = 0
    combats_done: int = 0
    extra_combats: int = 0
    mulligans_taken: int = 0
    free_mulligan_used: bool = False
    won_turn: int | None = None
    combos: list = field(default_factory=list)          # [[names], ...]
    combo_wins: list = field(default_factory=list)      # [bool per combo]
    combo_assembled_turn: list = field(default_factory=list)   # [int|None per combo]
    combo_castable_turn: list = field(default_factory=list)
    rng: random.Random = field(default_factory=random.Random)
    log: list = field(default_factory=list)
    _next_id: int = 1

    # -- identity helpers -------------------------------------------------
    def card(self, name: str) -> SimCard:
        return self.cards[name]

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
        return {
            "turn": self.turn, "phase": self.phase,
            "mana_pool": dict(self.mana_pool),
            "land_drops_remaining": self.land_drops_remaining,
            "spells_cast_this_turn": self.spells_cast_this_turn,
            "combats_done": self.combats_done, "extra_combats": self.extra_combats,
            "mulligans_taken": self.mulligans_taken,
            "free_mulligan_used": self.free_mulligan_used,
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
                land_drops_remaining=d["land_drops_remaining"],
                spells_cast_this_turn=d["spells_cast_this_turn"],
                combats_done=d["combats_done"], extra_combats=d["extra_combats"],
                mulligans_taken=d["mulligans_taken"],
                free_mulligan_used=d["free_mulligan_used"],
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


def new_game(cards: dict, deck: list, commander: str, seed: int,
             opponents: int = 1, combos: list | None = None) -> Game:
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
