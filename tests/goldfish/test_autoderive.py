"""Auto-derivation tests. Fixtures are hand-trimmed real Scryfall JSON
(fields kept: name, type_line, mana_cost, oracle_text, power, toughness,
keywords, produced_mana, card_faces), pulled once via the Scryfall API.

The first six fixtures and the five tests that use them are the plan's
Task 17 Step-1 block verbatim. Note: the plan's Step-3 fetch modeling
(sac_self honored by the engine) was NOT built into the frozen engine, so
fetch lands are approximated as producing lands (see
test_fetch_approximated_as_producing_land) — an approx note flags this.
"""
from goldfish.autoderive import derive

PLAINS = {"name": "Plains", "type_line": "Basic Land — Plains",
          "oracle_text": "({T}: Add {W}.)", "produced_mana": ["W"], "keywords": []}
GUILDGATE = {"name": "Boros Guildgate", "type_line": "Land — Gate",
             "oracle_text": "Boros Guildgate enters the battlefield tapped.\n{T}: Add {R} or {W}.",
             "produced_mana": ["R", "W"], "keywords": []}
SOL_RING = {"name": "Sol Ring", "type_line": "Artifact", "mana_cost": "{1}",
            "oracle_text": "{T}: Add {C}{C}.", "produced_mana": ["C"], "keywords": []}
DIVINATION = {"name": "Divination", "type_line": "Sorcery", "mana_cost": "{2}{U}",
              "oracle_text": "Draw two cards.", "keywords": []}
SWORDS = {"name": "Swords to Plowshares", "type_line": "Instant", "mana_cost": "{W}",
          "oracle_text": "Exile target creature. Its controller gains life equal to its power.",
          "keywords": []}
SAGA = {"name": "Summon: Kujata", "type_line": "Enchantment — Saga",
        "mana_cost": "{1}{G}", "oracle_text": "(As this Saga enters...)", "keywords": []}

# ── Additional trimmed real Scryfall fixtures ───────────────────────────────

# Current (post-2024) Scryfall templating of the same Guildgate — "This land
# enters tapped." instead of "<Name> enters the battlefield tapped."
GUILDGATE_CURRENT = {
    "name": "Boros Guildgate", "type_line": "Land — Gate", "mana_cost": "",
    "oracle_text": "This land enters tapped.\n{T}: Add {R} or {W}.",
    "keywords": [], "produced_mana": ["R", "W"]}

CULTIVATE = {
    "name": "Cultivate", "type_line": "Sorcery", "mana_cost": "{2}{G}",
    "oracle_text": "Search your library for up to two basic land cards, "
                   "reveal those cards, put one onto the battlefield tapped "
                   "and the other into your hand, then shuffle.",
    "keywords": []}

ARCANE_SIGNET = {
    "name": "Arcane Signet", "type_line": "Artifact", "mana_cost": "{2}",
    "oracle_text": "{T}: Add one mana of any color in your commander's color identity.",
    "keywords": [], "produced_mana": ["B", "G", "R", "U", "W"]}

LIGHTNING_GREAVES = {
    "name": "Lightning Greaves", "type_line": "Artifact — Equipment",
    "mana_cost": "{2}",
    "oracle_text": "Equipped creature has haste and shroud. (It can't be the "
                   "target of spells or abilities.)\nEquip {0}",
    "keywords": ["Equip"]}

BONESPLITTER = {
    "name": "Bonesplitter", "type_line": "Artifact — Equipment", "mana_cost": "{1}",
    "oracle_text": "Equipped creature gets +2/+0.\nEquip {1}",
    "keywords": ["Equip"]}

COMMAND_TOWER = {
    "name": "Command Tower", "type_line": "Land", "mana_cost": "",
    "oracle_text": "{T}: Add one mana of any color in your commander's color identity.",
    "keywords": [], "produced_mana": ["B", "G", "R", "U", "W"]}

EVOLVING_WILDS = {
    "name": "Evolving Wilds", "type_line": "Land", "mana_cost": "",
    "oracle_text": "{T}, Sacrifice this land: Search your library for a basic "
                   "land card, put it onto the battlefield tapped, then shuffle.",
    "keywords": []}

DESERTED_BEACH = {
    "name": "Deserted Beach", "type_line": "Land", "mana_cost": "",
    "oracle_text": "This land enters tapped unless you control two or more "
                   "other lands.\n{T}: Add {W} or {U}.",
    "keywords": [], "produced_mana": ["U", "W"]}

DELVER = {
    "name": "Delver of Secrets // Insectile Aberration",
    "type_line": "Creature — Human Wizard // Creature — Human Insect",
    "keywords": ["Flying", "Transform"],
    "card_faces": [
        {"name": "Delver of Secrets", "type_line": "Creature — Human Wizard",
         "mana_cost": "{U}",
         "oracle_text": "At the beginning of your upkeep, look at the top card "
                        "of your library. You may reveal that card. If an "
                        "instant or sorcery card is revealed this way, "
                        "transform this creature.",
         "power": "1", "toughness": "1"},
        {"name": "Insectile Aberration", "type_line": "Creature — Human Insect",
         "mana_cost": "", "oracle_text": "Flying", "power": "3", "toughness": "2"},
    ]}

TORMENT = {
    "name": "Torment of Hailfire", "type_line": "Sorcery", "mana_cost": "{X}{B}{B}",
    "oracle_text": "Repeat the following process X times. Each opponent loses "
                   "3 life unless that player sacrifices a nonland permanent "
                   "of their choice or discards a card.",
    "keywords": []}

KRENKO = {
    "name": "Krenko, Mob Boss", "type_line": "Legendary Creature — Goblin Warrior",
    "mana_cost": "{2}{R}{R}",
    "oracle_text": "{T}: Create X 1/1 red Goblin creature tokens, where X is "
                   "the number of Goblins you control.",
    "power": "3", "toughness": "3", "keywords": []}

GRIZZLY_BEARS = {
    "name": "Grizzly Bears", "type_line": "Creature — Bear", "mana_cost": "{1}{G}",
    "oracle_text": "", "power": "2", "toughness": "2", "keywords": []}

SERRA_ANGEL = {
    "name": "Serra Angel", "type_line": "Creature — Angel", "mana_cost": "{3}{W}{W}",
    "oracle_text": "Flying\nVigilance (Attacking doesn't cause this creature to tap.)",
    "power": "4", "toughness": "4", "keywords": ["Flying", "Vigilance"]}

LLANOWAR_ELVES = {
    "name": "Llanowar Elves", "type_line": "Creature — Elf Druid", "mana_cost": "{G}",
    "oracle_text": "{T}: Add {G}.", "power": "1", "toughness": "1",
    "keywords": [], "produced_mana": ["G"]}

DAY_OF_JUDGMENT = {
    "name": "Day of Judgment", "type_line": "Sorcery", "mana_cost": "{2}{W}{W}",
    "oracle_text": "Destroy all creatures.", "keywords": []}

COUNTERSPELL = {
    "name": "Counterspell", "type_line": "Instant", "mana_cost": "{U}{U}",
    "oracle_text": "Counter target spell.", "keywords": []}

HEROIC_INTERVENTION = {
    "name": "Heroic Intervention", "type_line": "Instant", "mana_cost": "{1}{G}",
    "oracle_text": "Permanents you control gain hexproof and indestructible "
                   "until end of turn.",
    "keywords": []}

COUNCILS_JUDGMENT = {
    "name": "Council's Judgment", "type_line": "Sorcery", "mana_cost": "{1}{W}{W}",
    "oracle_text": "Will of the council — Starting with you, each player votes "
                   "for a nonland permanent you don't control. Exile each "
                   "permanent with the most votes or tied for most votes.",
    "keywords": ["Will of the council"]}

KRARKS_THUMB = {
    "name": "Krark's Thumb", "type_line": "Legendary Artifact", "mana_cost": "{2}",
    "oracle_text": "If you would flip a coin, instead flip two coins and ignore one.",
    "keywords": []}

ASHNODS_ALTAR = {
    "name": "Ashnod's Altar", "type_line": "Artifact", "mana_cost": "{3}",
    "oracle_text": "Sacrifice a creature: Add {C}{C}.",
    "keywords": [], "produced_mana": ["C"]}

NECROPOTENCE = {
    "name": "Necropotence", "type_line": "Enchantment", "mana_cost": "{B}{B}{B}",
    "oracle_text": "Skip your draw step.\nWhenever you discard a card, exile "
                   "that card from your graveyard.\nPay 1 life: Exile the top "
                   "card of your library face down. Put that card into your "
                   "hand at the beginning of your next end step.",
    "keywords": []}

ALL_FIVE = {"W": 1, "U": 1, "B": 1, "R": 1, "G": 1}


# ── Plan Task 17 tests (verbatim) ───────────────────────────────────────────

def test_land_derivation():
    d = derive([PLAINS, GUILDGATE])
    assert d["Plains"].card.data.produces == {"W": 1}
    assert d["Boros Guildgate"].card.data.enters_tapped is True
    assert d["Boros Guildgate"].card.data.produces == {"R": 1, "W": 1}


def test_rock_quantity():
    d = derive([SOL_RING])
    assert d["Sol Ring"].card.data.produces == {"C": 2}
    assert d["Sol Ring"].auto_annotated is True


def test_draw_spell_gets_trigger():
    d = derive([DIVINATION])
    ann = d["Divination"].card.ann
    assert ann and ann.triggers[0].do == "draw" and ann.triggers[0].count == 2


def test_out_of_scope_classes():
    d = derive([SWORDS, SAGA])
    assert d["Swords to Plowshares"].card.scope_class == "interaction_removal"
    assert d["Summon: Kujata"].card.scope_class == "unmodeled_other"
    assert d["Swords to Plowshares"].needs_annotation is False   # classified, not asked


def test_unrecognized_flagged_for_annotation():
    weird = {"name": "Puresteel Paladin", "type_line": "Creature — Human Knight",
             "mana_cost": "{W}{W}", "power": "2", "toughness": "2",
             "oracle_text": "Metalcraft — ... equip abilities you activate cost {0}...",
             "keywords": []}
    d = derive([weird])
    assert d["Puresteel Paladin"].needs_annotation is True
    assert d["Puresteel Paladin"].card.scope_class is None


# ── Lands ───────────────────────────────────────────────────────────────────

def test_land_current_scryfall_templating():
    d = derive([GUILDGATE_CURRENT])
    got = d["Boros Guildgate"]
    assert got.card.data.enters_tapped is True
    assert got.card.data.produces == {"R": 1, "W": 1}
    assert got.auto_annotated is True and got.needs_annotation is False


def test_conditional_tapland_approximated_untapped():
    d = derive([DESERTED_BEACH])
    got = d["Deserted Beach"]
    assert got.card.data.enters_tapped is False        # common case
    assert got.card.data.produces == {"W": 1, "U": 1}
    assert any("conditional" in n for n in got.approx_notes)


def test_any_color_land():
    d = derive([COMMAND_TOWER])
    got = d["Command Tower"]
    assert got.card.data.produces == ALL_FIVE
    assert any("any" in n for n in got.approx_notes)   # honesty flag
    assert got.auto_annotated is True


def test_fetch_approximated_as_producing_land():
    # DEVIATION from the plan's Step-3 fetch clause: the frozen engine has no
    # sac_self mechanism, so fetches are modeled as producing lands (union of
    # fetchable basic colors), entering untapped, with an approx note.
    d = derive([EVOLVING_WILDS])
    got = d["Evolving Wilds"]
    assert got.card.is_land
    assert got.card.data.produces == ALL_FIVE          # "basic land card" = any basic
    assert got.card.data.enters_tapped is False
    assert any("fetch approximated" in n for n in got.approx_notes)
    assert any("landfall" in n for n in got.approx_notes)
    assert got.auto_annotated is True and got.needs_annotation is False


# ── Rocks and dorks ─────────────────────────────────────────────────────────

def test_any_color_rock():
    d = derive([ARCANE_SIGNET])
    got = d["Arcane Signet"]
    assert got.card.data.produces == ALL_FIVE
    assert got.auto_annotated is True
    assert any("any" in n for n in got.approx_notes)


def test_mana_dork_flagged_for_sickness_approximation():
    d = derive([LLANOWAR_ELVES])
    got = d["Llanowar Elves"]
    assert got.card.data.produces == {"G": 1}
    assert got.auto_annotated is True
    assert any("sickness" in n for n in got.approx_notes)


# ── Ramp spells ─────────────────────────────────────────────────────────────

def test_ramp_spell_cultivate():
    d = derive([CULTIVATE])
    got = d["Cultivate"]
    ann = got.card.ann
    assert ann is not None
    assert ann.triggers[0].on == "cast"
    assert ann.triggers[0].do == "ramp_land"
    assert ann.triggers[0].count == 2                  # "two" in the search clause
    assert got.auto_annotated is True
    # one land actually goes to hand — flagged, not silently over-modeled
    assert any("hand" in n for n in got.approx_notes)


# ── Equipment ───────────────────────────────────────────────────────────────

def test_equipment_stat_grants():
    d = derive([BONESPLITTER])
    got = d["Bonesplitter"]
    assert got.card.ann is not None and got.card.ann.grants is not None
    assert got.card.ann.grants["power"] == 2
    assert got.card.ann.grants["toughness"] == 0
    assert got.card.data.equip_cost is not None
    assert got.card.data.equip_cost.mv == 1
    assert got.auto_annotated is True


def test_equipment_keyword_grants_equip_zero():
    d = derive([LIGHTNING_GREAVES])
    got = d["Lightning Greaves"]
    assert got.card.ann is not None and got.card.ann.grants is not None
    assert "haste" in got.card.ann.grants["keywords"]
    assert got.card.data.equip_cost is not None
    assert got.card.data.equip_cost.mv == 0            # Equip {0} is a real cost
    assert got.auto_annotated is True


# ── Double-faced cards ──────────────────────────────────────────────────────

def test_dfc_derives_from_front_face():
    d = derive([DELVER])
    got = d["Delver of Secrets // Insectile Aberration"]
    assert got.card.mv == 1                            # front face {U}
    assert got.card.data.power == 1 and got.card.data.toughness == 1
    assert got.needs_annotation is True                # upkeep flip isn't modeled
    assert any("back face" in n for n in got.approx_notes)


# ── D9 out-of-scope classes ─────────────────────────────────────────────────

def test_x_cost_is_unmodeled_other():
    d = derive([TORMENT])
    got = d["Torment of Hailfire"]
    assert got.card.scope_class == "unmodeled_other"
    assert got.card.data.cost is None                  # {X} never reaches parse_cost
    assert got.needs_annotation is False


def test_wipe_counter_protection_political():
    d = derive([DAY_OF_JUDGMENT, COUNTERSPELL, HEROIC_INTERVENTION,
                COUNCILS_JUDGMENT])
    assert d["Day of Judgment"].card.scope_class == "interaction_wipe"
    assert d["Counterspell"].card.scope_class == "interaction_counter"
    assert d["Heroic Intervention"].card.scope_class == "protection"
    assert d["Council's Judgment"].card.scope_class == "political"
    assert all(not d[n].needs_annotation for n in d)


def test_unmodeled_other_patterns():
    d = derive([KRARKS_THUMB, ASHNODS_ALTAR, NECROPOTENCE])
    assert d["Krark's Thumb"].card.scope_class == "unmodeled_other"     # coin flip
    assert d["Ashnod's Altar"].card.scope_class == "unmodeled_other"    # sac cost
    assert d["Necropotence"].card.scope_class == "unmodeled_other"      # life payment
    assert all(not d[n].needs_annotation for n in d)
    assert all(not d[n].auto_annotated for n in d)


# ── Vanilla creatures ───────────────────────────────────────────────────────

def test_vanilla_creature_needs_nothing():
    d = derive([GRIZZLY_BEARS])
    got = d["Grizzly Bears"]
    assert got.needs_annotation is False
    assert got.auto_annotated is True
    assert got.card.scope_class is None
    assert got.card.data.power == 2 and got.card.data.toughness == 2


def test_keywords_only_creature_is_vanilla():
    d = derive([SERRA_ANGEL])
    got = d["Serra Angel"]
    assert got.needs_annotation is False
    assert got.auto_annotated is True
    assert got.card.data.keywords == frozenset({"flying", "vigilance"})


# ── Needs annotation ────────────────────────────────────────────────────────

def test_krenko_needs_annotation():
    d = derive([KRENKO])
    got = d["Krenko, Mob Boss"]
    assert got.needs_annotation is True
    assert got.card.scope_class is None
    assert got.auto_annotated is False
    assert "legendary" in got.card.data.types and got.card.is_creature
    assert got.oracle == KRENKO["oracle_text"]         # oracle surfaced for Claude
