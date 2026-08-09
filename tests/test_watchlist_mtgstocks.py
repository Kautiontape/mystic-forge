"""MTGStocks print-id resolution and the badge that depends on it.

MTGStocks' API is undocumented, so the parsers here are deliberately keyed off
shape rather than an exact schema; these tests pin that tolerance down.
"""

import pytest

from mystic_forge.watchlist import db as watchlist_db
from mystic_forge.watchlist import ingest as watchlist_ingest
from mystic_forge.watchlist import mtgstocks
from mystic_forge.watchlist import pages as watchlist_pages

BMC = {"id": 141924, "name": "Black Market Connections",
       "slug": "black-market-connections"}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.Client; records the calls it was asked to make."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        for key, payload in self.routes.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(payload)
        raise AssertionError(f"unexpected request: {url}")


@pytest.fixture
def enabled(monkeypatch):
    """conftest disables ingest globally; re-enable for the resolver tests."""
    monkeypatch.delenv("MYSTIC_FORGE_NO_INGEST", raising=False)
    monkeypatch.delenv("MYSTIC_FORGE_NO_MTGSTOCKS", raising=False)
    monkeypatch.setattr(mtgstocks, "PAUSE", 0)


# ── URL shapes ──────────────────────────────────────────────────────────────

def test_print_url_matches_the_real_thing():
    assert (mtgstocks.print_url(141924, "black-market-connections")
            == "https://www.mtgstocks.com/prints/141924-black-market-connections")


def test_print_url_without_slug_still_routes():
    assert mtgstocks.print_url(141924) == "https://www.mtgstocks.com/prints/141924"


def test_slugify():
    assert mtgstocks.slugify("Black Market Connections") == "black-market-connections"
    assert mtgstocks.slugify("Sword of Feast and Famine") == "sword-of-feast-and-famine"
    assert mtgstocks.slugify("Yawgmoth, Thran Physician") == "yawgmoth-thran-physician"


def test_front_face_of_a_double_faced_card():
    assert mtgstocks.front_face("Delver of Secrets // Insectile Aberration") \
        == "Delver of Secrets"


# ── payload tolerance ───────────────────────────────────────────────────────

def test_resolve_name_reads_a_flat_autocomplete_list():
    c = FakeClient({"/search/autocomplete": [
        {"id": 999, "name": "Black Market", "slug": "black-market"}, BMC]})
    assert mtgstocks.resolve_name(c, "Black Market Connections") \
        == (141924, "black-market-connections")


def test_resolve_name_reads_a_wrapped_payload():
    """A `{"results": [...]}` envelope must resolve the same as a bare list."""
    c = FakeClient({"/search/autocomplete": {"results": [BMC]}})
    assert mtgstocks.resolve_name(c, "Black Market Connections")[0] == 141924


def test_resolve_name_derives_a_slug_when_the_api_omits_one():
    c = FakeClient({"/search/autocomplete": [{"id": 7, "name": "Sol Ring"}]})
    assert mtgstocks.resolve_name(c, "Sol Ring") == (7, "sol-ring")


def test_resolve_name_matches_a_double_faced_card_by_its_front_face():
    c = FakeClient({"/search/autocomplete": [
        {"id": 42, "name": "Delver of Secrets", "slug": "delver-of-secrets"}]})
    hit = mtgstocks.resolve_name(c, "Delver of Secrets // Insectile Aberration")
    assert hit == (42, "delver-of-secrets")
    assert c.calls[0][1] == {"query": "Delver of Secrets"}


def test_resolve_name_returns_none_for_an_unknown_card():
    assert mtgstocks.resolve_name(FakeClient({"/search/autocomplete": []}),
                                  "Nonesuch") is None


def test_printings_lists_other_sets():
    c = FakeClient({"/prints/141924": {
        "id": 141924, "name": "Black Market Connections",
        "sets": [{"id": 141924, "set_name": "Streets of New Capenna",
                  "slug": "black-market-connections"},
                 {"id": 200001, "set_name": "Commander Masters",
                  "slug": "black-market-connections"}]}})
    got = mtgstocks.printings(c, 141924)
    assert (200001, "black-market-connections", "Commander Masters") in got


def test_printings_of_an_unrecognised_payload_is_empty_not_an_error():
    assert mtgstocks.printings(FakeClient({"/prints/1": {"foo": "bar"}}), 1) == []


# ── cache ───────────────────────────────────────────────────────────────────

def test_cached_url_falls_back_from_printing_to_card(db):
    mtgstocks.remember(db, "Black Market Connections", "", 141924,
                       "black-market-connections")
    assert "141924" in mtgstocks.cached_url(db, "Black Market Connections", "SNC")
    mtgstocks.remember(db, "Black Market Connections", "CMM", 200001,
                       "black-market-connections")
    assert "200001" in mtgstocks.cached_url(db, "Black Market Connections", "CMM")
    assert "141924" in mtgstocks.cached_url(db, "Black Market Connections")


def test_cached_url_is_none_for_a_cached_miss(db):
    mtgstocks.remember(db, "Nonesuch", "", None, None)
    assert mtgstocks.cached_url(db, "Nonesuch") is None
    assert mtgstocks.cached_url(None, "Nonesuch") is None


def test_cached_url_is_case_insensitive(db):
    mtgstocks.remember(db, "Sol Ring", "", 7, "sol-ring")
    assert mtgstocks.cached_url(db, "SOL RING", "c21") is not None


# ── refresh ─────────────────────────────────────────────────────────────────

def test_refresh_resolves_watched_names(db, enabled, monkeypatch):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Black Market Connections")
    monkeypatch.setattr(mtgstocks, "httpx", _fake_httpx(
        {"/search/autocomplete": [BMC]}))
    assert mtgstocks.refresh(db) == 1
    assert mtgstocks.cached_url(db, "Black Market Connections") \
        == "https://www.mtgstocks.com/prints/141924-black-market-connections"


def test_refresh_skips_names_it_already_answered(db, enabled, monkeypatch):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Black Market Connections")
    fake = _fake_httpx({"/search/autocomplete": [BMC]})
    monkeypatch.setattr(mtgstocks, "httpx", fake)
    mtgstocks.refresh(db)
    before = len(fake.client.calls)
    assert mtgstocks.refresh(db) == 0
    assert len(fake.client.calls) == before          # no second lookup


def test_refresh_caches_a_miss_so_it_is_not_re_queried(db, enabled, monkeypatch):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Nonesuch")
    fake = _fake_httpx({"/search/autocomplete": []})
    monkeypatch.setattr(mtgstocks, "httpx", fake)
    assert mtgstocks.refresh(db) == 0
    assert mtgstocks.refresh(db) == 0
    assert len(fake.client.calls) == 1


def test_refresh_gives_up_when_mtgstocks_refuses(db, enabled, monkeypatch):
    """CloudFront 403s must not turn into one doomed call per watched card."""
    list_id, _, _ = watchlist_db.create_list(db)
    for n in ("Sol Ring", "Cultivate", "Counterspell", "Brainstorm", "Ponder"):
        watchlist_db.add_card(db, list_id, n)
    fake = _fake_httpx({"/search/autocomplete": RuntimeError("403")})
    monkeypatch.setattr(mtgstocks, "httpx", fake)
    assert mtgstocks.refresh(db) == 0
    assert len(fake.client.calls) == mtgstocks.GIVE_UP_AFTER


def test_refresh_pins_a_printing_when_the_set_matches(db, enabled, monkeypatch,
                                                      tmp_path):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Black Market Connections",
                          set_code="CMM", collector_number="215")
    monkeypatch.setattr(mtgstocks, "httpx", _fake_httpx({
        "/search/autocomplete": [BMC],
        "/prints/141924": {"sets": [
            {"id": 141924, "set_name": "Streets of New Capenna"},
            {"id": 200001, "set_name": "Commander Masters",
             "slug": "black-market-connections"}]}}))
    ap = _allprintings(tmp_path, {"CMM": "Commander Masters"})
    mtgstocks.refresh(db, ap)
    assert "200001" in mtgstocks.cached_url(db, "Black Market Connections", "CMM")


def test_refresh_is_disabled_by_env(db, monkeypatch):
    monkeypatch.setenv("MYSTIC_FORGE_NO_MTGSTOCKS", "1")
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    assert mtgstocks.refresh(db) == 0


def test_ingest_wrapper_swallows_resolver_failures(db, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("mtgstocks exploded")
    monkeypatch.setattr(mtgstocks, "refresh", boom)
    assert watchlist_ingest.resolve_mtgstocks(db) == 0


# ── badge rendering ─────────────────────────────────────────────────────────

def test_badge_is_omitted_until_the_id_is_known(db):
    entry = {"card_name": "Black Market Connections", "set_code": None,
             "collector_number": None}
    assert "mtgstocks.com" not in watchlist_pages._site_links(entry, db)
    assert "tcgplayer.com" in watchlist_pages._site_links(entry, db)


def test_badge_deep_links_once_the_id_is_known(db):
    mtgstocks.remember(db, "Black Market Connections", "", 141924,
                       "black-market-connections")
    entry = {"card_name": "Black Market Connections", "set_code": None,
             "collector_number": None}
    html = watchlist_pages._site_links(entry, db)
    assert "/prints/141924-black-market-connections" in html
    assert "search?query" not in html          # the 404 shape is gone for good


# ── helpers ─────────────────────────────────────────────────────────────────

def _fake_httpx(routes):
    """Module stand-in exposing the one attribute refresh() uses: Client()."""
    client = FakeClient(routes)

    class _Ctx:
        def __enter__(self):
            return client

        def __exit__(self, *a):
            return False

    class _Module:
        def __init__(self):
            self.client = client

        def Client(self, *a, **kw):
            return _Ctx()

    return _Module()


def _allprintings(tmp_path, sets):
    import sqlite3
    p = str(tmp_path / "AllPrintings.sqlite")
    ap = sqlite3.connect(p)
    ap.execute("CREATE TABLE sets (code TEXT, name TEXT)")
    ap.executemany("INSERT INTO sets VALUES (?,?)", list(sets.items()))
    ap.commit()
    ap.close()
    return p
