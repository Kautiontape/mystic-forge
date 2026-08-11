import gzip
import json
import os
import sqlite3
import threading
import tracemalloc

import httpx
import pytest

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


def test_run_ingest_feeds_the_sidecar_then_projects(db, db_path, tmp_path,
                                                    monkeypatch):
    """Nightly: AllPricesToday lands in the sidecar, and only the new dates
    are projected into prices."""
    src = tmp_path / "src"
    src.mkdir()
    ap = make_allprintings(src)
    seed = make_prices_gz(src, "seed.json.gz", {"uuid-a": PRICE_OBJ})
    today = make_prices_gz(src, "AllPricesToday.json.gz", {
        "uuid-a": {"paper": {"tcgplayer": {"retail": {
            "normal": {"2026-08-09": 6.75}}}}}})

    side = watchlist_ingest._sidecar_path(str(tmp_path))
    price_sidecar.build_from_allprices(side, seed)

    def fake_download(url, dest, _db):
        import shutil
        shutil.copy(ap if url.endswith(".sqlite") else today, dest)
        return dest
    monkeypatch.setattr(watchlist_ingest, "_download", fake_download)
    monkeypatch.setattr(watchlist_ingest, "notify_hits", lambda _db: 0)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    watchlist_ingest.run_ingest(db_path, str(tmp_path))

    assert price_sidecar.daily_through(side) == "2026-08-09"
    fresh = db.execute(
        "SELECT price FROM prices WHERE date='2026-08-09'").fetchone()
    assert fresh is not None and fresh["price"] == 6.75


def test_run_ingest_downsamples(db, db_path, tmp_path, monkeypatch):
    """Aged daily rows must not accumulate forever."""
    src = tmp_path / "src"
    src.mkdir()
    ap = make_allprintings(src)
    old = make_prices_gz(src, "seed.json.gz",
                         {"uuid-a": {"paper": {"tcgplayer": {"retail": {
                             "normal": {f"2020-01-{d:02d}": 1.0
                                        for d in range(6, 13)}}}}}})
    today = make_prices_gz(src, "AllPricesToday.json.gz", {"uuid-a": PRICE_OBJ})
    side = watchlist_ingest._sidecar_path(str(tmp_path))
    price_sidecar.build_from_allprices(side, old)

    def fake_download(url, dest, _db):
        import shutil
        shutil.copy(ap if url.endswith(".sqlite") else today, dest)
        return dest
    monkeypatch.setattr(watchlist_ingest, "_download", fake_download)
    monkeypatch.setattr(watchlist_ingest, "notify_hits", lambda _db: 0)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()
    watchlist_ingest.run_ingest(db_path, str(tmp_path))

    sdb = price_sidecar.connect(side)
    weekly = sdb.execute("SELECT COUNT(*) FROM points WHERE agg=1").fetchone()[0]
    sdb.close()
    assert weekly >= 1


def _nightly_fixture(tmp_path, monkeypatch, today_data, seed_data=None):
    """A built sidecar plus a stubbed _download serving AllPrintings and a
    caller-supplied AllPricesToday. Returns (sidecar_path, fetched_names)."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    ap = make_allprintings(src)
    seed = make_prices_gz(src, "seed.json.gz",
                          seed_data or {"uuid-a": PRICE_OBJ})
    today = make_prices_gz(src, "AllPricesToday.json.gz", today_data)
    side = watchlist_ingest._sidecar_path(str(tmp_path))
    price_sidecar.build_from_allprices(side, seed)

    fetched = []

    def fake_download(url, dest, _db):
        import shutil
        fetched.append(url.rsplit("/", 1)[-1])
        shutil.copy(ap if url.endswith(".sqlite") else today, dest)
        return dest
    monkeypatch.setattr(watchlist_ingest, "_download", fake_download)
    monkeypatch.setattr(watchlist_ingest, "notify_hits", lambda _db: 0)
    return side, fetched


def test_run_ingest_projects_only_the_new_dates(db, db_path, tmp_path,
                                                monkeypatch):
    """`since` is load-bearing, and this is the test that says so.

    The projection covers the old watermark date and everything after it, and
    nothing before. The watermark date is deliberately *included*: a same-date
    revision is a changed price on exactly that day, and `since` being
    exclusive would drop it forever (see
    test_run_ingest_projects_a_same_date_revision). So the floor sits one day
    below the old watermark, and the sentinels below check the other half of
    the bargain — that widening the window by a day did not widen it by more.

    They carry deliberately wrong values on dates the sidecar also holds, at
    and below the floor. Drop the `since` argument and they are overwritten
    with the sidecar's own values, and a foil point that has never been
    projected appears out of nowhere."""
    side, _ = _nightly_fixture(tmp_path, monkeypatch, {
        "uuid-a": {"paper": {"tcgplayer": {"retail": {
            "normal": {"2026-08-09": 6.75}}}}}},
        seed_data={"uuid-a": {"paper": {"tcgplayer": {"retail": {
            "normal": {"2026-08-01": 8.0, "2026-08-07": 7.5,
                       "2026-08-08": 7.0},
            "foil": {"2026-08-01": 30.0}}}}}})
    assert price_sidecar.daily_through(side) == "2026-08-08"

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-01", "tcgplayer",
                              "normal", 999.0)
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-07", "tcgplayer",
                              "normal", 999.0)
    db.commit()

    watchlist_ingest.run_ingest(db_path, str(tmp_path))

    assert price_sidecar.daily_through(side) == "2026-08-09"
    got = {(r["date"], r["finish"]): r["price"] for r in db.execute(
        "SELECT * FROM prices WHERE uuid='uuid-a' AND provider='tcgplayer'")}
    assert got[("2026-08-09", "normal")] == 6.75     # the day just applied
    assert got[("2026-08-08", "normal")] == 7.0      # the watermark, re-projected
    assert got[("2026-08-01", "normal")] == 999.0    # older: not re-projected
    # The floor itself is exclusive, so this pins the step-back at exactly one
    # day: step back two and this sentinel is overwritten with 7.5.
    assert got[("2026-08-07", "normal")] == 999.0
    # The sidecar holds foil/2026-08-01 (30.0) and nothing has ever projected
    # it. It can only appear if the projection ignored `since`.
    assert ("2026-08-01", "foil") not in got


def test_run_ingest_projects_a_same_date_revision(db, db_path, tmp_path,
                                                  monkeypatch):
    """MTGJSON republishing the watermark date with a revised price must reach
    `prices`.

    `since` is exclusive, so projecting from the old watermark forward skips
    that date entirely: the revision lands in the sidecar and no later run can
    ever copy it, because every later run projects from a strictly greater
    date. On a fresh watchlist it is worse still — the first nightly run after
    a build sees AllPricesToday carrying the date the build already had, and
    projects nothing at all, leaving `prices` empty. Stepping the floor back
    one day is what closes both."""
    side, _ = _nightly_fixture(tmp_path, monkeypatch, {
        "uuid-a": {"paper": {"tcgplayer": {"retail": {
            "normal": {"2026-08-08": 6.50}}}}}})     # same date, revised price
    assert price_sidecar.daily_through(side) == "2026-08-08"

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    watchlist_ingest.run_ingest(db_path, str(tmp_path))

    # 7.0 was the seeded price for that day; 8.0 on 2026-08-01 sits below the
    # floor and is ensure_history's job, not the nightly run's.
    got = {r["date"]: r["price"] for r in db.execute(
        "SELECT date, price FROM prices WHERE uuid='uuid-a'"
        " AND provider='tcgplayer' AND finish='normal'")}
    assert got == {"2026-08-08": 6.50}


def test_run_ingest_catches_up_on_several_days_at_once(db, db_path, tmp_path,
                                                       monkeypatch):
    """The claim the `since` comment makes: because `previous` is exclusive
    rather than "yesterday", a sidecar advanced by several days projects every
    one of them in a single pass."""
    side, _ = _nightly_fixture(tmp_path, monkeypatch, {
        "uuid-a": {"paper": {"tcgplayer": {"retail": {"normal": {
            "2026-08-09": 6.75, "2026-08-10": 6.50, "2026-08-11": 6.25}}}}}})

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-01", "tcgplayer",
                              "normal", 999.0)
    db.commit()

    watchlist_ingest.run_ingest(db_path, str(tmp_path))

    assert price_sidecar.daily_through(side) == "2026-08-11"
    got = {r["date"]: r["price"] for r in db.execute(
        "SELECT date, price FROM prices WHERE uuid='uuid-a'"
        " AND provider='tcgplayer' AND finish='normal'")}
    assert got["2026-08-09"] == 6.75
    assert got["2026-08-10"] == 6.50
    assert got["2026-08-11"] == 6.25
    assert got["2026-08-01"] == 999.0     # still bounded below by `since`


def test_run_ingest_skips_the_allprices_scan_when_the_sidecar_is_ready(
        db, db_path, tmp_path, monkeypatch):
    """Regression: `_needs_backfill` alone must not gate the 1.4 GB scan.

    Any watched printing MTGJSON never prices — a promo, a token — satisfies
    `_needs_backfill` tonight, tomorrow night and forever, so on that gate
    alone the nightly ingest downloads and full-scans AllPrices every single
    night to accomplish nothing. A ready sidecar already holds every card
    MTGJSON prices, so the projection covers it."""
    _, fetched = _nightly_fixture(tmp_path, monkeypatch, {
        "uuid-a": {"paper": {"tcgplayer": {"retail": {
            "normal": {"2026-08-09": 6.75}}}}}})

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    watchlist_ingest.run_ingest(db_path, str(tmp_path))

    # uuid-b is a watched Sol Ring printing with no prices anywhere, so the
    # condition that used to gate the scan is still true afterwards.
    assert db.execute("SELECT 1 FROM prices WHERE uuid='uuid-b'"
                      ).fetchone() is None
    assert watchlist_ingest._needs_backfill(db)
    assert fetched == ["AllPrintings.sqlite", "AllPricesToday.json.gz"]


def test_run_ingest_still_scans_allprices_without_a_sidecar(
        db, db_path, tmp_path, monkeypatch):
    """The gate above is a gate, not an unconditional skip: with no sidecar
    the AllPrices scan is still the only source of history."""
    src = tmp_path / "src"
    src.mkdir()
    ap = make_allprintings(src)
    allp = make_prices_gz(src, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    today = make_prices_gz(src, "AllPricesToday.json.gz", {
        "uuid-a": {"paper": {"tcgplayer": {"retail": {
            "normal": {"2026-08-09": 6.75}}}}}})
    fetched = []

    def fake_download(url, dest, _db):
        import shutil
        name = url.rsplit("/", 1)[-1]
        fetched.append(name)
        shutil.copy({"AllPrintings.sqlite": ap,
                     "AllPrices.json.gz": allp,
                     "AllPricesToday.json.gz": today}[name], dest)
        return dest
    monkeypatch.setattr(watchlist_ingest, "_download", fake_download)
    monkeypatch.setattr(watchlist_ingest, "notify_hits", lambda _db: 0)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    watchlist_ingest.run_ingest(db_path, str(tmp_path))

    assert fetched == ["AllPrintings.sqlite", "AllPrices.json.gz",
                       "AllPricesToday.json.gz"]
    dates = {r["date"] for r in db.execute("SELECT date FROM prices")}
    assert {"2026-08-01", "2026-08-08", "2026-08-09"} <= dates


def test_run_ingest_projects_before_it_downsamples(db, db_path, tmp_path,
                                                   monkeypatch):
    """apply -> project -> downsample, in that order.

    Under the normal `since` bound the order is inert: the projection window
    starts at the old watermark and downsample's cutoff sits 120 days below
    it, so they cannot overlap. It goes live when `previous` is None, which
    price_sidecar documents as reachable — a build whose points were all
    unparseable writes built_at but no daily_through, and is_ready() accepts
    it. Then the projection is unbounded, and downsampling first would replace
    the aged dailies with weekly means before they are copied, so `prices`
    would record one Sunday mean as if it were the week's only observation."""
    src = tmp_path / "src"
    src.mkdir()
    ap = make_allprintings(src)
    seed = make_prices_gz(src, "seed.json.gz", {"uuid-a": {"paper": {
        "tcgplayer": {"retail": {"normal": {f"2026-01-{d:02d}": 5.0
                                            for d in range(5, 12)}}}}}})
    today = make_prices_gz(src, "AllPricesToday.json.gz", {
        "uuid-a": {"paper": {"tcgplayer": {"retail": {
            "normal": {"2026-08-09": 6.75}}}}}})
    side = watchlist_ingest._sidecar_path(str(tmp_path))
    price_sidecar.build_from_allprices(side, seed)
    sdb = price_sidecar.connect(side)
    sdb.execute("DELETE FROM meta WHERE key='daily_through'")
    sdb.commit()
    sdb.close()
    assert price_sidecar.is_ready(side) and price_sidecar.daily_through(side) is None

    def fake_download(url, dest, _db):
        import shutil
        shutil.copy(ap if url.endswith(".sqlite") else today, dest)
        return dest
    monkeypatch.setattr(watchlist_ingest, "_download", fake_download)
    monkeypatch.setattr(watchlist_ingest, "notify_hits", lambda _db: 0)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    watchlist_ingest.run_ingest(db_path, str(tmp_path))

    dates = {r["date"] for r in db.execute("SELECT date FROM prices")}
    assert "2026-08-09" in dates             # the day just applied
    # Collapsed in the sidecar afterwards, but projected as a daily first.
    assert "2026-01-06" in dates, "downsample ran before the projection"
    sdb = price_sidecar.connect(side)
    weekly = sdb.execute("SELECT COUNT(*) FROM points WHERE agg=1").fetchone()[0]
    sdb.close()
    assert weekly >= 1, "downsample never ran at all"


def test_run_ingest_stamps_last_ingest_when_downsample_fails(
        db, db_path, tmp_path, monkeypatch):
    """Retention is housekeeping. If it fails, the day's prices have already
    landed — losing the last_ingest stamp would report the night as un-run and
    make /health say ingest_stale for work that actually succeeded."""
    _nightly_fixture(tmp_path, monkeypatch, {
        "uuid-a": {"paper": {"tcgplayer": {"retail": {
            "normal": {"2026-08-09": 6.75}}}}}})

    tried = []

    def boom(*a, **k):
        tried.append(1)
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(price_sidecar, "downsample", boom)

    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()

    watchlist_ingest.run_ingest(db_path, str(tmp_path))

    assert tried, "run_ingest never even attempted to downsample"
    assert db.execute("SELECT value FROM meta WHERE key='last_ingest'"
                      ).fetchone() is not None
    fresh = db.execute(
        "SELECT price FROM prices WHERE date='2026-08-09'").fetchone()
    assert fresh is not None and fresh["price"] == 6.75


# ── _download ──────────────────────────────────────────────────────────────

URL = "https://mtgjson.test/api/v5/AllPrices.json.gz"


class FakeStream:
    """Stand-in for httpx.stream: a context manager over a canned response.

    `chunks` may be a generator, which is what lets a test drive the body one
    write at a time."""

    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", URL),
                response=httpx.Response(self.status_code))

    def iter_bytes(self):
        return iter(self._chunks)


def part_files(directory):
    return sorted(n for n in os.listdir(directory) if ".part" in n)


def test_two_concurrent_downloads_publish_one_whole_body(
        db_path, tmp_path, monkeypatch):
    """Two callers fetching the same URL to the same dest at once.

    Reachable since the lifespan hook schedules sidecar_build_once() and
    watchlist_ingest_loop() together and both fetch AllPrices.json.gz to the
    same path. Sharing one `<dest>.part` splices the two bodies together, and
    the ETag stored afterwards then certifies the splice as current until
    MTGJSON's own ETag changes.

    The barrier forces the interleave instead of hoping for it, and the two
    chunk sizes differ so the writers' offsets diverge — equal sizes would let
    a shared file still come out looking like one whole body."""
    dest = str(tmp_path / "AllPrices.json.gz")
    chunk = {"a": b"A" * (64 * 1024), "b": b"B" * (96 * 1024)}
    rounds = 4
    whole = {which: piece * rounds for which, piece in chunk.items()}
    barrier = threading.Barrier(2)

    def body(which):
        for _ in range(rounds):
            barrier.wait(timeout=30)     # both writers step together
            yield chunk[which]

    handed = iter(("a", "b"))
    lock = threading.Lock()

    def fake_stream(method, url, **kw):
        with lock:
            which = next(handed)
        return FakeStream(headers={"etag": f'"{which}"'}, chunks=body(which))

    monkeypatch.setattr(watchlist_ingest.httpx, "stream", fake_stream)

    failures = []

    def download():
        # Own connection per thread: sqlite3 forbids sharing one across them.
        conn = watchlist_db.connect(db_path)
        try:
            watchlist_ingest._download(URL, dest, conn)
        except BaseException as exc:
            failures.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=download) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a download never finished"

    assert not failures, f"a concurrent download raised: {failures!r}"
    with open(dest, "rb") as f:
        published = f.read()
    assert published in whole.values(), (
        f"published {len(published)} bytes, but a whole body is "
        f"{sorted(len(w) for w in whole.values())} — the two downloads "
        f"interleaved into the same file")
    assert part_files(tmp_path) == []


def test_a_failed_download_leaves_no_temp_file_behind(db, tmp_path,
                                                     monkeypatch):
    """Nothing else sweeps these — price_sidecar._sweep_stale_parts only
    matches its own `price_sidecar.sqlite.part.*`, so one leaked file per
    failed attempt would accumulate at ~1.4 GB each."""
    dest = str(tmp_path / "AllPrices.json.gz")

    def body():
        yield b"x" * (64 * 1024)
        raise httpx.ReadError("connection reset by peer")

    monkeypatch.setattr(watchlist_ingest.httpx, "stream",
                        lambda *a, **k: FakeStream(headers={"etag": '"v1"'},
                                                   chunks=body()))
    with pytest.raises(httpx.ReadError):
        watchlist_ingest._download(URL, dest, db)

    assert part_files(tmp_path) == []
    assert not os.path.exists(dest), "a truncated body was published"
    # The ETag must never outlive a body that did not land, or the next run
    # gets a 304 for a file it does not have.
    assert watchlist_ingest._get_meta(db, f"etag:{URL}") is None


def test_a_download_that_errors_writes_nothing(db, tmp_path, monkeypatch):
    """raise_for_status still fires, before any file is created."""
    dest = str(tmp_path / "AllPrices.json.gz")
    monkeypatch.setattr(
        watchlist_ingest.httpx, "stream",
        lambda *a, **k: FakeStream(status_code=503, chunks=[b"nope"]))
    with pytest.raises(httpx.HTTPStatusError):
        watchlist_ingest._download(URL, dest, db)

    assert part_files(tmp_path) == []
    assert not os.path.exists(dest)
    assert watchlist_ingest._get_meta(db, f"etag:{URL}") is None


def test_download_short_circuits_on_304(db, tmp_path, monkeypatch):
    dest = str(tmp_path / "AllPrices.json.gz")
    with open(dest, "wb") as f:
        f.write(b"cached body")
    watchlist_ingest._set_meta(db, f"etag:{URL}", '"v1"')
    sent = {}

    def fake_stream(method, url, headers=None, **kw):
        sent.update(headers or {})
        return FakeStream(status_code=304, headers={"etag": '"v2"'},
                          chunks=[b"never read"])

    monkeypatch.setattr(watchlist_ingest.httpx, "stream", fake_stream)

    assert watchlist_ingest._download(URL, dest, db) == dest
    assert sent.get("If-None-Match") == '"v1"'
    with open(dest, "rb") as f:
        assert f.read() == b"cached body"
    assert watchlist_ingest._get_meta(db, f"etag:{URL}") == '"v1"'
    assert part_files(tmp_path) == []


def test_download_stores_the_etag_on_success(db, tmp_path, monkeypatch):
    dest = str(tmp_path / "AllPrices.json.gz")
    monkeypatch.setattr(
        watchlist_ingest.httpx, "stream",
        lambda *a, **k: FakeStream(headers={"etag": '"v9"'},
                                   chunks=[b"first ", b"second"]))

    assert watchlist_ingest._download(URL, dest, db) == dest
    with open(dest, "rb") as f:
        assert f.read() == b"first second"
    assert watchlist_ingest._get_meta(db, f"etag:{URL}") == '"v9"'
    assert part_files(tmp_path) == []
