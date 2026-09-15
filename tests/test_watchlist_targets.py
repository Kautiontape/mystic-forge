"""The target grammar shared by the page, the import box, and the tools."""

import pytest

from mystic_forge.watchlist import targets


@pytest.mark.parametrize("text, expected", [
    ("2.50", {"target_mode": "fixed", "target_pct": 0.0, "target_price": 2.5}),
    ("$ 12", {"target_mode": "fixed", "target_pct": 0.0, "target_price": 12.0}),
    ("€3,50", {"target_mode": "fixed", "target_pct": 0.0, "target_price": 3.5}),
    ("low", {"target_mode": "low", "target_pct": 0.0, "target_price": None}),
    ("Historic Low", {"target_mode": "low", "target_pct": 0.0, "target_price": None}),
    ("low-10%", {"target_mode": "low", "target_pct": 10.0, "target_price": None}),
    ("floor 25%", {"target_mode": "low", "target_pct": 25.0, "target_price": None}),
    ("-20%", {"target_mode": "fixed", "target_pct": 0.0, "target_price": None,
              "pct_of_current": 20.0}),
    ("20% off", {"target_mode": "fixed", "target_pct": 0.0, "target_price": None,
                 "pct_of_current": 20.0}),
])
def test_parse_spec(text, expected):
    assert targets.parse_spec(text) == expected


@pytest.mark.parametrize("text", ["", "   ", None, "abc", "low-x%", "1.2.3", "$"])
def test_parse_spec_rejects_non_targets(text):
    assert targets.parse_spec(text) is None


def test_resolve_percent_of_current_needs_a_price():
    spec = targets.parse_spec("-25%")
    assert targets.resolve(spec, 8.0) == {"target_mode": "fixed", "target_pct": 0.0,
                                         "target_price": 6.0}
    assert targets.resolve(spec, None) is None
    assert targets.resolve(None, 8.0) is None
    assert targets.resolve(targets.parse_spec("low-10%"), None) == \
        {"target_mode": "low", "target_pct": 10.0, "target_price": None}


def test_describe_and_to_spec_round_trip():
    low = {"target_mode": "low", "target_pct": 10.0, "target_price": None}
    assert targets.describe(low) == "historic low −10%"
    assert targets.describe(low, "$", effective=3.6) == "historic low −10% (now $3.60)"
    assert targets.describe({"target_mode": "low", "target_pct": 0}) == "historic low"
    assert targets.describe({"target_price": 12.0}, "€") == "€12.00"
    assert targets.describe({"target_price": None}) == ""
    for e in (low, {"target_mode": "low", "target_pct": 0}, {"target_price": 2.5}):
        spec = targets.to_spec(e)
        back = targets.parse_spec(spec)
        assert back["target_mode"] == (e.get("target_mode") or "fixed")
        assert back["target_pct"] == float(e.get("target_pct") or 0)
    assert targets.to_spec({"target_price": None}) == ""
