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


def test_suggest_case_folds_and_recovers_obsolete_terms():
    real = (pathlib.Path(__file__).parent.parent / "MagicCompRules.txt").read_text(encoding="utf-8-sig")
    idx = rulebook.parse(real)
    assert any("Mana Burn" in s for s in idx.suggest("mana burn"))
    assert "Stack" in idx.suggest("the stack")


def test_search_all_terms_ranked_first(idx):
    hits, total = idx.search("deathtouch state-based actions")
    assert hits[0].ref == "702.2b"
    assert total == 8  # any-match count on the fixture corpus


def test_search_phrase_match_wins(idx):
    hits, _ = idx.search("keyword abilities")
    assert hits[0].ref == "702"
    assert hits[0].kind == "rule"


def test_search_glossary_hits_flagged(idx):
    hits, _ = idx.search("especially effective")
    assert hits[0].kind == "glossary"
    assert hits[0].ref == "Deathtouch"


def test_search_limit_and_total(idx):
    hits, total = idx.search("deathtouch", limit=2)
    assert len(hits) == 2
    assert total > 2


def test_search_no_token_query(idx):
    assert idx.search("!!! ???") == ([], 0)


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
    # Citation lists ("rules X and Y") are captured in full, in order.
    assert idx.glossary_refs("win the game") == ["104", "810.8", "809.5"]
    # Compound headwords are reachable by each part.
    assert "banding" in idx.glossary
    assert idx.glossary_display["banding"] == "Banding"
    assert "partner" in idx.glossary
    assert "kicked" in idx.glossary
    # Aliases don't produce duplicate search documents.
    hits, _ = idx.search("bands with other", limit=10)
    gl = [h for h in hits if h.kind == "glossary" and "Bands with Other" in h.ref]
    assert len(gl) == 1


def test_search_ranking_real_cr():
    real = (pathlib.Path(__file__).parent.parent / "MagicCompRules.txt").read_text(encoding="utf-8-sig")
    idx = rulebook.parse(real)

    def top(query, n=5):
        hits, _ = idx.search(query, limit=n)
        return [h.ref for h in hits]

    assert any(r.startswith("704") for r in top("state-based actions"))
    assert any(r.startswith("613") for r in top("layers continuous effects"))
    assert any(r.startswith("601.2") for r in top("casting a spell"))
    assert "103.5" in top("mulligan")
    # Stopword handling: natural-language phrasing must reach the token rules.
    assert any(r.startswith("111") for r in top("when does a token cease to exist"))
    # Deterministic ordering: same query, same ranked refs, across parses.
    idx2 = rulebook.parse(real)
    assert top("deathtouch", 10) == [h.ref for h in idx2.search("deathtouch", limit=10)[0]]
