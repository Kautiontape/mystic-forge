import glob
import gzip
import json
import math
import os
import sqlite3
import time
import tracemalloc
import unittest.mock as mock
from datetime import date, datetime, timedelta

import pytest

import price_sidecar


def test_src_packing_round_trips():
    seen = set()
    for provider in price_sidecar.PROVIDERS:
        for finish in price_sidecar.FINISHES:
            src = price_sidecar.pack_src(provider, finish)
            assert src not in seen, "packed codes must be unique"
            seen.add(src)
            assert price_sidecar.unpack_src(src) == (provider, finish)


def test_pack_src_returns_none_for_unknown():
    """A provider MTGJSON adds later must be skipped, not crash the ingest."""
    assert price_sidecar.pack_src("somenewstore", "normal") is None
    assert price_sidecar.pack_src("tcgplayer", "glossy") is None


def test_day_offsets_round_trip():
    for iso in ("2020-01-01", "2024-02-29", "2026-08-08", "2031-12-31"):
        assert price_sidecar.from_day(price_sidecar.to_day(iso)) == iso
    assert price_sidecar.to_day("2020-01-01") == 0


def test_cents_quantize_to_two_places():
    assert price_sidecar.to_cents(4.25) == 425
    assert price_sidecar.to_cents("3.999") == 400      # documented lossy step
    assert price_sidecar.from_cents(425) == 4.25


def test_fresh_file_is_not_ready(tmp_path):
    assert price_sidecar.is_ready(str(tmp_path / "nope.sqlite")) is False


def test_corrupt_file_is_not_ready(tmp_path):
    p = str(tmp_path / "corrupt.sqlite")
    with open(p, "wb") as f:
        f.write(b"this is not a database")
    assert price_sidecar.is_ready(p) is False


def test_initialized_but_unbuilt_file_is_not_ready(tmp_path):
    """Readiness means a completed build, not merely a valid schema."""
    p = str(tmp_path / "empty.sqlite")
    db = price_sidecar.connect(p)
    db.executescript(price_sidecar.SCHEMA)
    db.commit()
    db.close()
    assert price_sidecar.is_ready(p) is False


def make_prices_gz(tmp_path, name, data):
    p = str(tmp_path / name)
    with gzip.open(p, "wt") as f:
        json.dump({"meta": {"version": "5"}, "data": data}, f)
    return p


PRICE_OBJ = {"paper": {
    "tcgplayer": {"currency": "USD", "retail": {
        "normal": {"2026-08-01": 8.0, "2026-08-08": 7.0},
        "foil": {"2026-08-08": 30.0}}},
    "manapool": {"currency": "USD", "retail": {
        "normal": {"2026-08-08": 6.5}}},
}}


def test_build_writes_every_point(tmp_path):
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        {"uuid-a": PRICE_OBJ, "uuid-b": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    n = price_sidecar.build_from_allprices(p, gz)
    assert n == 8                      # 4 points per uuid, 2 uuids
    assert price_sidecar.is_ready(p)
    db = price_sidecar.connect(p)
    assert db.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM points").fetchone()[0] == 8
    assert db.execute("SELECT COUNT(*) FROM points WHERE agg=1").fetchone()[0] == 0
    db.close()
    assert price_sidecar.daily_through(p) == "2026-08-08"


def test_build_skips_unknown_providers_and_mtgo(tmp_path):
    obj = {"paper": {"somenewstore": {"retail": {"normal": {"2026-08-08": 1.0}}},
                     "tcgplayer": {"retail": {"normal": {"2026-08-08": 2.0}}}},
           "mtgo": {"cardhoarder": {"retail": {"normal": {"2026-08-08": 3.0}}}}}
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": obj})
    p = str(tmp_path / "side.sqlite")
    assert price_sidecar.build_from_allprices(p, gz) == 1
    db = price_sidecar.connect(p)
    src = db.execute("SELECT src FROM points").fetchone()["src"]
    db.close()
    assert price_sidecar.unpack_src(src) == ("tcgplayer", "normal")


def test_build_is_atomic_on_failure(tmp_path):
    """A crashed build must not leave a half-built file that looks ready."""
    p = str(tmp_path / "side.sqlite")
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    price_sidecar.build_from_allprices(p, gz)
    before = os.path.getsize(p)

    def boom(_gz, _counts=None):
        yield "uuid-a", 0, 100, 500
        raise RuntimeError("disk fell over")

    with mock.patch.object(price_sidecar, "_iter_points", boom):
        with pytest.raises(RuntimeError):
            price_sidecar.build_from_allprices(p, gz)
    # Glob, not p + ".part": the part file is named per-pid, so a literal
    # ".part" would assert on a path the build never creates and pass however
    # much debris were actually left behind.
    assert glob.glob(p + ".part*") == []
    assert os.path.getsize(p) == before      # previous build untouched
    assert price_sidecar.is_ready(p)


def test_build_refuses_without_disk_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr(price_sidecar, "_free_bytes", lambda _d: 1000)
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    with pytest.raises(OSError, match="free space"):
        price_sidecar.build_from_allprices(p, gz)
    assert price_sidecar.is_ready(p) is False


def test_build_streams_without_loading_whole_file(tmp_path):
    data = {f"uuid-{i}": PRICE_OBJ for i in range(5000)}
    gz = make_prices_gz(tmp_path, "big.json.gz", data)
    p = str(tmp_path / "side.sqlite")
    tracemalloc.start()
    price_sidecar.build_from_allprices(p, gz)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # The ceiling has to sit between the two implementations or it proves
    # nothing: on this fixture streaming peaks at 4.3-4.5 MiB, while a
    # json.load of the whole document peaks at 11.9 MiB. 20 MiB — the obvious
    # round number — passes for both and would not catch the regression.
    assert peak < 8 * 1024 * 1024


def test_build_discards_a_stale_wal_from_an_unclean_shutdown(tmp_path):
    """A -wal left behind by a kill must not be replayed over the new build.

    os.replace swaps only the main file; SQLite would replay a surviving
    sibling -wal on the next open and silently resurrect the old database."""
    p = str(tmp_path / "side.sqlite")
    old = make_prices_gz(tmp_path, "old.json.gz", {"uuid-old": PRICE_OBJ})
    price_sidecar.build_from_allprices(p, old)

    # Capture a real WAL to stand in for the debris. Arbitrary bytes will not
    # do: SQLite validates the WAL header and silently discards anything that
    # fails it, so only a genuine WAL reproduces the replay.
    db = price_sidecar.connect(p)
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("INSERT INTO cards (card_id, uuid) VALUES (9999,'uuid-ghost')")
    db.commit()
    with open(p + "-wal", "rb") as f:
        stale_wal = f.read()
    db.close()

    new = make_prices_gz(tmp_path, "new.json.gz", {"uuid-new": PRICE_OBJ})
    # In place before the build starts, exactly as a kill would have left it.
    with open(p + "-wal", "wb") as f:
        f.write(stale_wal)
    price_sidecar.build_from_allprices(p, new)
    db = price_sidecar.connect(p)
    uuids = {r["uuid"] for r in db.execute("SELECT uuid FROM cards")}
    db.close()
    assert uuids == {"uuid-new"}


TODAY_OBJ = {"paper": {"tcgplayer": {"retail": {
    "normal": {"2026-08-09": 6.75}}}}}


def test_apply_daily_appends_and_advances_the_watermark(tmp_path):
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    assert price_sidecar.daily_through(p) == "2026-08-08"

    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-a": TODAY_OBJ})
    assert price_sidecar.apply_daily(p, today) == 1
    assert price_sidecar.daily_through(p) == "2026-08-09"
    db = price_sidecar.connect(p)
    got = db.execute(
        "SELECT cents FROM points WHERE day=?",
        (price_sidecar.to_day("2026-08-09"),)).fetchone()["cents"]
    db.close()
    assert got == 675


def test_apply_daily_learns_new_cards(tmp_path):
    """A card printed after the build must get a card_id, not be dropped."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-a": TODAY_OBJ, "uuid-new": TODAY_OBJ})
    assert price_sidecar.apply_daily(p, today) == 2
    db = price_sidecar.connect(p)
    uuids = {r["uuid"] for r in db.execute("SELECT uuid FROM cards")}
    db.close()
    assert uuids == {"uuid-a", "uuid-new"}


def test_apply_daily_is_idempotent(tmp_path):
    """Applying the same day twice must leave every stored value unchanged,
    not merely the row count -- a bug that corrupts cents on replay while
    keeping the same primary key must not slip past this test."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-a": TODAY_OBJ})
    price_sidecar.apply_daily(p, today)
    db = price_sidecar.connect(p)
    before = db.execute(
        "SELECT card_id, src, day, cents, agg FROM points ORDER BY 1,2,3").fetchall()
    db.close()
    price_sidecar.apply_daily(p, today)
    db = price_sidecar.connect(p)
    after = db.execute(
        "SELECT card_id, src, day, cents, agg FROM points ORDER BY 1,2,3").fetchall()
    db.close()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_apply_daily_on_unbuilt_sidecar_is_a_noop(tmp_path):
    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-a": TODAY_OBJ})
    assert price_sidecar.apply_daily(str(tmp_path / "absent.sqlite"), today) == 0


def test_apply_daily_does_not_regress_the_watermark(tmp_path):
    """A stale or reprocessed daily file must never move daily_through
    backward."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    newer = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-a": TODAY_OBJ})
    price_sidecar.apply_daily(p, newer)
    assert price_sidecar.daily_through(p) == "2026-08-09"

    stale_obj = {"paper": {"tcgplayer": {"retail": {
        "normal": {"2026-08-05": 5.0}}}}}
    older = make_prices_gz(tmp_path, "AllPricesStale.json.gz",
                           {"uuid-a": stale_obj})
    price_sidecar.apply_daily(p, older)
    assert price_sidecar.daily_through(p) == "2026-08-09"


def test_apply_daily_assigns_ids_past_a_gap_in_card_id(tmp_path):
    """max(card_id)+1 must stay collision-free even with a gap in the
    sequence -- the exact case that motivates max+1 over len(cards)+1."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        {"uuid-a": PRICE_OBJ, "uuid-b": PRICE_OBJ,
                         "uuid-c": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    db = price_sidecar.connect(p)
    db.execute("DELETE FROM points WHERE card_id="
              "(SELECT card_id FROM cards WHERE uuid='uuid-b')")
    db.execute("DELETE FROM cards WHERE uuid='uuid-b'")
    db.commit()
    remaining = {r["uuid"]: r["card_id"]
                for r in db.execute("SELECT uuid, card_id FROM cards")}
    db.close()
    assert remaining == {"uuid-a": 1, "uuid-c": 3}   # len(cards)==2, but max==3

    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-new": TODAY_OBJ})
    price_sidecar.apply_daily(p, today)

    db = price_sidecar.connect(p)
    rows = {r["uuid"]: r["card_id"]
           for r in db.execute("SELECT uuid, card_id FROM cards")}
    db.close()
    assert rows["uuid-new"] == 4          # max(1,3)+1, not len(cards)+1 == 3
    assert len(set(rows.values())) == len(rows)     # no collisions


def test_apply_daily_crash_then_retry_converges(tmp_path, monkeypatch):
    """Per-batch commits mean a crash mid-apply can leave some but not all
    of a day's rows committed. That must leave the watermark unadvanced,
    and re-running the same file afterward must converge to the complete,
    correct state -- the invariant that replaces the all-or-nothing
    guarantee build_from_allprices has."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    previous = price_sidecar.daily_through(p)

    monkeypatch.setattr(price_sidecar, "BATCH", 2)
    day9 = price_sidecar.to_day("2026-08-09")

    def crashes_after_two_batches(_gz, _counts=None):
        for i in range(4):
            yield f"uuid-{i}", 0, day9, 100 + i
        yield "uuid-4", 0, day9, 104          # appended, never committed
        raise RuntimeError("simulated crash mid-apply")

    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-a": TODAY_OBJ})
    with mock.patch.object(price_sidecar, "_iter_points", crashes_after_two_batches):
        with pytest.raises(RuntimeError):
            price_sidecar.apply_daily(p, today)

    assert price_sidecar.daily_through(p) == previous
    db = price_sidecar.connect(p)
    partial = db.execute("SELECT COUNT(*) FROM points WHERE day=?",
                         (day9,)).fetchone()[0]
    db.close()
    assert partial == 4          # the first two committed batches, not the fifth

    def full_day(_gz, _counts=None):
        for i in range(5):
            yield f"uuid-{i}", 0, day9, 100 + i

    with mock.patch.object(price_sidecar, "_iter_points", full_day):
        n = price_sidecar.apply_daily(p, today)

    assert n == 5
    assert price_sidecar.daily_through(p) == "2026-08-09"
    db = price_sidecar.connect(p)
    cents = {r["cents"] for r in db.execute(
        "SELECT cents FROM points WHERE day=?", (day9,))}
    db.close()
    assert cents == {100, 101, 102, 103, 104}


def test_series_for_uuids_returns_upsert_shaped_tuples(tmp_path):
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        {"uuid-a": PRICE_OBJ, "uuid-b": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    got = sorted(price_sidecar.series_for_uuids(p, ["uuid-a"]))
    assert got == sorted([
        ("uuid-a", "2026-08-01", "tcgplayer", "normal", 8.0),
        ("uuid-a", "2026-08-08", "tcgplayer", "normal", 7.0),
        ("uuid-a", "2026-08-08", "tcgplayer", "foil", 30.0),
        ("uuid-a", "2026-08-08", "manapool", "normal", 6.5),
    ])


def test_series_filters_by_provider_and_since(tmp_path):
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    only = list(price_sidecar.series_for_uuids(p, ["uuid-a"],
                                               providers=["manapool"]))
    assert only == [("uuid-a", "2026-08-08", "manapool", "normal", 6.5)]
    recent = list(price_sidecar.series_for_uuids(p, ["uuid-a"],
                                                 since="2026-08-01"))
    assert all(d > "2026-08-01" for _u, d, _p, _f, _v in recent)
    assert len(recent) == 3


def test_series_on_unbuilt_sidecar_yields_nothing(tmp_path):
    assert list(price_sidecar.series_for_uuids(
        str(tmp_path / "absent.sqlite"), ["uuid-a"])) == []


def test_series_skips_reserved_and_future_src_codes(tmp_path):
    """A rollback reading rows a newer build wrote for an unnamed provider or
    finish must skip them, not raise -- pack_src already treats unknowns as
    None on write; unpack_src must mirror that on read."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    db = price_sidecar.connect(p)
    card_id = db.execute(
        "SELECT card_id FROM cards WHERE uuid='uuid-a'").fetchone()[0]
    day = price_sidecar.to_day("2026-08-09")
    db.execute("INSERT INTO points (card_id, src, day, cents, agg)"
              " VALUES (?,3,?,100,0)", (card_id, day))       # reserved finish
    db.execute("INSERT INTO points (card_id, src, day, cents, agg)"
              " VALUES (?,16,?,200,0)", (card_id, day))      # future provider
    db.commit()
    db.close()
    got = list(price_sidecar.series_for_uuids(p, ["uuid-a"]))
    assert len(got) == 4                    # only the original 4 valid points
    assert all(row[0] == "uuid-a" for row in got)


def test_chunks_covers_every_item_including_a_partial_tail():
    assert list(price_sidecar._chunks(list(range(7)), 3)) == [[0, 1, 2], [3, 4, 5], [6]]
    assert list(price_sidecar._chunks(list(range(6)), 3)) == [[0, 1, 2], [3, 4, 5]]
    assert list(price_sidecar._chunks([], 3)) == []


def test_series_stitches_results_across_several_queries(tmp_path, monkeypatch):
    """A watchlist larger than one chunk is split across queries and stitched
    back with nothing dropped. Asserts the query count, not just the result:
    SQLite's parameter cap is 32766, so a row-count assertion alone passes
    even with chunking removed entirely."""
    many = {f"uuid-{i}": TODAY_OBJ for i in range(50)}
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", many)
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)

    monkeypatch.setattr(price_sidecar, "READ_CHUNK", 7)
    seen = []
    real_connect = price_sidecar.connect

    def tracing_connect(path):
        db = real_connect(path)
        db.set_trace_callback(
            lambda s: seen.append(s) if "FROM points" in s else None)
        return db
    monkeypatch.setattr(price_sidecar, "connect", tracing_connect)

    got = list(price_sidecar.series_for_uuids(p, list(many)))
    assert len({u for u, *_ in got}) == 50
    assert len(seen) == math.ceil(50 / 7) == 8


def _daily_series(uuid, start, days, price=1.0):
    """A synthetic AllPrices block: `days` consecutive daily tcgplayer prices."""
    d0 = date.fromisoformat(start)
    return {uuid: {"paper": {"tcgplayer": {"retail": {"normal": {
        (d0 + timedelta(days=i)).isoformat(): price + i
        for i in range(days)}}}}}}


def test_anchor_lands_on_the_iso_week_sunday():
    for iso in ("2020-01-01", "2026-03-02", "2026-03-08", "2026-08-09"):
        day = price_sidecar.to_day(iso)
        got = date.fromisoformat(price_sidecar.from_day(
            price_sidecar.week_anchor(day)))
        assert got.isoweekday() == 7
        assert got >= date.fromisoformat(iso)
        assert (got - date.fromisoformat(iso)).days < 7


def test_downsample_collapses_aged_weeks_to_means(tmp_path):
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14, price=10.0))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    # 2026-01-05 is a Monday, so these are two whole ISO weeks.
    deleted = price_sidecar.downsample(p, keep_daily_days=30,
                                       today="2026-06-01")
    assert deleted == 12                       # 14 rows in, 2 weekly rows out
    db = price_sidecar.connect(p)
    rows = db.execute("SELECT day, cents, agg FROM points ORDER BY day").fetchall()
    db.close()
    assert len(rows) == 2
    assert all(r["agg"] == 1 for r in rows)
    assert price_sidecar.from_day(rows[0]["day"]) == "2026-01-11"   # Sunday
    assert price_sidecar.from_day(rows[1]["day"]) == "2026-01-18"
    assert rows[0]["cents"] == 1300            # mean of 10.0..16.0 = 13.00
    assert rows[1]["cents"] == 2000            # mean of 17.0..23.0 = 20.00


def test_downsample_leaves_the_daily_window_alone(tmp_path):
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    assert price_sidecar.downsample(p, keep_daily_days=120,
                                    today="2026-01-20") == 0
    db = price_sidecar.connect(p)
    assert db.execute("SELECT COUNT(*) FROM points").fetchone()[0] == 14
    db.close()


def test_downsample_never_collapses_a_partial_week(tmp_path):
    """A week straddling the cutoff must wait until it is wholly behind it."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    # cutoff falls mid-week-2: week 1 collapses, week 2 is left whole.
    price_sidecar.downsample(p, keep_daily_days=1, today="2026-01-15")
    db = price_sidecar.connect(p)
    daily = db.execute("SELECT COUNT(*) FROM points WHERE agg=0").fetchone()[0]
    weekly = db.execute("SELECT COUNT(*) FROM points WHERE agg=1").fetchone()[0]
    db.close()
    assert weekly == 1 and daily == 7


def test_downsample_is_idempotent_and_never_averages_an_average(tmp_path):
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14, price=10.0))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    price_sidecar.downsample(p, keep_daily_days=30, today="2026-06-01")
    db = price_sidecar.connect(p)
    first = db.execute("SELECT day, cents, agg FROM points ORDER BY day").fetchall()
    db.close()
    assert price_sidecar.downsample(p, keep_daily_days=30,
                                    today="2026-06-01") == 0
    db = price_sidecar.connect(p)
    second = db.execute("SELECT day, cents, agg FROM points ORDER BY day").fetchall()
    db.close()
    assert [tuple(r) for r in first] == [tuple(r) for r in second]


def test_anchor_sql_and_python_agree_across_the_epoch_boundary():
    """week_anchor and _ANCHOR_SQL are two transcriptions of one formula, and
    only the SQL runs in production. Python's % floors, SQLite's truncates
    toward zero, so for a pre-EPOCH day offset -- which nothing in the ingest
    path rejects -- the un-normalized SQL lands a whole week late while still
    falling on a Sunday, so nothing about the result looks wrong. Compare the
    two real artifacts by executing the SQL, not a reimplementation of it."""
    lo = price_sidecar.to_day("2018-01-01")
    hi = price_sidecar.to_day("2022-12-31")
    assert lo < 0 < hi                     # the range must straddle EPOCH
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (day INTEGER)")
    db.executemany("INSERT INTO t VALUES (?)", [(d,) for d in range(lo, hi + 1)])
    from_sql = dict(db.execute(f"SELECT day, {price_sidecar._ANCHOR_SQL} FROM t"))
    db.close()
    for d in range(lo, hi + 1):
        iso = price_sidecar.from_day(d)
        want = date.fromisoformat(iso)
        want += timedelta(days=7 - want.isoweekday())
        assert price_sidecar.week_anchor(d) == (want - price_sidecar.EPOCH).days, \
            f"python anchor is not the ISO-week Sunday for {iso}"
        assert from_sql[d] == price_sidecar.week_anchor(d), \
            f"SQL anchor differs from the python one for {iso}"


def test_downsample_never_recomputes_a_weekly_mean_from_a_mixture(tmp_path):
    """The invariant the agg column exists to protect. Reaching it needs a
    daily row planted in a week that has already collapsed: a Sunday is its
    own anchor, so an established weekly row is otherwise alone in its group
    and AVG of one row is itself, which hides the bug from every other test."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14, price=10.0))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    price_sidecar.downsample(p, keep_daily_days=30, today="2026-06-01")

    db = price_sidecar.connect(p)
    card_id = db.execute(
        "SELECT card_id FROM cards WHERE uuid='uuid-a'").fetchone()[0]
    db.execute("INSERT INTO points (card_id, src, day, cents, agg)"
               " VALUES (?,0,?,9900,0)",
               (card_id, price_sidecar.to_day("2026-01-07")))
    db.commit()
    db.close()

    price_sidecar.downsample(p, keep_daily_days=30, today="2026-06-01")
    db = price_sidecar.connect(p)
    rows = [(price_sidecar.from_day(r["day"]), r["cents"], r["agg"]) for r in
            db.execute("SELECT day, cents, agg FROM points ORDER BY day")]
    db.close()
    # 1300 is the untouched seven-day mean. 9900 would be the raw day having
    # overwritten it; 5600 would be the mean of the mean and the raw day.
    assert rows == [("2026-01-11", 1300, 1), ("2026-01-18", 2000, 1)]


def test_downsample_discards_a_late_daily_row_rather_than_reweighting(tmp_path):
    """Task 9 runs apply_daily then downsample. A daily file carrying a date
    older than the cutoff lands as agg=0 inside an already-collapsed week.
    The stored mean carries no day count, so it cannot absorb the newcomer:
    the week is left alone and the late row is dropped with its cohort."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14, price=10.0))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    price_sidecar.downsample(p, keep_daily_days=30, today="2026-06-01")

    late = make_prices_gz(tmp_path, "AllPricesLate.json.gz",
                          {"uuid-a": {"paper": {"tcgplayer": {"retail": {
                              "normal": {"2026-01-07": 99.0}}}}}})
    assert price_sidecar.apply_daily(p, late) == 1
    assert price_sidecar.downsample(p, keep_daily_days=30,
                                    today="2026-06-01") == 1
    db = price_sidecar.connect(p)
    rows = [(price_sidecar.from_day(r["day"]), r["cents"], r["agg"]) for r in
            db.execute("SELECT day, cents, agg FROM points ORDER BY day")]
    db.close()
    assert rows == [("2026-01-11", 1300, 1), ("2026-01-18", 2000, 1)]


def test_downsample_commits_per_chunk_and_resumes_after_a_crash(tmp_path,
                                                                monkeypatch):
    """Chunking trades away all-or-nothing to bound the WAL. That is only
    safe because cards collapse independently of each other: a crash must
    leave whole cards done and the rest untouched, and the next run must
    finish them with identical values."""
    data = {}
    for i in range(6):
        data.update(_daily_series(f"uuid-{i}", "2026-01-05", 14, price=10.0))
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", data)
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    monkeypatch.setattr(price_sidecar, "DOWNSAMPLE_CARDS", 2)

    real_connect = price_sidecar.connect

    class CrashAfterFirstChunk:
        """Commits, then dies -- a kill after a chunk is already durable."""

        def __init__(self, db):
            self._db, self._commits = db, 0

        def __getattr__(self, name):
            return getattr(self._db, name)

        def commit(self):
            self._db.commit()
            self._commits += 1
            if self._commits == 1:
                raise RuntimeError("simulated crash between chunks")

    with mock.patch.object(price_sidecar, "connect",
                           lambda path: CrashAfterFirstChunk(real_connect(path))):
        with pytest.raises(RuntimeError):
            price_sidecar.downsample(p, keep_daily_days=30, today="2026-06-01")

    db = price_sidecar.connect(p)
    done = {r["card_id"] for r in
            db.execute("SELECT card_id FROM points WHERE agg=1")}
    pending = {r["card_id"] for r in
               db.execute("SELECT card_id FROM points WHERE agg=0")}
    db.close()
    # A single transaction would have rolled the whole thing back to nothing.
    assert done and pending and not (done & pending)

    assert price_sidecar.downsample(p, keep_daily_days=30,
                                    today="2026-06-01") == 12 * len(pending)
    db = price_sidecar.connect(p)
    per_card = {}
    for r in db.execute("SELECT card_id, day, cents, agg FROM points"
                        " ORDER BY card_id, day"):
        per_card.setdefault(r["card_id"], []).append(
            (price_sidecar.from_day(r["day"]), r["cents"], r["agg"]))
    db.close()
    assert len(per_card) == 6
    assert all(v == [("2026-01-11", 1300, 1), ("2026-01-18", 2000, 1)]
               for v in per_card.values())


def test_downsample_will_not_collapse_ahead_of_the_data(tmp_path, monkeypatch):
    """The wall clock is an untrusted input on the one irreversible operation
    in the module, and production takes this branch: task 9 calls downsample()
    with no arguments. A container without NTP, an NTP step after a drift, a
    restored snapshot or a dead RTC battery all present as a jump forward, and
    one run with a fast clock collapses the whole daily window -- past ~90 days
    MTGJSON cannot replay it, so nothing recovers it. daily_through is what the
    data itself says; it bounds the cutoff."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14, price=10.0))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    assert price_sidecar.daily_through(p) == "2026-01-18"

    class ClockAYearFast:
        @staticmethod
        def now(tz=None):
            return datetime(2027, 1, 18, tzinfo=tz)

    monkeypatch.setattr(price_sidecar, "datetime", ClockAYearFast)
    # Unguarded this collapses all 14 rows: 2027-01-18 minus 120 days puts the
    # cutoff at 2026-09-20, well past every anchor in the fixture.
    assert price_sidecar.downsample(p) == 0
    db = price_sidecar.connect(p)
    assert db.execute("SELECT COUNT(*) FROM points WHERE agg=0").fetchone()[0] == 14
    assert db.execute("SELECT COUNT(*) FROM points WHERE agg=1").fetchone()[0] == 0
    db.close()


def test_downsample_leaves_no_half_applied_state_inside_a_chunk(tmp_path):
    """The INSERT and the DELETE are two statements. Dying between them is the
    window where a bug would double-count a week or strand its daily rows, so
    it needs its own cover: the crash-between-chunks test commits first and
    never enters it."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14, price=10.0))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)

    real_connect = price_sidecar.connect

    class CrashBeforeDelete:
        def __init__(self, db):
            self._db = db

        def __getattr__(self, name):
            return getattr(self._db, name)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("DELETE"):
                raise RuntimeError("simulated crash between INSERT and DELETE")
            return self._db.execute(sql, *args)

    with mock.patch.object(price_sidecar, "connect",
                           lambda path: CrashBeforeDelete(real_connect(path))):
        with pytest.raises(RuntimeError):
            price_sidecar.downsample(p, keep_daily_days=30, today="2026-06-01")

    db = price_sidecar.connect(p)
    daily = db.execute("SELECT COUNT(*) FROM points WHERE agg=0").fetchone()[0]
    weekly = db.execute("SELECT COUNT(*) FROM points WHERE agg=1").fetchone()[0]
    db.close()
    assert (daily, weekly) == (14, 0)      # the uncommitted INSERT rolled back

    assert price_sidecar.downsample(p, keep_daily_days=30,
                                    today="2026-06-01") == 12
    db = price_sidecar.connect(p)
    rows = [(price_sidecar.from_day(r["day"]), r["cents"], r["agg"]) for r in
            db.execute("SELECT day, cents, agg FROM points ORDER BY day")]
    db.close()
    assert rows == [("2026-01-11", 1300, 1), ("2026-01-18", 2000, 1)]


def test_stats_reports_shape_and_span(tmp_path):
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    s = price_sidecar.stats(p)
    assert s["ready"] is True
    assert s["cards"] == 1
    assert s["points"] == 4
    assert s["daily_points"] == 4
    assert s["weekly_points"] == 0
    assert s["earliest"] == "2026-08-01"
    assert s["latest"] == "2026-08-08"
    assert s["bytes"] > 0
    assert s["built_at"]
    # The stored watermark and the computed max(day) agree under normal
    # operation; publishing both turns a future divergence into a free
    # partial-write signal.
    assert s["daily_through"] == s["latest"]


def test_stats_distinguishes_daily_and_weekly_points(tmp_path):
    """A freshly built fixture never populates agg=1, so a stats()
    implementation that counts every row instead of filtering on agg=0
    would still pass test_stats_reports_shape_and_span above -- daily_points
    and weekly_points there are 4 and 0 either way. This fixture actually
    contains both tiers, produced by a real downsample rather than planted
    by hand, so the two counts can only agree with a real filter."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    # cutoff falls mid-week-2: week 1 (7 rows) collapses to one weekly mean,
    # week 2 (7 rows) is left daily.
    price_sidecar.downsample(p, keep_daily_days=1, today="2026-01-15")
    s = price_sidecar.stats(p)
    assert s["daily_points"] == 7
    assert s["weekly_points"] == 1
    assert s["daily_points"] != s["weekly_points"]
    assert s["daily_points"] + s["weekly_points"] == s["points"]


def test_stats_bytes_includes_a_live_wal(tmp_path):
    """bytes must count the whole on-disk unit, not just the main file: a
    concurrent reader -- a slow watchlist page render, or another stats()
    call -- can hold a checkpoint back long enough for the -wal to become a
    meaningful fraction of what the sidecar actually costs on disk."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)

    # Establish WAL mode, exactly as the server's first connect() would.
    warm = price_sidecar.connect(p)
    warm.close()
    main_only = os.path.getsize(p)

    # A long-lived reader holds a snapshot open and blocks checkpointing.
    reader = sqlite3.connect(p)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM points").fetchone()

    writer = price_sidecar.connect(p)
    writer.execute("INSERT INTO points (card_id, src, day, cents, agg)"
                  " VALUES (1, 3, 999, 100, 0)")
    writer.commit()
    writer.close()

    assert os.path.exists(p + "-wal")
    assert os.path.getsize(p + "-wal") > 0

    s = price_sidecar.stats(p)
    expected = sum(os.path.getsize(p + suffix)
                   for suffix in ("", "-wal", "-shm")
                   if os.path.exists(p + suffix))
    assert s["bytes"] == expected
    assert s["bytes"] > main_only

    reader.close()


def test_stats_on_missing_file_is_not_ready(tmp_path):
    # `reason` is reported alongside `ready`, never instead of it: /health is
    # the only operator surface, and not-ready triggers a rebuild that
    # permanently discards the weekly tier.
    assert price_sidecar.stats(str(tmp_path / "absent.sqlite")) == {
        "ready": False, "reason": "absent"}


def test_build_logs_skip_counts_for_unknown_provider(tmp_path, caplog):
    """The failure this guards: MTGJSON renames a provider or adds a new
    finish, pack_src returns None for it, and the build completes with a
    plausible-looking total while an entire provider silently vanishes.
    A grep of the deploy log must be able to catch that."""
    obj = {"paper": {
        "somenewstore": {"retail": {"normal": {"2026-08-08": 1.0,
                                                "2026-08-07": 1.5}}},
        "tcgplayer": {"retail": {"normal": {"2026-08-08": 2.0}}}}}
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": obj})
    p = str(tmp_path / "side.sqlite")
    with caplog.at_level("INFO", logger="mystic_forge.sidecar"):
        price_sidecar.build_from_allprices(p, gz)
    built = [r for r in caplog.records if "sidecar built" in r.getMessage()]
    assert len(built) == 1
    msg = built[0].getMessage()
    assert "somenewstore" in msg
    assert "skipped" in msg
    # exactly the two points under the unknown provider, not e.g. 1 group
    assert "{('somenewstore', 'normal'): 2}" in msg
    assert "{('tcgplayer', 'normal'): 1}" in msg


def test_apply_daily_warns_on_a_day_older_than_the_daily_window(tmp_path, caplog):
    """downsample can only discard such a point, and cannot say which one it
    was without a second scan. apply_daily has every day in hand already."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)

    spanning = make_prices_gz(tmp_path, "AllPricesWide.json.gz",
                              {"uuid-a": {"paper": {"tcgplayer": {"retail": {
                                  "normal": {"2025-01-01": 1.0,
                                             "2026-08-09": 2.0}}}}}})
    with caplog.at_level("WARNING", logger="mystic_forge.sidecar"):
        price_sidecar.apply_daily(p, spanning)
    assert "2025-01-01" in caplog.text and "older than" in caplog.text

    caplog.clear()
    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-a": TODAY_OBJ})
    with caplog.at_level("WARNING", logger="mystic_forge.sidecar"):
        price_sidecar.apply_daily(p, today)
    assert caplog.text == ""            # a normal one-day file stays quiet


def _points(p):
    db = price_sidecar.connect(p)
    rows = [(price_sidecar.from_day(r["day"]), r["cents"], r["agg"]) for r in
            db.execute("SELECT day, cents, agg FROM points ORDER BY day")]
    db.close()
    return rows


def _late_file(tmp_path, name, iso, price):
    return make_prices_gz(tmp_path, name, {"uuid-a": {"paper": {"tcgplayer": {
        "retail": {"normal": {iso: price}}}}}})


def _collapsed_two_weeks(tmp_path):
    """Two whole ISO weeks already collapsed to means at 1300 and 2000."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14, price=10.0))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    price_sidecar.downsample(p, keep_daily_days=30, today="2026-06-01")
    assert _points(p) == [("2026-01-11", 1300, 1), ("2026-01-18", 2000, 1)]
    return p


def test_apply_daily_will_not_overwrite_a_weekly_mean_on_its_own_anchor(tmp_path):
    """downsample's guard cannot see this one, and it fires 1 day in 7.

    A weekly mean is stored at its week's Sunday, so a late daily row for
    that Sunday shares the primary key (card_id, src, day) with it. An
    INSERT OR REPLACE clobbers the agg=1 row *before* downsample runs, so
    downsample's HAVING NOT EXISTS finds nothing left to protect and
    recomputes the "mean" from the single surviving row -- then relabels it
    agg=1, which is what makes the loss silent: stats() still counts it as a
    weekly point. The neighbouring test uses 2026-01-07, a Wednesday, and so
    covers only the 6/7 safe case. Past ~90 days MTGJSON cannot replay the
    week, so this is unrecoverable.
    """
    p = _collapsed_two_weeks(tmp_path)
    assert date.fromisoformat("2026-01-11").isoweekday() == 7   # the anchor

    late = _late_file(tmp_path, "AllPricesLate.json.gz", "2026-01-11", 99.0)
    price_sidecar.apply_daily(p, late)
    assert _points(p) == [("2026-01-11", 1300, 1), ("2026-01-18", 2000, 1)], \
        "the established weekly mean must survive a late row on its anchor"

    price_sidecar.downsample(p, keep_daily_days=30, today="2026-06-01")
    assert _points(p) == [("2026-01-11", 1300, 1), ("2026-01-18", 2000, 1)]


def test_apply_daily_still_revises_a_price_for_a_day_already_stored(tmp_path):
    """Protecting agg=1 rows must not also freeze agg=0 ones: MTGJSON does
    restate a day's price, and re-applying a corrected file has to land."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)

    revised = _late_file(tmp_path, "AllPricesRevised.json.gz", "2026-08-08", 7.5)
    price_sidecar.apply_daily(p, revised)
    db = price_sidecar.connect(p)
    got = db.execute("SELECT cents, agg FROM points WHERE src=0 AND day=?",
                     (price_sidecar.to_day("2026-08-08"),)).fetchone()
    db.close()
    assert (got["cents"], got["agg"]) == (750, 0)


def test_apply_daily_logs_skip_counts_for_a_new_provider(tmp_path, caplog):
    """build runs once, possibly years ago; this path runs every night. A
    provider MTGJSON adds later is dropped silently here forever unless the
    nightly log names it."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)

    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz", {"uuid-a": {
        "paper": {
            "brandnewshop": {"retail": {"normal": {"2026-08-09": 1.0,
                                                   "2026-08-10": 1.5}}},
            "tcgplayer": {"retail": {"normal": {"2026-08-09": 2.0}}}}}})
    with caplog.at_level("WARNING", logger="mystic_forge.sidecar"):
        price_sidecar.apply_daily(p, today)
    assert "brandnewshop" in caplog.text
    assert "{('brandnewshop', 'normal'): 2}" in caplog.text   # points, not groups


def test_is_ready_survives_a_uri_metacharacter_in_the_path(tmp_path):
    """`?` and `#` are URI syntax. Interpolating a raw path into
    file:...?mode=ro truncates it at the first `?`, so the sidecar is never
    seen as ready -- and because mode=ro is no longer parsed either, the
    probe *creates* a stray database at the truncated path. Every other
    function opens the plain path and works fine, so nothing else reveals it.
    """
    directory = tmp_path / "weird?x#y"
    directory.mkdir()
    p = str(directory / "side.sqlite")
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    price_sidecar.build_from_allprices(p, gz)

    assert price_sidecar.is_ready(p) is True
    assert not os.path.exists(str(tmp_path / "weird"))     # no stray database
    assert len(list(price_sidecar.series_for_uuids(p, ["uuid-a"]))) == 4


def test_is_ready_leaves_the_file_untouched(tmp_path):
    """The readiness probe is a probe. It must not create, modify, or
    upgrade anything -- it runs on paths that may not be sidecars at all."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    before = (os.path.getsize(p), sorted(os.listdir(str(tmp_path))))
    assert price_sidecar.is_ready(p) is True
    assert (os.path.getsize(p), sorted(os.listdir(str(tmp_path)))) == before


def test_stats_names_why_the_sidecar_is_not_ready(tmp_path, monkeypatch,
                                                  caplog):
    """`{"ready": False}` collapses four unrelated states into one. The
    system's answer to not-ready is a full rebuild that permanently discards
    the weekly tier, so an operator must be able to tell "first boot" from
    "your history was just thrown away."""
    absent = str(tmp_path / "absent.sqlite")
    assert price_sidecar.stats(absent) == {"ready": False, "reason": "absent"}

    corrupt = str(tmp_path / "corrupt.sqlite")
    with open(corrupt, "wb") as f:
        f.write(b"this is not a database")
    with caplog.at_level("WARNING", logger="mystic_forge.sidecar"):
        assert price_sidecar.stats(corrupt) == {"ready": False,
                                                "reason": "corrupt"}
    assert corrupt in caplog.text

    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    assert price_sidecar.stats(p)["ready"] is True

    caplog.clear()
    monkeypatch.setattr(price_sidecar, "SCHEMA_VERSION",
                        price_sidecar.SCHEMA_VERSION + 1)
    with caplog.at_level("WARNING", logger="mystic_forge.sidecar"):
        assert price_sidecar.stats(p) == {"ready": False, "reason": "schema"}
    # A version bump silently destroys the weekly tier; it must say so.
    assert p in caplog.text
    monkeypatch.undo()

    caplog.clear()
    monkeypatch.setenv("MYSTIC_FORGE_NO_SIDECAR", "1")
    with caplog.at_level("WARNING", logger="mystic_forge.sidecar"):
        assert price_sidecar.stats(p) == {"ready": False, "reason": "disabled"}
    assert caplog.text == ""            # the escape hatch is normal operation


def test_absent_and_disabled_sidecars_do_not_warn(tmp_path, monkeypatch,
                                                  caplog):
    """First boot and the operator escape hatch are both expected states.
    Warning on them would train operators to ignore the log -- which is the
    log that has to carry the schema-mismatch and corruption alarms."""
    with caplog.at_level("WARNING", logger="mystic_forge.sidecar"):
        assert price_sidecar.is_ready(str(tmp_path / "absent.sqlite")) is False
    assert caplog.text == ""

    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    caplog.clear()
    monkeypatch.setenv("MYSTIC_FORGE_NO_SIDECAR", "1")
    with caplog.at_level("WARNING", logger="mystic_forge.sidecar"):
        assert price_sidecar.is_ready(p) is False
    assert caplog.text == ""


def test_next_card_id_skips_past_a_gap():
    """build and apply_daily must allocate identically. len()+1 collides the
    moment the sequence has a hole; max()+1 does not."""
    assert price_sidecar._next_card_id({}) == 1
    assert price_sidecar._next_card_id({"a": 1, "c": 3}) == 4   # not len()+1 == 3


def test_build_uses_a_private_part_file_and_sweeps_only_stale_ones(tmp_path):
    """A fixed `.part` name is shared state between builds. Reproduced: B's
    os.remove unlinks A's in-progress file, B builds a fresh `.part`, and A's
    os.replace then publishes B's half-finished database as ready. Per-pid
    names remove the collision; sweeping only *old* siblings stops a crashed
    build leaking files forever without touching a live one."""
    p = str(tmp_path / "side.sqlite")
    stale = p + ".part.999999"
    fresh = p + ".part.999998"
    for f in (stale, fresh):
        with open(f, "wb") as fh:
            fh.write(b"x")
    old = time.time() - price_sidecar.STALE_PART_SECONDS - 60
    os.utime(stale, (old, old))

    seen = []
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    real_iter = price_sidecar._iter_points

    def watching(gz_path, counts=None):
        for row in real_iter(gz_path, counts):
            seen.append(sorted(glob.glob(p + ".part*")))
            yield row

    with mock.patch.object(price_sidecar, "_iter_points", watching):
        price_sidecar.build_from_allprices(p, gz)

    mine = p + f".part.{os.getpid()}"
    assert seen and all(mine in names for names in seen), \
        "the build must write into a name no other process can pick"
    assert not os.path.exists(stale)          # swept
    assert os.path.exists(fresh)              # another build may be mid-write
    assert not os.path.exists(mine)           # renamed onto the final path
    assert price_sidecar.is_ready(p)


def test_downsample_will_not_collapse_without_a_watermark(tmp_path):
    """`if watermark:` falls through to the raw system clock when
    daily_through is absent -- the unsafe default on the one irreversible
    operation in the module. A missing watermark means the data cannot say
    how far it runs, so nothing may be collapsed on its authority."""
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz",
                        _daily_series("uuid-a", "2026-01-05", 14, price=10.0))
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    db = price_sidecar.connect(p)
    db.execute("DELETE FROM meta WHERE key='daily_through'")
    db.commit()
    db.close()
    assert price_sidecar.daily_through(p) is None

    class ClockAYearFast:
        @staticmethod
        def now(tz=None):
            return datetime(2027, 1, 18, tzinfo=tz)

    with mock.patch.object(price_sidecar, "datetime", ClockAYearFast):
        assert price_sidecar.downsample(p) == 0
    db = price_sidecar.connect(p)
    assert db.execute("SELECT COUNT(*) FROM points WHERE agg=0").fetchone()[0] == 14
    db.close()
