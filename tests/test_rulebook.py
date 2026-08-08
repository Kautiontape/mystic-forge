import pathlib

import pytest

import rulebook

FIXTURE = (pathlib.Path(__file__).parent / "fixtures" / "cr_fixture.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def idx():
    return rulebook.parse(FIXTURE)


def test_effective_date(idx):
    assert idx.effective_date == "August 7, 2026"
    assert idx.effective_yyyymmdd == "20260807"


def test_sections_and_subsections(idx):
    assert idx.rules["7"].text == "Additional Rules"
    assert idx.rules["702"].text == "Keyword Abilities"
    assert idx.rules["702"].parent == "7"
    assert idx.rules["702"].children == ["702.1", "702.2"]


def test_rule_headings_and_text(idx):
    assert idx.rules["702.2"].text == "Deathtouch"
    assert idx.rules["702.2"].parent == "702"
    assert idx.rules["100.1"].text.startswith("These Magic rules apply")


def test_subrules_attach_to_parent(idx):
    assert idx.rules["702.2"].children == ["702.2a", "702.2b", "702.2c"]
    assert idx.rules["702.2b"].parent == "702.2"
    assert "state-based action" in idx.rules["702.2b"].text


def test_subrule_letter_skip(idx):
    # Upstream skips 'l' and 'o'; the parser must attach whatever letters exist.
    assert idx.rules["704.5"].children == ["704.5k", "704.5m"]


def test_example_line_attaches_to_rule_above(idx):
    assert "Example:" in idx.rules["702.1"].text


def test_toc_is_not_parsed_as_rules(idx):
    # The TOC repeats '702. Keyword Abilities' etc. — it must not double-add.
    assert sorted(idx.rules) == sorted([
        "1", "100", "100.1", "100.1a", "100.1b",
        "7", "702", "702.1", "702.2", "702.2a", "702.2b", "702.2c",
        "704", "704.5", "704.5k", "704.5m",
    ])


def test_missing_markers_raise():
    with pytest.raises(ValueError):
        rulebook.parse("no glossary or credits here")


def test_unknown_month_gives_empty_yyyymmdd():
    weird = FIXTURE.replace("August", "Floopuary")
    assert rulebook.parse(weird).effective_yyyymmdd == ""


def test_glossary_definition_and_display(idx):
    assert "especially effective" in idx.glossary["deathtouch"]
    assert idx.glossary_display["deathtouch"] == "Deathtouch"


def test_glossary_multi_paragraph_definition(idx):
    assert "interchangeably" in idx.glossary["dies"]


def test_glossary_refs_extracted_in_order(idx):
    assert idx.glossary_refs("Deathtouch") == ["702.2"]
    assert idx.glossary_refs("Destroy") == ["701.7"]
    assert idx.glossary_refs("nonexistent term") == []


def test_suggest_close_matches(idx):
    assert "Deathtouch" in idx.suggest("deathtuch")
    assert any(s.startswith("702.2") for s in idx.suggest("702.9"))


def test_real_cr_parses_structurally():
    real = (pathlib.Path(__file__).parent.parent / "MagicCompRules.txt").read_text(encoding="utf-8-sig")
    idx = rulebook.parse(real)
    assert len(idx.rules) > 3300
    assert len(idx.glossary) > 730
    # Every child link resolves; no orphan parents.
    assert not [n for n, r in idx.rules.items() if r.parent and r.parent not in idx.rules]
    # Upstream header irregularities that once broke the regexes:
    for n in ("119.1d", "606.5", "704.5aa"):
        assert n in idx.rules
    # Glossary headwords ending in a curly quote must stay separate entries.
    assert "banding, “bands with other”" in idx.glossary
    assert any(k.startswith("partner,") for k in idx.glossary)
