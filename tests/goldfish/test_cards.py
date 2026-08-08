import pytest
from goldfish.cards import Cost, parse_cost, CostParseError
from goldfish.cards import (
    validate_annotations, AnnotationError,
    EVENTS, VERBS, SYMBOLIC_COUNTS, STATIC_KINDS,
)


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


def test_parse_cost_none_is_all_zero():
    c = parse_cost(None)
    assert c.pips == {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "generic": 0}
    assert c.mv == 0


def test_parse_cost_x_rejection_message_mentions_out_of_scope():
    with pytest.raises(CostParseError, match="out of scope"):
        parse_cost("{X}")


def test_parse_cost_rejects_trailing_garbage():
    with pytest.raises(CostParseError):
        parse_cost("3RW")


def test_parse_cost_rejects_interior_garbage():
    with pytest.raises(CostParseError):
        parse_cost("{3}garbage{R}")


def test_parse_cost_rejects_empty_braces():
    with pytest.raises(CostParseError):
        parse_cost("{}")


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
