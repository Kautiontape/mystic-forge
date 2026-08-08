"""Comprehensive Rules parser and in-memory index.

Pure and synchronous: no network, no persistence. server.py owns fetching
the CR text (vendored snapshot, disk cache, background refresh) and hands
raw text to parse(); everything here is unit-testable from a fixture.

Verified upstream format (MagicCompRules 20260807.txt):
- 'Glossary' and 'Credits' each appear twice as standalone lines — once in
  the table of contents, once opening the body section. The rules body sits
  between the TOC's Credits line and the body's Glossary line.
- Line shapes: section '1. Game Concepts', subsection '702. Keyword
  Abilities', rule '702.2. Deathtouch' (trailing period), subrule
  '702.2a Deathtouch is a static ability.' (letter, no period). Subrule
  letters skip 'l' and 'o'.
- Blank-looking separator lines are a single U+00A0 (non-breaking space),
  not empty — blank checks must use str.strip(), never == "".
- Glossary entries are a bare term line followed by definition line(s);
  definitions cite rules as 'See rule 702.2, "Deathtouch."'.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import get_close_matches

_SUBRULE_RE = re.compile(r"^(\d{3}\.\d+)([a-z]+)\.? (.*)$")
_RULE_RE = re.compile(r"^(\d{3}\.\d+)\.? (.*)$")
_SUBSECTION_RE = re.compile(r"^(\d{3})\. (.*)$")
_SECTION_RE = re.compile(r"^(\d)\. (.*)$")
_RULE_REF_RE = re.compile(r"(?<!\d)(\d{3}(?:\.\d+)?[a-z]{0,2})(?!\d)")
_EFFECTIVE_RE = re.compile(r"effective as of (\w+) (\d{1,2}), (\d{4})")
_TOKEN_RE = re.compile(r"[a-z0-9']+")

_MONTHS = {name: i for i, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


@dataclass
class Rule:
    number: str
    text: str
    parent: str | None = None
    children: list[str] = field(default_factory=list)


@dataclass
class SearchHit:
    ref: str
    kind: str  # "rule" | "glossary"
    text: str
    score: float


def _tokenize(s: str) -> list[str]:
    return _TOKEN_RE.findall(s.lower())


class RulesIndex:
    def __init__(self) -> None:
        self.rules: dict[str, Rule] = {}
        self.glossary: dict[str, str] = {}          # lower term -> definition
        self.glossary_display: dict[str, str] = {}  # lower term -> original casing
        self.effective_date: str = ""               # "August 7, 2026"
        self.effective_yyyymmdd: str = ""            # "20260807"

    def glossary_refs(self, term: str) -> list[str]:
        """Rule numbers cited by a glossary definition, in order, deduped."""
        seen: list[str] = []
        for num in _RULE_REF_RE.findall(self.glossary.get(term.lower(), "")):
            if num not in seen:
                seen.append(num)
        return seen

    def suggest(self, ref: str, n: int = 5) -> list[str]:
        """Closest rule numbers and glossary terms for a failed lookup."""
        pool = list(self.rules) + list(self.glossary_display.values())
        return get_close_matches(ref, pool, n=n, cutoff=0.6)


def parse(text: str) -> RulesIndex:
    idx = RulesIndex()
    lines = [ln.rstrip() for ln in text.lstrip("﻿").splitlines()]

    m = _EFFECTIVE_RE.search(text[:2000])  # date sits on line 3; window is generous
    if m:
        month, day, year = m.group(1), int(m.group(2)), int(m.group(3))
        idx.effective_date = f"{month} {day}, {year}"
        if month in _MONTHS:
            idx.effective_yyyymmdd = f"{year:04d}{_MONTHS[month]:02d}{day:02d}"

    glossary_marks = [i for i, ln in enumerate(lines) if ln.strip() == "Glossary"]
    credits_marks = [i for i, ln in enumerate(lines) if ln.strip() == "Credits"]
    if len(glossary_marks) < 2 or len(credits_marks) < 2:
        raise ValueError("CR text is missing its TOC/body Glossary or Credits markers")

    _parse_rules(idx, lines[credits_marks[0] + 1:glossary_marks[1]])
    _parse_glossary(idx, lines[glossary_marks[1] + 1:credits_marks[1]])
    return idx


def _add(idx: RulesIndex, rule: Rule) -> Rule:
    idx.rules[rule.number] = rule
    if rule.parent and rule.parent in idx.rules:
        idx.rules[rule.parent].children.append(rule.number)
    return rule


def _parse_rules(idx: RulesIndex, lines: list[str]) -> None:
    last: Rule | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if m := _SUBRULE_RE.match(line):
            last = _add(idx, Rule(m.group(1) + m.group(2), m.group(3), parent=m.group(1)))
        elif m := _RULE_RE.match(line):
            last = _add(idx, Rule(m.group(1), m.group(2), parent=m.group(1).split(".", 1)[0]))
        elif m := _SUBSECTION_RE.match(line):
            last = _add(idx, Rule(m.group(1), m.group(2), parent=m.group(1)[0]))
        elif m := _SECTION_RE.match(line):
            last = _add(idx, Rule(m.group(1), m.group(2)))
        elif last is not None:
            # Examples and wrapped text belong to the rule right above them.
            last.text += "\n" + line


def _parse_glossary(idx: RulesIndex, lines: list[str]) -> None:
    blocks: list[list[str]] = []
    cur: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line:
            cur.append(line)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    term: str | None = None
    for block in blocks:
        # Term lines are bare headwords; definition paragraphs end with
        # sentence punctuation, so a block opening with one continues the
        # previous entry (multi-paragraph definitions).
        if len(block) == 1 and block[0].endswith((".", "”", '"')) and term is not None:
            idx.glossary[term] = idx.glossary[term] + " " + " ".join(block)
        else:
            term = block[0].lower()
            idx.glossary[term] = " ".join(block[1:])
            idx.glossary_display[term] = block[0]

    # Compound headwords ('Banding, "Bands with Other"') get alias keys for
    # each part, so lookups by the primary word resolve. Real entries win.
    for key in list(idx.glossary):
        display = idx.glossary_display[key]
        if "," not in display:
            continue
        for part in display.split(","):
            alias_display = part.strip().strip("“”\"")
            alias = alias_display.lower()
            if alias and alias not in idx.glossary:
                idx.glossary[alias] = idx.glossary[key]
                idx.glossary_display[alias] = alias_display
