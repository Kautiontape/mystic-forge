import math

from goldfish.metrics import (
    GameRecord,
    aggregate,
    mcnemar_exact_p,
    median_iqr,
    median_low,
    paired_delta,
    wilson_ci,
)

# ---------------------------------------------------------------- plan tests


def test_wilson_ci_known_value():
    lo, hi = wilson_ci(80, 100)                  # p̂=0.8, n=100, z=1.96
    assert abs(lo - 0.7112) < 0.002 and abs(hi - 0.8661) < 0.002


def test_mcnemar_exact_p():
    # 10 discordant pairs, 9 one way: two-sided exact binomial
    p = mcnemar_exact_p(b=9, c=1)
    expected = 2 * sum(math.comb(10, k) for k in range(2)) / 2 ** 10
    assert abs(p - min(1.0, expected)) < 1e-9


def test_paired_delta():
    a = [1, 1, 0, 1]
    b = [0, 1, 0, 0]
    d = paired_delta(a, b)
    assert abs(d.mean - 0.5) < 1e-9 and d.n == 4 and d.ci_low < 0.5 < d.ci_high


def test_aggregate_censoring():
    recs = [GameRecord(commander_cast_turn=3), GameRecord(commander_cast_turn=None)]
    m = aggregate(recs, until_turn=8)
    cc = m["commander_cast"]
    assert cc["reached_pct"]["value"] == 0.5
    assert cc["median_among_reached"] == 3


# ------------------------------------------------------------- wilson bounds


def test_wilson_ci_zero_successes():
    lo, hi = wilson_ci(0, 10)
    assert lo == 0.0
    assert 0.0 < hi < 1.0


def test_wilson_ci_all_successes():
    lo, hi = wilson_ci(10, 10)
    assert hi == 1.0
    assert 0.0 < lo < 1.0


def test_wilson_ci_empty():
    assert wilson_ci(0, 0) == (0.0, 1.0)


# ----------------------------------------------------------------- mcnemar


def test_mcnemar_balanced_discordance_is_one():
    assert mcnemar_exact_p(5, 5) == 1.0


def test_mcnemar_no_discordance_is_one():
    assert mcnemar_exact_p(0, 0) == 1.0


# ------------------------------------------------------------- paired_delta


def test_paired_delta_zero_delta_not_significant():
    a = [1, 0, 1, 0]
    d = paired_delta(a, list(a))
    assert d.mean == 0.0
    assert d.sd == 0.0
    assert d.significant is False
    assert d.ci_low == 0.0 and d.ci_high == 0.0


def test_paired_delta_mcnemar_only_for_binary():
    binary = paired_delta([1, 1, 0, 1], [0, 1, 0, 0])
    # 2 discordant pairs, both a-wins: 2 * C(2,0)/2^2 = 0.5
    assert binary.mcnemar_p is not None and abs(binary.mcnemar_p - 0.5) < 1e-9
    nonbinary = paired_delta([2.0, 3.0], [1.0, 1.0])
    assert nonbinary.mcnemar_p is None


def test_paired_delta_significant_when_ci_excludes_zero():
    d = paired_delta([5.0] * 10, [1.0] * 10)
    assert d.mean == 4.0 and d.sd == 0.0 and d.significant is True


# -------------------------------------------------------------- median/IQR


def test_median_low_even_count_takes_lower_middle():
    assert median_low([1, 2, 3, 4]) == 2
    assert median_low([4, 1, 3, 2]) == 2   # unsorted input handled


def test_median_low_odd_count():
    assert median_low([5, 1, 3]) == 3


def test_median_iqr_even():
    med, iqr = median_iqr([1, 2, 3, 4])
    assert med == 2 and iqr == 2           # q1=1, q3=3


def test_median_iqr_odd():
    med, iqr = median_iqr([1, 2, 3, 4, 5])
    assert med == 3 and iqr == 3           # q1=1, q3=4 (median excluded)


def test_median_iqr_singleton():
    assert median_iqr([7]) == (7, 0)


# --------------------------------------------------------- aggregate shapes


def full_record():
    return GameRecord(
        mulligans_taken=1,
        kept_hand_size=6,
        kept_hand_lands=3,
        kept_hand_sources=4,
        opening_hand=["Plains"] * 4 + ["Bear"] * 3,
        commander_cast_turn=3,
        equipped_on_arrival=True,
        equipment_at_arrival=2,
        kill_turn=6,
        cmdr21_turn=None,
        table_lethal_turn=5,
        won_turn=6,
        damage_by_turn=[0, 0, 3, 6, 10, 12],
        cmdr_damage_by_turn=[0, 0, 3, 3, 5, 5],
        noncombat_damage_by_turn=[0, 0, 0, 1, 0, 0],
        damage_per_opponent_by_turn=[[0], [0], [3], [6], [10], [12]],
        board_width_by_turn=[0, 1, 2, 3, 3, 4],
        total_power_by_turn=[0, 2, 4, 7, 9, 12],
        casts_by_turn=[1, 1, 2, 4, 1, 0],
        lands_played_by_turn=[1, 1, 1, 1, 1, 0],
        mana_available_by_turn=[1, 2, 3, 4, 5, 5],
        tokens_created_by_turn=[0, 0, 1, 0, 2, 0],
        tokens_by_source={"Krenko": 3},
        trigger_fires={"Puresteel Paladin|etb_equipment|equip_free": 2},
        static_active_turn={"Puresteel Paladin|metalcraft": 3},
        combo_assembled_turn=[5],
        combo_castable_turn=[6],
        drawn_names={"Puresteel Paladin", "Sun Titan", "Swords to Plowshares",
                     "Mystery Card"},
    )


def synthetic_records():
    r1 = full_record()
    # r2: censored everywhere — nothing reached, short per-turn data
    r2 = GameRecord(casts_by_turn=[0, 1, 1], lands_played_by_turn=[1, 1, 0],
                    drawn_names={"Sun Titan"}, combo_assembled_turn=[None],
                    combo_castable_turn=[None])
    # r3: commander on 4, cmdr21 beyond the horizon (censored by until_turn)
    r3 = GameRecord(commander_cast_turn=4, equipped_on_arrival=False,
                    equipment_at_arrival=0,
                    cmdr21_turn=7, casts_by_turn=[1, 0, 1, 1, 1, 1],
                    lands_played_by_turn=[1, 1, 1, 1, 1, 1],
                    static_active_turn={"Puresteel Paladin|metalcraft": 5})
    return [r1, r2, r3]


def assert_prop(d):
    assert set(d) >= {"value", "ci"}
    assert 0.0 <= d["value"] <= 1.0
    lo, hi = d["ci"]
    assert 0.0 <= lo <= hi <= 1.0


def assert_turn_metric(d):
    assert set(d) >= {"reached_pct", "median_among_reached", "histogram"}
    assert_prop(d["reached_pct"])
    assert isinstance(d["histogram"], dict)


def test_aggregate_shapes():
    m = aggregate(synthetic_records(), until_turn=6)
    assert m["n"] == 3 and m["until_turn"] == 6

    mull = m["mulligans"]
    assert abs(mull["avg_taken"] - 1 / 3) < 1e-9
    assert_prop(mull["pct_mulliganed"])
    assert abs(mull["pct_mulliganed"]["value"] - 1 / 3) < 1e-9
    assert abs(mull["avg_kept_hand_size"] - 20 / 3) < 1e-9
    assert "avg_kept_lands" in mull and "avg_kept_sources" in mull

    cc = m["commander_cast"]
    assert_turn_metric(cc)
    assert abs(cc["reached_pct"]["value"] - 2 / 3) < 1e-9
    assert cc["median_among_reached"] == 3     # lower-middle of [3, 4]
    assert cc["histogram"] == {"3": 1, "4": 1}     # str keys: JSON round trip
    assert set(cc["pct_by_turn"]) == {"4", "5", "6"}
    for t in ("4", "5", "6"):
        assert_prop(cc["pct_by_turn"][t])
    assert abs(cc["pct_by_turn"]["4"]["value"] - 2 / 3) < 1e-9

    for key in ("kill", "cmdr21", "table_lethal", "won"):
        assert_turn_metric(m[key])
    # cmdr21_turn=7 is beyond until_turn=6: censored, not reached
    assert m["cmdr21"]["reached_pct"]["value"] == 0.0
    assert m["cmdr21"]["median_among_reached"] is None
    assert m["cmdr21"]["histogram"] == {}
    assert abs(m["kill"]["reached_pct"]["value"] - 1 / 3) < 1e-9

    eq = m["equipped_on_arrival"]
    assert_prop(eq)
    assert eq["n_arrived"] == 2 and abs(eq["value"] - 0.5) < 1e-9
    # avg equipment on board at arrival, among games it arrived: (2 + 0) / 2
    assert abs(eq["avg_equipment_at_arrival"] - 1.0) < 1e-9

    for block, keys in (
        ("damage", ("avg_by_turn", "avg_cmdr_by_turn", "avg_noncombat_by_turn",
                    "avg_total", "combat", "noncombat",
                    "avg_per_opponent_by_turn")),
        ("board", ("avg_width_by_turn", "avg_power_by_turn")),
        ("tokens", ("avg_by_turn", "avg_total", "by_source")),
    ):
        assert set(m[block]) >= set(keys)
    assert len(m["damage"]["avg_by_turn"]) == 6
    assert len(m["board"]["avg_width_by_turn"]) == 6
    assert abs(m["tokens"]["avg_total"] - 1.0) < 1e-9
    assert abs(m["tokens"]["by_source"]["Krenko"] - 1.0) < 1e-9   # 3 fires / 3

    dm = m["damage"]
    # explicit combat/noncombat split: combat = total - noncombat, per turn
    # (only r1 has damage data, so turn 4 is r1's alone: 6 total, 1 noncombat)
    assert abs(dm["combat"]["avg_by_turn"][3] - 5.0) < 1e-9
    assert abs(dm["noncombat"]["avg_by_turn"][3] - 1.0) < 1e-9
    assert abs(dm["combat"]["avg_total"] + dm["noncombat"]["avg_total"]
               - dm["avg_total"]) < 1e-9
    # per-opponent by turn: [turn][opponent], mean among games with data
    assert len(dm["avg_per_opponent_by_turn"]) == 6
    assert dm["avg_per_opponent_by_turn"][3] == [6.0]

    casts = m["casts"]
    assert casts["distribution"] == {"0": 3, "1": 10, "2": 1, "4": 1}
    assert len(casts["avg_by_turn"]) == 6
    assert casts["max_chain"]["max"] == 4
    assert abs(casts["max_chain"]["avg"] - (4 + 1 + 1) / 3) < 1e-9
    assert_prop(casts["pct_4plus_turn_by_t6"])
    assert abs(casts["pct_4plus_turn_by_t6"]["value"] - 1 / 3) < 1e-9

    oc = m["on_curve"]
    assert_prop(oc["pct_all_drops_through_t5"])
    # r1 and r3 made every drop T1-T5; r2 missed its third drop
    assert abs(oc["pct_all_drops_through_t5"]["value"] - 2 / 3) < 1e-9
    assert len(oc["avg_lands_by_turn"]) == 6
    assert len(oc["avg_mana_by_turn"]) == 6

    tf = m["trigger_fires"]
    assert abs(tf["Puresteel Paladin|etb_equipment|equip_free"] - 2 / 3) < 1e-9

    st = m["statics"]["Puresteel Paladin|metalcraft"]
    assert_prop(st["pct_online_by_t4"])
    assert abs(st["pct_online_by_t4"]["value"] - 1 / 3) < 1e-9  # turn 5 misses T4

    combos = m["combos"]
    assert len(combos) == 1
    assert_turn_metric(combos[0]["assembled"])
    assert_turn_metric(combos[0]["castable"])
    assert abs(combos[0]["assembled"]["reached_pct"]["value"] - 1 / 3) < 1e-9
    assert combos[0]["assembled"]["median_among_reached"] == 5

    assert "honesty" not in m   # only present when card_scopes is supplied


def test_avg_by_turn_none_when_no_game_has_data():
    # games ended by turn 2: later turns have NO data and emit None, not 0.0
    recs = [GameRecord(damage_by_turn=[1, 2]), GameRecord(damage_by_turn=[3])]
    m = aggregate(recs, until_turn=4)
    assert m["damage"]["avg_by_turn"] == [2.0, 2.0, None, None]


def test_aggregate_empty_records_rejected():
    try:
        aggregate([], until_turn=6)
    except ValueError:
        pass
    else:
        raise AssertionError("aggregate([]) should raise ValueError")


# ------------------------------------------------------------------ honesty


def test_honesty_three_way_split():
    scopes = {
        "Swords to Plowshares": "interaction_removal",
        "Mystery Card": "unrecognized",
        "Puresteel Paladin": None,   # modeled, fires triggers
        "Sun Titan": None,           # modeled, drawn, never fires -> low impact
    }
    m = aggregate(synthetic_records(), until_turn=6, card_scopes=scopes)
    h = m["honesty"]
    assert set(h) == {"out_of_scope", "unrecognized", "modeled_low_impact"}

    oos = h["out_of_scope"]
    assert list(oos) == ["interaction_removal"]
    (entry,) = oos["interaction_removal"]
    assert entry["name"] == "Swords to Plowshares"
    assert_prop(entry["drawn_pct"])
    assert abs(entry["drawn_pct"]["value"] - 1 / 3) < 1e-9

    (unrec,) = h["unrecognized"]
    assert unrec["name"] == "Mystery Card"
    assert_prop(unrec["drawn_pct"])

    assert h["modeled_low_impact"] == ["Sun Titan"]
