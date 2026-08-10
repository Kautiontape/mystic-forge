import gzip
import json
import os
import sqlite3
import tracemalloc

import price_sidecar
import watchlist_db
import watchlist_ingest


def make_allprintings(tmp_path):
    """Minimal AllPrintings.sqlite lookalike, mirroring the real MTGJSON
    schema: cards has no scryfallId; it lives in cardIdentifiers."""
    p = str(tmp_path / "AllPrintings.sqlite")
    ap = sqlite3.connect(p)
    ap.execute("CREATE TABLE cards (name TEXT, uuid TEXT, setCode TEXT,"
               " number TEXT)")
    ap.execute("CREATE TABLE cardIdentifiers (uuid TEXT, scryfallId TEXT)")
    ap.executemany("INSERT INTO cards VALUES (?,?,?,?)", [
        ("Sol Ring", "uuid-a", "C21", "263"),
        ("Sol Ring", "uuid-b", "LTC", "284"),
        ("Cultivate", "uuid-c", "M21", "177"),
    ])
    ap.executemany("INSERT INTO cardIdentifiers VALUES (?,?)", [
        ("uuid-a", "scry-a"), ("uuid-b", "scry-b"), ("uuid-c", "scry-c"),
    ])
    ap.commit()
    ap.close()
    return p


def make_prices_gz(tmp_path, name, data):
    p = str(tmp_path / name)
    with gzip.open(p, "wt") as f:
        json.dump({"meta": {"version": "5"}, "data": data}, f)
    return p


PRICE_OBJ = {"paper": {"tcgplayer": {"currency": "USD", "retail": {
    "normal": {"2026-08-01": 8.0, "2026-08-08": 7.0},
    "foil": {"2026-08-08": 30.0}}}}}


def test_resolve_watched_names(db, tmp_path):
    ap = make_allprintings(tmp_path)
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_db.add_card(db, list_id, "Cultivate", set_code="M21",
                          collector_number="177")
    n = watchlist_ingest.resolve_watched(db, ap)
    assert n == 3
    uuids = {r["uuid"] for r in db.execute("SELECT uuid FROM card_uuids")}
    assert uuids == {"uuid-a", "uuid-b", "uuid-c"}
    # idempotent
    assert watchlist_ingest.resolve_watched(db, ap) == 0


def test_ingest_prices_only_watched_uuids(db, tmp_path):
    ap = make_allprintings(tmp_path)
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_ingest.resolve_watched(db, ap)
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {
        "uuid-a": PRICE_OBJ,
        "uuid-unwatched": PRICE_OBJ,
    })
    n = watchlist_ingest.ingest_prices_file(db, gz)
    assert n == 3                       # 2 normal + 1 foil rows for uuid-a
    uuids = {r["uuid"] for r in db.execute("SELECT DISTINCT uuid FROM prices")}
    assert uuids == {"uuid-a"}


def test_ingest_idempotent(db, tmp_path):
    """Spec acceptance: re-running the same day changes nothing."""
    ap = make_allprintings(tmp_path)
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_ingest.resolve_watched(db, ap)
    gz = make_prices_gz(tmp_path, "AllPricesToday.json.gz", {"uuid-a": PRICE_OBJ})
    watchlist_ingest.ingest_prices_file(db, gz)
    before = db.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    watchlist_ingest.ingest_prices_file(db, gz)
    assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == before


def test_streaming_never_loads_whole_file(db, tmp_path):
    """Spec acceptance: bounded memory on a large AllPrices file."""
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','uuid-0')")
    db.commit()
    data = {f"uuid-{i}": PRICE_OBJ for i in range(5000)}
    gz = make_prices_gz(tmp_path, "big.json.gz", data)
    tracemalloc.start()
    watchlist_ingest.ingest_prices_file(db, gz)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 20 * 1024 * 1024      # far below the ~15MB decoded JSON


def test_backfill_entry_uses_cached_files_for_one_card(db, db_path, tmp_path):
    """Adding a card gets instant history from the nightly-cached files:
    resolve via local AllPrintings, stream cached AllPrices for its uuids."""
    make_allprintings(tmp_path)
    make_prices_gz(tmp_path, "AllPrices.json.gz", {
        "uuid-a": PRICE_OBJ, "uuid-c": PRICE_OBJ, "uuid-other": PRICE_OBJ})
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_db.add_card(db, list_id, "Cultivate")
    db.commit()

    n = watchlist_ingest.backfill_entry(db_path, str(tmp_path), "Sol Ring")
    assert n == 3                                     # only Sol Ring's uuids
    uuids = {r["uuid"] for r in db.execute("SELECT DISTINCT uuid FROM prices")}
    assert uuids == {"uuid-a"}                        # uuid-c/other untouched
    # resolution happened for ALL pending names as a side effect (it's cheap)
    assert db.execute("SELECT COUNT(*) FROM card_uuids").fetchone()[0] == 3


def test_ensure_history_downloads_when_cache_is_cold(db, db_path, tmp_path,
                                                     monkeypatch):
    """A fresh server must not make users wait for the nightly cycle: history
    is a static backfill, so ensure_history fetches the files on demand."""
    src = tmp_path / "src"
    src.mkdir()
    ap = make_allprintings(src)
    gz = make_prices_gz(src, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    fetched = []

    def fake_download(url, dest, _db):
        import shutil
        fetched.append(url.rsplit("/", 1)[-1])
        shutil.copy(ap if url.endswith(".sqlite") else gz, dest)
        return dest
    monkeypatch.setattr(watchlist_ingest, "_download", fake_download)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    n = watchlist_ingest.ensure_history(db_path, str(tmp_path))
    assert fetched == ["AllPrintings.sqlite", "AllPrices.json.gz"]
    assert n == 3
    assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 3
    # idempotent: nothing missing now, so no second pass and no re-download
    assert watchlist_ingest.ensure_history(db_path, str(tmp_path)) == 0
    assert len(fetched) == 2


def test_empty_watchlist_does_not_stamp_last_ingest(db, db_path, tmp_path):
    """Regression: stamping last_ingest on an empty list made the first cards
    added that day wait until tomorrow for any prices."""
    watchlist_ingest.run_ingest(db_path, str(tmp_path))
    db2 = watchlist_db.connect(db_path)
    row = db2.execute("SELECT value FROM meta WHERE key='last_ingest'").fetchone()
    db2.close()
    assert row is None


def test_backfill_entry_defers_when_nothing_cached(db, db_path, tmp_path):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()
    assert watchlist_ingest.backfill_entry(db_path, str(tmp_path),
                                           "Sol Ring") == 0


def test_notify_hits_pushes_once_per_new_hit(db, monkeypatch):
    posts = []
    monkeypatch.setattr(watchlist_ingest.httpx, "post",
                        lambda url, **kw: posts.append((url, kw)))
    list_id, _, sc = watchlist_db.create_list(db, label="Alerts")
    seq, _ = watchlist_db.add_card(db, list_id, "Sol Ring", target_price=10.0)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "tcgplayer", "normal", 7.0)

    assert watchlist_ingest.notify_hits(db) == 1
    url, kw = posts[0]
    assert watchlist_ingest.ntfy_topic(sc) in url
    assert "Sol Ring $7.00" in kw["content"]
    assert kw["headers"]["Title"] == "Alerts"

    assert watchlist_ingest.notify_hits(db) == 0     # same hit: no re-ping
    watchlist_db.set_bought(db, list_id, seq)
    assert watchlist_ingest.notify_hits(db) == 0     # bought never pings
    watchlist_db.set_bought(db, list_id, seq, bought=False)
    assert watchlist_ingest.notify_hits(db) == 0     # still not NEW


def test_notify_hits_respects_off_switch(db, monkeypatch):
    monkeypatch.setenv("MYSTIC_FORGE_NTFY_OFF", "1")
    posts = []
    monkeypatch.setattr(watchlist_ingest.httpx, "post",
                        lambda url, **kw: posts.append(url))
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=10.0)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "tcgplayer", "normal", 7.0)
    assert watchlist_ingest.notify_hits(db) == 0 and posts == []


def test_run_ingest_records_last_ingest(db, db_path, tmp_path, monkeypatch):
    ap = make_allprintings(tmp_path)
    gz_all = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    gz_today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                              {"uuid-a": PRICE_OBJ})
    monkeypatch.setattr(watchlist_ingest, "_download",
                        lambda url, dest, db: {"AllPrintings.sqlite": ap,
                                               "AllPrices.json.gz": gz_all,
                                               "AllPricesToday.json.gz": gz_today
                                               }[url.rsplit("/", 1)[-1]])
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.close()
    watchlist_ingest.run_ingest(db_path, str(tmp_path))
    db2 = watchlist_db.connect(db_path)
    assert db2.execute("SELECT value FROM meta WHERE key='last_ingest'"
                       ).fetchone() is not None
    assert db2.execute("SELECT COUNT(*) FROM prices").fetchone()[0] > 0
    db2.close()


MANAPOOL_OBJ = {"paper": {"manapool": {"currency": "USD", "retail": {
    "normal": {"2026-08-08": 4.25}}}}}


def test_manapool_is_ingested(db, tmp_path):
    """MTGJSON added a fourth paper provider; it must be tracked like the rest."""
    ap = make_allprintings(tmp_path)
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_ingest.resolve_watched(db, ap)
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": MANAPOOL_OBJ})
    watchlist_ingest.ingest_prices_file(db, gz)
    row = db.execute("SELECT price FROM prices WHERE provider='manapool'").fetchone()
    assert row is not None and row["price"] == 4.25


def test_manapool_is_a_selectable_shop():
    import watchlist_pages
    assert watchlist_pages.SHOPS["manapool"] == "$"


# 0.015 is deliberate: round(x, 2) and cents quantization round at different
# scales and disagree on ~4% of three-decimal prices, 0.015 among them.
# 7.129 alone (the value the plan named) happens to agree, which would let an
# exact-equality equivalence assertion pass while still being wrong.
THREE_DP_OBJ = {"paper": {"tcgplayer": {"retail": {
    "normal": {"2026-08-01": 8.0, "2026-08-08": 7.129, "2026-08-09": 0.015}}}}}


def test_sidecar_path_sits_beside_the_database(tmp_path):
    assert watchlist_ingest._sidecar_path(str(tmp_path)) == \
        os.path.join(str(tmp_path), "price_sidecar.sqlite")


def test_ensure_history_uses_the_sidecar_without_touching_mtgjson(
        db, db_path, tmp_path, monkeypatch):
    """The whole point: a ready sidecar means no file scan and no download."""
    ap = make_allprintings(tmp_path)
    gz = make_prices_gz(tmp_path, "src-allprices.json.gz", {"uuid-a": PRICE_OBJ})
    price_sidecar.build_from_allprices(
        watchlist_ingest._sidecar_path(str(tmp_path)), gz)

    def boom(*a, **k):
        raise AssertionError("must not download with a ready sidecar")
    monkeypatch.setattr(watchlist_ingest, "_download", boom)
    monkeypatch.setattr(watchlist_ingest, "ingest_prices_file", boom)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()
    watchlist_ingest.resolve_watched(db, ap)
    db.commit()

    n = watchlist_ingest.ensure_history(db_path, str(tmp_path))
    assert n == 3
    got = {(r["provider"], r["finish"], r["date"], r["price"])
           for r in db.execute("SELECT * FROM prices")}
    assert ("tcgplayer", "normal", "2026-08-08", 7.0) in got


def test_sidecar_projection_matches_the_legacy_scan(db, db_path, tmp_path):
    """Equivalence — the safety net for the whole feature.

    Rows must match exactly; prices must match to within the one cent the
    sidecar's quantization is documented to cost. NOT exact equality: the
    legacy scan stores the raw float and round(x, 2) rounds at a different
    scale than round(x * 100), so the two disagree by a cent on ~4% of
    three-decimal prices. The fixture carries one such price (0.015) so the
    tolerance is exercised rather than dodged."""
    ap = make_allprintings(tmp_path)
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": THREE_DP_OBJ})
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()
    watchlist_ingest.resolve_watched(db, ap)
    db.commit()

    watchlist_ingest.ingest_prices_file(db, gz)
    legacy = {(r["uuid"], r["date"], r["provider"], r["finish"]): r["price"]
              for r in db.execute("SELECT * FROM prices")}
    assert legacy, "nothing to compare — the legacy scan wrote no rows"
    db.execute("DELETE FROM prices")
    db.commit()

    side = watchlist_ingest._sidecar_path(str(tmp_path))
    price_sidecar.build_from_allprices(side, gz)
    watchlist_ingest._project(db, side, ["uuid-a"])
    projected = {(r["uuid"], r["date"], r["provider"], r["finish"]): r["price"]
                 for r in db.execute("SELECT * FROM prices")}

    # A dropped or invented point is a failure, not a rounding difference.
    assert projected.keys() == legacy.keys()
    for key, want in legacy.items():
        assert abs(projected[key] - want) <= 0.01, key


def test_projection_quantizes_a_three_decimal_price_to_the_nearest_cent(
        db, db_path, tmp_path):
    """Pins the one divergence the equivalence test tolerates, so the cent of
    slack stays a known quantization and cannot quietly widen."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": THREE_DP_OBJ})
    side = watchlist_ingest._sidecar_path(str(tmp_path))
    price_sidecar.build_from_allprices(side, gz)
    watchlist_ingest._project(db, side, ["uuid-a"])

    got = {r["date"]: r["price"] for r in db.execute(
        "SELECT date, price FROM prices WHERE finish='normal'")}
    assert got["2026-08-08"] == 7.13
    assert got["2026-08-09"] == 0.02      # the legacy scan stores 0.015


def test_ensure_history_falls_back_when_no_sidecar(db, db_path, tmp_path,
                                                   monkeypatch):
    """With no sidecar, behaviour is exactly what it was before this feature."""
    src = tmp_path / "src"
    src.mkdir()
    ap = make_allprintings(src)
    gz = make_prices_gz(src, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    fetched = []

    def fake_download(url, dest, _db):
        import shutil
        fetched.append(url.rsplit("/", 1)[-1])
        shutil.copy(ap if url.endswith(".sqlite") else gz, dest)
        return dest
    monkeypatch.setattr(watchlist_ingest, "_download", fake_download)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    n = watchlist_ingest.ensure_history(db_path, str(tmp_path))
    assert n > 0
    assert "AllPrices.json.gz" in fetched


def test_ensure_history_falls_back_when_sidecar_disabled(
        db, db_path, tmp_path, monkeypatch):
    """MYSTIC_FORGE_NO_SIDECAR is the operator escape hatch."""
    ap = make_allprintings(tmp_path)
    gz = make_prices_gz(tmp_path, "src.json.gz", {"uuid-a": PRICE_OBJ})
    price_sidecar.build_from_allprices(
        watchlist_ingest._sidecar_path(str(tmp_path)), gz)
    monkeypatch.setenv("MYSTIC_FORGE_NO_SIDECAR", "1")

    scanned = []
    real = watchlist_ingest.ingest_prices_file
    monkeypatch.setattr(watchlist_ingest, "ingest_prices_file",
                        lambda *a, **k: (scanned.append(1), real(*a, **k))[1])
    monkeypatch.setattr(watchlist_ingest, "_download",
                        lambda url, dest, _db: __import__("shutil").copy(gz, dest) or dest)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()
    watchlist_ingest.ensure_history(db_path, str(tmp_path))
    assert scanned, "disabled sidecar must take the legacy path"


def test_ensure_history_falls_back_on_a_corrupt_sidecar(db, db_path, tmp_path,
                                                        monkeypatch):
    """A damaged sidecar must degrade to the scan, never raise into a page."""
    src = tmp_path / "src"
    src.mkdir()
    ap = make_allprintings(src)
    gz = make_prices_gz(src, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    with open(watchlist_ingest._sidecar_path(str(tmp_path)), "wb") as f:
        f.write(b"not a database at all")

    def fake_download(url, dest, _db):
        import shutil
        shutil.copy(ap if url.endswith(".sqlite") else gz, dest)
        return dest
    monkeypatch.setattr(watchlist_ingest, "_download", fake_download)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    n = watchlist_ingest.ensure_history(db_path, str(tmp_path))
    assert n > 0
    assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] > 0


def test_ensure_history_falls_back_when_the_sidecar_read_raises(
        db, db_path, tmp_path, monkeypatch):
    """A *ready* sidecar that fails mid-read must still degrade to the scan.

    is_ready() only proves the file opens; it says nothing about the read that
    follows. Letting that read's exception escape is worse than a 500 — the
    caller (_schedule_backfill) swallows it, so `missing` stays missing and
    every later page load re-fires a fill that fails identically forever."""
    ap = make_allprintings(tmp_path)
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    price_sidecar.build_from_allprices(
        watchlist_ingest._sidecar_path(str(tmp_path)), gz)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(price_sidecar, "series_for_uuids", boom)

    def no_download(*a, **k):
        raise AssertionError("both files are already cached")
    monkeypatch.setattr(watchlist_ingest, "_download", no_download)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    n = watchlist_ingest.ensure_history(db_path, str(tmp_path))
    assert n == 3
    assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 3


def test_a_failed_projection_leaves_none_of_its_rows_behind(
        db, db_path, tmp_path, monkeypatch):
    """A projection that dies part-way must not leak its staged rows.

    _project writes with commit=False so an abort leaves `prices` untouched --
    but the scan it falls back to runs on the *same* connection and ends in its
    own commit, which would happily persist whatever the failed projection had
    already staged. Only an explicit rollback makes the docstring true."""
    ap = make_allprintings(tmp_path)
    # The sidecar knows 2026-08-01; the file the scan reads does not, so a row
    # for that date in `prices` can only have come from the failed projection.
    sidecar_gz = make_prices_gz(tmp_path, "seed.json.gz", {"uuid-a": PRICE_OBJ})
    price_sidecar.build_from_allprices(
        watchlist_ingest._sidecar_path(str(tmp_path)), sidecar_gz)
    make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": {"paper": {
        "tcgplayer": {"retail": {"normal": {"2026-08-08": 7.0}}}}}})

    real = price_sidecar.series_for_uuids

    def yield_then_die(*a, **k):
        for row in real(*a, **k):
            yield row          # stage at least one row, then fail
            raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(price_sidecar, "series_for_uuids", yield_then_die)

    def no_download(*a, **k):
        raise AssertionError("both files are already cached")
    monkeypatch.setattr(watchlist_ingest, "_download", no_download)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    assert watchlist_ingest.ensure_history(db_path, str(tmp_path)) == 1
    dates = {r["date"] for r in db.execute("SELECT date FROM prices")}
    assert dates == {"2026-08-08"}, "a staged sidecar row survived the abort"


def test_projection_carries_both_daily_and_weekly_points(db, db_path, tmp_path):
    """Spec acceptance 3: a card with history past the window projects daily
    points inside it and weekly means beyond, and price_series returns both."""
    ap = make_allprintings(tmp_path)
    old = {"uuid-a": {"paper": {"tcgplayer": {"retail": {"normal": {
        **{f"2026-01-{d:02d}": 5.0 for d in range(5, 12)},     # one whole week
        "2026-08-08": 7.0}}}}}}
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", old)
    side = watchlist_ingest._sidecar_path(str(tmp_path))
    price_sidecar.build_from_allprices(side, gz)
    price_sidecar.downsample(side, keep_daily_days=120, today="2026-08-09")

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()
    watchlist_ingest.resolve_watched(db, ap)
    db.commit()
    watchlist_ingest._project(db, side, ["uuid-a"])

    dates = {r["date"] for r in db.execute("SELECT date FROM prices")}
    assert "2026-01-11" in dates          # the collapsed week's Sunday mean
    assert "2026-08-08" in dates          # still daily inside the window
    assert "2026-01-06" not in dates      # collapsed away
    series = watchlist_db.price_series(db, ["uuid-a"], days=400,
                                       today="2026-08-09")
    assert len(series["points"]) == 2
