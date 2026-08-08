# Goldfish Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the seeded Monte Carlo goldfish simulator specced in `docs/superpowers/specs/2026-08-08-goldfish-simulator-design.md` — batch stats, paired A/B, interactive mode, closed-form odds, and server-rendered proof-of-work reports — as a `goldfish/` package plus eight MCP tools in `server.py`.

**Architecture:** Pure, deterministic game core (`engine.py`) with a client-supplied card-effect DSL (`cards.py`), auto-derivation from Scryfall data (`autoderive.py`), a greedy policy with reason strings (`policy.py`), statistics with Wilson CIs and paired A/B (`metrics.py`, `runner.py`), and stdlib-SVG HTML reports (`report.py`). `server.py` wraps everything in thin async tools running sims off the event loop.

**Tech Stack:** Python 3.14 (Docker), stdlib-only inside `goldfish/` except pydantic (already a server dep, used for DSL models), pytest + pytest-asyncio (asyncio_mode=auto), FastMCP 1.x (`mcp.server.fastmcp`).

**Ground rules for every task:**
- The engine must never call the network or read the clock. All randomness flows through the `random.Random` instance handed in.
- Read the spec section named in each task before starting it. The spec pins semantics; when plan and spec disagree, the spec wins — flag the discrepancy in the commit message.
- Commit style is `goldfish: <Message>` (repo convention, no Co-Authored-By).
- Run `python -m pytest tests/ -x -q` before every commit; all green.

---

## File map (created across tasks)

| File | Responsibility | Task |
|---|---|---|
| `goldfish/__init__.py` | `ENGINE_VERSION` constant | 1 |
| `goldfish/cards.py` | Cost parsing, DSL registries, annotation models + validation, `CardData`/`SimCard` | 1–3 |
| `goldfish/engine.py` | `Game` state, mana solver, events/verbs, actions, `step()`, `legal_actions()`, mulligan, combos | 4–11 |
| `goldfish/policy.py` | Greedy `choose_action(game) -> (action, reason)` | 12 |
| `goldfish/metrics.py` | Wilson CI, McNemar, per-game records → aggregate metrics, honesty report | 13 |
| `goldfish/runner.py` | Seed derivation, `run_batch`, `run_ab` with alignment + paired stats | 14–15 |
| `goldfish/odds.py` | Exact (multi-group) hypergeometrics | 16 |
| `goldfish/autoderive.py` | Scryfall card JSON → `CardData` + annotations + D9 scope classes | 17 |
| `goldfish/report.py` | Run results → self-contained HTML (inline SVG), run codes | 21 |
| `server.py` | 8 `@mcp.tool` wrappers, game/report stores, semaphore, hosted route | 18–21 |
| `tests/goldfish/…` | One test module per source module + `test_golden.py`, `test_acceptance.py` | all |
| `Dockerfile` | `COPY goldfish/ goldfish/` | 22 |

`odds.py` is a deliberate addition to the spec's file list: the closed forms share nothing with `runner.py` and Task 16 keeps them isolated for the sim-vs-closed-form acceptance test.

---

### Task 1: Package skeleton + mana-cost parsing

**Spec:** §Engine (Mana), §Card annotation DSL (auto-derived fields).

**Files:**
- Create: `goldfish/__init__.py`, `goldfish/cards.py`
- Create: `tests/goldfish/__init__.py` (empty), `tests/goldfish/test_cards.py`

- [ ] **Step 1: Write failing tests for `parse_cost`**

```python
# tests/goldfish/test_cards.py
import pytest
from goldfish.cards import Cost, parse_cost, CostParseError


def test_parse_simple_cost():
    c = parse_cost("{3}{R}{W}")
    assert c.pips == {"W": 1, "U": 0, "B": 0, "R": 1, "G": 0, "C": 0, "generic": 3}
    assert c.mv == 5


def test_parse_zero_and_empty():
    assert parse_cost("{0}").mv == 0
    assert parse_cost("").mv == 0          # lands have no cost string


def test_parse_colorless_pip_distinct_from_generic():
    c = parse_cost("{C}{C}{2}")
    assert c.pips["C"] == 2 and c.pips["generic"] == 2 and c.mv == 4


def test_parse_hybrid_and_x_rejected():
    with pytest.raises(CostParseError):
        parse_cost("{X}{R}")               # X-costs are out of scope in v1
    with pytest.raises(CostParseError):
        parse_cost("{W/U}")                # hybrid pips are out of scope in v1
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/goldfish/test_cards.py -q`
Expected: FAIL — `ModuleNotFoundError: goldfish`

- [ ] **Step 3: Implement**

```python
# goldfish/__init__.py
ENGINE_VERSION = "0.1.0"
```

```python
# goldfish/cards.py
"""Card cost parsing, DSL registries, annotation models, merged card model."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

COLORS = ("W", "U", "B", "R", "G")
_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


class CostParseError(ValueError):
    pass


@dataclass(frozen=True)
class Cost:
    pips: dict  # keys: W U B R G C generic

    @property
    def mv(self) -> int:
        return sum(self.pips.values())

    def colored(self) -> dict:
        return {k: v for k, v in self.pips.items()
                if k not in ("generic",) and v}


def parse_cost(cost_str: str) -> Cost:
    pips = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "generic": 0}
    for sym in _SYMBOL_RE.findall(cost_str or ""):
        if sym.isdigit():
            pips["generic"] += int(sym)
        elif sym in pips:
            pips[sym] += 1
        else:
            raise CostParseError(
                f"unsupported mana symbol {{{sym}}} (X, hybrid, and phyrexian "
                f"costs are out of scope in v1)")
    return Cost(pips)
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/goldfish/test_cards.py -q` → 4 passed.

- [ ] **Step 5: Commit** — `git add goldfish tests/goldfish && git commit -m "goldfish: Add package skeleton and mana-cost parser"`

---

### Task 2: DSL registries + annotation models + validation

**Spec:** §Card annotation DSL — the registries and every pinned semantic there. Read it in full first.

**Files:**
- Modify: `goldfish/cards.py`
- Test: `tests/goldfish/test_cards.py`

- [ ] **Step 1: Failing tests**

```python
from goldfish.cards import (
    validate_annotations, AnnotationError,
    EVENTS, VERBS, SYMBOLIC_COUNTS, STATIC_KINDS,
)


def test_valid_cloud_annotation_parses():
    anns = validate_annotations([{
        "name": "Cloud, Ex-SOLDIER",
        "triggers": [
            {"on": "etb", "do": "attach_from_board"},
            {"on": "attack", "do": "draw", "count": "per_equipped_attacker"},
            {"on": "attack", "if": "power_gte:7", "do": "treasure", "count": 2},
        ],
        "activated": [{"cost": "{3}{R}{W}", "do": "extra_combat"}],
    }])
    a = anns["Cloud, Ex-SOLDIER"]
    assert a.triggers[1].count == "per_equipped_attacker"
    assert a.triggers[2].condition == ("power_gte", 7)


def test_unknown_verb_rejected_with_allowed_list():
    with pytest.raises(AnnotationError) as ei:
        validate_annotations([{"name": "X", "triggers": [{"on": "etb", "do": "explode"}]}])
    msg = str(ei.value)
    assert "X" in msg and "explode" in msg and "create_token" in msg  # allowed list present


def test_static_forms():
    anns = validate_annotations([{
        "name": "Chief", "statics": [
            "equip_free",
            {"kind": "anthem", "power": 1, "toughness": 1, "keywords": ["haste"]},
            {"kind": "extra_land_drops", "count": 2},
            {"kind": "cost_reduction", "filter": "color:R", "amount": 1},
        ]}])
    kinds = [s.kind for s in anns["Chief"].statics]
    assert kinds == ["equip_free", "anthem", "extra_land_drops", "cost_reduction"]


def test_spell_cast_tutor_combination_rejected():
    # 'filter' is the event filter on spell_cast; tutor also uses 'filter' — pinned unsupported
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "triggers": [
            {"on": "spell_cast", "filter": "noncreature", "do": "tutor"}]}])


def test_tap_cost_activation():
    anns = validate_annotations([{
        "name": "Krenko, Mob Boss",
        "activated": [{"cost": "{T}", "do": "create_token",
                       "power": 1, "toughness": 1, "count": "per_creature"}]}])
    act = anns["Krenko, Mob Boss"].activated[0]
    assert act.tap is True and act.mana.mv == 0
```

- [ ] **Step 2: Run, expect FAIL** (`ImportError: validate_annotations`).

- [ ] **Step 3: Implement in `goldfish/cards.py`**

```python
SELF_EVENTS = frozenset({"cast", "etb"})
GLOBAL_EVENTS = frozenset({"spell_cast", "creature_etb", "equipment_etb",
                           "land_etb", "attack", "combat_begin", "upkeep"})
EVENTS = SELF_EVENTS | GLOBAL_EVENTS
VERBS = frozenset({"draw", "gain_life", "treasure", "damage", "create_token",
                   "ramp_land", "ramp_mana", "add_mana", "tutor", "attach",
                   "attach_from_board", "pump", "extra_combat", "token_copy"})
CONDITIONS = frozenset({"power_gte", "metalcraft", "equipped"})
SYMBOLIC_COUNTS = frozenset({"per_equipped_attacker", "per_artifact", "per_creature",
                             "per_attacker", "per_land", "per_spell_cast_this_turn"})
STATIC_KINDS = frozenset({"equip_free", "equip_free_if_metalcraft", "anthem",
                          "token_doubling", "cost_reduction", "extra_land_drops"})
SPELL_CAST_FILTERS = frozenset({"instant_or_sorcery", "noncreature", "any"})
TUTOR_FILTERS = frozenset({"equipment", "land", "creature", "instant", "sorcery",
                           "artifact", "enchantment", "planeswalker", "any"})
COST_REDUCTION_FILTERS = frozenset({"instant_or_sorcery", "noncreature", "creature",
                                    "artifact", "equipment", "any"})
DAMAGE_TARGETS = frozenset({"one_opponent", "each_opponent"})
SCOPE_CLASSES = frozenset({"interaction_removal", "interaction_wipe",
                           "interaction_counter", "protection", "political",
                           "unmodeled_other"})


class AnnotationError(ValueError):
    def __init__(self, card: str, field_name: str, bad, allowed):
        super().__init__(
            f"annotation for {card!r}: invalid {field_name} {bad!r}; "
            f"allowed: {', '.join(sorted(allowed))}")
        self.card, self.field_name = card, field_name


@dataclass
class Trigger:
    on: str
    do: str
    count: object = 1                       # int or symbolic string
    condition: tuple | None = None          # ("power_gte", 7) | ("metalcraft",) | ("equipped",)
    event_filter: str | None = None         # spell_cast only
    target: str | None = None               # damage
    power: int | None = None                # create_token / pump
    toughness: int | None = None
    duration: str = "eot"                   # pump: eot|permanent
    keywords: tuple = ()
    tutor_filter: str | None = None         # "land" | ... | "name:Sol Ring"
    pips: str | None = None                 # add_mana: "{R}{R}" or None with count+any
    any_mana: bool = False                  # add_mana {count, colors: any}


@dataclass
class Activated:
    do: str
    mana: Cost
    tap: bool = False
    # same verb params as Trigger:
    count: object = 1
    target: str | None = None
    power: int | None = None
    toughness: int | None = None
    keywords: tuple = ()
    tutor_filter: str | None = None
    pips: str | None = None
    any_mana: bool = False


@dataclass
class Static:
    kind: str
    power: int = 0
    toughness: int = 0
    keywords: tuple = ()
    count: int = 1                          # extra_land_drops
    filter: str = "any"                     # cost_reduction; also "color:R"
    amount: int = 0                         # cost_reduction


@dataclass
class Annotation:
    name: str
    triggers: list = field(default_factory=list)
    statics: list = field(default_factory=list)
    activated: list = field(default_factory=list)
    grants: dict | None = None              # equipment override {power,toughness,keywords}
    inert: bool = False
```

Validation is a plain function (no pydantic here — the error messages must name
card/field/allowed exactly, which hand-rolled checks do best; pydantic remains
at the tool-input layer in `server.py`):

```python
def _parse_condition(card, raw):
    if raw is None:
        return None
    head, _, arg = str(raw).partition(":")
    if head not in CONDITIONS:
        raise AnnotationError(card, "if", raw, CONDITIONS)
    if head == "power_gte":
        if not arg.isdigit():
            raise AnnotationError(card, "if", raw, {"power_gte:<N>"})
        return ("power_gte", int(arg))
    return (head,)


def _parse_count(card, raw):
    if isinstance(raw, int):
        return raw
    if raw in SYMBOLIC_COUNTS:
        return raw
    raise AnnotationError(card, "count", raw, SYMBOLIC_COUNTS | {"<int>"})


def _check_verb_params(card, do, t: Trigger | Activated):
    if do == "damage" and t.target not in DAMAGE_TARGETS:
        raise AnnotationError(card, "target", t.target, DAMAGE_TARGETS)
    if do == "create_token" and (t.power is None or t.toughness is None):
        raise AnnotationError(card, "create_token", "missing power/toughness",
                              {"power:<int>", "toughness:<int>"})
    if do == "tutor":
        f = t.tutor_filter or "any"
        if not (f in TUTOR_FILTERS or f.startswith("name:")):
            raise AnnotationError(card, "filter", f, TUTOR_FILTERS | {"name:<CardName>"})
    if do == "add_mana" and not (t.pips or t.any_mana):
        raise AnnotationError(card, "add_mana", "missing pips",
                              {'pips:"{R}{R}"', "colors:any + count"})
    if do == "pump" and (t.power is None or t.toughness is None):
        raise AnnotationError(card, "pump", "missing power/toughness",
                              {"power:<int>", "toughness:<int>"})


def _parse_trigger(card, raw: dict) -> Trigger:
    on, do = raw.get("on"), raw.get("do")
    if on not in EVENTS:
        raise AnnotationError(card, "on", on, EVENTS)
    if do not in VERBS:
        raise AnnotationError(card, "do", do, VERBS)
    if on == "spell_cast" and do == "tutor":
        raise AnnotationError(card, "trigger", "spell_cast+tutor",
                              {"any other event/verb pair (filter is ambiguous — pinned unsupported)"})
    ev_filter = raw.get("filter") if on == "spell_cast" else None
    if on == "spell_cast" and ev_filter is not None and ev_filter not in SPELL_CAST_FILTERS:
        raise AnnotationError(card, "filter", ev_filter, SPELL_CAST_FILTERS)
    t = Trigger(
        on=on, do=do,
        count=_parse_count(card, raw.get("count", 1)),
        condition=_parse_condition(card, raw.get("if")),
        event_filter=ev_filter,
        target=raw.get("target"),
        power=raw.get("power"), toughness=raw.get("toughness"),
        duration=raw.get("duration", "eot"),
        keywords=tuple(raw.get("keywords", ())),
        tutor_filter=raw.get("filter") if on != "spell_cast" else None,
        pips=raw.get("pips"),
        any_mana=(raw.get("colors") == "any"),
    )
    if t.duration not in ("eot", "permanent"):
        raise AnnotationError(card, "duration", t.duration, {"eot", "permanent"})
    _check_verb_params(card, do, t)
    return t


def _parse_activated(card, raw: dict) -> Activated:
    do = raw.get("do")
    if do not in VERBS:
        raise AnnotationError(card, "do", do, VERBS)
    cost_str = raw.get("cost", "")
    tap = "{T}" in cost_str
    a = Activated(
        do=do, mana=parse_cost(cost_str.replace("{T}", "")), tap=tap,
        count=_parse_count(card, raw.get("count", 1)),
        target=raw.get("target"),
        power=raw.get("power"), toughness=raw.get("toughness"),
        keywords=tuple(raw.get("keywords", ())),
        tutor_filter=raw.get("filter"),
        pips=raw.get("pips"), any_mana=(raw.get("colors") == "any"),
    )
    _check_verb_params(card, do, a)
    return a


def _parse_static(card, raw) -> Static:
    if isinstance(raw, str):
        kind, obj = raw, {}
    else:
        kind, obj = raw.get("kind"), raw
    if kind not in STATIC_KINDS:
        raise AnnotationError(card, "static", kind, STATIC_KINDS)
    s = Static(kind=kind,
               power=obj.get("power", 0), toughness=obj.get("toughness", 0),
               keywords=tuple(obj.get("keywords", ())),
               count=obj.get("count", 1),
               filter=obj.get("filter", "any"), amount=obj.get("amount", 0))
    if kind == "cost_reduction":
        f = s.filter
        if not (f in COST_REDUCTION_FILTERS or
                (f.startswith("color:") and f[6:] in COLORS)):
            raise AnnotationError(card, "filter", f,
                                  COST_REDUCTION_FILTERS | {"color:<W|U|B|R|G>"})
    return s


def validate_annotations(raw_list: list) -> dict:
    """[dict, ...] -> {card_name: Annotation}. Raises AnnotationError."""
    out = {}
    for raw in raw_list:
        name = raw.get("name")
        if not name:
            raise AnnotationError("<unnamed>", "name", None, {"a card name"})
        out[name] = Annotation(
            name=name,
            triggers=[_parse_trigger(name, t) for t in raw.get("triggers", [])],
            statics=[_parse_static(name, s) for s in raw.get("statics", [])],
            activated=[_parse_activated(name, a) for a in raw.get("activated", [])],
            grants=raw.get("grants"),
            inert=bool(raw.get("inert", False)),
        )
    return out
```

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `goldfish: Add DSL registries, annotation models, validation`

---

### Task 3: CardData / SimCard merge

**Spec:** §Card annotation DSL (auto-derived fields list).

**Files:** Modify `goldfish/cards.py`; test `tests/goldfish/test_cards.py`.

- [ ] **Step 1: Failing tests**

```python
from goldfish.cards import CardData, SimCard, merge_card


def make_data(name, **kw):
    base = dict(name=name, cost=None, types=frozenset(), power=None, toughness=None,
                keywords=frozenset(), produces=None, enters_tapped=False,
                equip_cost=None, oracle="")
    base.update(kw)
    return CardData(**base)


def test_land_card():
    c = SimCard(data=make_data("Plains", types=frozenset({"land"}),
                               produces={"W": 1}), ann=None, scope_class=None)
    assert c.is_land and not c.is_creature and c.mv == 0


def test_merge_annotation_and_scope():
    data = make_data("Cait Sith", cost=parse_cost("{1}{W}"),
                     types=frozenset({"creature"}), power=2, toughness=2)
    c = merge_card(data, ann=None, scope_class="unmodeled_other")
    assert c.inert_reason == "unmodeled_other"
    c2 = merge_card(data, ann=Annotation(name="Cait Sith"), scope_class="unmodeled_other")
    assert c2.inert_reason is None   # explicit annotation overrides the scope class
```

- [ ] **Step 2: Run, FAIL.**

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class CardData:
    name: str
    cost: Cost | None
    types: frozenset          # lowercase: land creature artifact equipment enchantment
                              # instant sorcery planeswalker commander legendary
    power: int | None
    toughness: int | None
    keywords: frozenset       # lowercase: haste flying ...
    produces: dict | None     # {"W": 1} / {"C": 2}; None = not a producer
    enters_tapped: bool
    equip_cost: Cost | None
    oracle: str


@dataclass
class SimCard:
    data: CardData
    ann: Annotation | None
    scope_class: str | None       # D9 class, or None

    @property
    def name(self): return self.data.name
    @property
    def mv(self): return self.data.cost.mv if self.data.cost else 0
    @property
    def is_land(self): return "land" in self.data.types
    @property
    def is_creature(self): return "creature" in self.data.types
    @property
    def is_equipment(self): return "equipment" in self.data.types
    @property
    def is_artifact(self): return "artifact" in self.data.types
    @property
    def inert_reason(self):
        """Scope class when the card has no effect model. Annotation overrides."""
        if self.ann is not None and not self.ann.inert:
            return None
        if self.ann is not None and self.ann.inert:
            return "annotated_inert"
        return self.scope_class

    def triggers_for(self, event: str):
        if self.ann is None or self.inert_reason:
            return []
        return [t for t in self.ann.triggers if t.on == event]

    def statics(self):
        if self.ann is None or self.inert_reason:
            return []
        return self.ann.statics


def merge_card(data: CardData, ann: Annotation | None, scope_class: str | None) -> SimCard:
    return SimCard(data=data, ann=ann, scope_class=scope_class)
```

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add CardData/SimCard merge model`

---

### Task 4: Game state, permanents, serialization, seed derivation

**Spec:** §Interactive state schema, §Architecture (determinism), D3.

**Files:** Create `goldfish/engine.py`; test `tests/goldfish/test_engine.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/goldfish/test_engine.py
import random
from goldfish.cards import parse_cost, CardData, SimCard, Annotation
from goldfish.engine import Game, derive_seed, new_game
from tests.goldfish.test_cards import make_data          # reuse the factory


def mini_cards():
    """Six-name pool used across engine tests."""
    defs = {
        "Plains":   make_data("Plains", types=frozenset({"land"}), produces={"W": 1}),
        "Mountain": make_data("Mountain", types=frozenset({"land"}), produces={"R": 1}),
        "Bear":     make_data("Bear", cost=parse_cost("{1}{G}"),
                              types=frozenset({"creature"}), power=2, toughness=2),
        "Runner":   make_data("Runner", cost=parse_cost("{R}"),
                              types=frozenset({"creature"}), power=1, toughness=1,
                              keywords=frozenset({"haste"})),
        "Hammer":   make_data("Hammer", cost=parse_cost("{1}"),
                              types=frozenset({"artifact", "equipment"}),
                              equip_cost=parse_cost("{8}")),
        "Boss":     make_data("Boss", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature", "legendary", "commander"}),
                              power=3, toughness=3),
    }
    return {n: SimCard(data=d, ann=None, scope_class=None) for n, d in defs.items()}


def test_seed_derivation_stable_and_distinct():
    assert derive_seed(42, 0) == derive_seed(42, 0)
    assert derive_seed(42, 0) != derive_seed(42, 1) != derive_seed(43, 0)


def test_new_game_shuffles_and_draws_seven():
    cards = mini_cards()
    deck = ["Plains"] * 20 + ["Mountain"] * 20 + ["Bear"] * 30 + ["Hammer"] * 29
    g1 = new_game(cards, deck, commander="Boss", seed=7)
    g2 = new_game(cards, deck, commander="Boss", seed=7)
    assert g1.hand == g2.hand and g1.library == g2.library     # deterministic
    assert len(g1.hand) == 7 and len(g1.library) == 92          # 99 - 7
    assert g1.command == ["Boss"] and g1.phase == "mulligan"


def test_state_roundtrip():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, commander="Boss", seed=3)
    g.rng.random()                                              # advance rng
    blob = g.to_dict()
    g2 = Game.from_dict(blob, cards)
    assert g2.rng.random() == g.rng.random()                    # rng state travels
    assert g2.hand == g.hand and g2.turn == g.turn
```

- [ ] **Step 2: Run, FAIL.**

- [ ] **Step 3: Implement in `goldfish/engine.py`**

```python
"""Pure deterministic goldfish game core. No network, no clock."""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

from .cards import (Cost, SimCard, parse_cost, COLORS)


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
            "combos": self.combos, "combo_wins": self.combo_wins,
            "combo_assembled_turn": self.combo_assembled_turn,
            "combo_castable_turn": self.combo_castable_turn,
            "rng_state": _rng_state_to_json(self.rng.getstate()),
            "log": list(self.log), "next_id": self._next_id,
        }

    @classmethod
    def from_dict(cls, d: dict, cards: dict) -> "Game":
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
                won_turn=d["won_turn"], combos=list(d["combos"]),
                combo_wins=list(d["combo_wins"]),
                combo_assembled_turn=list(d["combo_assembled_turn"]),
                combo_castable_turn=list(d["combo_castable_turn"]))
        g.log = list(d["log"]); g._next_id = d["next_id"]
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
    g.combos = [c["cards"] if isinstance(c, dict) else list(c) for c in g.combos]
    g.combo_assembled_turn = [None] * len(g.combos)
    g.combo_castable_turn = [None] * len(g.combos)
    g.rng = rng
    return g
```

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add game state, permanents, serialization, seeds`

---

### Task 5: Mana payment solver

**Spec:** §Engine (Mana) — pips, quantities, wildcards, cost_reduction order, per-turn pool.

**Files:** Modify `goldfish/engine.py`; test `tests/goldfish/test_engine.py`.

- [ ] **Step 1: Failing tests**

```python
from goldfish.engine import can_pay, pay, untapped_producers


def bf_land(g, name):
    p = g.new_perm(name)
    return p


def test_cannot_pay_red_with_only_plains():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    for _ in range(3):
        bf_land(g, "Plains")
    assert can_pay(g, parse_cost("{R}")) is False
    assert can_pay(g, parse_cost("{2}")) is True


def test_greedy_prefers_flexible_land_for_generic():
    # 1 Mountain + 1 Plains, cost {1}{R}: the Mountain must fund {R}, Plains the {1}
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "Mountain"); bf_land(g, "Plains")
    assert can_pay(g, parse_cost("{1}{R}")) is True
    pay(g, parse_cost("{1}{R}"))
    assert all(p.tapped for p in g.battlefield)


def test_quantity_producer_and_pool_wildcard():
    cards = mini_cards()
    cards["Karoo"] = SimCard(data=make_data("Karoo", types=frozenset({"land"}),
                                            produces={"W": 2}), ann=None, scope_class=None)
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    bf_land(g, "Karoo")
    g.mana_pool["any"] = 1
    assert can_pay(g, parse_cost("{2}{R}")) is True   # W W from Karoo + wildcard R
```

- [ ] **Step 2: FAIL.**

- [ ] **Step 3: Implement (engine.py)**

> **CORRECTED during execution (quality review of fb105ef):** the reference
> code below has two known defects — do not transcribe it as-is. (1) The
> colored loop orders pips by demand; it must order by **dynamic scarcity**
> (fewest remaining candidate sources: matching producers in `avail` +
> `pool[color]` + `pool["any"]`, recomputed after each payment, ties by need
> then WUBRG) or overlapping duals produce false "can't pay" verdicts
> (~0.5–1.2% of payable 3-color checks). (2) The generic phase must bank
> producer overshoot (`take = min(generic, qty)`, surplus routed like the
> colored phase) or Sol Ring-class rocks leak mana. Also: a true `{C}` pip is
> never payable from `pool["any"]`. See the fix commit on branch `goldfish`.

```python
def untapped_producers(g: Game):
    """[(perm, colors: frozenset|{'C'}, qty)] for untapped mana permanents,
    summoning-sickness-aware for creatures with {T} in produces (none in v1 —
    rocks/lands only), deterministic order by perm id."""
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
    """Greedy: colored pips from strictest producers, generic from the rest
    (largest quantity first), pool wildcards last. Returns [perm ids] or None."""
    producers = untapped_producers(g)
    used, avail = [], list(producers)
    pool = dict(g.mana_pool)
    need = dict(cost.pips)

    for color in sorted([c for c in ("W", "U", "B", "R", "G", "C") if need.get(c)],
                        key=lambda c: -need[c]):
        for _ in range(need[color]):
            if pool.get(color, 0) > 0:
                pool[color] -= 1
                continue
            hit = next((t for t in avail if color in t[1]), None)
            if hit:
                avail.remove(hit)
                used.append(hit)
                surplus = hit[2] - 1
                if surplus:
                    pool["any" if len(hit[1]) > 1 else next(iter(hit[1]))] = \
                        pool.get("any" if len(hit[1]) > 1 else next(iter(hit[1])), 0) + surplus
            elif pool.get("any", 0) > 0:
                pool["any"] -= 1
            else:
                return None

    generic = need.get("generic", 0)
    avail.sort(key=lambda t: (-t[2], len(t[1])))    # big colorless rocks first
    for t in list(avail):
        if generic <= 0:
            break
        avail.remove(t); used.append(t); generic -= t[2]
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
```

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add greedy colored-mana payment solver`

---

### Task 6: Events, verbs, counts, conditions

**Spec:** §Card annotation DSL (global-listener rule, per-token creature_etb, count timing) and §Engine. Read both.

**Files:** Modify `goldfish/engine.py`; test `tests/goldfish/test_engine.py`.

Key contracts:
- `fire(g, event, source_perm=None, spell_card=None)` walks the battlefield (plus the spell itself for self `cast`/`etb` semantics handled at cast time), collects matching triggers, executes verbs via a dispatch dict.
- Global listeners fire only for sources on the battlefield; a card's own cast never fires its own `spell_cast` listener (pinned: `fire("spell_cast", exclude_perm=None)` is called *before* the caster hits the battlefield for permanents, and the spell itself is never a listener).
- `spells_cast_this_turn` increments before the spell's own `cast` triggers run.
- `resolve_count(g, count, ctx)` handles symbolic counts; `ctx` carries `attackers` for `per_attacker`/`per_equipped_attacker`.
- `token_doubling`: multiply `create_token` count by `2 ** n_doublers`.
- Each token created fires `creature_etb` individually.

- [ ] **Step 1: Failing tests** (representative — the executor writes all of these)

```python
from goldfish.engine import execute_verb, fire, resolve_count
from goldfish.cards import validate_annotations


def annotated(cards, name, ann_dict):
    anns = validate_annotations([ann_dict])
    c = cards[name]
    cards[name] = SimCard(data=c.data, ann=anns[name], scope_class=None)


def test_damage_each_opponent_and_one():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1, opponents=3)
    execute_verb(g, source=None, verb="damage", count=3, target="each_opponent")
    assert g.opponents == [37, 37, 37]
    execute_verb(g, source=None, verb="damage", count=2, target="one_opponent")
    assert g.opponents == [35, 37, 37]        # focus-fire: lowest index first


def test_create_token_fires_creature_etb_per_token_and_doubles():
    cards = mini_cards()
    cards["Tremors"] = SimCard(data=make_data("Tremors", cost=parse_cost("{1}{R}"),
                               types=frozenset({"enchantment"})), ann=None, scope_class=None)
    cards["Doubler"] = SimCard(data=make_data("Doubler", cost=parse_cost("{4}"),
                               types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Tremors", {"name": "Tremors", "triggers": [
        {"on": "creature_etb", "do": "damage", "target": "each_opponent", "count": 1}]})
    annotated(cards, "Doubler", {"name": "Doubler", "statics": [{"kind": "token_doubling"}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.new_perm("Tremors"); g.new_perm("Doubler")
    execute_verb(g, source=None, verb="create_token", count=2, power=1, toughness=1)
    tokens = [p for p in g.battlefield if p.is_token]
    assert len(tokens) == 4                     # doubled
    assert g.opponents == [36]                  # 4 Tremors pings


def test_spell_cast_listener_not_self_and_count_includes_self():
    cards = mini_cards()
    cards["Snipe"] = SimCard(data=make_data("Snipe", cost=parse_cost("{2}{R}"),
                             types=frozenset({"creature"}), power=2, toughness=2),
                             ann=None, scope_class=None)
    annotated(cards, "Snipe", {"name": "Snipe", "triggers": [
        {"on": "spell_cast", "filter": "instant_or_sorcery",
         "do": "damage", "target": "each_opponent", "count": 2}]})
    cards["Bolt"] = SimCard(data=make_data("Bolt", cost=parse_cost("{R}"),
                            types=frozenset({"instant"})), ann=None, scope_class=None)
    annotated(cards, "Bolt", {"name": "Bolt", "triggers": [
        {"on": "cast", "do": "damage", "target": "one_opponent",
         "count": "per_spell_cast_this_turn"}]})
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g.hand[:] = ["Bolt"]; g.phase = "main1"; g.turn = 1
    for _ in range(3):
        g.new_perm("Mountain")
    from goldfish.engine import step
    step(g, {"type": "cast", "card": "Bolt"})
    # spells_cast_this_turn incremented before its own cast trigger: count == 1
    assert g.opponents == [39]                  # 1 dmg from Bolt's own counter...
    # ...and Snipe was NOT on the battlefield, so no +2
```

- [ ] **Step 2: FAIL.**

- [ ] **Step 3: Implement.** Core shape (executor fills the remaining verbs following these patterns exactly):

```python
def effective_power(g: Game, p: Permanent) -> int:
    card = g.card(p.name)
    base = p.token_power if p.is_token else (card.data.power or 0)
    base += p.pump_perm[0] + p.pump_eot[0]
    for eq_id in p.attached:
        eq = g.perm(eq_id)
        grants = _equipment_grants(g, eq)
        base += grants.get("power", 0)
    for src in g.battlefield:
        for s in g.card(src.name).statics():
            if s.kind == "anthem" and _is_creature_perm(g, p):
                base += s.power
    return base


def effective_keywords(g: Game, p: Permanent) -> set:
    card = g.card(p.name)
    kws = set(p.token_keywords if p.is_token else card.data.keywords)
    for eq_id in p.attached:
        kws |= set(_equipment_grants(g, g.perm(eq_id)).get("keywords", ()))
    for src in g.battlefield:
        for s in g.card(src.name).statics():
            if s.kind == "anthem":
                kws |= set(s.keywords)
    return kws


def _is_creature_perm(g, p):
    return p.is_token or g.card(p.name).is_creature


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


def execute_verb(g: Game, source, verb: str, ctx: dict | None = None, **params):
    ctx = ctx or {}
    n = resolve_count(g, params.get("count", 1), ctx)
    if verb == "draw":
        for _ in range(n):
            if g.library:
                card = g.library.pop(0)
                g.hand.append(card)
                g.emit(f"drew {card}")
    elif verb == "damage":
        if params.get("target") == "each_opponent":
            g.opponents = [life - n for life in g.opponents]
        else:
            i = _focus_target(g)
            g.opponents[i] -= n
        g.emit(f"{n} damage ({params.get('target')})")
    elif verb == "create_token":
        doublers = sum(1 for p in g.battlefield
                       for s in g.card(p.name).statics() if s.kind == "token_doubling")
        total = n * (2 ** doublers)
        for _ in range(total):
            tok = g.new_perm(params.get("token_name", "Token"), is_token=True,
                             token_power=params["power"],
                             token_toughness=params["toughness"],
                             token_keywords=tuple(params.get("keywords", ())))
            fire(g, "creature_etb", entering=tok)
        g.emit(f"created {total} token(s)")
    elif verb == "treasure":
        for _ in range(n):
            g.new_perm("Treasure", is_token=True)   # Treasure SimCard added in new_game
    # ... gain_life, add_mana, ramp_land, ramp_mana, tutor, pump, attach,
    #     attach_from_board, extra_combat, token_copy — one elif each, same shape;
    #     exact semantics for each are pinned in spec §Card annotation DSL.
    else:
        raise IllegalAction(f"verb {verb!r} not implemented")


def _focus_target(g: Game) -> int:
    """Focus-fire: first opponent still above 0, else 0."""
    for i, life in enumerate(g.opponents):
        if life > 0:
            return i
    return 0


def fire(g: Game, event: str, source_perm=None, entering=None, spell=None, ctx=None):
    """Run all battlefield listeners for a global event, plus the entering/attacking
    permanent's own self triggers where applicable."""
    listeners = []
    for p in g.battlefield:
        if entering is not None and p.id == entering.id:
            continue          # a permanent doesn't hear its own arrival as a global event
        for t in g.card(p.name).triggers_for(event):
            if event == "spell_cast" and spell is not None:
                if not _spell_filter_ok(g, t.event_filter, spell):
                    continue
            listeners.append((p, t))
    for p, t in listeners:
        if check_condition(g, t.condition, p, ctx or {}):
            g.emit(f"{p.name} trigger — {t.do}")
            execute_verb(g, p, t.do, ctx=ctx, count=t.count, target=t.target,
                         power=t.power, toughness=t.toughness,
                         keywords=t.keywords, tutor_filter=t.tutor_filter,
                         pips=t.pips, any_mana=t.any_mana, duration=t.duration)


def _spell_filter_ok(g, f, spell_card) -> bool:
    if f in (None, "any"):
        return True
    if f == "instant_or_sorcery":
        return spell_card.data.types & {"instant", "sorcery"}
    if f == "noncreature":
        return not spell_card.is_creature
    return False
```

Also in this task: `new_game` injects two synthetic SimCards into the card pool so `g.card(name)` never KeyErrors on engine-created permanents: `"Treasure"` (`produces` = all five colors qty 1 — sacrificed-on-use is v2; v1 treats it as a reusable rock, honesty-report-noted, pin with a comment) and `"Token"` (typeless placeholder whose stats live on the `Permanent`'s `token_power`/`token_toughness`; `_is_creature_perm` already keys off `p.is_token`).

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add event dispatch, verb handlers, counts, conditions`

---

### Task 7: Actions — play_land, cast, pass, turn advance

**Spec:** §Engine (turn structure, dynamic land drops), §Interactive mode (action schema, non-mutation on rejection).

**Files:** Modify `goldfish/engine.py`; test `tests/goldfish/test_engine.py`.

- [ ] **Step 1: Failing tests**

```python
from goldfish.engine import step, legal_actions, IllegalAction


def started(cards, deck, seed=1, hand=None):
    g = new_game(cards, deck, "Boss", seed=seed)
    g.phase = "main1"; g.turn = 1; g.land_drops_remaining = 1
    if hand is not None:
        g.hand[:] = hand
    return g


def test_play_land_and_drop_counter():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=["Plains", "Plains"])
    step(g, {"type": "play_land", "card": "Plains"})
    assert g.land_drops_remaining == 0
    with pytest.raises(IllegalAction):
        step(g, {"type": "play_land", "card": "Plains"})
    assert len(g.hand) == 1                      # unmutated on rejection


def test_cast_commander_with_tax():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=[])
    for _ in range(3):
        g.new_perm("Mountain")
    step(g, {"type": "cast", "card": "Boss"})            # {2}{R}, exact
    assert g.commander_casts == 1 and g.command == []
    # return to command zone by hand for tax test:
    g.battlefield = [p for p in g.battlefield if p.name != "Boss"]
    g.command = ["Boss"]
    for p in g.battlefield:
        p.tapped = False
    assert not any(a["type"] == "cast" and a["card"] == "Boss"
                   for a in legal_actions(g))            # 3 lands can't pay {4}{R}


def test_pass_advances_and_new_turn_untaps_draws():
    cards = mini_cards()
    g = started(cards, ["Bear"] * 40, hand=[])
    p = g.new_perm("Plains"); p.tapped = True
    for ph in ("combat", "main2", "end"):
        step(g, {"type": "pass"})
        if ph != "end":
            assert g.phase == ph
    assert g.turn == 2 and g.phase == "main1"
    assert p.tapped is False and len(g.hand) == 1        # untap + draw
    assert g.land_drops_remaining == 1 and g.spells_cast_this_turn == 0


def test_dynamic_extra_land_drops():
    cards = mini_cards()
    cards["Azusa"] = SimCard(data=make_data("Azusa", cost=parse_cost("{2}{G}"),
                             types=frozenset({"creature"}), power=1, toughness=2),
                             ann=None, scope_class=None)
    annotated(cards, "Azusa", {"name": "Azusa",
                               "statics": [{"kind": "extra_land_drops", "count": 2}]})
    g = started(cards, ["Plains"] * 40, hand=["Plains", "Plains", "Plains"])
    step(g, {"type": "play_land", "card": "Plains"})
    g.new_perm("Azusa")                                   # arrives mid-turn
    assert g.land_drops_remaining == 2                    # dynamic: 1+2 total, 1 used
    step(g, {"type": "play_land", "card": "Plains"})
    step(g, {"type": "play_land", "card": "Plains"})
    assert g.land_drops_remaining == 0
```

- [ ] **Step 2: FAIL.**

- [ ] **Step 3: Implement.** Contracts:

```python
def _land_drops_allowed(g: Game) -> int:
    extra = sum(s.count for p in g.battlefield
                for s in g.card(p.name).statics() if s.kind == "extra_land_drops")
    return 1 + extra
```

`land_drops_remaining` is stored as *drops used* internally? No — keep the spec's field. Recompute on read: `land_drops_remaining` property = `_land_drops_allowed(g) - g._land_drops_used`; store `_land_drops_used` in serialization as `land_drops_used` and expose `land_drops_remaining` in `to_dict`. (This is what makes Azusa dynamic.)

`step(g, action)` — validate FIRST, mutate after; the last validation line before any mutation is a comment `# --- all checks passed; mutation begins ---`.

- `play_land`: requires phase in (main1, main2), card in hand, is_land, drops remaining. Effects: move to battlefield (`tapped=enters_tapped`), fire `land_etb` (entering perm), emit log.
- `cast`: card in hand or (commander in command zone). Cost = card cost + `{2}`×`commander_casts` for the commander (build a new Cost adding to generic) − cost_reduction statics (generic only, floor 0, filter match per spec vocabulary incl. `color:<C>` = card's cost has that colored pip). Requires `can_pay`. Effects in order: `pay`; `g.spells_cast_this_turn += 1`; fire `spell_cast` (spell=card, listeners exclude nothing on battlefield — the spell isn't there); if permanent type → `new_perm`, fire self `etb` triggers (run `t.on=="etb"` on the card directly), then `fire("creature_etb"/"equipment_etb"/"land_etb", entering=perm)` per type; if instant/sorcery → run self `cast` triggers then move to graveyard; commander bookkeeping (`commander_casts += 1`, remove from command).
- `pass`: advance phase; entering a new turn runs untap (all), clear `pump_eot`, reset per-turn counters, fire `upkeep`, draw 1 (turn > 1 or always — pinned: the play-first player draws in this solitaire format, matching the hypergeometric tests; document in code comment), combat bookkeeping reset.
- After every mutation that could complete a combo, call `check_combos(g)` (stub until Task 11 — define it now returning None).

`legal_actions(g)` returns the factored list per spec §Interactive mode: one entry per castable card, per playable land name (deduped), attach pairs, activations, `{"type":"attack","eligible":[ids]}` during combat, `pass` always (outside mulligan).

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add play_land/cast/pass actions and turn engine`

---

### Task 8: Actions — attach, activate; equip statics

**Spec:** §Card annotation DSL (equip_free statics, grants), §Engine (summoning sickness on `{T}`).

**Files:** Modify `goldfish/engine.py`; test `tests/goldfish/test_engine.py`.

- [ ] **Step 1: Failing tests**

```python
def test_attach_pays_equip_cost_and_free_static():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=[])
    bear = g.new_perm("Bear"); ham = g.new_perm("Hammer")
    for _ in range(8):
        g.new_perm("Plains")
    step(g, {"type": "attach", "card": ham.id, "target": bear.id})
    assert ham.attached_to == bear.id and bear.attached == [ham.id]
    assert sum(1 for p in g.battlefield if p.tapped) == 8      # paid {8}
    # free-equip static:
    cards["Aid"] = SimCard(data=make_data("Aid", cost=parse_cost("{W}"),
                           types=frozenset({"enchantment"})), ann=None, scope_class=None)
    annotated(cards, "Aid", {"name": "Aid", "statics": ["equip_free"]})
    g2 = started(cards, ["Plains"] * 40, hand=[])
    b2, h2 = g2.new_perm("Bear"), g2.new_perm("Hammer")
    g2.new_perm("Aid")
    step(g2, {"type": "attach", "card": h2.id, "target": b2.id})
    assert not any(p.tapped for p in g2.battlefield)           # free


def test_tap_activation_respects_summoning_sickness():
    cards = mini_cards()
    cards["Krenko"] = SimCard(data=make_data("Krenko", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature"}), power=3, toughness=3),
                              ann=None, scope_class=None)
    annotated(cards, "Krenko", {"name": "Krenko", "activated": [
        {"cost": "{T}", "do": "create_token", "power": 1, "toughness": 1,
         "count": "per_creature"}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    k = g.new_perm("Krenko")                     # arrived this turn, no haste
    with pytest.raises(IllegalAction):
        step(g, {"type": "activate", "card": k.id, "ability": 0})
    g.turn += 1                                  # next turn
    step(g, {"type": "activate", "card": k.id, "ability": 0})
    assert k.tapped and sum(1 for p in g.battlefield if p.is_token) == 1
```

- [ ] **Step 2: FAIL.** **Step 3: Implement** — `attach` resolves card/target by perm id or unique name (ambiguity → `IllegalAction` listing candidate ids, per spec §Error handling); equip cost skipped when any battlefield static `equip_free` (or `equip_free_if_metalcraft` + metalcraft) exists; re-attach moves the equipment (clear old `attached`/`attached_to`). `activate` indexes `card.ann.activated[ability]`; `{T}` requires untapped + (not creature or arrived before this turn or haste in `effective_keywords`); pays mana part via `pay`; executes verb with source perm.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add attach/activate actions with equip statics`

---

### Task 9: Combat — attack subsets, sickness, focus-fire, extra combats

**Spec:** §Engine (Combat), D8; §Interactive mode (attack action).

**Files:** Modify `goldfish/engine.py`; test `tests/goldfish/test_engine.py`.

- [ ] **Step 1: Failing tests**

```python
def test_attack_deals_damage_and_tracks_commander():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=[])
    g.turn = 3
    boss = g.new_perm("Boss"); boss.arrived_turn = 2
    bear = g.new_perm("Bear"); bear.arrived_turn = 2
    g.phase = "combat"
    step(g, {"type": "attack", "attackers": [boss.id, bear.id]})
    assert g.opponents == [35]                    # 3 + 2
    assert g.cmdr_damage == [3]
    assert boss.tapped and bear.tapped


def test_summoning_sick_attacker_rejected_haste_allowed():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=[])
    g.phase = "combat"
    bear = g.new_perm("Bear")                     # arrived this turn
    runner = g.new_perm("Runner")                 # haste
    with pytest.raises(IllegalAction):
        step(g, {"type": "attack", "attackers": [bear.id]})
    step(g, {"type": "attack", "attackers": [runner.id]})
    assert g.opponents == [39]


def test_attack_fires_combat_events_and_extra_combat():
    cards = mini_cards()
    annotated(cards, "Boss", {"name": "Boss", "triggers": [
        {"on": "attack", "do": "draw", "count": 1}]})
    g = started(cards, ["Bear"] * 40, hand=[])
    g.turn = 2
    boss = g.new_perm("Boss"); boss.arrived_turn = 1
    g.phase = "combat"
    hand_before = len(g.hand)
    step(g, {"type": "attack", "attackers": [boss.id]})
    assert len(g.hand) == hand_before + 1         # attack trigger drew
    g.extra_combats = 1
    step(g, {"type": "pass"})                     # leaving combat with extra pending
    assert g.phase == "combat"                    # replays combat, not main2
    assert boss.tapped                            # still tapped from first swing
```

- [ ] **Step 2: FAIL.** **Step 3: Implement** — `attack` legal only in combat & once per combat instance; validates every attacker: on battlefield, creature, untapped, not sick (arrived_turn < turn or haste). Effects: fire `combat_begin` (once per combat, before damage), fire `attack` (ctx = attackers list, so `per_attacker`/`per_equipped_attacker` resolve), tap attackers, sum `effective_power` per attacker into focus-fire opponent (per-attacker: apply to first living opponent at damage time; commander adds to `cmdr_damage`), emit log with total. `pass` in combat: if `extra_combats > 0`, decrement, reset the per-combat attack flag, stay in `combat`; else go to `main2`.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add combat with sickness, focus-fire, extra combats`

---

### Task 10: Mulligan engine

**Spec:** §Engine (Mulligan) — every pin: London, EDH free-first, mana-source window + 2-land floor, ≤5 auto-keep, deterministic bottoming.

**Files:** Modify `goldfish/engine.py`; test `tests/goldfish/test_mulligan.py` (new).

- [ ] **Step 1: Failing tests**

```python
# tests/goldfish/test_mulligan.py
from goldfish.engine import (new_game, step, auto_mulligan, keep_decision,
                             MulliganRules)
from tests.goldfish.test_engine import mini_cards


def test_keep_decision_pins():
    cards = mini_cards()
    rules = MulliganRules()                     # defaults: sources 3..5, free_first True
    rocks0 = keep_decision(["Plains"] * 2 + ["Bear"] * 5, cards, rules, hand_size=7)
    assert rocks0 is False                      # 2 sources < min 3
    ok = keep_decision(["Plains"] * 3 + ["Bear"] * 4, cards, rules, hand_size=7)
    assert ok is True
    flood = keep_decision(["Plains"] * 6 + ["Bear"], cards, rules, hand_size=7)
    assert flood is False                       # 6 > max 5
    small = keep_decision(["Plains"] * 6, cards, rules, hand_size=5)
    assert small is True                        # <=5 cards always kept


def test_two_real_lands_floor():
    cards = mini_cards()
    cards["Signet"] = SimCard(data=make_data("Signet", cost=parse_cost("{2}"),
                              types=frozenset({"artifact"}), produces={"C": 1}),
                              ann=None, scope_class=None)
    rules = MulliganRules()
    hand = ["Plains", "Signet", "Signet", "Signet"] + ["Bear"] * 3
    assert keep_decision(hand, cards, rules, hand_size=7) is False  # 1 land < floor 2


def test_london_bottoming_deterministic():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=11)
    auto_mulligan(g, MulliganRules())
    assert g.phase == "main1" and g.turn == 1
    assert len(g.hand) + len([1]) >= 1          # kept some hand
    g2 = new_game(cards, ["Plains"] * 50 + ["Bear"] * 49, "Boss", seed=11)
    auto_mulligan(g2, MulliganRules())
    assert g.hand == g2.hand and g.log == g2.log  # byte-identical decisions
```

- [ ] **Step 2: FAIL.** **Step 3: Implement**

```python
@dataclass
class MulliganRules:
    min_sources: int = 3
    max_sources: int = 5
    lands_only: bool = False
    free_first: bool = True          # EDH free mulligan
    min_real_lands: int = 2


def _count_sources(hand, cards, rules):
    lands = sum(1 for n in hand if cards[n].is_land)
    if rules.lands_only:
        return lands, lands
    rocks = sum(1 for n in hand
                if not cards[n].is_land and cards[n].data.produces
                and cards[n].mv <= 2)
    return lands + rocks, lands


def keep_decision(hand, cards, rules, hand_size) -> bool:
    if hand_size <= 5:
        return True
    sources, lands = _count_sources(hand, cards, rules)
    return (rules.min_sources <= sources <= rules.max_sources
            and lands >= rules.min_real_lands)


def bottom_order(hand, cards, rules, n_bottom) -> list:
    """Pinned: bottom excess mana sources above max (lands first), then
    highest-MV spells; ties broken by name for determinism."""
    hand = list(hand)
    to_bottom = []
    sources, _ = _count_sources(hand, cards, rules)
    excess = max(0, sources - rules.max_sources)
    lands_sorted = sorted([n for n in hand if cards[n].is_land])
    for n in lands_sorted[:excess]:
        if len(to_bottom) < n_bottom:
            to_bottom.append(n); hand.remove(n)
    spells = sorted([n for n in hand if not cards[n].is_land],
                    key=lambda n: (-cards[n].mv, n))
    for n in spells:
        if len(to_bottom) < n_bottom:
            to_bottom.append(n); hand.remove(n)
    for n in sorted(hand):                        # lands as last resort
        if len(to_bottom) < n_bottom:
            to_bottom.append(n); hand.remove(n)
    return to_bottom


def auto_mulligan(g: Game, rules: MulliganRules, max_mulls: int = 3):
    """London mulligan loop, then enter turn 1 main1."""
    while True:
        hand_size = 7 - _effective_mulls(g, rules)
        if keep_decision(g.hand, g.cards, rules, hand_size) or hand_size <= 4:
            bottoms = bottom_order(g.hand, g.cards, rules, len(g.hand) - hand_size)
            _apply_keep(g, bottoms)
            break
        _apply_mulligan(g, rules)
    _begin_turn_one(g)
```

`_apply_mulligan` shuffles hand back (append then `g.rng.shuffle`), draws 7,
increments counters honoring `free_first`; `_apply_keep` moves bottoms to the
bottom of the library in order and logs `kept N (bottomed: ...)`. Interactive
variants are the `{"type": "mulligan"}` / `{"type": "keep", "bottom": [...]}`
actions in `step` — validate bottom count = effective mulligans taken.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add London mulligan with pinned keep/bottom rules`

---

### Task 11: Combo detection + wins

**Spec:** §Engine (Combo detection) — read the whole block; every clause there is a test.

**Files:** Modify `goldfish/engine.py`; test `tests/goldfish/test_engine.py`.

- [ ] **Step 1: Failing tests**

```python
def combo_setup(wins=False):
    cards = mini_cards()
    cards["PieceA"] = SimCard(data=make_data("PieceA", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature"}), power=2, toughness=2),
                              ann=None, scope_class=None)
    cards["PieceB"] = SimCard(data=make_data("PieceB", cost=parse_cost("{3}{R}"),
                              types=frozenset({"creature"}), power=3, toughness=3),
                              ann=None, scope_class=None)
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1,
                 combos=[{"cards": ["PieceA", "PieceB"], "wins": wins}])
    g.phase = "main1"; g.turn = 3
    return cards, g


def test_assembled_counts_battlefield():
    cards, g = combo_setup()
    g.hand[:] = ["PieceB"]
    g.new_perm("PieceA")                          # A already deployed
    for _ in range(4):
        g.new_perm("Mountain")
    from goldfish.engine import check_combos
    check_combos(g)
    assert g.combo_assembled_turn[0] == 3
    assert g.combo_castable_turn[0] == 3          # B's {3}{R} payable, A costs 0


def test_castable_includes_commander_tax():
    cards, g = combo_setup()
    g.combos = [["PieceA", "Boss"]]
    g.combo_assembled_turn = [None]; g.combo_castable_turn = [None]; g.combo_wins = [False]
    g.commander_casts = 2                          # tax {4}
    g.hand[:] = ["PieceA"]                         # Boss in command zone
    for _ in range(6):
        g.new_perm("Mountain")
    from goldfish.engine import check_combos
    check_combos(g)
    assert g.combo_assembled_turn[0] == 3
    # need {2}{R} + {2}{R}+{4} tax = 9 mv total vs 6 mountains:
    assert g.combo_castable_turn[0] is None


def test_wins_ends_game_and_counts_casts():
    cards, g = combo_setup(wins=True)
    g.hand[:] = ["PieceA", "PieceB"]
    for _ in range(6):
        g.new_perm("Mountain")
    from goldfish.engine import check_combos
    check_combos(g)
    assert g.won_turn == 3
    assert g.spells_cast_this_turn == 2            # pieces logged as cast
```

- [ ] **Step 2: FAIL.** **Step 3: Implement** — `check_combos(g)` called at the end of each main phase (wire the call into `step`'s `pass` handling when leaving main1/main2, and after any hand/battlefield mutation inside main phases). Accessible = hand ∪ command ∪ battlefield names. Castable check: joint Cost = sum of costs of pieces not on battlefield (command-zone pieces add tax); reuse `_payment_plan` with a synthetic combined Cost. On `wins`: set `won_turn`, log each undeployed piece as cast (`g.spells_cast_this_turn += len(...)`, emit lines), mark game over (a `g.won_turn is not None` guard makes `step` accept only `pass` afterward and batch loops stop).

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add passive combo detection with wins early-exit`

---

### Task 12: Greedy policy

**Spec:** §Policy — all nine rules + tutor targeting + reason strings. Every rule number below matches the spec's numbering; keep it that way.

**Files:** Create `goldfish/policy.py`; test `tests/goldfish/test_policy.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/goldfish/test_policy.py
from goldfish.policy import choose_action
from goldfish.engine import new_game, step
from tests.goldfish.test_engine import mini_cards, annotated, started
from goldfish.cards import SimCard, parse_cost
from tests.goldfish.test_cards import make_data


def drive_main(g, max_actions=30):
    """Run policy until it passes out of the current phase."""
    taken = []
    for _ in range(max_actions):
        action, reason = choose_action(g)
        taken.append((action, reason))
        step(g, action)
        if action["type"] == "pass":
            break
    return taken


def test_equipment_before_commander_when_both_payable():
    cards = mini_cards()
    annotated(cards, "Boss", {"name": "Boss", "triggers": [
        {"on": "etb", "do": "attach_from_board"}]})
    g = started(cards, ["Plains"] * 40, hand=["Hammer"])   # Hammer {1}, Boss {2}{R}
    for _ in range(4):
        g.new_perm("Mountain")
    taken = drive_main(g)
    order = [a["card"] for a, _ in taken if a["type"] == "cast"]
    assert order.index("Hammer") < order.index("Boss")
    boss_perm = next(p for p in g.battlefield if p.name == "Boss")
    assert boss_perm.attached                              # ETB attached the Hammer
    reasons = [r for a, r in taken if a["type"] == "cast" and a["card"] == "Hammer"]
    assert "rule 4" in reasons[0]


def test_commander_first_when_equipment_would_starve_it():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=["Hammer"])
    for _ in range(3):
        g.new_perm("Mountain")                              # exactly {2}{R}
    taken = drive_main(g)
    casts = [a["card"] for a, _ in taken if a["type"] == "cast"]
    assert casts and casts[0] == "Boss"                     # guard: Boss stays payable


def test_tap_producer_withheld_from_attack():
    cards = mini_cards()
    cards["Krenko"] = SimCard(data=make_data("Krenko", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature"}), power=3, toughness=3),
                              ann=None, scope_class=None)
    annotated(cards, "Krenko", {"name": "Krenko", "activated": [
        {"cost": "{T}", "do": "create_token", "power": 1, "toughness": 1, "count": 2}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    g.turn = 4
    k = g.new_perm("Krenko"); k.arrived_turn = 3
    b = g.new_perm("Bear"); b.arrived_turn = 3
    # main1 -> combat -> main2 full turn under policy:
    for _ in range(40):
        action, _ = choose_action(g)
        step(g, action)
        if g.phase == "end":
            break
    assert k.tapped and any(p.is_token for p in g.battlefield)  # activated post-combat
    assert g.opponents == [38]                                  # only Bear attacked
```

- [ ] **Step 2: FAIL.** **Step 3: Implement `goldfish/policy.py`** — single entry:

```python
def choose_action(g) -> tuple[dict, str]:
    """Greedy policy per spec §Policy. Returns (action, reason). The action is
    always legal at call time. Falls through to {"type": "pass"}."""
```

Rule order inside, each returning early with its reason string (`"rule 1: play untapped land enabling a cast"` etc.):
1. Mulligan phase → apply `keep_decision`/`bottom_order` from engine.
2. `play_land` while drops remain (choose: any land enabling a currently
   unaffordable cast this turn, else first untapped, else first; sorted by name).
3. Cast static enablers / engine permanents (has statics or global-event triggers, not commander).
4. Cast rocks (`produces` non-land), then `ramp_land` spells.
5. Rule-4 guard + commander (as tested above). Compute with
   `can_pay(cost_a) and can_pay_after(cost_b)` — implement `can_pay_after` by
   simulating the payment plan on copies (use `Game.to_dict/from_dict` for the
   probe, never mutate the live game).
6. Remaining annotated spells cheapest-first; rituals (add_mana verbs) early
   only when hand cost > pool, else last.
7. Attach: free attaches first (equip_free active), then paid, target = the
   commander if on battlefield else highest effective_power creature.
8. Activate: non-token-producer activations when payable; token producers in
   main2 only (post-combat), unless a haste anthem is on the battlefield.
9. In combat: `attack` with all eligible except withheld `{T}` producers
   (withheld = has a `{T}` activated ability the policy can pay for in main2).
Fallback: `pass`.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add greedy policy with reasons and guards`

---

### Task 13: Metrics + statistics primitives

**Spec:** §Statistics, §v1 metrics. Formulas here are the contract.

**Files:** Create `goldfish/metrics.py`; test `tests/goldfish/test_metrics.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/goldfish/test_metrics.py
import math
from goldfish.metrics import wilson_ci, mcnemar_exact_p, paired_delta, GameRecord, aggregate


def test_wilson_ci_known_value():
    lo, hi = wilson_ci(80, 100)                  # p̂=0.8, n=100, z=1.96
    assert abs(lo - 0.7112) < 0.002 and abs(hi - 0.8661) < 0.002


def test_mcnemar_exact_p():
    # 10 discordant pairs, 9 one way: two-sided exact binomial
    p = mcnemar_exact_p(b=9, c=1)
    expected = 2 * sum(math.comb(10, k) for k in range(0, 2)) / 2 ** 10
    assert abs(p - min(1.0, expected)) < 1e-9


def test_paired_delta():
    a = [1, 1, 0, 1]; b = [0, 1, 0, 0]
    d = paired_delta(a, b)
    assert abs(d.mean - 0.5) < 1e-9 and d.n == 4 and d.ci_low < 0.5 < d.ci_high


def test_aggregate_censoring():
    recs = [GameRecord(commander_cast_turn=3), GameRecord(commander_cast_turn=None)]
    m = aggregate(recs, until_turn=8)
    cc = m["commander_cast"]
    assert cc["reached_pct"]["value"] == 0.5
    assert cc["median_among_reached"] == 3
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.**

```python
def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact binomial test on discordant pairs."""
    n, k = b + c, min(b, c)
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * tail)
```

`GameRecord` is a dataclass with every per-game observable (mulligans, kept-hand
lands/sources, commander_cast_turn, equipped_on_arrival: bool|None,
kill_turn, cmdr21_turn, table_lethal_turn, per-turn lists: damage, board_width,
total_power, casts, lands_played, mana_available, tokens_created,
trigger_fires: dict[(card,event,verb) -> int], static_active_turn: dict,
combo_assembled_turn/castable per combo, drawn_names: set). `aggregate(records,
until_turn)` builds the metrics JSON exactly as the spec's §v1 metrics lists,
with every proportion as `{"value": v, "ci": [lo, hi]}` and every turn-to-event
metric as `{"reached_pct": {...}, "median_among_reached": m, "histogram": {...}}`.
`paired_delta(a_values, b_values)` returns mean, sd, `ci_low/ci_high`
(±1.96·sd/√n), `significant: bool`, plus `mcnemar_p` when values are 0/1.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add metrics aggregation, Wilson CI, McNemar`

---

### Task 14: Batch runner + golden-seed test

**Spec:** §Architecture (determinism), acceptance criterion 1 and 2.

**Files:** Create `goldfish/runner.py`; test `tests/goldfish/test_runner.py`, `tests/goldfish/test_golden.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/goldfish/test_runner.py
from goldfish.runner import run_batch
from tests.goldfish.test_engine import mini_cards


def small_deck():
    return ["Plains"] * 15 + ["Mountain"] * 15 + ["Bear"] * 5 + ["Runner"] * 4


def test_batch_deterministic_and_seed_paired():
    cards = mini_cards()
    r1 = run_batch(cards, small_deck(), "Boss", n=20, seed=42, until_turn=6)
    r2 = run_batch(cards, small_deck(), "Boss", n=20, seed=42, until_turn=6)
    assert r1["metrics"] == r2["metrics"]
    r3 = run_batch(cards, small_deck(), "Boss", n=20, seed=43, until_turn=6)
    assert r1["metrics"] != r3["metrics"]


def test_run_plays_games_to_until_turn():
    cards = mini_cards()
    r = run_batch(cards, small_deck(), "Boss", n=5, seed=1, until_turn=6)
    assert r["n"] == 5
    assert r["metrics"]["commander_cast"]["reached_pct"]["value"] > 0
```

```python
# tests/goldfish/test_golden.py
from goldfish.runner import play_one_game
from tests.goldfish.test_engine import mini_cards
from tests.goldfish.test_runner import small_deck

def test_golden_log_byte_identical():
    cards = mini_cards()
    rec1, log1 = play_one_game(cards, small_deck(), "Boss",
                               game_seed=1234, until_turn=6)
    rec2, log2 = play_one_game(cards, small_deck(), "Boss",
                               game_seed=1234, until_turn=6)
    assert log1 == log2
    # Freeze the first 5 lines as the golden prefix once implemented; any
    # engine/policy change that alters them must update this list consciously.
    assert log1[:1] == [log1[0]]   # replaced with literal lines at implementation time
```

At implementation time, run the game once, paste the first ~10 actual log lines
into the test as a literal list, and assert equality. That literal paste is the
golden seal — the point of the test.

- [ ] **Step 2: FAIL.** **Step 3: Implement `goldfish/runner.py`**

```python
from .engine import new_game, step, auto_mulligan, derive_seed, MulliganRules
from .policy import choose_action
from .metrics import GameRecord, aggregate


def play_one_game(cards, deck, commander, game_seed, until_turn,
                  opponents=1, combos=None, rules=None) -> tuple[GameRecord, list]:
    g = new_game(cards, deck, commander, seed=game_seed,
                 opponents=opponents, combos=combos)
    auto_mulligan(g, rules or MulliganRules())
    guard = 0
    while g.turn <= until_turn and g.won_turn is None:
        action, _ = choose_action(g)
        step(g, action)
        guard += 1
        if guard > 500 * until_turn:
            raise RuntimeError("policy livelock — bug")
    return build_record(g, until_turn), g.log


def run_batch(cards, deck, commander, n, seed, until_turn,
              opponents=1, combos=None, rules=None) -> dict:
    records = []
    for i in range(n):
        rec, _ = play_one_game(cards, deck, commander,
                               game_seed=derive_seed(seed, i),
                               until_turn=until_turn, opponents=opponents,
                               combos=combos, rules=rules)
        records.append(rec)
    return {"n": n, "seed": seed, "until_turn": until_turn,
            "metrics": aggregate(records, until_turn), "records": records}
```

`build_record(g, until_turn)` extracts the `GameRecord` fields from the game's
counters/log; add the per-turn observers into `engine.py` where needed (a
`g.turn_stats` dict appended at each turn end) as part of this task.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add batch runner and golden-seed test`

---

### Task 15: A/B — alignment, paired stats, correlation

**Spec:** §Statistics — position-stable alignment is THE point of this task; acceptance criteria 6.

**Files:** Modify `goldfish/runner.py`; test `tests/goldfish/test_runner.py`.

- [ ] **Step 1: Failing tests**

```python
from goldfish.runner import align_decks, run_ab


def test_align_decks_perturbs_exactly_k_positions():
    a = ["Plains"] * 10 + ["Bear"] * 5 + ["Hammer"] * 5
    b = ["Plains"] * 10 + ["Bear"] * 5 + ["Hammer"] * 4 + ["Runner"]
    a2, b2 = align_decks(a, b)
    assert sorted(a2) == sorted(a) and sorted(b2) == sorted(b)
    diffs = sum(1 for x, y in zip(a2, b2) if x != y)
    assert diffs == 1                              # 1-card swap → 1 position


def test_ab_identical_decks_zero_delta():
    cards = mini_cards()
    d = small_deck()
    r = run_ab(cards, d, list(d), "Boss", n=10, seed=42, until_turn=6)
    for name, metric in r["deltas"].items():
        assert abs(metric["mean"]) < 1e-12, name
    assert r["pair_correlation"] == 1.0


def test_ab_one_swap_high_correlation():
    cards = mini_cards()
    a = small_deck()
    b = list(a); b[b.index("Bear")] = "Runner"
    r = run_ab(cards, a, b, "Boss", n=30, seed=42, until_turn=6)
    assert r["pair_correlation"] > 0.5             # the acceptance floor
```

- [ ] **Step 2: FAIL.** **Step 3: Implement**

```python
def align_decks(deck_a: list, deck_b: list) -> tuple[list, list]:
    """Shared cards at identical indices; swapped cards occupy the leftover slots.
    Deterministic: process in sorted multiset order."""
    from collections import Counter
    ca, cb = Counter(deck_a), Counter(deck_b)
    shared = ca & cb
    only_a = list((ca - shared).elements()); only_a.sort()
    only_b = list((cb - shared).elements()); only_b.sort()
    base = list(shared.elements()); base.sort()
    a2 = base + only_a
    b2 = base + only_b
    # pad the shorter unique tail so lengths match (unequal deck sizes rejected upstream)
    return a2, b2
```

`run_ab` runs both aligned decks with identical `derive_seed(seed, i)` per index,
collects both record lists, computes per-metric paired deltas (`paired_delta`
from metrics.py over the per-game scalar for each metric: commander cast turn
reached-by-T flags, kill flags, kept-hand lands, etc.), the Pearson correlation
of per-game total-damage-by-until_turn between arms (this is the reported
`pair_correlation`), and carries both honesty reports. Same-commander guard:
raise `ValueError` unless `allow_different_commanders=True`.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add paired A/B with position-stable alignment`

---

### Task 16: Closed-form odds

**Spec:** §MCP tools (`goldfish_odds`), acceptance criterion 3.

**Files:** Create `goldfish/odds.py`; test `tests/goldfish/test_odds.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/goldfish/test_odds.py
import math
from goldfish.odds import odds_at_least, odds_groups
from goldfish.runner import run_batch
from tests.goldfish.test_engine import mini_cards


def test_single_group_matches_closed_form():
    # ≥1 of 4 copies in top 10 of 99: 1 - C(95,10)/C(99,10)
    expected = 1 - math.comb(95, 10) / math.comb(99, 10)
    assert abs(odds_at_least(99, 10, copies=4, min_successes=1) - expected) < 1e-12


def test_groups_joint_probability():
    # P(≥1 A and ≥1 B), 3 As and 2 Bs in 40, draw 7 — brute-force cross-check
    from itertools import combinations
    deck = ["A"] * 3 + ["B"] * 2 + ["x"] * 35
    hits = sum(1 for hand in combinations(range(40), 7)
               if any(deck[i] == "A" for i in hand) and any(deck[i] == "B" for i in hand))
    expected = hits / math.comb(40, 7)
    got = odds_groups(40, 7, [{"copies": 3, "min_successes": 1},
                              {"copies": 2, "min_successes": 1}])
    assert abs(got - expected) < 1e-12


def test_sim_matches_hypergeometric_within_1pct():
    # Acceptance criterion 3: ≥1 Runner in opening 7 (post-free-mulligan decks skew
    # this, so run with mulligans disabled via wide keep window)
    from goldfish.engine import MulliganRules
    cards = mini_cards()
    deck = ["Plains"] * 30 + ["Runner"] * 9
    r = run_batch(cards, deck, "Boss", n=4000, seed=9, until_turn=1,
                  rules=MulliganRules(min_sources=0, max_sources=7, min_real_lands=0))
    seen = sum(1 for rec in r["records"] if "Runner" in rec.opening_hand) / 4000
    expected = odds_at_least(39, 7, copies=9, min_successes=1)
    assert abs(seen - expected) < 0.01
```

- [ ] **Step 2: FAIL.** **Step 3: Implement `goldfish/odds.py`**

```python
import math
from itertools import product


def odds_at_least(deck_size: int, draws: int, copies: int, min_successes: int = 1) -> float:
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
```

Also add `opening_hand` to `GameRecord` (set in `build_record` from the kept
hand before turn 1) — needed by the sanity test above.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add exact single/multi-group hypergeometric odds`

---

### Task 17: Auto-derivation from Scryfall data

**Spec:** §Card annotation DSL (auto-derived list, fetch semantics), D9. The classifier consumes Scryfall card JSON as returned by the existing `/cards/collection` code in `server.py` — study `_scryfall_post` usage in `scryfall_price_list` first.

**Files:** Create `goldfish/autoderive.py`; test `tests/goldfish/test_autoderive.py` with inline Scryfall-shaped dict fixtures (no network).

- [ ] **Step 1: Failing tests** (fixtures are trimmed real Scryfall JSON — the executor pulls each card's actual JSON once via `curl https://api.scryfall.com/cards/named?exact=...` and trims to the used fields: `name, type_line, mana_cost, oracle_text, power, toughness, keywords, produced_mana, card_faces`)

```python
# tests/goldfish/test_autoderive.py
from goldfish.autoderive import derive

PLAINS = {"name": "Plains", "type_line": "Basic Land — Plains",
          "oracle_text": "({T}: Add {W}.)", "produced_mana": ["W"], "keywords": []}
GUILDGATE = {"name": "Boros Guildgate", "type_line": "Land — Gate",
             "oracle_text": "Boros Guildgate enters the battlefield tapped.\n{T}: Add {R} or {W}.",
             "produced_mana": ["R", "W"], "keywords": []}
SOL_RING = {"name": "Sol Ring", "type_line": "Artifact", "mana_cost": "{1}",
            "oracle_text": "{T}: Add {C}{C}.", "produced_mana": ["C"], "keywords": []}
DIVINATION = {"name": "Divination", "type_line": "Sorcery", "mana_cost": "{2}{U}",
              "oracle_text": "Draw two cards.", "keywords": []}
SWORDS = {"name": "Swords to Plowshares", "type_line": "Instant", "mana_cost": "{W}",
          "oracle_text": "Exile target creature. Its controller gains life equal to its power.",
          "keywords": []}
SAGA = {"name": "Summon: Kujata", "type_line": "Enchantment — Saga",
        "mana_cost": "{1}{G}", "oracle_text": "(As this Saga enters...)", "keywords": []}


def test_land_derivation():
    d = derive([PLAINS, GUILDGATE])
    assert d["Plains"].card.data.produces == {"W": 1}
    assert d["Boros Guildgate"].card.data.enters_tapped is True
    assert d["Boros Guildgate"].card.data.produces == {"R": 1, "W": 1}


def test_rock_quantity():
    d = derive([SOL_RING])
    assert d["Sol Ring"].card.data.produces == {"C": 2}
    assert d["Sol Ring"].auto_annotated is True


def test_draw_spell_gets_trigger():
    d = derive([DIVINATION])
    ann = d["Divination"].card.ann
    assert ann and ann.triggers[0].do == "draw" and ann.triggers[0].count == 2


def test_out_of_scope_classes():
    d = derive([SWORDS, SAGA])
    assert d["Swords to Plowshares"].card.scope_class == "interaction_removal"
    assert d["Summon: Kujata"].card.scope_class == "unmodeled_other"
    assert d["Swords to Plowshares"].needs_annotation is False   # classified, not asked


def test_unrecognized_flagged_for_annotation():
    weird = {"name": "Puresteel Paladin", "type_line": "Creature — Human Knight",
             "mana_cost": "{W}{W}", "power": "2", "toughness": "2",
             "oracle_text": "Metalcraft — ... equip abilities you activate cost {0}...",
             "keywords": []}
    d = derive([weird])
    assert d["Puresteel Paladin"].needs_annotation is True
    assert d["Puresteel Paladin"].card.scope_class is None
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** `derive(scryfall_cards: list[dict]) -> dict[name, Derived]` where `Derived = (card: SimCard, auto_annotated: bool, needs_annotation: bool, oracle: str)`. Classification order (first match wins):

1. Land: parse `produced_mana` + `enters the battlefield tapped` (unconditional line → `enters_tapped=True`; `unless`/`if` → common-case False + `approx` note). Fetch land: oracle matches `Sacrifice .*: Search your library for .* land .* onto the battlefield` → auto-annotation `{on: etb→ none}`; model per spec: give it an `activated` `{cost: "{T}", do: "ramp_land"}` plus `sac_self=True` behavior — v1 pins fetches as: activating removes the fetch permanent and runs `ramp_land` (implement `sac_self` as a `Derived`-set flag the engine honors in `activate`).
2. Mana rock/dork: `{T}: Add` in oracle + not land → `produces` with quantity = count of mana symbols in the Add clause.
3. Draw spell: oracle exactly `Draw (a|two|three|N) cards?\.` (whole-text match after reminder-text stripping) → `cast: draw N`.
4. Ramp spell: `Search your library for .* land .* onto the battlefield` → `cast: ramp_land` (count 2 if "two").
5. Equipment: `type_line` contains Equipment → parse `Equip {N}` and `Equipped creature gets +X/+Y` into grants.
6. Out-of-scope patterns (D9): `destroy target|exile target` → `interaction_removal`; `destroy all|exile all` → `interaction_wipe`; `counter target` → `interaction_counter`; `hexproof|indestructible|protection` granting instants → `protection`; `each opponent may|vote` → `political`; Saga type, `{X}` in mana_cost, `flip a coin`, `sacrifice a` cost → `unmodeled_other`.
7. Vanilla creature (has P/T, oracle empty or only keywords) → no annotation needed.
8. Everything else → `needs_annotation=True`.

Double-faced cards: use `card_faces[0]` when top-level `mana_cost`/`oracle_text` are absent; note the back face is ignored (honesty note).

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add Scryfall auto-derivation with D9 scope classes`

---

### Task 18: Server tools — `goldfish_annotate` + `goldfish_odds`

**Spec:** §MCP tools, D4. Follow `server.py`'s existing conventions exactly: pydantic input model + `@mcp.tool(name=...)` + formatted-string return + error-helper style.

**Files:** Modify `server.py`; test `tests/goldfish/test_server_tools.py` (call the tool functions directly, monkeypatching the Scryfall fetch).

- [ ] **Step 1: Failing tests**

```python
# tests/goldfish/test_server_tools.py
import json
import pytest
import server as srv


async def test_goldfish_odds_flat_and_groups():
    out = await srv.goldfish_odds(srv.GoldfishOddsInput(
        deck_size=99, draws=7, copies=10, min_successes=1))
    assert "%" in out
    out2 = await srv.goldfish_odds(srv.GoldfishOddsInput(
        deck_size=99, draws=12,
        groups=[{"copies": 1, "min_successes": 1}, {"copies": 1, "min_successes": 1}]))
    assert "%" in out2


async def test_goldfish_annotate_reports_gaps(monkeypatch):
    from tests.goldfish.test_autoderive import PLAINS, SOL_RING
    puresteel = {"name": "Puresteel Paladin", "type_line": "Creature — Human Knight",
                 "mana_cost": "{W}{W}", "power": "2", "toughness": "2",
                 "oracle_text": "Metalcraft — equip abilities cost {0}", "keywords": []}

    async def fake_fetch(names):
        return [PLAINS, SOL_RING, puresteel], []
    monkeypatch.setattr(srv, "_goldfish_fetch_cards", fake_fetch)
    out = await srv.goldfish_annotate(srv.GoldfishAnnotateInput(
        deck="1 Plains\n1 Sol Ring\n1 Puresteel Paladin"))
    assert "Puresteel Paladin" in out          # needs annotation, oracle shown
    assert "Sol Ring" not in out.split("Needs annotation")[1] if "Needs annotation" in out else True
```

- [ ] **Step 2: FAIL.** **Step 3: Implement in `server.py`** (new section `# ═══ GOLDFISH ═══` at the bottom, before the entrypoint):

- `_goldfish_fetch_cards(names) -> (cards_json, not_found)`: batch `POST /cards/collection` in chunks of 75 via existing `_scryfall_post`, requesting identifiers by name; in-process dict cache `_GOLDFISH_CARD_CACHE`.
- `_goldfish_load_deck(deck: str) -> (deck_names: list, commander: str)`: if the string matches the Archidekt URL/ID pattern reuse `_parse_deck_id` + `_archidekt_get` + `_archidekt_in_deck_cards`; else `_parse_decklist`. Commander = card flagged commander (Archidekt category) or the sole legendary creature; ambiguous → error listing candidates.
- `GoldfishOddsInput(BaseModel)`: `deck_size: int`, `draws: int`, `copies: int | None`, `min_successes: int = 1`, `groups: list[dict] | None`. Tool formats: `P(≥1 of 10 copies in top 7 of 99) = 54.28%  [exact hypergeometric]`.
- `GoldfishAnnotateInput(BaseModel)`: `deck: str`. Tool: load deck → fetch → `autoderive.derive` → output three sections: `## Auto-derived (N cards)` one-line each; `## Out of scope (M cards)` grouped by D9 class with one-line explanation; `## Needs annotation (K cards)` — name + mv + type + full oracle text + a reminder of the registry vocabulary and an example annotation. This is the section Claude answers.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add goldfish_annotate and goldfish_odds tools`

---

### Task 19: Server tools — `goldfish_run` + `goldfish_ab`

**Spec:** §MCP tools, §Statistics (scope banner!), §Concurrency (to_thread + semaphore + n cap), D4.

**Files:** Modify `server.py`; test `tests/goldfish/test_server_tools.py`.

- [ ] **Step 1: Failing tests**

```python
async def test_goldfish_run_output_shape(monkeypatch):
    _patch_fetch_minideck(monkeypatch)     # helper: fake fetch returning mini fixtures
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=20, seed=42, until_turn=6))
    assert "```json" in out                # embedded metrics JSON (D4)
    assert "run_id" in out
    assert "Honesty report" in out
    payload = json.loads(out.split("```json")[1].split("```")[0])
    assert payload["n"] == 20
    cc = payload["metrics"]["commander_cast"]["reached_pct"]
    assert "ci" in cc                      # every proportion carries a CI


async def test_goldfish_run_n_capped(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    with pytest.raises(Exception):
        await srv.goldfish_run(srv.GoldfishRunInput(deck=MINI_DECK_TEXT, n=50_000))


async def test_goldfish_ab_scope_banner(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_ab(srv.GoldfishAbInput(
        deck_a=MINI_DECK_TEXT, deck_b=MINI_DECK_TEXT, n=10, seed=1))
    first_paragraph = out.split("\n\n")[0]
    assert "out of scope" in first_paragraph.lower()
    assert "pair correlation" in out.lower()
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.** First define the shared test
helpers at the top of `tests/goldfish/test_server_tools.py` (imported from
there by `test_interactive.py` and `test_acceptance.py`): `MINI_DECK_TEXT` — a
decklist string naming the Task 4 mini-card pool ("20 Plains\n17 Mountain\n30
Bear\n20 Runner\n12 Hammer\n1 Boss") — and `_patch_fetch_minideck(monkeypatch)`
— monkeypatches `srv._goldfish_fetch_cards` to return Scryfall-shaped dicts for
those six names (reuse the Task 17 fixture style) and, if `goldfish_annotate`'s
deriver output lacks it, marks "Boss" as commander.

```python
GOLDFISH_MAX_N = 20_000
_GOLDFISH_SEMAPHORE = asyncio.Semaphore(2)


async def _run_sim_offloop(fn, *args, **kw):
    async with _GOLDFISH_SEMAPHORE:
        return await asyncio.to_thread(fn, *args, **kw)
```

`GoldfishRunInput`: `deck: str`, `annotations: list[dict] = []`, `combos: list = []`, `n: int = 1000` (validator ≤ GOLDFISH_MAX_N), `seed: int = 42`, `until_turn: int = 10`, `opponents: int = 1`, `mulligan: dict = {}` (maps onto `MulliganRules` fields). Flow: load deck → fetch → derive → `validate_annotations` (AnnotationError → return its message verbatim, it is the self-correction signal) → merge client annotations over derived → `_run_sim_offloop(run_batch, ...)` → store `(inputs, result)` in `_GOLDFISH_RUNS` LRU (OrderedDict, cap 50) under `run_id = report.run_code(inputs)` → format output: header line (deck, commander, n, seed, run_id), 8-line text summary of headline metrics, ```json metrics block```, honesty report (three sections per spec §v1 metrics), footer `Full report: goldfish_report("<run_id>")` + `report_url` when hosted mode on.

`goldfish_ab`: same loading twice, same-commander guard, `align_decks`, `_run_sim_offloop(run_ab, ...)`, output opens with the SCOPE BANNER paragraph built exactly per spec §Statistics (counts of simulated vs out-of-scope changed cards by class, per-deck out-of-scope totals, the "do not read results as 'cut your removal'" sentence verbatim), then per-metric delta table (`mean ± ci  [significant?]`), `pair correlation: 0.87`, sequencing caveat line, honesty report.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add goldfish_run and goldfish_ab tools`

---

### Task 20: Interactive tools — store, start/step/state

**Spec:** §Interactive mode (all of it), §Concurrency (uuid4, LRU+TTL), D3, D10.

**Files:** Modify `server.py`; test `tests/goldfish/test_interactive.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/goldfish/test_interactive.py
import json
import server as srv


async def _start(monkeypatch, **kw):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_start(srv.GoldfishStartInput(
        deck=MINI_DECK_TEXT, seed=7, **kw))
    return json.loads(out)


async def test_start_step_state_roundtrip(monkeypatch):
    payload = await _start(monkeypatch)
    gid = payload["game_id"]
    assert payload["state"]["phase"] in ("mulligan", "main1")
    out = await srv.goldfish_step(srv.GoldfishStepInput(game_id=gid))
    stepped = json.loads(out)
    assert stepped["applied"]["reason"]            # policy chose, reason present
    assert stepped["legal_actions"]


async def test_illegal_action_rejected_unmutated(monkeypatch):
    payload = await _start(monkeypatch)
    gid = payload["game_id"]
    before = json.loads(await srv.goldfish_state(
        srv.GoldfishStateInput(game_id=gid)))["state"]
    out = await srv.goldfish_step(srv.GoldfishStepInput(
        game_id=gid, action={"type": "cast", "card": "Nonexistent"}))
    assert "illegal" in out.lower() or "error" in out.lower()
    after = json.loads(await srv.goldfish_state(
        srv.GoldfishStateInput(game_id=gid)))["state"]
    assert before == after


async def test_until_fast_forward_and_resume(monkeypatch):
    payload = await _start(monkeypatch)
    gid = payload["game_id"]
    out = json.loads(await srv.goldfish_step(
        srv.GoldfishStepInput(game_id=gid, until="turn:4")))
    assert out["state"]["turn"] == 4
    blob = out["state"]
    srv._GOLDFISH_GAMES.clear()                     # simulate eviction
    out2 = await srv.goldfish_start(srv.GoldfishStartInput(resume_state=blob))
    resumed = json.loads(out2)
    assert resumed["state"]["turn"] == 4
    assert resumed["state"]["log"] == blob["log"]


async def test_interactive_mulligan(monkeypatch):
    payload = await _start(monkeypatch, interactive_mulligan=True)
    assert payload["state"]["phase"] == "mulligan"
    kinds = {a["type"] for a in payload["legal_actions"]}
    assert kinds == {"mulligan", "keep"}
```

- [ ] **Step 2: FAIL.** **Step 3: Implement.**

```python
_GOLDFISH_GAMES: "OrderedDict[str, dict]" = OrderedDict()   # gid -> {"game": Game, "cards": ..., "touched": time.time()}
GOLDFISH_MAX_GAMES = 100
GOLDFISH_GAME_TTL = 24 * 3600


def _games_evict():
    now = time.time()
    for gid in [g for g, v in _GOLDFISH_GAMES.items()
                if now - v["touched"] > GOLDFISH_GAME_TTL]:
        del _GOLDFISH_GAMES[gid]
    while len(_GOLDFISH_GAMES) > GOLDFISH_MAX_GAMES:
        _GOLDFISH_GAMES.popitem(last=False)
```

`goldfish_start`: either `deck` (+ optional annotations/combos/interactive_mulligan) or `resume_state` blob (rebuild card pool: the blob's `resume` key carries deck text + annotations; re-fetch/derive, then `Game.from_dict`). `game_id = str(uuid.uuid4())`. When not interactive_mulligan, run `auto_mulligan` immediately. Response JSON: `{"game_id", "state": game.to_dict() + {"resume": {...}}, "legal_actions": legal_actions(g)}`.

`goldfish_step`: resolve game (unknown id → error naming `goldfish_start(resume_state=...)` as the recovery); with `action` → `step` inside try/except IllegalAction returning the reason; without → loop `choose_action`+`step` once (or to the `until` boundary: parse `"turn:N"`/`"phase:X"`/`"end"`, guard-capped like the runner). Response includes `applied: {action, reason}` (reason `null` for user actions), new state, legal actions, and the log lines added this call.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add interactive start/step/state tools with store`

---

### Task 21: Reports — HTML/SVG, run codes, `goldfish_report`, hosted route

**Spec:** §Run reports — every clause. **Before writing any chart code, invoke the `dataviz` skill** and follow its palette/mark guidance for the SVG histograms and delta whiskers.

**Files:** Create `goldfish/report.py`; modify `server.py`, `Dockerfile`; test `tests/goldfish/test_report.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/goldfish/test_report.py
from goldfish.report import run_code, render_report


def test_run_code_content_addressed():
    inputs = {"deck": "1 Plains", "annotations": [], "seed": 42, "n": 100,
              "until_turn": 8, "combos": [], "engine_version": "0.1.0"}
    assert run_code(inputs) == run_code(dict(inputs))          # stable
    assert run_code(inputs) != run_code({**inputs, "seed": 43})
    assert len(run_code(inputs)) == 13 and run_code(inputs).isalnum()


def test_report_self_contained(sample_run_result):
    html = render_report(sample_run_result)                     # fixture from runner
    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html.replace(
        "https://kautiontape.com", "")                          # no external fetches
    assert "<svg" in html
    for key in ("seed", "run code", "engine"):
        assert key in html.lower()
    assert "re-run with these inputs" in html.lower()
```

(`sample_run_result` is a pytest fixture in this test file that calls
`run_batch` on the mini deck, n=30.)

- [ ] **Step 2: FAIL.** **Step 3: Implement.**

```python
def run_code(inputs: dict) -> str:
    canon = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canon.encode()).digest()
    return base64.b32encode(digest[:8]).decode().rstrip("=").lower()
```

`render_report(result, inputs, ab=False)` builds sections: fingerprint header
table; stat tiles (proportions with CI ranges printed); `_svg_hist(counter,
title)` and `_svg_reached_curve(pcts_by_turn, title)` helpers emitting fixed-size
(640×240) SVG with axis labels as `<text>`; trigger-fire table; honesty report;
for A/B: delta rows with CI whisker SVGs + the scope banner verbatim at top.
Styling: single `<style>` block, `prefers-color-scheme: dark` overrides. Every
number is read from the metrics dict — the function takes no liberty to
recompute anything.

`server.py`: `_GOLDFISH_RUNS` LRU keeps `(inputs, result, html)` per run_id
(html rendered lazily on first `goldfish_report` call). `goldfish_report(run_id)`
returns the HTML string (unknown id → error listing live run_ids). Hosted mode:

```python
GOLDFISH_REPORT_DIR = os.environ.get("GOLDFISH_REPORT_DIR")
GOLDFISH_PUBLIC_BASE_URL = os.environ.get("GOLDFISH_PUBLIC_BASE_URL", "").rstrip("/")

if GOLDFISH_REPORT_DIR:
    @mcp.custom_route("/goldfish/r/{code}", methods=["GET"])
    async def goldfish_report_page(request):
        from starlette.responses import HTMLResponse, PlainTextResponse
        code = request.path_params["code"]
        if not re.fullmatch(r"[a-z0-9]{13}", code):
            return PlainTextResponse("bad code", status_code=404)
        path = os.path.join(GOLDFISH_REPORT_DIR, f"{code}.html")
        if not os.path.exists(path):
            return PlainTextResponse("unknown run code", status_code=404)
        with open(path) as f:
            return HTMLResponse(f.read())
```

Persist on run completion when configured (write file + size-capped cleanup:
delete oldest files beyond 500). `goldfish_run`/`_ab` append
`report_url = f"{GOLDFISH_PUBLIC_BASE_URL}/goldfish/r/{run_id}"` when both env
vars are set. `Dockerfile`: add `COPY goldfish/ goldfish/` after the `server.py`
COPY. `docker-compose.yml`: document (comment) the two env vars + a volume for
the report dir.

- [ ] **Step 4: PASS. Step 5: Commit** — `goldfish: Add HTML reports, run codes, hosted route`

---

### Task 22: Acceptance sweep

**Spec:** §Acceptance criteria — this task turns the checklist into executable proof.

**Files:** Create `tests/goldfish/test_acceptance.py`; modify `server.py` (instructions), `README.md`.

- [ ] **Step 1: Write the acceptance tests**

```python
# tests/goldfish/test_acceptance.py
import time
import asyncio
import pytest


@pytest.mark.slow
def test_10k_games_under_30s():
    from goldfish.runner import run_batch
    from tests.goldfish.test_engine import mini_cards
    cards = mini_cards()
    deck = (["Plains"] * 20 + ["Mountain"] * 17 + ["Bear"] * 30 +
            ["Runner"] * 20 + ["Hammer"] * 12)          # 99 cards
    t0 = time.perf_counter()
    run_batch(cards, deck, "Boss", n=10_000, seed=1, until_turn=8)
    assert time.perf_counter() - t0 < 30


async def test_concurrent_tool_call_not_blocked(monkeypatch):
    """A quick tool answers while a sim churns (acceptance: concurrency)."""
    import server as srv
    _patch_fetch_minideck(monkeypatch)
    sim = asyncio.create_task(srv.goldfish_run(
        srv.GoldfishRunInput(deck=MINI_DECK_TEXT, n=5000, seed=1)))
    t0 = time.perf_counter()
    await srv.goldfish_odds(srv.GoldfishOddsInput(deck_size=99, draws=7, copies=8))
    quick_elapsed = time.perf_counter() - t0
    await sim
    assert quick_elapsed < 1.0
```

- [ ] **Step 2: Run the FULL suite** — `python -m pytest tests/ -q`. Walk the spec's §Acceptance criteria checklist item by item; each must map to a passing test written in Tasks 1–22. Any unmapped criterion gets its test added here before proceeding.

- [ ] **Step 3: Server instructions + README.** Append to the `FastMCP(instructions=...)` string: "For deck simulation questions (how fast, how consistent, what turn, did this swap help), use the goldfish tools: goldfish_annotate to prepare a deck's effect annotations, goldfish_run for seeded Monte Carlo stats, goldfish_ab for paired A/B of two lists, goldfish_odds for exact draw probabilities, goldfish_start/step/state to play an interactive seeded game. Goldfish sims measure speed and consistency only — they cannot value interaction (removal, counterspells); never present goldfish deltas as judgments of interaction cards. Publish run reports verbatim via goldfish_report; never hand-author the numbers." Add the tool table to `README.md` under a `### Goldfish — Deck Simulation` heading, matching the existing tables' format.

- [ ] **Step 4: Commit** — `goldfish: Add acceptance tests, instructions, README`

---

## Execution notes

**Dependency order:** Tasks 1→11 are sequential (each builds on the last). After Task 12, three tracks can run in parallel: {13,14,15} (stats), {16} (odds), {17} (autoderive). Tasks 18–21 depend on all tracks; 22 is last. With subagent-driven execution: run 1–12 as a chain, fan out 13–17, then chain 18–22.

**Golden-log discipline:** after Task 14, any change to engine/policy that alters the golden log must update the literal in `test_golden.py` in the same commit, with the diff called out in the commit message.

**The spec is the tiebreak.** Each task names its spec sections; read them before coding. Verb semantics not fully spelled in Task 6's dispatch skeleton (gain_life, add_mana, ramp_land, ramp_mana, tutor, pump, attach, attach_from_board, extra_combat, token_copy) are each pinned in spec §Card annotation DSL — implement exactly those semantics, one `elif` per verb, with at least one test per verb following Task 6's test patterns.
