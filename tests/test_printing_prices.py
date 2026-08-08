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


def test_lookup_does_not_substitute_a_different_printing_from_the_same_set():
    # A named collector number is a request for one exact printing. Degrading
    # to another printing of the same card in the same set would report
    # someone else's price as the user's.
    other_sld_sol_ring = {
        "name": "Sol Ring", "set": "sld", "collector_number": "2417",
        "finishes": ["foil"], "prices": {"usd_foil": "48.21"},
    }
    index = server._index_collection_results([other_sld_sol_ring])
    entry = server.DecklistEntry(1, "Sol Ring", "sld", "9999", None)
    assert server._lookup_entry(index, entry) is None


def test_lookup_still_degrades_when_only_a_set_was_named():
    # Contrast with the above: no collector number means the caller did not ask
    # for a specific printing, so falling back to the bare-name key is correct.
    index = server._index_collection_results([SOL_RING_LTC])
    entry = server.DecklistEntry(1, "Sol Ring", "c21", None, None)
    assert server._lookup_entry(index, entry) is SOL_RING_LTC


# ── _identifier_label ─────────────────────────────────────────────────────────

def test_identifier_label_renders_each_identifier_shape_readably():
    # not_found echoes back the identifier object we sent, so the old
    # `item.get("name", str(item))` printed a raw dict for printing lookups.
    assert server._identifier_label(
        {"set": "ltc", "collector_number": "284"}) == "LTC #284"
    assert server._identifier_label(
        {"name": "Arcane Signet", "set": "otc"}) == "Arcane Signet (OTC)"
    assert server._identifier_label({"name": "Rhystic Study"}) == "Rhystic Study"


# ── display helpers ───────────────────────────────────────────────────────────

def test_printing_label_names_the_exact_printing_and_finish():
    assert server._printing_label(COUNTERSPELL_DMR, "foil") == \
        "Counterspell (DMR #281, foil)"
    assert server._printing_label(COUNTERSPELL_DMR, None) == \
        "Counterspell (DMR #281, nonfoil)"


def test_available_finishes_lists_prices_the_printing_actually_has():
    assert server._available_finishes(ALL_FINISHES) == \
        "nonfoil $29.04, foil (no price), etched $26.31"


def test_available_finishes_omits_finishes_the_printing_lacks():
    assert server._available_finishes(FOIL_ONLY) == "foil $48.21"


def test_available_finishes_handles_a_printing_with_no_finish_data():
    assert server._available_finishes({"prices": {}}) == "none listed"


def test_entry_suffix_describes_what_the_line_asked_for():
    assert server._entry_suffix(
        server.DecklistEntry(1, "Sol Ring", "ltc", "284", "foil")) == " (LTC #284) foil"
    assert server._entry_suffix(
        server.DecklistEntry(1, "Sol Ring", "otc", None, None)) == " (OTC)"
    assert server._entry_suffix(
        server.DecklistEntry(1, "Sol Ring", None, None, None)) == ""


# ── section assembly ──────────────────────────────────────────────────────────

def test_price_sections_group_lines_and_total_only_what_it_priced():
    entries = [
        server.DecklistEntry(2, "Counterspell", "dmr", "281", "foil"),   # 2 x 2.17
        server.DecklistEntry(1, "Sol Ring", "ltc", "284", None),          # 2.51
        server.DecklistEntry(1, "Arcane Signet", "sld", "589", "foil"),   # no foil price
        server.DecklistEntry(3, "Rhystic Study", None, None, None),       # default printing
        server.DecklistEntry(1, "Black Lotus", None, None, None),         # not found
    ]
    rhystic = {
        "name": "Rhystic Study", "set": "j22", "collector_number": "114",
        "finishes": ["nonfoil"], "prices": {"usd": "69.53"},
    }
    index = server._index_collection_results(
        [COUNTERSPELL_DMR, SOL_RING_LTC, ALL_FINISHES, rhystic])

    result = server._build_price_sections(entries, index, [{"name": "Black Lotus"}])

    assert result["total"] == Decimal("215.44")   # 4.34 + 2.51 + 208.59
    assert result["priced_cards"] == 6            # 2 + 1 + 3
    assert len(result["priced"]) == 2             # the two lines naming a printing
    assert len(result["defaulted"]) == 1          # Rhystic Study
    assert len(result["no_price"]) == 1           # Arcane Signet foil
    assert len(result["missing"]) == 1            # Black Lotus


def test_price_sections_exclude_unpriced_lines_from_the_total():
    entries = [server.DecklistEntry(1, "Arcane Signet", "sld", "589", "foil")]
    index = server._index_collection_results([ALL_FINISHES])
    result = server._build_price_sections(entries, index, [])
    assert result["total"] == Decimal("0")
    assert result["priced_cards"] == 0
    assert "foil" in result["no_price"][0]
    assert "nonfoil $29.04" in result["no_price"][0]


def test_price_sections_use_exact_decimal_arithmetic():
    # 100 lines at $0.07 is exactly $7.00; float accumulation drifts.
    card = {"name": "Island", "set": "unf", "collector_number": "240",
            "finishes": ["nonfoil"], "prices": {"usd": "0.07"}}
    entries = [server.DecklistEntry(1, "Island", "unf", "240", None)] * 100
    index = server._index_collection_results([card])
    result = server._build_price_sections(entries, index, [])
    assert result["total"] == Decimal("7.00")


def test_price_sections_multiply_by_quantity():
    entries = [server.DecklistEntry(10, "Sol Ring", "ltc", "284", None)]
    index = server._index_collection_results([SOL_RING_LTC])
    result = server._build_price_sections(entries, index, [])
    assert result["total"] == Decimal("25.10")


# ── identifier dedupe and batching ────────────────────────────────────────────

def test_dedupe_identifiers_collapses_repeated_printings():
    entries = [
        server.DecklistEntry(1, "Sol Ring", "ltc", "284", None),
        server.DecklistEntry(1, "Sol Ring", "ltc", "284", "foil"),   # same printing
        server.DecklistEntry(1, "Counterspell", "dmr", "281", None),
    ]
    # Finish is not part of the identifier, so the first two collapse to one.
    assert server._dedupe_identifiers(entries) == [
        {"set": "ltc", "collector_number": "284"},
        {"set": "dmr", "collector_number": "281"},
    ]


def test_dedupe_identifiers_preserves_first_seen_order():
    entries = [
        server.DecklistEntry(1, "Rhystic Study", None, None, None),
        server.DecklistEntry(1, "Sol Ring", "ltc", "284", None),
        server.DecklistEntry(1, "Rhystic Study", None, None, None),
    ]
    assert server._dedupe_identifiers(entries) == [
        {"name": "Rhystic Study"},
        {"set": "ltc", "collector_number": "284"},
    ]


def test_chunk_splits_at_the_batch_limit():
    assert server._chunk(list(range(160)), 75) == [
        list(range(75)), list(range(75, 150)), list(range(150, 160))]
    assert server._chunk([], 75) == []
    assert server._chunk([1, 2], 75) == [[1, 2]]


def test_identifier_key_is_stable_across_construction_order():
    assert server._identifier_key({"set": "ltc", "collector_number": "284"}) == \
        server._identifier_key({"collector_number": "284", "set": "ltc"})
    assert server._identifier_key({"name": "Sol Ring"}) != \
        server._identifier_key({"name": "Sol Ring", "set": "ltc"})


def test_unchecked_entries_are_not_reported_as_not_found():
    # A batch that failed leaves its identifiers unchecked. Reporting them as
    # "not found" would claim a real card does not exist.
    entries = [
        server.DecklistEntry(1, "Sol Ring", "ltc", "284", None),
        server.DecklistEntry(1, "Black Lotus", None, None, None),
    ]
    result = server._build_price_sections(
        entries, {}, [{"name": "Black Lotus"}],
        [{"set": "ltc", "collector_number": "284"}])
    assert result["unchecked"] == ["1x Sol Ring (LTC #284)"]
    assert result["missing"] == ["1x Black Lotus"]


# ── scryfall_price query building and sorting ─────────────────────────────────

import pytest
from pydantic import ValidationError


def test_price_query_is_just_the_name_and_a_digital_filter_by_default():
    params = server.PriceInput(name="Sol Ring")
    assert server._price_query(params) == '!"Sol Ring" -is:digital'


def test_price_query_includes_set_collector_and_finish():
    params = server.PriceInput(
        name="Counterspell", set_code="dmr", collector_number="281", finish="foil")
    assert server._price_query(params) == \
        '!"Counterspell" set:dmr cn:281 is:foil -is:digital'


def test_price_query_can_include_digital_printings():
    params = server.PriceInput(name="Counterspell", include_digital=True)
    assert server._price_query(params) == '!"Counterspell"'


def test_collector_number_without_set_code_is_rejected():
    # Collector numbers are only unique within a set, so this is a real error
    # rather than something to silently ignore.
    with pytest.raises(ValidationError):
        server.PriceInput(name="Sol Ring", collector_number="284")


def test_sort_puts_priced_printings_first_and_unpriced_last():
    # The live defect: Scryfall's order=usd&dir=asc sorts nulls FIRST, so the
    # ten rows this tool displayed for Counterspell were all unpriced.
    unpriced = {"name": "C", "set": "tpr", "collector_number": "1",
                "finishes": ["nonfoil"], "prices": {"usd": None}}
    cheap = {"name": "C", "set": "dmr", "collector_number": "281",
             "finishes": ["nonfoil"], "prices": {"usd": "2.15"}}
    dear = {"name": "C", "set": "6ed", "collector_number": "77",
            "finishes": ["nonfoil"], "prices": {"usd": "2.27"}}

    ordered = server._sort_by_price([unpriced, dear, cheap], None)
    assert [c["set"] for c in ordered] == ["dmr", "6ed", "tpr"]


def test_sort_uses_the_requested_finish_column():
    a = {"name": "C", "set": "a", "collector_number": "1",
         "finishes": ["nonfoil", "foil"], "prices": {"usd": "10.00", "usd_foil": "1.00"}}
    b = {"name": "C", "set": "b", "collector_number": "2",
         "finishes": ["nonfoil", "foil"], "prices": {"usd": "1.00", "usd_foil": "10.00"}}
    assert [c["set"] for c in server._sort_by_price([a, b], "foil")] == ["a", "b"]
    assert [c["set"] for c in server._sort_by_price([a, b], "nonfoil")] == ["b", "a"]


# ── single-printing detail view ───────────────────────────────────────────────

DMR_COUNTERSPELL = {
    "name": "Counterspell", "set": "dmr", "collector_number": "281",
    "set_name": "Dominaria Remastered", "rarity": "common",
    "finishes": ["nonfoil", "foil"],
    "prices": {"usd": "2.15", "usd_foil": "2.17", "eur": "2.83", "tix": "0.35"},
    "artist": "Zack Stella", "frame": "1997", "border_color": "black",
    "promo_types": ["boosterfun"], "released_at": "2023-01-13",
    "scryfall_uri": "https://scryfall.com/card/dmr/281/counterspell",
}


def test_printing_header_is_shared_by_both_views():
    assert server._printing_header(DMR_COUNTERSPELL) == \
        "**Dominaria Remastered** (DMR #281, common)"


def test_single_printing_view_identifies_the_physical_copy():
    out = server._format_single_printing(DMR_COUNTERSPELL, "foil")
    assert "# Counterspell" in out
    assert "**Dominaria Remastered** (DMR #281, common)" in out
    assert "Available finishes: nonfoil $2.15, foil $2.17" in out
    assert "Requested finish (foil): $2.17" in out
    # The fields that let someone confirm which copy they are holding.
    assert "Artist: Zack Stella" in out
    assert "Frame/border: 1997, black" in out
    assert "Promo types: boosterfun" in out


def test_single_printing_view_says_so_when_the_requested_finish_has_no_price():
    out = server._format_single_printing(ALL_FINISHES, "foil")
    assert "Requested finish (foil): no price for this printing" in out
    # And still shows what the printing does cost, so the user learns why.
    assert "nonfoil $29.04" in out


def test_single_printing_view_omits_the_requested_finish_line_when_none_asked():
    out = server._format_single_printing(DMR_COUNTERSPELL, None)
    assert "Requested finish" not in out


def test_single_printing_view_tolerates_a_sparse_card():
    out = server._format_single_printing(
        {"name": "X", "prices": {}}, None)
    assert "# X" in out
    assert "No price data" in out
    assert "Artist:" not in out
