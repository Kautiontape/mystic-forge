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
