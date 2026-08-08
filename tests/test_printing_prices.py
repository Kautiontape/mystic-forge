import server


# ── _parse_decklist_entries ───────────────────────────────────────────────────

def test_parses_bare_name():
    (entry,) = server._parse_decklist_entries("Sol Ring")
    assert (entry.quantity, entry.name) == (1, "Sol Ring")
    assert entry.set_code is None
    assert entry.collector_number is None
    assert entry.finish is None


def test_parses_quantity_with_and_without_x():
    a, b = server._parse_decklist_entries("2 Sol Ring\n3x Arcane Signet")
    assert (a.quantity, a.name) == (2, "Sol Ring")
    assert (b.quantity, b.name) == (3, "Arcane Signet")


def test_parses_set_and_collector_number():
    (entry,) = server._parse_decklist_entries("1x Sol Ring (ltc) 284")
    assert entry.name == "Sol Ring"
    assert entry.set_code == "ltc"
    assert entry.collector_number == "284"


def test_set_code_is_lowercased_regardless_of_input_case():
    (entry,) = server._parse_decklist_entries("1x Sol Ring (LTC) 284")
    assert entry.name == "Sol Ring"
    assert entry.set_code == "ltc"


def test_parses_foil_and_etched_markers():
    a, b = server._parse_decklist_entries(
        "1x Counterspell (dmr) 281 *F*\n1x Arcane Signet (sld) 589 *E*"
    )
    assert (a.name, a.finish) == ("Counterspell", "foil")
    assert (b.name, b.finish) == ("Arcane Signet", "etched")


def test_parses_word_finish_forms():
    a, b = server._parse_decklist_entries("1x Sol Ring (foil)\n1x Sol Ring (Etched)")
    assert a.finish == "foil"
    assert b.finish == "etched"
    assert a.name == b.name == "Sol Ring"


def test_collector_number_may_be_non_numeric():
    (entry,) = server._parse_decklist_entries("1x Sol Ring (sld) IFIYW-10")
    assert entry.set_code == "sld"
    assert entry.collector_number == "IFIYW-10"


def test_parses_the_verified_archidekt_export_line():
    # Captured verbatim from Archidekt's own text export, 2026-08-08.
    # Exercises a DFC name containing ' // ', a marker between the collector
    # number and the category, and a category carrying a {top} flag — at once.
    line = ("1x Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel "
            "(fin) 382 *F* [Commander{top}]")
    (entry,) = server._parse_decklist_entries(line)
    assert entry.quantity == 1
    assert entry.name == "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel"
    assert entry.set_code == "fin"
    assert entry.collector_number == "382"
    assert entry.finish == "foil"


def test_parses_full_archidekt_line_with_label():
    line = "1x Cultivate (m21) 177 [Ramp] ^Test,#2ccce4^"
    (entry,) = server._parse_decklist_entries(line)
    assert entry.name == "Cultivate"
    assert entry.set_code == "m21"
    assert entry.collector_number == "177"
    assert entry.finish is None


def test_card_names_containing_parentheses_are_not_mistaken_for_set_codes():
    text = "1x Erase (Not the Urza's Legacy One)\n1x B.F.M. (Big Furry Monster)"
    a, b = server._parse_decklist_entries(text)
    assert a.name == "Erase (Not the Urza's Legacy One)"
    assert a.set_code is None
    assert b.name == "B.F.M. (Big Furry Monster)"
    assert b.set_code is None


def test_comment_and_blank_lines_are_skipped():
    text = "# a comment\n\n// another\n1x Sol Ring\n"
    entries = server._parse_decklist_entries(text)
    assert len(entries) == 1
    assert entries[0].name == "Sol Ring"


# ── _parse_decklist backward compatibility ────────────────────────────────────

def test_parse_decklist_still_returns_qty_name_tuples():
    text = (
        "1 Sol Ring\n"
        "2x Arcane Signet\n"
        "1x Cultivate (m21) 177 [Ramp] ^Test,#2ccce4^\n"
        "1x Counterspell (dmr) 281 *F*\n"
        "# comment\n"
        "Rhystic Study\n"
    )
    assert server._parse_decklist(text) == [
        (1, "Sol Ring"),
        (2, "Arcane Signet"),
        (1, "Cultivate"),
        (1, "Counterspell"),
        (1, "Rhystic Study"),
    ]


def test_parse_decklist_now_strips_uppercase_set_codes():
    # Deliberate behavior change. The legacy strip was lowercase-only, so this
    # previously yielded "Sol Ring (LTC)" and made validate_decklist report a
    # real card as unrecognized. Moxfield emits uppercase set codes, so the new
    # parser is case-insensitive.
    assert server._parse_decklist("1x Sol Ring (LTC)") == [(1, "Sol Ring")]


def test_parse_decklist_strips_trailing_number_without_a_set_code():
    # Legacy behavior preserved: the bare trailing-digits strip still applies.
    assert server._parse_decklist("1x Sol Ring 284") == [(1, "Sol Ring")]
