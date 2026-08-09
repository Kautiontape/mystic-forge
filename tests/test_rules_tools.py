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
