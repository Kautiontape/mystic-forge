import os
import sqlite3

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
