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
