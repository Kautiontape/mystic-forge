"""The target grammar shared by the page, the import box, and the MCP tools.

A target is written after ' @ ' on a decklist line, or typed into the
target box. It is either a number or a rule:

    2.50          fixed target of $2.50 (a leading $ or € is tolerated)
    low           the historic low: alert when the price is at or under the
                  lowest it has been (the rule moves as new lows are set)
    low-10%       ten percent under the historic low
    -20%          twenty percent under the price at the moment it is set —
                  a shortcut that resolves to a fixed number, gg.deals-style

Parsing yields a dict shaped like the entry columns (`target_price`,
`target_mode`, `target_pct`) plus `pct_of_current` for the last form, which
the caller resolves once it knows the price.
"""

import re

_NUM = r"(\d+(?:[.,]\d+)?)"
_FIXED_RE = re.compile(rf"^[$€]?\s*{_NUM}$")
_LOW_RE = re.compile(rf"^(?:historic(?:al)?[\s-]*)?(?:low|min|floor)"
                     rf"(?:\s*(?:-|minus|less)?\s*{_NUM}\s*%)?$", re.I)
_OFF_RE = re.compile(rf"^-?\s*{_NUM}\s*%(?:\s*(?:off|under|below|down))?$", re.I)


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def parse_spec(text) -> dict | None:
    """The target a spec describes, or None when it is not one.

    'low' / 'low-10%' → {'target_mode': 'low', 'target_pct': 10}
    '2.50'            → {'target_mode': 'fixed', 'target_price': 2.5}
    '-20%'            → {'target_mode': 'fixed', 'pct_of_current': 20}"""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    m = _LOW_RE.match(s)
    if m:
        pct = _num(m.group(1)) if m.group(1) else 0.0
        return {"target_mode": "low", "target_pct": max(0.0, min(pct, 99.0)),
                "target_price": None}
    m = _OFF_RE.match(s)
    if m:
        pct = _num(m.group(1))
        return {"target_mode": "fixed", "target_pct": 0.0,
                "target_price": None, "pct_of_current": max(0.0, min(pct, 99.0))}
    m = _FIXED_RE.match(s)
    if m:
        return {"target_mode": "fixed", "target_pct": 0.0,
                "target_price": round(_num(m.group(1)), 2)}
    return None


def resolve(spec: dict | None, current) -> dict | None:
    """Turn a parsed spec into storable columns. A percent-of-current spec
    needs a price; without one it becomes 'no target' (the caller may say
    so). Returns None for no target."""
    if not spec:
        return None
    if "pct_of_current" in spec:
        if current is None:
            return None
        return {"target_mode": "fixed", "target_pct": 0.0,
                "target_price": round(float(current)
                                      * (1 - spec["pct_of_current"] / 100), 2)}
    return {k: spec.get(k) for k in ("target_mode", "target_pct", "target_price")}


def describe(entry: dict, cur: str = "$", effective=None) -> str:
    """Human wording of an entry's target rule, e.g. 'historic low −10%
    (now $3.60)' or '$12.00'. Empty string when there is no target."""
    mode = entry.get("target_mode") or "fixed"
    if mode == "low":
        pct = float(entry.get("target_pct") or 0)
        rule = "historic low" + (f" −{pct:g}%" if pct else "")
        if effective is not None:
            rule += f" (now {cur}{effective:.2f})"
        return rule
    tp = entry.get("target_price")
    return f"{cur}{tp:.2f}" if tp is not None else ""


def to_spec(entry: dict) -> str:
    """The inverse of parse_spec, for export lines: 'low-10%' or '2.50'."""
    mode = entry.get("target_mode") or "fixed"
    if mode == "low":
        pct = float(entry.get("target_pct") or 0)
        return f"low-{pct:g}%" if pct else "low"
    tp = entry.get("target_price")
    return f"{tp:.2f}" if tp is not None else ""
