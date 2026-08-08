"""Task 12: greedy policy — rule order, reasons, guards, determinism."""
from goldfish.cards import SimCard, parse_cost
from goldfish.engine import IllegalAction, new_game, step
from goldfish.policy import choose_action
from tests.goldfish.test_cards import make_data
from tests.goldfish.test_engine import annotated, mini_cards, started


def drive_main(g, max_actions=30):
    """Run policy until it passes out of the current phase."""
    taken = []
    for _ in range(max_actions):
        action, reason = choose_action(g)
        taken.append((action, reason))
        step(g, action)
        if action["type"] == "pass":
            break
    return taken


def scripted_cards():
    """mini_cards plus one card per policy rule family."""
    cards = mini_cards()
    cards["Sol"] = SimCard(data=make_data(
        "Sol", cost=parse_cost("{1}"), types=frozenset({"artifact"}),
        produces={"C": 2}), ann=None, scope_class=None)
    cards["Anthem"] = SimCard(data=make_data(
        "Anthem", cost=parse_cost("{1}"), types=frozenset({"enchantment"})),
        ann=None, scope_class=None)
    annotated(cards, "Anthem", {"name": "Anthem", "statics": [
        {"kind": "anthem", "power": 1, "toughness": 1}]})
    cards["Orb"] = SimCard(data=make_data(
        "Orb", cost=parse_cost("{1}"), types=frozenset({"artifact"})),
        ann=None, scope_class=None)
    annotated(cards, "Orb", {"name": "Orb", "activated": [
        {"cost": "{T}", "do": "draw"}]})
    cards["Greaves"] = SimCard(data=make_data(
        "Greaves", cost=parse_cost("{1}"),
        types=frozenset({"artifact", "equipment"}),
        equip_cost=parse_cost("{0}")), ann=None, scope_class=None)
    return cards


def full_cards():
    cards = scripted_cards()
    cards["Krenko"] = SimCard(data=make_data(
        "Krenko", cost=parse_cost("{2}{R}"), types=frozenset({"creature"}),
        power=3, toughness=3), ann=None, scope_class=None)
    annotated(cards, "Krenko", {"name": "Krenko", "activated": [
        {"cost": "{T}", "do": "create_token", "power": 1, "toughness": 1,
         "count": 2}]})
    return cards


def rich_deck():
    return (["Plains"] * 12 + ["Mountain"] * 12 + ["Bear"] * 4 + ["Runner"] * 4
            + ["Hammer"] * 2 + ["Sol"] * 2 + ["Anthem"] * 2 + ["Orb"] * 2
            + ["Greaves"] * 2 + ["Krenko"] * 2)


# -- plan tests --------------------------------------------------------------

def test_equipment_before_commander_when_both_payable():
    cards = mini_cards()
    annotated(cards, "Boss", {"name": "Boss", "triggers": [
        {"on": "etb", "do": "attach_from_board"}]})
    g = started(cards, ["Plains"] * 40, hand=["Hammer"])   # Hammer {1}, Boss {2}{R}
    for _ in range(4):
        g.new_perm("Mountain")
    taken = drive_main(g)
    order = [a["card"] for a, _ in taken if a["type"] == "cast"]
    assert order.index("Hammer") < order.index("Boss")
    boss_perm = next(p for p in g.battlefield if p.name == "Boss")
    assert boss_perm.attached                              # ETB attached the Hammer
    reasons = [r for a, r in taken if a["type"] == "cast" and a["card"] == "Hammer"]
    assert "rule 4" in reasons[0]


def test_commander_first_when_equipment_would_starve_it():
    cards = mini_cards()
    g = started(cards, ["Plains"] * 40, hand=["Hammer"])
    for _ in range(3):
        g.new_perm("Mountain")                              # exactly {2}{R}
    taken = drive_main(g)
    casts = [a["card"] for a, _ in taken if a["type"] == "cast"]
    assert casts and casts[0] == "Boss"                     # guard: Boss stays payable


def test_tap_producer_withheld_from_attack():
    cards = mini_cards()
    cards["Krenko"] = SimCard(data=make_data("Krenko", cost=parse_cost("{2}{R}"),
                              types=frozenset({"creature"}), power=3, toughness=3),
                              ann=None, scope_class=None)
    annotated(cards, "Krenko", {"name": "Krenko", "activated": [
        {"cost": "{T}", "do": "create_token", "power": 1, "toughness": 1, "count": 2}]})
    g = started(cards, ["Plains"] * 40, hand=[])
    g.turn = 4
    k = g.new_perm("Krenko"); k.arrived_turn = 3
    b = g.new_perm("Bear"); b.arrived_turn = 3
    # main1 -> combat -> main2 full turn under policy. Break at the turn
    # boundary BEFORE stepping the main2 pass: that pass rolls straight into
    # the next turn ("end" is transient) and _begin_new_turn untaps every
    # permanent, which would erase the tapped state asserted below.
    start = g.turn
    for _ in range(40):
        action, _ = choose_action(g)
        if g.phase == "main2" and action["type"] == "pass":
            break
        step(g, action)
    assert g.turn == start
    assert k.tapped and any(p.is_token for p in g.battlefield)  # activated post-combat
    assert g.opponents == [38]                                  # only Bear attacked


# -- reasons name the fired rule ---------------------------------------------

def test_reason_strings_name_each_rule():
    cards = scripted_cards()
    g = started(cards, ["Plains"] * 40,
                hand=["Mountain", "Sol", "Anthem", "Orb", "Greaves"])
    for _ in range(4):
        g.new_perm("Mountain")
    reasons = []
    start = g.turn
    for _ in range(60):
        action, reason = choose_action(g)
        reasons.append(reason)
        step(g, action)
        if g.turn > start:
            break
    for n in range(1, 10):
        assert any(r.startswith(f"rule {n}:") for r in reasons), \
            f"rule {n} never fired; reasons: {reasons}"


# -- mulligan phase ------------------------------------------------------------

def test_mulligan_decisions_and_reasons():
    cards = mini_cards()
    g = new_game(cards, ["Bear"] * 40, "Boss", seed=5)     # zero-source hands
    taken = []
    while g.phase == "mulligan":
        action, reason = choose_action(g)
        taken.append((action, reason))
        step(g, action)
    kinds = [a["type"] for a, _ in taken]
    # free mulligan, two real ones, then the London always-keep at 5
    assert kinds == ["mulligan", "mulligan", "mulligan", "keep"]
    assert all(r.startswith("mulligan:") for _, r in taken)
    assert len(taken[-1][0]["bottom"]) == 2
    assert g.kept_hand_size == 5
    # kept 5, then the turn-1 draw (this solitaire format always draws)
    assert g.phase == "main1" and g.turn == 1 and len(g.hand) == 6

    g2 = new_game(cards, ["Plains"] * 40, "Boss", seed=1)
    g2.hand[:] = ["Plains", "Plains", "Plains", "Plains", "Bear", "Bear", "Bear"]
    action, reason = choose_action(g2)
    assert action["type"] == "keep" and action["bottom"] == []
    assert reason.startswith("mulligan:") and "4 sources" in reason


# -- combo-piece holding -------------------------------------------------------

def test_combo_instant_piece_held_permanent_piece_deployed():
    cards = mini_cards()
    cards["Kiki"] = SimCard(data=make_data(
        "Kiki", cost=parse_cost("{1}{R}"), types=frozenset({"creature"}),
        power=2, toughness=2), ann=None, scope_class=None)
    cards["Twin"] = SimCard(data=make_data(
        "Twin", cost=parse_cost("{R}"), types=frozenset({"sorcery"})),
        ann=None, scope_class=None)
    g = new_game(cards, ["Plains"] * 40, "Boss", seed=1,
                 combos=[["Kiki", "Twin"]])
    g.phase, g.turn = "main1", 1
    g.hand[:] = ["Kiki", "Twin"]
    for _ in range(6):
        g.new_perm("Mountain")
    taken = drive_main(g)
    casts = [a["card"] for a, _ in taken if a["type"] == "cast"]
    assert "Kiki" in casts               # permanent piece: deploying is fine
    assert "Twin" not in casts           # instant/sorcery piece: held
    assert "Twin" in g.hand
    assert any(p.name == "Kiki" for p in g.battlefield)


# -- rule 6: ritual ordering ---------------------------------------------------

def test_rule6_ritual_early_when_starved_else_last():
    cards = mini_cards()
    cards["Rite"] = SimCard(data=make_data(
        "Rite", cost=parse_cost("{R}"), types=frozenset({"sorcery"})),
        ann=None, scope_class=None)
    annotated(cards, "Rite", {"name": "Rite", "triggers": [
        {"on": "cast", "do": "add_mana", "pips": "{R}{R}{R}"}]})
    cards["Dragon"] = SimCard(data=make_data(
        "Dragon", cost=parse_cost("{3}{R}"), types=frozenset({"creature"}),
        power=5, toughness=5), ann=None, scope_class=None)

    # hand cost (Dragon: 4) exceeds available mana (2): ritual casts early
    g = started(cards, ["Plains"] * 40, hand=["Rite", "Dragon"])
    for _ in range(2):
        g.new_perm("Mountain")
    taken = drive_main(g)
    casts = [(a["card"], r) for a, r in taken if a["type"] == "cast"]
    assert casts[0][0] == "Rite" and "ritual early" in casts[0][1]

    # hand cost (Runner: 1) fits available mana (6): ritual casts last
    g2 = started(cards, ["Plains"] * 40, hand=["Rite", "Runner"])
    for _ in range(6):
        g2.new_perm("Mountain")
    taken2 = drive_main(g2)
    casts2 = [(a["card"], r) for a, r in taken2 if a["type"] == "cast"]
    assert [c for c, _ in casts2].index("Rite") == len(casts2) - 1
    assert "ritual last" in casts2[-1][1]


# -- combat: attack every combat instance --------------------------------------

def test_policy_attacks_every_combat_even_with_no_creatures():
    cards = mini_cards()
    cards["Shrine"] = SimCard(data=make_data(
        "Shrine", cost=parse_cost("{1}"), types=frozenset({"enchantment"})),
        ann=None, scope_class=None)
    annotated(cards, "Shrine", {"name": "Shrine", "triggers": [
        {"on": "combat_begin", "do": "gain_life", "count": 1}]})
    g = started(cards, ["Plains"] * 40)          # W producers: Boss never castable
    g.new_perm("Shrine")
    for _ in range(300):
        if g.turn > 3:
            break
        action, _ = choose_action(g)
        step(g, action)
    # empty attacks still consume each combat: combat_begin fired every turn
    assert g.trigger_fires["Shrine|combat_begin|gain_life"] == 3
    assert g.life_gained == 3


# -- rule 1: untapped-land preference -------------------------------------------

def test_untapped_land_preferred_when_it_enables_cast():
    cards = mini_cards()
    cards["Gate"] = SimCard(data=make_data(
        "Gate", types=frozenset({"land"}), produces={"R": 1},
        enters_tapped=True), ann=None, scope_class=None)
    g = started(cards, ["Plains"] * 40, hand=["Gate", "Mountain", "Runner"])
    action, reason = choose_action(g)
    # "Gate" sorts before "Mountain", but only an untapped Mountain lets
    # Runner ({R}) be cast this turn.
    assert action == {"type": "play_land", "card": "Mountain"}
    assert reason.startswith("rule 1:") and "enabling" in reason
    taken = drive_main(g)
    assert {"type": "cast", "card": "Runner"} in [a for a, _ in taken]


# -- determinism + legality ------------------------------------------------------

def _play_game(seed, until_turn=8, max_actions=600):
    cards = full_cards()
    g = new_game(cards, rich_deck(), "Boss", seed=seed)
    taken = []
    for _ in range(max_actions):
        if g.turn > until_turn or g.won_turn is not None:
            return g, taken
        action, reason = choose_action(g)
        taken.append((action, reason))
        step(g, action)
    raise AssertionError(f"seed {seed}: game stalled before turn {until_turn + 1}")


def test_full_game_determinism_two_seeds():
    for seed in (7, 21):
        g1, a1 = _play_game(seed)
        g2, a2 = _play_game(seed)
        assert a1 == a2                          # identical action+reason sequence
        assert g1.log == g2.log                  # byte-identical logs
    ga, _ = _play_game(7)
    gb, _ = _play_game(21)
    assert ga.log != gb.log                      # different seeds actually diverge


def test_policy_never_illegal_across_seeds():
    for seed in range(50):
        cards = full_cards()
        g = new_game(cards, rich_deck(), "Boss", seed=seed)
        for _ in range(600):
            if g.turn > 8 or g.won_turn is not None:
                break
            action, reason = choose_action(g)
            try:
                step(g, action)
            except IllegalAction as e:           # zero tolerance
                raise AssertionError(
                    f"seed {seed}: illegal {action!r} ({reason}): {e}") from e
        else:
            raise AssertionError(f"seed {seed}: game stalled before turn 9")
