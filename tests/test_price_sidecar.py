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
