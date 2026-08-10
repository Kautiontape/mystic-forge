import gzip
import json
import os
import sqlite3
import tracemalloc
import unittest.mock as mock

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

    def boom(_gz):
        yield "uuid-a", 0, 100, 500
        raise RuntimeError("disk fell over")

    with mock.patch.object(price_sidecar, "_iter_points", boom):
        with pytest.raises(RuntimeError):
            price_sidecar.build_from_allprices(p, gz)
    assert not os.path.exists(p + ".part")
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

    def crashes_after_two_batches(_gz):
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

    def full_day(_gz):
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
