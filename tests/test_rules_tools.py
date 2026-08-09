import asyncio
import pathlib
import time

import httpx
import pytest

import rulebook
import server

FIXTURE = (pathlib.Path(__file__).parent / "fixtures" / "cr_fixture.txt").read_text(encoding="utf-8")
FIXTURE_2099 = FIXTURE.replace("August 7, 2026", "January 1, 2099")

RULES_PAGE_HTML = (
    '<a href="https://media.wizards.com/2026/downloads/'
    'MagicCompRules 20260807.txt">TXT</a>'
)
CR_TXT_URL = "https://media.wizards.com/2026/downloads/MagicCompRules%2020260807.txt"


@pytest.fixture(autouse=True)
def small_corpus_ok(monkeypatch, tmp_path):
    """Fixture-sized corpora pass the sanity check; disk paths are isolated."""
    monkeypatch.setattr(server, "CR_MIN_RULES", 5)
    monkeypatch.setattr(server, "CR_CACHE_PATH", str(tmp_path / "cr_cache.txt"))
    monkeypatch.setattr(server, "CR_VENDORED_PATH", str(tmp_path / "MagicCompRules.txt"))
    monkeypatch.setitem(server._rules_state, "index", None)
    monkeypatch.setitem(server._rules_state, "checked_at", 0.0)
    monkeypatch.setitem(server._rules_state, "source_date", "")
    monkeypatch.setitem(server._rules_state, "refresh_task", None)
    return tmp_path


def _install_index(monkeypatch, text=FIXTURE):
    idx = rulebook.parse(text)
    monkeypatch.setitem(server._rules_state, "index", idx)
    monkeypatch.setitem(server._rules_state, "checked_at", time.time())  # no refresh
    return idx


def _install_fetch(monkeypatch, pages):
    """Fake _fetch_page_text keyed by URL; an Exception value raises."""
    calls: list[str] = []

    async def fake(url, timeout=None):
        calls.append(url)
        val = pages[url]
        if isinstance(val, Exception):
            raise val
        return val

    monkeypatch.setattr(server, "_fetch_page_text", fake)
    return calls


# ── URL discovery ────────────────────────────────────────────────────────────

def test_discover_cr_url_encodes_the_space():
    assert server._discover_cr_url(RULES_PAGE_HTML) == (CR_TXT_URL, "20260807")


def test_discover_cr_url_missing_link():
    assert server._discover_cr_url("<html>no link</html>") is None


def test_discover_cr_url_newest_link_and_preencoded():
    two = ('<a href="https://media.wizards.com/2015/downloads/MagicCompRules 20150101.txt">old</a>'
           '<a href="https://media.wizards.com/2026/downloads/MagicCompRules 20260807.txt">new</a>')
    assert server._discover_cr_url(two) == (CR_TXT_URL, "20260807")
    pre = '<a href="https://media.wizards.com/2026/downloads/MagicCompRules%2020260807.txt">x</a>'
    assert server._discover_cr_url(pre) == (CR_TXT_URL, "20260807")


# ── Disk loading ─────────────────────────────────────────────────────────────

def test_load_prefers_cache_over_vendored(small_corpus_ok):
    (small_corpus_ok / "cr_cache.txt").write_text(FIXTURE_2099, encoding="utf-8")
    (small_corpus_ok / "MagicCompRules.txt").write_text(FIXTURE, encoding="utf-8")
    idx = server._load_rules_from_disk()
    assert idx.effective_yyyymmdd == "20990101"


def test_load_falls_back_to_vendored(small_corpus_ok):
    (small_corpus_ok / "MagicCompRules.txt").write_text(FIXTURE, encoding="utf-8")
    idx = server._load_rules_from_disk()
    assert idx.effective_yyyymmdd == "20260807"


def test_load_skips_malformed_cache(small_corpus_ok):
    (small_corpus_ok / "cr_cache.txt").write_text("garbage", encoding="utf-8")
    (small_corpus_ok / "MagicCompRules.txt").write_text(FIXTURE, encoding="utf-8")
    idx = server._load_rules_from_disk()
    assert idx.effective_yyyymmdd == "20260807"


def test_load_nothing_on_disk(small_corpus_ok):
    assert server._load_rules_from_disk() is None


def test_load_prefers_newer_vendored_over_stale_cache(small_corpus_ok):
    (small_corpus_ok / "cr_cache.txt").write_text(FIXTURE, encoding="utf-8")
    (small_corpus_ok / "MagicCompRules.txt").write_text(FIXTURE_2099, encoding="utf-8")
    assert server._load_rules_from_disk().effective_yyyymmdd == "20990101"


# ── Refresh ──────────────────────────────────────────────────────────────────

async def test_refresh_swaps_in_newer_cr(monkeypatch, small_corpus_ok):
    _install_index(monkeypatch, FIXTURE)
    html = RULES_PAGE_HTML.replace("20260807", "20990101")
    url = CR_TXT_URL.replace("20260807", "20990101")
    _install_fetch(monkeypatch, {server.RULES_PAGE_URL: html, url: FIXTURE_2099})
    await server._refresh_rules()
    assert server._rules_state["index"].effective_yyyymmdd == "20990101"
    assert "20990101" not in FIXTURE  # sanity: swap came from the download
    assert (small_corpus_ok / "cr_cache.txt").read_text(encoding="utf-8") == FIXTURE_2099


async def test_refresh_same_date_skips_download(monkeypatch, small_corpus_ok):
    _install_index(monkeypatch, FIXTURE)
    calls = _install_fetch(monkeypatch, {server.RULES_PAGE_URL: RULES_PAGE_HTML})
    await server._refresh_rules()  # would KeyError on the txt URL if fetched
    assert calls == [server.RULES_PAGE_URL]


async def test_refresh_network_failure_keeps_index(monkeypatch, small_corpus_ok):
    idx = _install_index(monkeypatch, FIXTURE)
    _install_fetch(monkeypatch, {server.RULES_PAGE_URL: httpx.ConnectError("boom")})
    await server._refresh_rules()
    assert server._rules_state["index"] is idx


async def test_refresh_malformed_download_rejected(monkeypatch, small_corpus_ok):
    idx = _install_index(monkeypatch, FIXTURE)
    html = RULES_PAGE_HTML.replace("20260807", "20990101")
    url = CR_TXT_URL.replace("20260807", "20990101")
    _install_fetch(monkeypatch, {server.RULES_PAGE_URL: html, url: "garbage"})
    await server._refresh_rules()
    assert server._rules_state["index"] is idx


async def test_refresh_failure_backs_off_not_zero(monkeypatch, small_corpus_ok):
    # No disk copy, network down: retry is rate-limited, not immediate.
    _install_fetch(monkeypatch, {server.RULES_PAGE_URL: httpx.ConnectError("boom")})
    await server._refresh_rules()
    remaining = server._rules_state["checked_at"] + server.CR_REFRESH_INTERVAL - time.time()
    assert server.CR_RETRY_INTERVAL - 5 <= remaining <= server.CR_RETRY_INTERVAL


# ── Concurrency ──────────────────────────────────────────────────────────────

async def test_cold_start_race_loads_disk_once(monkeypatch, small_corpus_ok):
    (small_corpus_ok / "MagicCompRules.txt").write_text(FIXTURE, encoding="utf-8")
    calls = []
    real_load = server._load_rules_from_disk

    def slow_load():
        calls.append(1)
        time.sleep(0.05)
        return real_load()

    monkeypatch.setattr(server, "_load_rules_from_disk", slow_load)
    _install_fetch(monkeypatch, {server.RULES_PAGE_URL: "<html>no link</html>"})
    a, b = await asyncio.gather(server._get_rules_index(), server._get_rules_index())
    assert a is b and a is not None
    assert len(calls) == 1


async def test_refresh_spawned_once_per_interval(monkeypatch, small_corpus_ok):
    (small_corpus_ok / "MagicCompRules.txt").write_text(FIXTURE, encoding="utf-8")
    calls = _install_fetch(monkeypatch, {server.RULES_PAGE_URL: "<html>no link</html>"})
    for _ in range(5):
        await server._get_rules_index()
    await asyncio.sleep(0.01)  # let the spawned refresh task run
    assert calls.count(server.RULES_PAGE_URL) == 1


# ── rules_get: numbers ───────────────────────────────────────────────────────

async def test_get_subrule_includes_parent_heading(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="702.2b"))
    assert "effective August 7, 2026" in out
    assert "702.2. Deathtouch" in out
    assert "destroyed as a state-based action" in out
    assert "702.2a" not in out  # siblings stay out of a subrule lookup


async def test_get_uppercase_subrule_letter(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="702.2B"))
    assert "destroyed as a state-based action" in out


async def test_get_rule_includes_all_subrules(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="702.2"))
    for expected in ("702.2. Deathtouch", "702.2a", "702.2b", "702.2c"):
        assert expected in out


async def test_get_tolerates_trailing_period(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="702.2."))
    assert "702.2a Deathtouch is a static ability." in out


async def test_get_subsection_lists_children_only(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="702"))
    assert "702. Keyword Abilities" in out
    assert "- 702.2. Deathtouch" in out
    assert "702.2a" not in out  # titles only, no subrule bodies
    assert "rules_get" in out   # pointer to drill down


async def test_get_section_lists_subsections(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="7"))
    assert "7. Additional Rules" in out
    assert "- 702. Keyword Abilities" in out


async def test_get_unknown_number_suggests(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="702.9"))
    assert "No rule or glossary entry" in out
    assert "702.2" in out


async def test_get_when_no_rules_available(monkeypatch):
    monkeypatch.setitem(server._rules_state, "index", None)
    monkeypatch.setitem(server._rules_state, "checked_at", time.time())
    out = await server.rules_get(server.RulesGetInput(ref="702.2"))
    assert "unavailable" in out


async def test_get_rule_over_cap_lists_subrule_snippets(monkeypatch):
    _install_index(monkeypatch)
    monkeypatch.setattr(server, "RULES_GET_MAX_CHARS", 120)
    out = await server.rules_get(server.RulesGetInput(ref="702.2"))
    assert "702.2. Deathtouch" in out
    assert "- 702.2a Deathtouch is a static ability." in out
    assert "Subrules trimmed" in out
    assert "See rule 704" not in out  # 702.2b trimmed to its first sentence


async def test_get_subsection_listing_is_single_line_per_child(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="702"))
    assert "Example:" not in out  # 702.1's attached Example stays out of listings


async def test_get_childless_subsection_points_elsewhere(monkeypatch):
    text = FIXTURE.replace("creature is destroyed.\n",
                           "creature is destroyed.\n\n705. Dead End\n", 1)
    _install_index(monkeypatch, text)
    out = await server.rules_get(server.RulesGetInput(ref="705"))
    assert "705. Dead End" in out
    assert "rules_search" in out


def test_rules_num_re_accepts_two_letter_subrules():
    assert server._RULES_NUM_RE.fullmatch("704.5aa")
    assert not server._RULES_NUM_RE.fullmatch("704.5aaa")


# ── rules_get: glossary ──────────────────────────────────────────────────────

async def test_get_glossary_expands_cited_rules(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="deathtouch"))
    assert "Glossary: Deathtouch" in out
    assert "especially effective" in out
    assert "702.2a Deathtouch is a static ability." in out  # cited rule expanded


async def test_get_glossary_case_insensitive(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="DEATHTOUCH"))
    assert "Glossary: Deathtouch" in out


async def test_get_glossary_missing_cited_rule_is_skipped(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="destroy"))
    assert "Glossary: Destroy" in out
    assert "owner’s graveyard" in out  # definition present; missing 701.7 is no crash


async def test_get_glossary_typo_suggests_term(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_get(server.RulesGetInput(ref="deathtuch"))
    assert "Deathtouch" in out


async def test_get_glossary_caps_expansion(monkeypatch):
    _install_index(monkeypatch)
    monkeypatch.setattr(server, "RULES_GET_MAX_CHARS", 200)
    out = await server.rules_get(server.RulesGetInput(ref="deathtouch"))
    assert "Glossary: Deathtouch" in out
    assert "702.2a Deathtouch is a static ability." not in out
    assert "omitted for length" in out


async def test_get_glossary_subsection_citation_lists_rules(monkeypatch):
    text = FIXTURE.replace(
        "Dies\n", "Dead\nSee rule 704, “State-Based Actions.”\n \nDies\n", 1)
    _install_index(monkeypatch, text)
    out = await server.rules_get(server.RulesGetInput(ref="dead"))
    assert "- 704.5. The state-based actions are as follows:" in out
    assert "to expand it" in out
    assert "704.5k" not in out  # grandchildren stay behind the pointer


async def test_get_glossary_omission_notice_comes_last(monkeypatch):
    text = FIXTURE.replace(
        "Dies\n", "Both\nSee rule 702.2 and rule 100.1.\n \nDies\n", 1)
    _install_index(monkeypatch, text)
    # Pinned at 450: 702.2's full block (450 chars) and its brief fallback
    # (470 chars, longer here because the fixture rules are already short)
    # both exceed the 358-char budget left after the glossary header, so
    # 702.2 is fully omitted; 100.1's full block (308 chars) fits and expands.
    monkeypatch.setattr(server, "RULES_GET_MAX_CHARS", 450)
    out = await server.rules_get(server.RulesGetInput(ref="both"))
    assert "702.2a" not in out
    assert out.index("100.1a") < out.index("omitted for length")


async def test_glossary_renders_all_real_terms(monkeypatch):
    real = (pathlib.Path(__file__).parent.parent / "MagicCompRules.txt").read_text(encoding="utf-8-sig")
    idx = rulebook.parse(real)
    monkeypatch.setitem(server._rules_state, "index", idx)
    monkeypatch.setitem(server._rules_state, "checked_at", time.time())
    for key in list(idx.glossary):
        out = await server.rules_get(server.RulesGetInput(ref=key))
        assert "Glossary:" in out, key
        assert len(out) <= server.RULES_GET_MAX_CHARS + 200, key
        assert not out.rstrip().endswith("as follows:"), key  # no dangling list intros


# ── rules_search ─────────────────────────────────────────────────────────────

async def test_search_tool_lists_rule_and_glossary_hits(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_search(server.RulesSearchInput(query="deathtouch"))
    assert "effective August 7, 2026" in out
    assert "702.2" in out
    assert "Glossary: Deathtouch" in out
    assert "mention these terms" in out


async def test_search_tool_respects_limit(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_search(server.RulesSearchInput(query="deathtouch", limit=1))
    assert "showing the 1 " in out


async def test_search_tool_no_hits(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_search(server.RulesSearchInput(query="zzzznope"))
    assert "No Comprehensive Rules matches" in out


async def test_search_tool_unavailable(monkeypatch):
    monkeypatch.setitem(server._rules_state, "index", None)
    monkeypatch.setitem(server._rules_state, "checked_at", time.time())
    out = await server.rules_search(server.RulesSearchInput(query="deathtouch"))
    assert "unavailable" in out


async def test_search_tool_singular_grammar(monkeypatch):
    _install_index(monkeypatch)
    out = await server.rules_search(server.RulesSearchInput(query="interchangeably"))
    assert "1 CR entry mentions these terms" in out


async def test_search_output_stays_bounded_real_cr(monkeypatch):
    real = (pathlib.Path(__file__).parent.parent / "MagicCompRules.txt").read_text(encoding="utf-8-sig")
    idx = rulebook.parse(real)
    monkeypatch.setitem(server._rules_state, "index", idx)
    monkeypatch.setitem(server._rules_state, "checked_at", time.time())
    for q in ("what happens when a creature dies", "battlefield", "counter a spell"):
        out = await server.rules_search(server.RulesSearchInput(query=q, limit=25))
        assert len(out) <= server.RULES_GET_MAX_CHARS + 500, q
        assert "Example:" not in out, q  # hits are first-line only


async def test_search_compound_headword_roundtrips_real_cr(monkeypatch):
    real = (pathlib.Path(__file__).parent.parent / "MagicCompRules.txt").read_text(encoding="utf-8-sig")
    idx = rulebook.parse(real)
    monkeypatch.setitem(server._rules_state, "index", idx)
    monkeypatch.setitem(server._rules_state, "checked_at", time.time())
    out = await server.rules_search(server.RulesSearchInput(query="bands with other"))
    label = next(l for l in out.splitlines() if l.startswith("Glossary: Banding"))
    ref = label[len("Glossary: "):].split(" — ")[0]
    out2 = await server.rules_get(server.RulesGetInput(ref=ref))
    assert "Glossary:" in out2
