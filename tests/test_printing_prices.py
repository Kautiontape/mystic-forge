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


# ── _entry_identifier ─────────────────────────────────────────────────────────

def test_identifier_uses_set_and_collector_number_when_both_present():
    entry = server.DecklistEntry(1, "Sol Ring", "ltc", "284", None)
    assert server._entry_identifier(entry) == {"set": "ltc", "collector_number": "284"}


def test_identifier_uses_name_and_set_when_only_set_present():
    entry = server.DecklistEntry(1, "Arcane Signet", "otc", None, None)
    assert server._entry_identifier(entry) == {"name": "Arcane Signet", "set": "otc"}


def test_identifier_falls_back_to_name_alone():
    entry = server.DecklistEntry(1, "Rhystic Study", None, None, None)
    assert server._entry_identifier(entry) == {"name": "Rhystic Study"}


def test_identifier_ignores_finish():
    # Scryfall identifiers have no finish dimension — one card object carries
    # every finish's price, so finish only affects which column we read later.
    entry = server.DecklistEntry(1, "Counterspell", "dmr", "281", "foil")
    assert server._entry_identifier(entry) == {"set": "dmr", "collector_number": "281"}


# ── _price_for_finish ─────────────────────────────────────────────────────────

from decimal import Decimal

FOIL_ONLY = {
    "name": "Sol Ring",
    "set": "sld",
    "collector_number": "2417",
    "finishes": ["foil"],
    "prices": {"usd": None, "usd_foil": "48.21", "usd_etched": None},
}

ALL_FINISHES = {
    "name": "Arcane Signet",
    "set": "sld",
    "collector_number": "589",
    "finishes": ["nonfoil", "foil", "etched"],
    "prices": {"usd": "29.04", "usd_foil": None, "usd_etched": "26.31"},
}


def test_price_for_finish_reads_the_matching_column():
    assert server._price_for_finish(ALL_FINISHES, "nonfoil") == Decimal("29.04")
    assert server._price_for_finish(ALL_FINISHES, "etched") == Decimal("26.31")
    assert server._price_for_finish(FOIL_ONLY, "foil") == Decimal("48.21")


def test_price_for_finish_defaults_to_nonfoil():
    assert server._price_for_finish(ALL_FINISHES, None) == Decimal("29.04")


def test_price_for_finish_never_falls_back_to_another_finish():
    # The whole point: a foil-only printing has no nonfoil price, and the old
    # `usd or usd_foil or usd_etched` chain would have quoted $48.21 here.
    assert server._price_for_finish(FOIL_ONLY, "nonfoil") is None
    # And a missing foil price does not silently become the nonfoil price.
    assert server._price_for_finish(ALL_FINISHES, "foil") is None


def test_price_for_finish_handles_missing_and_malformed_data():
    assert server._price_for_finish({}, "nonfoil") is None
    assert server._price_for_finish({"prices": None}, "nonfoil") is None
    assert server._price_for_finish({"prices": {"usd": ""}}, "nonfoil") is None
    assert server._price_for_finish({"prices": {"usd": "n/a"}}, "nonfoil") is None


def test_price_for_finish_returns_decimal_not_float():
    # Totals sum over ~100 lines; float would accumulate representation error.
    assert isinstance(server._price_for_finish(ALL_FINISHES, "nonfoil"), Decimal)


# ── _index_collection_results / _lookup_entry ─────────────────────────────────

SOL_RING_LTC = {
    "name": "Sol Ring", "set": "ltc", "collector_number": "284",
    "finishes": ["nonfoil"], "prices": {"usd": "2.51"},
}
COUNTERSPELL_DMR = {
    "name": "Counterspell", "set": "dmr", "collector_number": "281",
    "finishes": ["nonfoil", "foil"], "prices": {"usd": "2.15", "usd_foil": "2.17"},
}
SEPHIROTH_FIN = {
    "name": "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel",
    "set": "fin", "collector_number": "382",
    "finishes": ["nonfoil", "foil"], "prices": {"usd": "12.00", "usd_foil": "20.00"},
}


def test_lookup_matches_by_set_and_collector_number():
    index = server._index_collection_results([SOL_RING_LTC, COUNTERSPELL_DMR])
    entry = server.DecklistEntry(1, "Sol Ring", "ltc", "284", None)
    assert server._lookup_entry(index, entry) is SOL_RING_LTC


def test_lookup_is_insensitive_to_response_order():
    reversed_index = server._index_collection_results([COUNTERSPELL_DMR, SOL_RING_LTC])
    entry = server.DecklistEntry(1, "Sol Ring", "ltc", "284", None)
    assert server._lookup_entry(reversed_index, entry) is SOL_RING_LTC


def test_lookup_matches_by_name_and_set():
    index = server._index_collection_results([COUNTERSPELL_DMR])
    entry = server.DecklistEntry(1, "Counterspell", "dmr", None, None)
    assert server._lookup_entry(index, entry) is COUNTERSPELL_DMR


def test_lookup_matches_by_bare_name_case_insensitively():
    index = server._index_collection_results([COUNTERSPELL_DMR])
    entry = server.DecklistEntry(1, "cOUNTERSPELL", None, None, None)
    assert server._lookup_entry(index, entry) is COUNTERSPELL_DMR


def test_lookup_matches_a_dfc_by_its_front_face_name():
    index = server._index_collection_results([SEPHIROTH_FIN])
    entry = server.DecklistEntry(1, "Sephiroth, Fabled SOLDIER", None, None, None)
    assert server._lookup_entry(index, entry) is SEPHIROTH_FIN


def test_lookup_matches_a_dfc_by_its_full_name():
    index = server._index_collection_results([SEPHIROTH_FIN])
    entry = server.DecklistEntry(
        1, "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel", None, None, None)
    assert server._lookup_entry(index, entry) is SEPHIROTH_FIN


def test_lookup_returns_none_when_absent():
    index = server._index_collection_results([SOL_RING_LTC])
    entry = server.DecklistEntry(1, "Black Lotus", None, None, None)
    assert server._lookup_entry(index, entry) is None


# ── _identifier_label ─────────────────────────────────────────────────────────

def test_identifier_label_renders_each_identifier_shape_readably():
    # not_found echoes back the identifier object we sent, so the old
    # `item.get("name", str(item))` printed a raw dict for printing lookups.
    assert server._identifier_label(
        {"set": "ltc", "collector_number": "284"}) == "LTC #284"
    assert server._identifier_label(
        {"name": "Arcane Signet", "set": "otc"}) == "Arcane Signet (OTC)"
    assert server._identifier_label({"name": "Rhystic Study"}) == "Rhystic Study"
