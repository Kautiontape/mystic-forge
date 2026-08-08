import pytest

from goldfish.cards import (
    Annotation,
    AnnotationError,
    CardData,
    CostParseError,
    SimCard,
    merge_card,
    parse_cost,
    validate_annotations,
)


def make_data(name, **kw):
    base = {"name": name, "cost": None, "types": frozenset(), "power": None,
            "toughness": None, "keywords": frozenset(), "produces": None,
            "enters_tapped": False, "equip_cost": None, "oracle": ""}
    base.update(kw)
    return CardData(**base)


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


def test_activated_pump_keeps_duration():
    anns = validate_annotations([{
        "name": "Berserk Charge",
        "activated": [{"cost": "{1}{R}", "do": "pump",
                       "power": 3, "toughness": 0, "duration": "permanent"}]}])
    act = anns["Berserk Charge"].activated[0]
    assert act.duration == "permanent"


def test_activated_cost_none_treated_as_empty():
    anns = validate_annotations([{
        "name": "Free Ability",
        "activated": [{"cost": None, "do": "extra_combat"}]}])
    act = anns["Free Ability"].activated[0]
    assert act.tap is False and act.mana.mv == 0


# -- sad paths ---------------------------------------------------------------

def test_bad_damage_target_rejected():
    with pytest.raises(AnnotationError) as ei:
        validate_annotations([{"name": "Zapper", "triggers": [
            {"on": "etb", "do": "damage", "target": "the_whole_table", "count": 1}]}])
    msg = str(ei.value)
    assert "Zapper" in msg and "one_opponent" in msg


def test_bad_tutor_filter_rejected():
    with pytest.raises(AnnotationError) as ei:
        validate_annotations([{"name": "Digger", "triggers": [
            {"on": "etb", "do": "tutor", "filter": "not_a_real_filter"}]}])
    msg = str(ei.value)
    assert "Digger" in msg and "creature" in msg  # allowed-list hint present


def test_bad_duration_rejected_on_trigger():
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "triggers": [
            {"on": "etb", "do": "pump", "power": 1, "toughness": 1,
             "duration": "forever"}]}])


def test_bad_duration_rejected_on_activated():
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "activated": [
            {"cost": "{1}", "do": "pump", "power": 1, "toughness": 1,
             "duration": "forever"}]}])


def test_bad_condition_rejected():
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "triggers": [
            {"on": "etb", "do": "draw", "if": "not_a_condition"}]}])


def test_bad_static_kind_rejected():
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "statics": ["not_a_static"]}])


def test_bad_cost_reduction_filter_rejected():
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "statics": [
            {"kind": "cost_reduction", "filter": "color:Z", "amount": 1}]}])


def test_create_token_missing_power_rejected():
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "triggers": [
            {"on": "etb", "do": "create_token", "toughness": 1}]}])


def test_add_mana_missing_pips_rejected():
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "triggers": [
            {"on": "etb", "do": "add_mana"}]}])


def test_add_mana_malformed_pips_rejected_on_trigger_and_activated():
    # A malformed pip string that survived validation would raise
    # CostParseError mid-cast, AFTER pay() — corrupting engine state.
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "triggers": [
            {"on": "cast", "do": "add_mana", "pips": "{Q}"}]}])
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "activated": [
            {"cost": "{T}", "do": "add_mana", "pips": "{W/U}"}]}])


def test_pump_missing_power_rejected():
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "triggers": [
            {"on": "etb", "do": "pump", "toughness": 1}]}])


def test_unnamed_card_rejected():
    with pytest.raises(AnnotationError) as ei:
        validate_annotations([{"triggers": []}])
    msg = str(ei.value)
    assert "unnamed" in msg and "a card name" in msg  # allowed-list hint present


def test_keywords_as_bare_string_rejected():
    with pytest.raises(AnnotationError) as ei:
        validate_annotations([{"name": "X", "statics": [
            {"kind": "anthem", "power": 1, "toughness": 1, "keywords": "haste"}]}])
    msg = str(ei.value)
    assert "X" in msg and "list of keyword strings" in msg


def test_keywords_none_accepted_as_empty():
    anns = validate_annotations([{"name": "X", "statics": [
        {"kind": "anthem", "power": 1, "toughness": 1, "keywords": None}]}])
    assert anns["X"].statics[0].keywords == ()


def test_negative_count_rejected():
    # negative counts are never legal DSL, in triggers or activated abilities
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "triggers": [
            {"on": "etb", "do": "draw", "count": -1}]}])
    with pytest.raises(AnnotationError):
        validate_annotations([{"name": "X", "activated": [
            {"cost": "{T}", "do": "draw", "count": -2}]}])


def test_activated_bad_cost_reraised_as_annotation_error():
    with pytest.raises(AnnotationError) as ei:
        validate_annotations([{"name": "Krenko, Mob Boss", "activated": [
            {"cost": "{X}", "do": "extra_combat"}]}])
    msg = str(ei.value)
    assert "Krenko, Mob Boss" in msg


# -- CardData / SimCard merge -------------------------------------------------

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


# -- sac_self (fetch lands) ----------------------------------------------------

def test_activated_sac_self_parsed_and_defaults_false():
    anns = validate_annotations([
        {"name": "Wilds", "activated": [
            {"cost": "{T}", "do": "ramp_land", "sac_self": True}]},
        {"name": "Orb", "activated": [{"cost": "{T}", "do": "draw"}]},
    ])
    assert anns["Wilds"].activated[0].sac_self is True
    assert anns["Orb"].activated[0].sac_self is False
