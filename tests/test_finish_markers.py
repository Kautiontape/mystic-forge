import server


def test_finish_marker_maps_finishes_to_archidekt_syntax():
    assert server._finish_marker("foil") == "*F*"
    assert server._finish_marker("etched") == "*E*"


def test_finish_marker_is_empty_for_nonfoil_and_none():
    assert server._finish_marker("nonfoil") == ""
    assert server._finish_marker(None) == ""
    assert server._finish_marker("") == ""


def test_deck_card_entry_accepts_a_finish():
    entry = server.DeckCardEntry(name="Sol Ring", finish="foil")
    assert entry.finish.value == "foil"


def test_deck_card_entry_finish_defaults_to_none():
    assert server.DeckCardEntry(name="Sol Ring").finish is None


def test_archidekt_line_places_the_marker_after_collector_before_category():
    # Matches Archidekt's own export grammar, verified 2026-08-08:
    #   1x Name (set) 382 *F* [Commander{top}]
    line = server._archidekt_line(
        quantity=1, name="Counterspell", set_code="dmr", collector="281",
        finish="foil", category=" [Draw]", labels=" ^Test,#2ccce4^")
    assert line == "1x Counterspell (dmr) 281 *F* [Draw] ^Test,#2ccce4^"


def test_archidekt_line_omits_the_marker_for_nonfoil_and_none():
    for finish in (None, "nonfoil"):
        line = server._archidekt_line(
            quantity=1, name="Counterspell", set_code="dmr", collector="281",
            finish=finish, category=" [Draw]", labels="")
        assert line == "1x Counterspell (dmr) 281 [Draw]"


def test_archidekt_line_without_a_printing():
    assert server._archidekt_line(
        quantity=2, name="Sol Ring", set_code="", collector="",
        finish=None, category="", labels="") == "2x Sol Ring"


async def test_format_archidekt_renders_every_category_and_label_branch(monkeypatch):
    """Permanent guard for the line-building refactor.

    format_archidekt batches one Scryfall lookup; stubbing just that call keeps
    the test offline like the rest of the suite while still exercising the real
    commander / maybeboard / category / label / finish branches end to end.
    """
    async def fake_scryfall_post(endpoint, body):
        return {
            "data": [
                {"name": i["name"], "set": "dmr", "collector_number": "281"}
                for i in body["identifiers"]
            ],
            "not_found": [],
        }
    monkeypatch.setattr(server, "_scryfall_post", fake_scryfall_post)

    out = await server.format_archidekt(server.FormatDeckInput(cards=[
        server.DeckCardEntry(name="Sol Ring", category="Ramp"),
        server.DeckCardEntry(name="Atraxa", commander=True),
        server.DeckCardEntry(name="Rhystic Study", maybeboard=True),
        server.DeckCardEntry(
            name="Cultivate", category="Ramp", label="To Buy", label_color="#2ccce4"),
        server.DeckCardEntry(name="Brainstorm", category="Draw", label="Have"),
        server.DeckCardEntry(name="Counterspell", category="Draw", finish="foil"),
    ]))
    lines = out.splitlines()

    assert "1x Sol Ring [Ramp]" in lines
    assert "1x Atraxa [Commander{top}]" in lines
    assert "1x Rhystic Study [Maybeboard{noDeck}{noPrice}]" in lines
    assert "1x Cultivate [Ramp] ^To Buy,#2ccce4^" in lines
    assert "1x Brainstorm [Draw] ^Have^" in lines      # label with no colour
    assert "1x Counterspell *F* [Draw]" in lines       # marker without set codes


async def test_format_archidekt_places_the_marker_after_the_collector_number(monkeypatch):
    async def fake_scryfall_post(endpoint, body):
        return {
            "data": [
                {"name": i["name"], "set": "dmr", "collector_number": "281"}
                for i in body["identifiers"]
            ],
            "not_found": [],
        }
    monkeypatch.setattr(server, "_scryfall_post", fake_scryfall_post)

    out = await server.format_archidekt(server.FormatDeckInput(
        include_set_codes=True,
        cards=[
            server.DeckCardEntry(name="Counterspell", category="Draw", finish="foil"),
            server.DeckCardEntry(name="Arcane Signet", category="Ramp", finish="etched"),
            server.DeckCardEntry(name="Sol Ring", category="Ramp"),
        ]))
    lines = out.splitlines()

    assert "1x Counterspell (dmr) 281 *F* [Draw]" in lines
    assert "1x Arcane Signet (dmr) 281 *E* [Ramp]" in lines
    assert "1x Sol Ring (dmr) 281 [Ramp]" in lines     # no finish, no marker


def test_modifier_finishes_maps_archidekt_values():
    assert server.MODIFIER_FINISHES["Foil"] == "foil"
    assert server.MODIFIER_FINISHES["Etched"] == "etched"
    assert server.MODIFIER_FINISHES["Normal"] == "nonfoil"


def test_export_line_carries_the_modifier_as_a_marker():
    assert server._archidekt_line(
        quantity=1, name="Sol Ring", set_code="ltc", collector="284",
        finish=server.MODIFIER_FINISHES.get("Foil"),
        category=" [Ramp]", labels="") == "1x Sol Ring (ltc) 284 *F* [Ramp]"


def test_export_line_has_no_marker_for_normal_or_unknown_modifiers():
    for modifier in ("Normal", "", "Something Else"):
        line = server._archidekt_line(
            quantity=1, name="Sol Ring", set_code="ltc", collector="284",
            finish=server.MODIFIER_FINISHES.get(modifier),
            category=" [Ramp]", labels="")
        assert line == "1x Sol Ring (ltc) 284 [Ramp]"


async def test_archidekt_export_preserves_foil_and_etched_modifiers(monkeypatch):
    """Archidekt gives us the modifier; we must hand it back.

    Dropping it is why a foil deck exported through this server used to come
    back all-nonfoil and then price at nonfoil prices.
    """
    deck = {
        "categories": [
            {"name": "Commander", "isPremier": True, "includedInDeck": True},
            {"name": "Ramp", "isPremier": False, "includedInDeck": True},
            {"name": "Maybeboard", "isPremier": False, "includedInDeck": False},
        ],
        "cards": [
            {"quantity": 1, "modifier": "Foil", "categories": ["Ramp"], "labels": [],
             "card": {"oracleCard": {"name": "Sol Ring"},
                      "edition": {"editioncode": "ltc"}, "collectorNumber": "284"}},
            {"quantity": 1, "modifier": "Etched", "categories": ["Ramp"], "labels": [],
             "card": {"oracleCard": {"name": "Arcane Signet"},
                      "edition": {"editioncode": "sld"}, "collectorNumber": "589"}},
            {"quantity": 1, "modifier": "Normal", "categories": ["Commander"], "labels": [],
             "card": {"oracleCard": {"name": "Atraxa"},
                      "edition": {"editioncode": "cmr"}, "collectorNumber": "1"}},
            {"quantity": 1, "categories": ["Maybeboard"], "labels": [],
             "card": {"oracleCard": {"name": "Rhystic Study"},
                      "edition": {"editioncode": "j22"}, "collectorNumber": "114"}},
        ],
    }

    async def fake_archidekt_get(path, params=None):
        return deck
    monkeypatch.setattr(server, "_archidekt_get", fake_archidekt_get)

    out = await server.archidekt_export(server.ArchidektDeckInput(deck="123"))
    lines = out.splitlines()

    assert "1x Sol Ring (ltc) 284 *F* [Ramp]" in lines
    assert "1x Arcane Signet (sld) 589 *E* [Ramp]" in lines
    assert "1x Atraxa (cmr) 1 [Commander{top}]" in lines
    # A card with no modifier key at all must not gain a marker.
    assert "1x Rhystic Study (j22) 114 [Maybeboard{noDeck}{noPrice}]" in lines
    # Maybeboard is excluded from the deck total.
    assert "# Total in deck: 3" in lines


def test_emitted_lines_parse_back_to_the_same_entries():
    # The emitters (Tasks 9/10) and the parser (Task 2) are two halves of one
    # format. This is what stops them drifting apart.
    cases = [
        (1, "Counterspell", "dmr", "281", "foil", " [Draw]", ""),
        (1, "Arcane Signet", "sld", "589", "etched", "", ""),
        (2, "Sol Ring", "ltc", "284", None, " [Ramp]", " ^Have,#2ccce4^"),
        (1, "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel",
         "fin", "382", "foil", " [Commander{top}]", ""),
    ]
    for qty, name, set_code, collector, finish, category, labels in cases:
        line = server._archidekt_line(
            quantity=qty, name=name, set_code=set_code, collector=collector,
            finish=finish, category=category, labels=labels)
        (entry,) = server._parse_decklist_entries(line)
        assert entry.quantity == qty, line
        assert entry.name == name, line
        assert entry.set_code == set_code, line
        assert entry.collector_number == collector, line
        assert entry.finish == finish, line


def test_round_trip_survives_a_multi_line_decklist():
    decklist = "\n".join([
        server._archidekt_line(1, "Counterspell", "dmr", "281", "foil", " [Draw]", ""),
        server._archidekt_line(10, "Island", "unf", "240", None, " [Lands]", ""),
        server._archidekt_line(1, "Arcane Signet", "sld", "589", "etched", "", ""),
    ])
    entries = server._parse_decklist_entries(decklist)
    assert [e.finish for e in entries] == ["foil", None, "etched"]
    assert [e.quantity for e in entries] == [1, 10, 1]
    assert [e.set_code for e in entries] == ["dmr", "unf", "sld"]
