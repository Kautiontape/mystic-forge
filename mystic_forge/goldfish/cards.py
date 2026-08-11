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


def parse_cost(cost_str: str | None) -> Cost:
    pips = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "generic": 0}
    s = cost_str or ""
    for sym in _SYMBOL_RE.findall(s):
        if sym.isdigit():
            pips["generic"] += int(sym)
        elif sym in pips:
            pips[sym] += 1
        else:
            raise CostParseError(
                f"unsupported mana symbol {{{sym}}} (X, hybrid, and phyrexian "
                f"costs are out of scope in v1)")
    if _SYMBOL_RE.sub("", s):
        raise CostParseError(
            f"malformed cost string {cost_str!r}: contains content outside "
            f"well-formed {{...}} symbols")
    return Cost(pips)


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
    sac_self: bool = False                  # fetch lands: the permanent is
                                            # sacrificed as part of the cost
    # same verb params as Trigger:
    count: object = 1
    target: str | None = None
    power: int | None = None
    toughness: int | None = None
    duration: str = "eot"                   # pump: eot|permanent
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
        if raw < 0:               # negative counts are never legal DSL
            raise AnnotationError(card, "count", raw,
                                  SYMBOLIC_COUNTS | {"<non-negative int>"})
        return raw
    if raw in SYMBOLIC_COUNTS:
        return raw
    raise AnnotationError(card, "count", raw, SYMBOLIC_COUNTS | {"<non-negative int>"})


def _parse_keywords(card, d: dict) -> tuple:
    """Shared keyword-list parsing for triggers, activated abilities, and
    statics. Accepts a JSON null or missing key as "no keywords"; rejects a
    bare string (which would silently explode into single-character tuples)
    and any non-string element."""
    kw = d.get("keywords") or ()
    if isinstance(kw, str):
        raise AnnotationError(card, "keywords", kw, {"a list of keyword strings"})
    try:
        items = tuple(kw)
    except TypeError:
        raise AnnotationError(card, "keywords", kw, {"a list of keyword strings"})
    if not all(isinstance(k, str) for k in items):
        raise AnnotationError(card, "keywords", kw, {"a list of keyword strings"})
    return items


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
    if t.pips is not None:
        # Validate here: a malformed pip string that survived to the engine
        # would raise CostParseError mid-cast, AFTER pay() — corrupting state.
        try:
            parse_cost(t.pips)
        except CostParseError:
            raise AnnotationError(card, "pips", t.pips,
                                  {"a mana pip string like {R}{R}"})
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
        keywords=_parse_keywords(card, raw),
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
    cost_str = raw.get("cost") or ""       # lands/free abilities legitimately have none
    tap = "{T}" in cost_str
    try:
        mana = parse_cost(cost_str.replace("{T}", ""))
    except CostParseError:
        raise AnnotationError(
            card, "cost", cost_str,
            {"a mana cost string like {2}{W}, optionally with {T}"})
    a = Activated(
        do=do, mana=mana, tap=tap,
        sac_self=bool(raw.get("sac_self", False)),
        count=_parse_count(card, raw.get("count", 1)),
        target=raw.get("target"),
        power=raw.get("power"), toughness=raw.get("toughness"),
        duration=raw.get("duration", "eot"),
        keywords=_parse_keywords(card, raw),
        tutor_filter=raw.get("filter"),
        pips=raw.get("pips"), any_mana=(raw.get("colors") == "any"),
    )
    if a.duration not in ("eot", "permanent"):
        raise AnnotationError(card, "duration", a.duration, {"eot", "permanent"})
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
               keywords=_parse_keywords(card, obj),
               count=obj.get("count", 1),
               filter=obj.get("filter", "any"), amount=obj.get("amount", 0))
    if kind == "cost_reduction":
        f = s.filter
        if not (f in COST_REDUCTION_FILTERS or
                (f.startswith("color:") and f[6:] in COLORS)):
            raise AnnotationError(card, "filter", f,
                                  COST_REDUCTION_FILTERS | {"color:<W|U|B|R|G>"})
    return s


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
