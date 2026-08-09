from mystic_forge.watchlist import db as watchlist_db


def seed(db):
    # uuid-a: cheap printing trending down; uuid-b: expensive printing
    rows = [
        ("uuid-a", "2026-07-09", 10.0), ("uuid-a", "2026-07-25", 9.0),
        ("uuid-a", "2026-08-01", 8.0), ("uuid-a", "2026-08-08", 7.0),
        ("uuid-b", "2026-08-08", 20.0),
    ]
    for uuid, date, price in rows:
        watchlist_db.upsert_price(db, uuid, date, "tcgplayer", "normal", price)
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-08", "tcgplayer", "foil", 30.0)
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-08", "cardkingdom", "normal", 1.0)


def test_upsert_price_idempotent(db):
    watchlist_db.upsert_price(db, "u", "2026-08-08", "tcgplayer", "normal", 5.0)
    watchlist_db.upsert_price(db, "u", "2026-08-08", "tcgplayer", "normal", 6.0)
    rows = db.execute("SELECT price FROM prices").fetchall()
    assert len(rows) == 1 and rows[0][0] == 6.0


def test_price_summary_picks_cheapest_printing_default_provider(db):
    seed(db)
    s = watchlist_db.price_summary(db, ["uuid-a", "uuid-b"],
                                   today="2026-08-08")
    assert s["uuid"] == "uuid-a"          # cheapest normal-finish tcgplayer
    assert s["current"] == 7.0
    assert s["d7"] == 7.0 - 8.0           # vs at-or-before 2026-08-01
    assert s["d30"] == 7.0 - 10.0         # vs at-or-before 2026-07-09
    assert s["date"] == "2026-08-08"


def test_price_summary_none_when_no_data(db):
    assert watchlist_db.price_summary(db, ["nope"], today="2026-08-08") is None
    assert watchlist_db.price_summary(db, [], today="2026-08-08") is None


def test_price_series_for_cheapest(db):
    seed(db)
    series = watchlist_db.price_series(db, ["uuid-a", "uuid-b"], days=90,
                                       today="2026-08-08")
    assert series["uuid"] == "uuid-a"
    assert series["points"][0] == ("2026-07-09", 10.0)
    assert series["points"][-1] == ("2026-08-08", 7.0)


def test_price_series_respects_days_window(db):
    seed(db)
    series = watchlist_db.price_series(db, ["uuid-a"], days=10,
                                       today="2026-08-08")
    assert all(d >= "2026-07-29" for d, _ in series["points"])


def test_entry_price_summary_falls_back_to_foil(db):
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Etched','u-f')")
    watchlist_db.upsert_price(db, "u-f", "2026-08-08", "tcgplayer", "foil", 42.0)
    entry = {"card_name": "Etched", "set_code": None, "collector_number": None,
             "uuid": None}
    s = watchlist_db.entry_price_summary(db, entry, today="2026-08-08")
    assert s["current"] == 42.0 and s["finish"] == "foil"


def test_envelope_survives_cheapest_printing_flip(db):
    """A reprint flipping which printing is cheapest must not rewrite history:
    deltas come from min-across-printings per date (persona: the Optimizer)."""
    watchlist_db.upsert_price(db, "A", "2026-08-01", "tcgplayer", "normal", 10.0)
    watchlist_db.upsert_price(db, "A", "2026-08-08", "tcgplayer", "normal", 10.0)
    watchlist_db.upsert_price(db, "B", "2026-08-01", "tcgplayer", "normal", 20.0)
    watchlist_db.upsert_price(db, "B", "2026-08-08", "tcgplayer", "normal", 8.0)
    s = watchlist_db.price_summary(db, ["A", "B"], today="2026-08-08")
    assert s["current"] == 8.0 and s["uuid"] == "B"
    assert s["d7"] == 8.0 - 10.0          # envelope: min(now) - min(7d ago)
    series = watchlist_db.price_series(db, ["A", "B"], today="2026-08-08")
    assert series["points"] == [("2026-08-01", 10.0), ("2026-08-08", 8.0)]


def test_entry_price_summary_prefers_normal(db):
    seed(db)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','uuid-a')")
    entry = {"card_name": "Sol Ring", "set_code": None,
             "collector_number": None, "uuid": None}
    s = watchlist_db.entry_price_summary(db, entry, today="2026-08-08")
    assert s["current"] == 7.0 and s["finish"] == "normal"
