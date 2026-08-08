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


async def test_add_kicks_instant_backfill_when_cache_exists(db_path, a_list,
                                                            fake_scryfall,
                                                            monkeypatch):
    kicked = []
    monkeypatch.setattr(server, "_schedule_backfill",
                        lambda *a: kicked.append(a) or True)
    list_id, pp, _ = a_list
    out = await server.watchlist_add(server.WatchlistAddInput(
        name="Sol Ring", passphrase=pp))
    assert len(kicked) == 1                      # single-flight, no per-card loop
    assert "fetching 90 days" in out.lower()


@pytest.fixture
def fake_scryfall_bulk(monkeypatch):
    """Batch /cards/collection fake: knows three cards, misses 'Nonexistent'."""
    known = {"sol ring": "Sol Ring", "rhystic study": "Rhystic Study",
             "cultivate": "Cultivate"}
    calls = {"batches": 0, "fuzzy": 0}

    async def _post(endpoint, body):
        assert endpoint == "/cards/collection"
        calls["batches"] += 1
        data, not_found = [], []
        for ident in body["identifiers"]:
            canon = known.get(ident["name"].lower())
            if canon:
                data.append({"name": canon, "prices": {"usd": "1.00"}})
            else:
                not_found.append(ident)
        return {"data": data, "not_found": not_found}

    async def _get(endpoint, params=None):
        calls["fuzzy"] += 1
        import httpx
        req = httpx.Request("GET", "x://x")
        raise httpx.HTTPStatusError("404", request=req,
                                    response=httpx.Response(404, request=req))

    monkeypatch.setattr(server, "_scryfall_post", _post)
    monkeypatch.setattr(server, "_scryfall_get", _get)
    return calls


async def test_bulk_add_one_batch_many_cards(db_path, a_list, fake_scryfall_bulk):
    list_id, pp, _ = a_list
    out = await server.watchlist_bulk_add(server.WatchlistBulkAddInput(
        decklist="1 Sol Ring\n1x Rhystic Study (c21) 12 [Draw]\nCultivate\n"
                 "# a comment\n\n",
        note="Cloud deck", passphrase=pp))
    assert "Added 3 card(s)" in out
    assert fake_scryfall_bulk["batches"] == 1        # ONE Scryfall round-trip
    db = watchlist_db.connect(db_path)
    entries = watchlist_db.current_entries(db, list_id)
    db.close()
    assert sorted(e["card_name"] for e in entries) == \
        ["Cultivate", "Rhystic Study", "Sol Ring"]
    assert all(e["note"] == "Cloud deck" for e in entries)


async def test_bulk_add_per_line_and_default_targets(db_path, a_list,
                                                     fake_scryfall_bulk):
    list_id, pp, _ = a_list
    await server.watchlist_bulk_add(server.WatchlistBulkAddInput(
        decklist="Rhystic Study @ 60\n1 Sol Ring @ $2.50\nCultivate",
        target_price=99.0, passphrase=pp))
    db = watchlist_db.connect(db_path)
    got = {e["card_name"]: e["target_price"]
           for e in watchlist_db.current_entries(db, list_id)}
    db.close()
    assert got == {"Rhystic Study": 60.0, "Sol Ring": 2.5, "Cultivate": 99.0}


async def test_bulk_add_reports_unknown_and_dedupes(db_path, a_list,
                                                    fake_scryfall_bulk):
    list_id, pp, _ = a_list
    out = await server.watchlist_bulk_add(server.WatchlistBulkAddInput(
        decklist="Sol Ring\nsol ring\nNonexistent Card", passphrase=pp))
    assert "Added 1 card(s)" in out
    assert "not recognized" in out and "Nonexistent Card" in out
    db = watchlist_db.connect(db_path)
    assert len(watchlist_db.current_entries(db, list_id)) == 1   # deduped
    db.close()


async def test_bulk_add_second_run_updates_not_duplicates(db_path, a_list,
                                                          fake_scryfall_bulk):
    list_id, pp, _ = a_list
    await server.watchlist_bulk_add(server.WatchlistBulkAddInput(
        decklist="Sol Ring", passphrase=pp))
    out = await server.watchlist_bulk_add(server.WatchlistBulkAddInput(
        decklist="Sol Ring @ 3\nCultivate", passphrase=pp))
    assert "Added 1 card(s)" in out and "already watched" in out
    db = watchlist_db.connect(db_path)
    got = {e["card_name"]: e["target_price"]
           for e in watchlist_db.current_entries(db, list_id)}
    db.close()
    assert got == {"Sol Ring": 3.0, "Cultivate": None}


async def test_bulk_add_kicks_one_backfill_for_the_batch(db_path, a_list,
                                                         fake_scryfall_bulk,
                                                         monkeypatch):
    kicked = []
    monkeypatch.setattr(server, "_schedule_backfill",
                        lambda *a: kicked.append(a) or True)
    _, pp, _ = a_list
    out = await server.watchlist_bulk_add(server.WatchlistBulkAddInput(
        decklist="Sol Ring\nCultivate\nRhystic Study", passphrase=pp))
    assert len(kicked) == 1                      # ONE fill for the whole batch
    assert "fetching 90 days" in out.lower()


async def test_bulk_add_requires_identity_and_rejects_empty(db_path,
                                                            fake_scryfall_bulk):
    out = await server.watchlist_bulk_add(
        server.WatchlistBulkAddInput(decklist="Sol Ring"))
    assert "watchlist_create" in out
    db = watchlist_db.connect(db_path)
    _, pp, _ = watchlist_db.create_list(db)
    db.close()
    out = await server.watchlist_bulk_add(server.WatchlistBulkAddInput(
        decklist="\n# nothing but comments\n", passphrase=pp))
    assert "No card names found" in out


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


@pytest.fixture
def fake_scryfall_printings(monkeypatch):
    """Scryfall fake that knows exactly one printing: C21 #263."""
    import httpx

    async def _fake(endpoint, params=None):
        if endpoint == "/cards/named":
            return {"name": "Sol Ring", "prices": {"usd": "1.23"}}
        if endpoint == "/cards/c21/263":
            return {"name": "Sol Ring", "set": "c21",
                    "collector_number": "263", "prices": {"usd": "2.50"}}
        req = httpx.Request("GET", "https://api.scryfall.com" + endpoint)
        raise httpx.HTTPStatusError("404", request=req,
                                    response=httpx.Response(404, request=req))

    monkeypatch.setattr(server, "_scryfall_get", _fake)


async def test_add_pinned_printing_validates(db_path, a_list,
                                             fake_scryfall_printings):
    list_id, pp, _ = a_list
    out = await server.watchlist_add(server.WatchlistAddInput(
        name="Sol Ring", set_code="C21", collector_number="263",
        passphrase=pp))
    assert "Sol Ring" in out and "C21" in out
    db = watchlist_db.connect(db_path)
    entries = watchlist_db.current_entries(db, list_id)
    db.close()
    assert entries[0]["set_code"] == "C21"


async def test_add_bad_printing_rejected(db_path, a_list,
                                         fake_scryfall_printings):
    list_id, pp, _ = a_list
    out = await server.watchlist_add(server.WatchlistAddInput(
        name="Sol Ring", set_code="XXX", collector_number="999",
        passphrase=pp))
    assert "No printing" in out and "XXX" in out
    db = watchlist_db.connect(db_path)
    assert watchlist_db.current_entries(db, list_id) == []   # nothing added
    db.close()


async def test_remove_specific_printing(db_path, a_list,
                                        fake_scryfall_printings):
    list_id, pp, _ = a_list
    db = watchlist_db.connect(db_path)
    watchlist_db.add_card(db, list_id, "Sol Ring", set_code="C21",
                          collector_number="263")
    watchlist_db.add_card(db, list_id, "Sol Ring", set_code="LTC",
                          collector_number="284")
    db.close()
    out = await server.watchlist_remove(server.WatchlistRemoveInput(
        name="Sol Ring", set_code="LTC", passphrase=pp))
    assert "Removed" in out
    db = watchlist_db.connect(db_path)
    remaining = watchlist_db.current_entries(db, list_id)
    db.close()
    assert len(remaining) == 1 and remaining[0]["set_code"] == "C21"


async def test_price_history_pinned_printing(db_path, a_list, fake_scryfall):
    db = watchlist_db.connect(db_path)
    db.executemany(
        "INSERT INTO card_uuids (card_name, uuid, set_code, collector_number)"
        " VALUES (?,?,?,?)",
        [("Sol Ring", "uuid-a", "C21", "263"),
         ("Sol Ring", "uuid-b", "LTC", "284")])
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-08", "tcgplayer",
                              "normal", 7.0)
    watchlist_db.upsert_price(db, "uuid-b", "2026-08-08", "tcgplayer",
                              "normal", 20.0)
    db.close()
    out = await server.price_history(server.PriceHistoryInput(
        name="Sol Ring", set_code="LTC", collector_number="284"))
    assert "uuid-b" in out and "20.0" in out
    out = await server.price_history(server.PriceHistoryInput(name="Sol Ring"))
    assert "uuid-a" in out                    # unpinned = cheapest


async def test_list_shows_foil_fallback(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    db = watchlist_db.connect(db_path)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES"
               " ('Sol Ring','uuid-foil')")
    watchlist_db.upsert_price(db, "uuid-foil", "2026-08-08", "tcgplayer",
                              "foil", 42.0)                  # foil-only printing
    db.close()
    out = await server.watchlist_list(server.WatchlistListInput(passphrase=pp))
    assert "42.00" in out and "foil" in out
