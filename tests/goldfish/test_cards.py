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
