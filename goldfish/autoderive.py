"""Scryfall card JSON -> SimCard + D9 scope classification.

Consumes trimmed Scryfall card objects (the shape returned by
``/cards/collection``) and produces, per card, a merged ``SimCard`` plus
honesty metadata: whether a functional model was fully auto-derived, whether
Claude should be asked for an annotation, and approximation notes for
anything modeled by its common case.

Classification order (first match wins): land -> mana rock/dork -> draw
spell -> ramp spell -> equipment grants -> D9 out-of-scope classes ->
vanilla creature -> needs annotation.

v1 deviation (flagged per-card via ``approx_notes``): the spec's fetch-land
semantics (sacrifice-to-search modeled as sac-self + the searched land
entering) require engine support that v1 does not have, so fetches are
approximated as producing lands — colors = the union of fetchable basic
colors, entering untapped. The landfall double-trigger is not modeled.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cards import (
    COLORS,
    Annotation,
    CardData,
    Cost,
    CostParseError,
    SimCard,
    Trigger,
    merge_card,
    parse_cost,
)


@dataclass
class Derived:
    """One card's derivation result. Exactly one of the three buckets holds:
    auto_annotated (fully modeled), card.scope_class set (classified out of
    scope, inert), or needs_annotation (Claude is asked, oracle shown)."""
    card: SimCard
    auto_annotated: bool
    needs_annotation: bool
    oracle: str
    approx_notes: list[str] = field(default_factory=list)


_REMINDER_RE = re.compile(r"\([^)]*\)")
_MANA_SYM_RE = re.compile(r"\{([WUBRGC])\}")
_ADD_CLAUSE_RE = re.compile(r"\bAdd ([^.\n]*)")
_ANY_COLOR_RE = re.compile(r"\bmana of any(?: one)? color", re.IGNORECASE)
_ENTERS_TAPPED_RE = re.compile(r"enters (?:the battlefield )?tapped", re.IGNORECASE)
_FETCH_RE = re.compile(
    r"Sacrifice [^:\n]*:[^.\n]*Search your library for "
    r"[^.\n]*land[^.\n]*onto the battlefield", re.IGNORECASE)
_RAMP_RE = re.compile(
    r"Search your library for [^.\n]*land[^.\n]*onto the battlefield",
    re.IGNORECASE)
_DRAW_RE = re.compile(r"draw (a|an|one|two|three|four|five|six|seven|\d+) cards?\.?",
                      re.IGNORECASE)
_EQUIP_COST_RE = re.compile(r"\bEquip ((?:\{[^}]+\})+)")
_GETS_RE = re.compile(r"Equipped creature gets \+(\d+)/\+(\d+)")
_HAS_RE = re.compile(r"Equipped creature[^.\n]*?has ([^.\n]+)")

_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
          "five": 5, "six": 6, "seven": 7}
_CARD_TYPES = frozenset({"land", "creature", "artifact", "enchantment",
                         "instant", "sorcery", "planeswalker", "legendary"})
_BASIC_COLORS = (("plains", "W"), ("island", "U"), ("swamp", "B"),
                 ("mountain", "R"), ("forest", "G"))


def _count_word(w: str) -> int:
    w = w.lower()
    return int(w) if w.isdigit() else _WORDS.get(w, 1)


def _int_or_none(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_types(type_line: str) -> frozenset:
    left, _, right = type_line.partition("—")
    types = {w.lower() for w in left.split()} & _CARD_TYPES
    if "equipment" in right.lower():
        types.add("equipment")
    return frozenset(types)


def _strip_reminder(oracle: str) -> str:
    """Drop reminder text, collapse horizontal whitespace, keep line breaks
    (enters-tapped and Equip parsing are line-oriented)."""
    lines = (" ".join(ln.split()) for ln in _REMINDER_RE.sub("", oracle).splitlines())
    return "\n".join(ln for ln in lines if ln).strip()


def _parse_produces(oracle: str, produced_mana) -> tuple[dict | None, list[str]]:
    """Mana produced, with quantity, from the Add clause(s). Parsed from the
    raw oracle so basics (whose whole ability is reminder text) still count.
    "or"-alternatives take a per-color max, so {R} or {W} is qty 1 each while
    {C}{C} is qty 2."""
    notes: list[str] = []
    produces: dict = {}
    for m in _ADD_CLAUSE_RE.finditer(oracle):
        clause = m.group(1)
        if _ANY_COLOR_RE.search(clause):
            qty = _count_word(clause.split()[0]) if clause.split() else 1
            for c in COLORS:
                produces[c] = max(produces.get(c, 0), qty)
            notes.append(
                f"any-color producer ('Add {clause.strip()}') approximated as "
                f"producing all five colors")
            continue
        for alt in clause.split(" or "):
            counts: dict = {}
            for sym in _MANA_SYM_RE.findall(alt):
                counts[sym] = counts.get(sym, 0) + 1
            for c, q in counts.items():
                produces[c] = max(produces.get(c, 0), q)
    if not produces and produced_mana:
        produces = {c: 1 for c in produced_mana if c in COLORS or c == "C"}
    return (produces or None), notes


def _enters_tapped(stripped: str) -> tuple[bool, str | None]:
    """Unconditional enters-tapped line -> True. A conditional ("unless"/"if")
    is approximated by the common case — untapped — and flagged."""
    for line in stripped.splitlines():
        if not _ENTERS_TAPPED_RE.search(line):
            continue
        low = line.lower()
        if "unless" in low or re.search(r"\bif\b", low):
            return False, (f"conditional enters-tapped approximated as "
                           f"untapped (common case): {line!r}")
        return True, None
    return False, None


def _fetch_produces(fetch_text: str) -> dict:
    """Union of fetchable basic colors; a plain 'basic land' search is any."""
    low = fetch_text.lower()
    colors = [c for basic, c in _BASIC_COLORS if basic in low]
    return {c: 1 for c in (colors or COLORS)}


def _equipment_grants(stripped: str) -> dict:
    grants: dict = {}
    m = _GETS_RE.search(stripped)
    if m:
        grants["power"] = int(m.group(1))
        grants["toughness"] = int(m.group(2))
    m = _HAS_RE.search(stripped)
    if m:
        kws = tuple(k.strip().lower()
                    for k in re.split(r",| and ", m.group(1)) if k.strip())
        if kws:
            grants["keywords"] = kws
    return grants


def _is_vanilla(stripped: str, keywords: frozenset) -> bool:
    """Empty oracle, or oracle that is nothing but the card's own keywords."""
    if not stripped:
        return True
    tokens = [t.strip().lower()
              for t in re.split(r"[,\n;]| and ", stripped.replace(".", ""))
              if t.strip()]
    return all(t in keywords for t in tokens)


def _d9_class(stripped: str, types: frozenset, type_line: str) -> str | None:
    low = stripped.lower()
    if re.search(r"\b(destroy|exile) target\b", low):
        return "interaction_removal"
    if re.search(r"\b(destroy all|exile all)\b", low) or \
            re.search(r"each (player|opponent) sacrifices", low):
        return "interaction_wipe"
    if "counter target" in low:
        return "interaction_counter"
    if "instant" in types and ("hexproof" in low or "indestructible" in low):
        return "protection"
    if "each opponent may" in low or re.search(r"\bvote(s|d)?\b", low):
        return "political"
    if "Saga" in type_line:
        return "unmodeled_other"                      # Sagas
    if "flip a coin" in low:
        return "unmodeled_other"                      # coin flips
    if re.search(r"sacrifice [^:\n.]*:", low):
        return "unmodeled_other"                      # sacrifice-as-cost (non-fetch)
    if re.search(r"pay \d+ life[^.:\n]*:", low):
        return "unmodeled_other"                      # life-payment activations
    return None


def _derive_one(raw: dict) -> Derived:
    notes: list[str] = []
    face = raw
    if raw.get("card_faces") and not raw.get("oracle_text"):
        face = raw["card_faces"][0]
        notes.append(f"multi-face card: derived from front face "
                     f"{face.get('name', '?')!r}; back face ignored")
    name = raw.get("name") or face.get("name") or "?"
    oracle = face.get("oracle_text") or ""
    stripped = _strip_reminder(oracle)
    type_line = face.get("type_line") or raw.get("type_line") or ""
    types = _parse_types(type_line)
    keywords = frozenset(k.lower() for k in raw.get("keywords") or ())
    mana_cost = face.get("mana_cost") or ""
    power = _int_or_none(face.get("power"))
    toughness = _int_or_none(face.get("toughness"))

    # {X} costs are pinned out of scope and never reach parse_cost (which
    # raises on X); other unparseable costs (hybrid/phyrexian) land in the
    # same class with a note.
    cost: Cost | None = None
    scope_class: str | None = None
    if "{X}" in mana_cost:
        scope_class = "unmodeled_other"
    elif mana_cost:
        try:
            cost = parse_cost(mana_cost)
        except CostParseError as exc:
            scope_class = "unmodeled_other"
            notes.append(f"unparseable mana cost {mana_cost!r}: {exc}")

    equip_cost: Cost | None = None
    if "equipment" in types:
        m = _EQUIP_COST_RE.search(stripped)
        if m:
            try:
                equip_cost = parse_cost(m.group(1))
            except CostParseError:
                pass                                  # e.g. Equip {X} — unmodeled

    produces: dict | None = None
    enters_tapped = False
    ann: Annotation | None = None
    auto = False
    needs = False

    if scope_class is not None:
        pass                                          # classified above, not asked
    elif "land" in types:
        auto = True
        fetch = _FETCH_RE.search(stripped)
        if fetch:
            # DEVIATION from spec fetch semantics (sac-self + land_etb): the
            # v1 engine has no sac_self mechanism, so fetches become plain
            # producing lands — honest via the note.
            produces = _fetch_produces(fetch.group(0))
            notes.append("fetch approximated as a producing land; landfall "
                         "double-trigger not modeled")
        else:
            produces, pnotes = _parse_produces(oracle, raw.get("produced_mana"))
            notes.extend(pnotes)
            enters_tapped, tnote = _enters_tapped(stripped)
            if tnote:
                notes.append(tnote)
    elif "{T}: Add" in stripped:
        auto = True
        produces, pnotes = _parse_produces(oracle, raw.get("produced_mana"))
        notes.extend(pnotes)
        enters_tapped, tnote = _enters_tapped(stripped)
        if tnote:
            notes.append(tnote)
        if "creature" in types:
            notes.append("creature mana producer: engine has no "
                         "summoning-sickness gate on mana tapping")
    elif (dm := _DRAW_RE.fullmatch(" ".join(stripped.split()))):
        auto = True
        ann = Annotation(name=name, triggers=[
            Trigger(on="cast", do="draw", count=_count_word(dm.group(1)))])
    elif ({"instant", "sorcery"} & types) and (rm := _RAMP_RE.search(stripped)):
        auto = True
        count = 2 if re.search(r"\btwo\b", rm.group(0), re.IGNORECASE) else 1
        ann = Annotation(name=name, triggers=[
            Trigger(on="cast", do="ramp_land", count=count)])
        if "into your hand" in stripped:
            notes.append("battlefield-plus-hand land split approximated as "
                         "all searched lands onto the battlefield")
    elif "equipment" in types and (grants := _equipment_grants(stripped)):
        auto = True
        ann = Annotation(name=name, grants=grants)
    elif (d9 := _d9_class(stripped, types, type_line)) is not None:
        scope_class = d9
    elif ("creature" in types and power is not None and toughness is not None
            and _is_vanilla(stripped, keywords)):
        auto = True                                   # data alone models it
    else:
        needs = True

    data = CardData(name=name, cost=cost, types=types, power=power,
                    toughness=toughness, keywords=keywords, produces=produces,
                    enters_tapped=enters_tapped, equip_cost=equip_cost,
                    oracle=oracle)
    return Derived(card=merge_card(data, ann, scope_class),
                   auto_annotated=auto, needs_annotation=needs,
                   oracle=oracle, approx_notes=notes)


def derive(scryfall_cards: list[dict]) -> dict[str, Derived]:
    """Scryfall card JSON objects -> {card name: Derived}."""
    return {d.card.name: d for d in map(_derive_one, scryfall_cards)}
