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
