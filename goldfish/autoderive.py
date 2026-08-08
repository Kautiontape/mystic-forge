"""Scryfall card JSON -> SimCard + D9 scope classification.

Consumes trimmed Scryfall card objects (the shape returned by
``/cards/collection``) and produces, per card, a merged ``SimCard`` plus
honesty metadata: whether a functional model was fully auto-derived, whether
Claude should be asked for an annotation, and approximation notes for
anything modeled by its common case.

Classification order (first match wins): land -> mana rock/dork -> draw
spell -> ramp spell -> equipment grants -> D9 out-of-scope classes ->
vanilla creature -> needs annotation.

Fetch lands (sacrifice-to-search) follow the spec's semantics: a sac-self
``{T}`` activation running ``ramp_land``, so cracking the fetch removes it
and the searched land enters (firing ``land_etb`` once). Remaining
approximations are flagged per-card via ``approx_notes``: the fetched land
enters tapped (ramp_land's pinned common case) and the crack happens at
sorcery speed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cards import (
    COLORS,
    Activated,
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
_ALT_SPLIT_RE = re.compile(r"\s*,\s*(?:or\s+)?|\s+or\s+")
_PROTECTION_RE = re.compile(r"protection from \w+(?: and from \w+)*")

_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
          "five": 5, "six": 6, "seven": 7}
_CARD_TYPES = frozenset({"land", "creature", "artifact", "enchantment",
                         "instant", "sorcery", "planeswalker", "legendary"})


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


def _add_cost_is_pure_tap(line: str, add_start: int) -> bool:
    """True when the activation cost preceding an Add clause is exactly {T}
    (basics' reminder parens tolerated). Filter-land pips ("{U/R}, {T}:"),
    generic costs ("{1}, {T}:"), sacrifice riders, and quoted gained
    abilities all disqualify the clause — only pure-tap Adds count as the
    card's own produce."""
    before = line[:add_start]
    if ":" not in before:
        return True                     # bare "Add ..." (no activation cost)
    cost = before.rsplit(":", 1)[0]
    cost = cost.rsplit(".", 1)[-1]      # drop a prior sentence on this line
    for junk in ("(", ")", '"', ","):
        cost = cost.replace(junk, " ")
    return not cost.replace("{T}", " ").strip()


def _parse_produces(oracle: str, produced_mana) -> tuple[dict | None, list[str], bool]:
    """(produces, notes, costed_add) — mana produced, with quantity, from
    pure-{T} Add clauses. Parsed from the raw oracle so basics (whose whole
    ability is reminder text) still count. Alternatives split on commas and
    "or" take a per-color max, so "{R} or {W}" is qty 1 each while {C}{C} is
    qty 2; a mixed alternative ("{W}{U}" — Karoos) yields its total for each
    color (controller pin: engine (color-set, max-qty) then gives 2 mana of
    either color). costed_add reports Add clauses skipped for having an
    activation cost beyond {T} (filter lands)."""
    notes: list[str] = []
    produces: dict = {}
    costed_add = False
    for line in oracle.splitlines():
        for m in _ADD_CLAUSE_RE.finditer(line):
            if not _add_cost_is_pure_tap(line, m.start()):
                costed_add = True
                continue
            clause = m.group(1)
            if _ANY_COLOR_RE.search(clause):
                qty = _count_word(clause.split()[0]) if clause.split() else 1
                for c in COLORS:
                    produces[c] = max(produces.get(c, 0), qty)
                notes.append(
                    f"any-color producer ('Add {clause.strip()}') approximated "
                    f"as producing all five colors")
                continue
            for alt in _ALT_SPLIT_RE.split(clause):
                syms = _MANA_SYM_RE.findall(alt)
                counts: dict = {}
                for sym in syms:
                    counts[sym] = counts.get(sym, 0) + 1
                if len(counts) > 1:
                    # Karoo/bounce pin: a mixed fixed Add gives its total
                    # quantity in the engine's (color-set, max-qty) shape.
                    for c in counts:
                        produces[c] = max(produces.get(c, 0), len(syms))
                    notes.append(
                        f"bounce land approximated as {len(syms)} mana of "
                        f"either color; bounce drawback not modeled")
                else:
                    for c, q in counts.items():
                        produces[c] = max(produces.get(c, 0), q)
    if not produces and not costed_add and produced_mana:
        produces = {c: 1 for c in produced_mana if c in COLORS or c == "C"}
    if produces and costed_add:
        notes.append("Add ability with an activation cost beyond {T} ignored "
                     "(filter-style)")
    return (produces or None), notes, costed_add


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


def _grant_keywords(text: str) -> tuple:
    """Keyword list from an "has X and Y" grant. "protection from red and
    from blue" yields two whole "protection from <x>" keywords — never a
    mangled "from blue" token."""
    low = text.lower()
    kws = []
    for m in _PROTECTION_RE.finditer(low):
        kws.extend(f"protection from {t}"
                   for t in re.findall(r"from (\w+)", m.group(0)))
    rest = _PROTECTION_RE.sub("", low)
    kws.extend(k.strip() for k in re.split(r",| and ", rest) if k.strip())
    return tuple(kws)


def _equipment_grants(stripped: str) -> dict:
    grants: dict = {}
    m = _GETS_RE.search(stripped)
    if m:
        grants["power"] = int(m.group(1))
        grants["toughness"] = int(m.group(2))
    m = _HAS_RE.search(stripped)
    if m:
        kws = _grant_keywords(m.group(1))
        if kws:
            grants["keywords"] = kws
    return grants


def _equipment_extra_text(stripped: str) -> bool:
    """True when rules text remains beyond the grant sentences and the Equip
    line (e.g. Sword of Fire and Ice's combat-damage trigger)."""
    for line in stripped.splitlines():
        for sent in line.split("."):
            s = sent.strip()
            if s and not s.startswith(("Equipped creature", "Equip")):
                return True
    return False


def _is_vanilla(stripped: str, keywords: frozenset) -> bool:
    """Empty oracle, or oracle that is nothing but the card's own keywords."""
    if not stripped:
        return True
    tokens = [t.strip().lower()
              for t in re.split(r"[,\n;]| and ", stripped.replace(".", ""))
              if t.strip()]
    return all(t in keywords for t in tokens)


def _d9_class(stripped: str, types: frozenset) -> str | None:
    # Sagas are classified before the branch chain (a land-Saga must never
    # reach the land branch), so no Saga check here.
    low = stripped.lower()
    if re.search(r"\b(destroy|exile) target\b", low):
        return "interaction_removal"
    if re.search(r"\b(destroy all|exile all)\b", low) or \
            re.search(r"each (player|opponent) sacrifices", low):
        return "interaction_wipe"
    if "counter target" in low:
        return "interaction_counter"
    if "instant" in types and ("hexproof" in low or "indestructible" in low
                               or "protection" in low):
        return "protection"
    if "each opponent may" in low or re.search(r"\bvote(s|d)?\b", low):
        return "political"
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

    # Sagas are pinned out of scope BEFORE the branch chain: a land-Saga
    # (Urza's Saga) must not classify as a plain land, and quoted chapter
    # abilities ('gains "{T}: Add {C}."') must not classify it as a rock.
    if scope_class is None and "Saga" in type_line:
        scope_class = "unmodeled_other"

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
        if _FETCH_RE.search(stripped):
            # Spec fetch semantics: sacrifice-to-search is a sac-self {T}
            # activation running ramp_land — the fetch is NOT a producer.
            auto = True
            ann = Annotation(name=name, activated=[
                Activated(do="ramp_land", mana=parse_cost(None), tap=True,
                          sac_self=True)])
            notes.append("fetched land enters tapped per ramp_land pin "
                         "(conditional-untap fetches like Fabled Passage "
                         "subsumed); fetch timing approximated as "
                         "sorcery-speed")
        else:
            produces, pnotes, costed_add = _parse_produces(
                oracle, raw.get("produced_mana"))
            notes.extend(pnotes)
            enters_tapped, tnote = _enters_tapped(stripped)
            if tnote:
                notes.append(tnote)
            if produces is None and costed_add:
                # Filter lands (Darkwater Catacombs): every mana ability has
                # a cost beyond {T} — conservative, ask for an annotation.
                needs = True
                notes.append("filter land: every mana ability has an "
                             "activation cost beyond {T}; not auto-modeled")
            else:
                auto = True
    elif "{T}: Add" in stripped:
        produces, pnotes, costed_add = _parse_produces(
            oracle, raw.get("produced_mana"))
        notes.extend(pnotes)
        enters_tapped, tnote = _enters_tapped(stripped)
        if tnote:
            notes.append(tnote)
        if produces is None and costed_add:
            needs = True
            notes.append("filter-style producer: every mana ability has an "
                         "activation cost beyond {T}; not auto-modeled")
        else:
            auto = True
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
        notes.append("ramp takes the first land in library order; 'basic "
                     "land' search restrictions widened to any land")
    elif "equipment" in types and (grants := _equipment_grants(stripped)):
        auto = True
        ann = Annotation(name=name, grants=grants)
        if _equipment_extra_text(stripped):
            notes.append("additional ability text not modeled")
    elif (d9 := _d9_class(stripped, types)) is not None:
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
