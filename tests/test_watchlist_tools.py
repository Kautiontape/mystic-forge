import pytest

import server
import watchlist_db


@pytest.fixture
def fake_scryfall(monkeypatch):
    async def _fake(endpoint, params=None):
        return {"name": "Sol Ring", "prices": {"usd": "1.23"},
                "set": "c21", "collector_number": "263"}
    monkeypatch.setattr(server, "_scryfall_get", _fake)


@pytest.fixture
def a_list(db_path):
    db = watchlist_db.connect(db_path)
    watchlist_db.init_db(db)
    list_id, pp, sc = watchlist_db.create_list(db, label="test")
    db.close()
    return list_id, pp, sc


async def test_create_returns_passphrase_and_urls(db_path):
    out = await server.watchlist_create(server.WatchlistCreateInput(label="x"))
    assert "/mtg/mcp/" in out and "/mtg/w/" in out and "SC-" in out


async def test_add_with_explicit_passphrase(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    out = await server.watchlist_add(server.WatchlistAddInput(
        name="sol ring", passphrase=pp, target_price=1.0))
    assert "Sol Ring" in out
    db = watchlist_db.connect(db_path)
    entries = watchlist_db.current_entries(db, list_id)
    db.close()
    assert len(entries) == 1 and entries[0]["card_name"] == "Sol Ring"


async def test_add_without_identity_errors_helpfully(db_path, fake_scryfall):
    out = await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring"))
    assert "watchlist_create" in out and "passphrase" in out.lower()


async def test_add_with_bad_passphrase_errors(db_path, fake_scryfall):
    out = await server.watchlist_add(server.WatchlistAddInput(
        name="Sol Ring", passphrase="bogus-bogus-bogus-bogus-00"))
    assert "passphrase" in out.lower() and "not" in out.lower()


async def test_url_context_used_when_no_param(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    token = server._current_list.set(list_id)
    try:
        await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring"))
    finally:
        server._current_list.reset(token)
    db = watchlist_db.connect(db_path)
    assert len(watchlist_db.current_entries(db, list_id)) == 1
    db.close()


async def test_remove(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    out = await server.watchlist_remove(server.WatchlistRemoveInput(
        name="Sol Ring", passphrase=pp))
    assert "Removed" in out
    db = watchlist_db.connect(db_path)
    assert watchlist_db.current_entries(db, list_id) == []
    db.close()


async def test_list_shows_prices_and_deltas(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    db = watchlist_db.connect(db_path)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES"
               " ('Sol Ring','uuid-a')")
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-01", "tcgplayer",
                              "normal", 8.0)
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-08", "tcgplayer",
                              "normal", 7.0)
    db.close()
    out = await server.watchlist_list(server.WatchlistListInput(passphrase=pp))
    assert "Sol Ring" in out and "7.0" in out


async def test_mutating_superseded_list_warns(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    db = watchlist_db.connect(db_path)
    new_id, _, _ = watchlist_db.clone_list(db, list_id, recovery=True)
    successor = watchlist_db.get_list(db, new_id)
    db.close()
    out = await server.watchlist_add(server.WatchlistAddInput(
        name="Sol Ring", passphrase=pp))
    assert "superseded" in out.lower()
    assert successor["share_code"] in out


def _seed_prices(db_path, name="Sol Ring", uuid="uuid-a"):
    db = watchlist_db.connect(db_path)
    db.execute("INSERT OR IGNORE INTO card_uuids (card_name, uuid) VALUES (?,?)",
               (name, uuid))
    watchlist_db.upsert_price(db, uuid, "2026-07-09", "tcgplayer", "normal", 10.0)
    watchlist_db.upsert_price(db, uuid, "2026-08-01", "tcgplayer", "normal", 8.0)
    watchlist_db.upsert_price(db, uuid, "2026-08-08", "tcgplayer", "normal", 7.0)
    db.close()


async def test_report_flags_target_hits(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(
        name="Sol Ring", passphrase=pp, target_price=30.0))
    _seed_prices(db_path)
    out = await server.watchlist_report(server.WatchlistListInput(passphrase=pp))
    assert "Sol Ring" in out and "target" in out.lower()


async def test_view_by_share_code_is_readonly_surface(db_path, a_list,
                                                      fake_scryfall):
    list_id, pp, sc = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    out = await server.watchlist_view(server.WatchlistViewInput(share_code=sc))
    assert "Sol Ring" in out
    out = await server.watchlist_view(server.WatchlistViewInput(
        share_code="SC-ZZZZZZ"))
    assert "not" in out.lower()          # unknown code


async def test_history_lists_chain(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    await server.watchlist_remove(server.WatchlistRemoveInput(
        name="Sol Ring", passphrase=pp))
    out = await server.watchlist_history(server.WatchlistHistoryInput(
        passphrase=pp))
    assert "add" in out and "remove" in out and "#3" in out


async def test_clone_own_list_is_recovery(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    out = await server.watchlist_clone(server.WatchlistCloneInput(passphrase=pp))
    assert "Passphrase" in out
    db = watchlist_db.connect(db_path)
    assert watchlist_db.get_list(db, list_id)["superseded_by"] is not None
    db.close()


async def test_clone_via_share_code_is_fork(db_path, a_list, fake_scryfall):
    list_id, pp, sc = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    out = await server.watchlist_clone(server.WatchlistCloneInput(share_code=sc))
    assert "Passphrase" in out
    db = watchlist_db.connect(db_path)
    assert watchlist_db.get_list(db, list_id)["superseded_by"] is None
    db.close()


async def test_price_history_series(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    _seed_prices(db_path)
    out = await server.price_history(server.PriceHistoryInput(name="Sol Ring"))
    assert "2026-08-08" in out and "7.0" in out
