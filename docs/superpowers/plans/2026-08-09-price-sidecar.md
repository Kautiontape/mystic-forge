# Price Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-add 1.4 GB `AllPrices` scan with an indexed read from a persistent local price store that also retains history past MTGJSON's 90-day window.

**Architecture:** A new `price_sidecar.py` owns one SQLite file holding every card MTGJSON prices, at daily resolution for a rolling 120 days and weekly means forever beyond that. Data flows one way: MTGJSON → sidecar → the main database's `prices` table (watched cards only) → the watchlist pages. No read path or page query changes.

**Tech Stack:** Python 3.14, SQLite (stdlib `sqlite3`), `ijson` for streaming JSON, pytest with `asyncio_mode = auto`.

**Spec:** `docs/superpowers/specs/2026-08-09-price-sidecar-design.md`

---

## Background you need before starting

Read the spec first. Beyond it, these facts about this codebase are not obvious:

- **Tests import repo-root modules directly.** `conftest.py` puts the repo root on `sys.path`, so it is `import price_sidecar`, not `from src...`. It also sets `MYSTIC_FORGE_NO_INGEST=1` so no background loop starts.
- **Two fixtures exist**, both in `conftest.py`: `db_path` (a path string, with `watchlist_db.DB_PATH` monkeypatched to it) and `db` (an open connection to it). Functions taking a path want `db_path`; functions taking a connection want `db`.
- **Run tests with** `python -m pytest -q -m "not slow"` from the repo root.
- **`ijson` yields `Decimal`**, not `float`, for JSON numbers. Always wrap in `float()` before arithmetic.
- **MTGJSON price shape** is `data.<uuid>.paper.<provider>.retail.<finish>.<date> = price`. There is also an `mtgo` sibling to `paper` which this project ignores.
- **Commit style is `topic: message`** (e.g. `sidecar: Add weekly downsampling`). Do not add `Co-Authored-By` lines.
- The `agg` column exists to protect one invariant: **a weekly mean is never fed back in as input to another mean.** Every query that computes averages filters `agg = 0`.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `price_sidecar.py` *(new)* | Owns the sidecar file end to end: schema, encodings, build, daily apply, reads, downsampling, stats. Knows nothing about watchlists or the main database. |
| `watchlist_ingest.py` *(modify)* | Orchestration. Decides when to build, when to fall back, and projects sidecar rows into `prices`. The only caller of `price_sidecar`. |
| `server.py` *(modify)* | Startup build scheduling, `/health` reporting, manapool in the shop surfaces. |
| `watchlist_pages.py` *(modify)* | manapool in `SHOPS` and `shop_names`. |
| `tests/test_price_sidecar.py` *(new)* | Unit tests for the sidecar module in isolation. |
| `tests/test_watchlist_ingest.py` *(modify)* | Integration: fast path, fallback, projection, dead-code removal. |

`price_sidecar.py` takes explicit paths in every function and never guesses its own location. That keeps it free of any import of `watchlist_ingest`, which would otherwise be circular.

---

## Task 1: Add manapool as a fourth provider

MTGJSON has added a `manapool` provider that `PROVIDERS` does not list. This task is independent of the sidecar and lands first as a small, self-contained change.

**Files:**
- Modify: `watchlist_ingest.py:23`
- Modify: `watchlist_pages.py:27`, `watchlist_pages.py:1005-1006`
- Modify: `server.py:5472`, `server.py:5735-5754`
- Test: `tests/test_watchlist_ingest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_watchlist_ingest.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_watchlist_ingest.py -k manapool -v`
Expected: FAIL — the first asserts `row is not None` and gets `None`; the second raises `KeyError: 'manapool'`.

- [ ] **Step 3: Add manapool to the provider tuple**

In `watchlist_ingest.py:23`:

```python
PROVIDERS = ("tcgplayer", "cardkingdom", "cardmarket", "manapool")
```

- [ ] **Step 4: Add manapool to the shop surfaces**

In `watchlist_pages.py:27`:

```python
SHOPS = {"tcgplayer": "$", "cardkingdom": "$", "cardmarket": "€",
         "manapool": "$"}
```

In `watchlist_pages.py:1005-1006`:

```python
    shop_names = {"tcgplayer": "TCGplayer", "cardkingdom": "Card Kingdom",
                  "cardmarket": "Cardmarket", "manapool": "Mana Pool"}
```

- [ ] **Step 5: Add manapool to the CSV export**

In `server.py:5735-5738`, the header row:

```python
        w.writerow(["card", "set_code", "collector_number", "tcgplayer_usd",
                    "cardkingdom_usd", "cardmarket_eur", "manapool_usd",
                    "d7", "d7_pct", "d30", "d30_pct", "target_usd",
                    "pct_to_target", "price_date"])
```

Then both provider tuples in the same function (`server.py:5741` and `server.py:5754`) become:

```python
                        for shop in ("tcgplayer", "cardkingdom", "cardmarket",
                                     "manapool")}
```

```python
                *(per_shop[shop]["current"] if per_shop[shop] else ""
                  for shop in ("tcgplayer", "cardkingdom", "cardmarket",
                               "manapool")),
```

And the field description at `server.py:5472`:

```python
    provider: str = Field("tcgplayer", description="tcgplayer | cardkingdom | cardmarket | manapool")
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q -m "not slow"`
Expected: PASS, including the two new tests. The shop-tab count changes, so if any test asserts on rendered shop links it will need its expectation updated.

- [ ] **Step 7: Commit**

```bash
git add watchlist_ingest.py watchlist_pages.py server.py tests/test_watchlist_ingest.py
git commit -m "watchlist: Track Mana Pool as a fourth price provider"
```

---

## Task 2: Sidecar module skeleton — schema, encodings, readiness

**Files:**
- Create: `price_sidecar.py`
- Test: `tests/test_price_sidecar.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_price_sidecar.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_price_sidecar.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'price_sidecar'`.

- [ ] **Step 3: Create the module**

Create `price_sidecar.py`:

```python
"""Persistent price sidecar: every card MTGJSON prices, kept past its window.

MTGJSON's AllPrices carries 90 days and costs a ~1.4 GB streaming parse to
read. This mirrors it into a compact local store so adding a watched card is
an indexed lookup instead, and so history outlives the upstream window:
daily resolution for a rolling KEEP_DAILY_DAYS, weekly means forever beyond.

Data flows one way. This module never reads the main watchlist database; the
`prices` table there is a projection of this file, written by
watchlist_ingest. Every function takes an explicit path — the module does not
know where it lives, which keeps it free of a circular import.
"""

import gzip
import logging
import os
import sqlite3
from datetime import date as _date, datetime, timedelta, timezone

import ijson

log = logging.getLogger("mystic_forge.sidecar")

SCHEMA_VERSION = 1
EPOCH = _date(2020, 1, 1)
PROVIDERS = ("tcgplayer", "cardkingdom", "cardmarket", "manapool")
FINISHES = ("normal", "foil", "etched")
FINISH_SLOTS = 4          # packing stride; room for a 4th finish without renumbering
KEEP_DAILY_DAYS = 120
BATCH = 50_000
BUILD_HEADROOM = 2_500_000_000     # bytes of free space a full build needs

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
  card_id INTEGER PRIMARY KEY,
  uuid    TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS points (
  card_id INTEGER NOT NULL,
  src     INTEGER NOT NULL,
  day     INTEGER NOT NULL,
  cents   INTEGER NOT NULL,
  agg     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (card_id, src, day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_PROV_IDX = {p: i for i, p in enumerate(PROVIDERS)}
_FIN_IDX = {f: i for i, f in enumerate(FINISHES)}


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def pack_src(provider: str, finish: str):
    """Packed provider/finish code, or None if either is unknown upstream."""
    p = _PROV_IDX.get(provider)
    f = _FIN_IDX.get(finish)
    if p is None or f is None:
        return None
    return p * FINISH_SLOTS + f


def unpack_src(src: int) -> tuple[str, str]:
    return PROVIDERS[src // FINISH_SLOTS], FINISHES[src % FINISH_SLOTS]


def to_day(iso: str) -> int:
    return (_date.fromisoformat(iso) - EPOCH).days


def from_day(day: int) -> str:
    return (EPOCH + timedelta(days=day)).isoformat()


def to_cents(price) -> int:
    """Quantize to cents. Deliberately lossy past two decimals — see the spec."""
    return int(round(float(price) * 100))


def from_cents(cents: int) -> float:
    return cents / 100.0


def _get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
               (key, str(value)))


def is_ready(path: str) -> bool:
    """A completed build of the current schema, openable and uncorrupted."""
    if os.environ.get("MYSTIC_FORGE_NO_SIDECAR"):
        return False
    if not path or not os.path.exists(path):
        return False
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            version = _get_meta(db, "schema_version")
            built = _get_meta(db, "built_at")
        finally:
            db.close()
    except sqlite3.DatabaseError:
        return False
    return bool(built) and version == str(SCHEMA_VERSION)


def daily_through(path: str):
    """Newest date applied, as an ISO string, or None."""
    if not is_ready(path):
        return None
    db = connect(path)
    try:
        return _get_meta(db, "daily_through")
    finally:
        db.close()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_price_sidecar.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add price_sidecar.py tests/test_price_sidecar.py
git commit -m "sidecar: Schema, encodings, and readiness checks"
```

---

## Task 3: Build the sidecar from AllPrices

**Files:**
- Modify: `price_sidecar.py`
- Test: `tests/test_price_sidecar.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_price_sidecar.py`:

```python
import gzip
import json
import tracemalloc


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

    import unittest.mock as mock
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
    assert peak < 20 * 1024 * 1024
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_price_sidecar.py -k build -v`
Expected: FAIL with `AttributeError: module 'price_sidecar' has no attribute 'build_from_allprices'`.

- [ ] **Step 3: Implement the build**

Append to `price_sidecar.py`:

```python
def _iter_points(gz_path: str):
    """Yield (uuid, src, day, cents) from an MTGJSON prices .gz.

    Shape: data.<uuid>.paper.<provider>.retail.<finish>.<date> = price.
    Streams with ijson, so peak memory is independent of file size. Unknown
    providers/finishes and unparseable values are skipped, not fatal — MTGJSON
    adds providers without warning."""
    with gzip.open(gz_path, "rb") as f:
        for uuid, obj in ijson.kvitems(f, "data"):
            paper = (obj or {}).get("paper") or {}
            for provider, pdata in paper.items():
                retail = (pdata or {}).get("retail") or {}
                for finish, series in retail.items():
                    src = pack_src(provider, finish)
                    if src is None:
                        continue
                    for d, price in (series or {}).items():
                        try:
                            yield uuid, src, to_day(d), to_cents(price)
                        except (ValueError, TypeError):
                            continue


def _free_bytes(directory: str) -> int:
    st = os.statvfs(directory)
    return st.f_bavail * st.f_frsize


def build_from_allprices(path: str, gz_path: str) -> int:
    """Full load from AllPrices.json.gz. Returns points written.

    Builds into <path>.part and atomically renames, so an interrupted build
    can never leave a file that is_ready() would accept."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    if _free_bytes(directory) < BUILD_HEADROOM:
        raise OSError(f"not enough free space in {directory} to build the "
                      f"sidecar ({BUILD_HEADROOM} bytes needed)")
    part = path + ".part"
    if os.path.exists(part):
        os.remove(part)
    db = sqlite3.connect(part)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(SCHEMA)
        db.execute("PRAGMA journal_mode=OFF")     # disposable until the rename
        db.execute("PRAGMA synchronous=OFF")
        ids: dict[str, int] = {}
        batch: list[tuple] = []
        rows = 0
        for uuid, src, day, cents in _iter_points(gz_path):
            cid = ids.get(uuid)
            if cid is None:
                cid = len(ids) + 1
                ids[uuid] = cid
                db.execute("INSERT INTO cards (card_id, uuid) VALUES (?,?)",
                           (cid, uuid))
            batch.append((cid, src, day, cents))
            if len(batch) >= BATCH:
                db.executemany(
                    "INSERT OR REPLACE INTO points (card_id,src,day,cents,agg)"
                    " VALUES (?,?,?,?,0)", batch)
                rows += len(batch)
                batch.clear()
        if batch:
            db.executemany(
                "INSERT OR REPLACE INTO points (card_id,src,day,cents,agg)"
                " VALUES (?,?,?,?,0)", batch)
            rows += len(batch)
        newest = db.execute("SELECT MAX(day) AS d FROM points").fetchone()["d"]
        _set_meta(db, "schema_version", SCHEMA_VERSION)
        if newest is not None:
            _set_meta(db, "daily_through", from_day(newest))
        _set_meta(db, "built_at",
                  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        db.commit()
    except BaseException:
        db.close()
        if os.path.exists(part):
            os.remove(part)
        raise
    db.close()
    os.replace(part, path)
    log.info("sidecar built: %d cards, %d points", len(ids), rows)
    return rows
```

Note the in-memory `ids` dict: ~111k uuid→int entries, a few MB, versus a query per row. That is the one thing the build holds in memory and it is what the `tracemalloc` ceiling proves stays bounded.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_price_sidecar.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add price_sidecar.py tests/test_price_sidecar.py
git commit -m "sidecar: Atomic streaming build from AllPrices"
```

---

## Task 4: Apply the nightly AllPricesToday file

**Files:**
- Modify: `price_sidecar.py`
- Test: `tests/test_price_sidecar.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_price_sidecar.py`:

```python
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
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-a": TODAY_OBJ})
    price_sidecar.apply_daily(p, today)
    db = price_sidecar.connect(p)
    before = db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    db.close()
    price_sidecar.apply_daily(p, today)
    db = price_sidecar.connect(p)
    after = db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    db.close()
    assert before == after


def test_apply_daily_on_unbuilt_sidecar_is_a_noop(tmp_path):
    today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                           {"uuid-a": TODAY_OBJ})
    assert price_sidecar.apply_daily(str(tmp_path / "absent.sqlite"), today) == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_price_sidecar.py -k apply_daily -v`
Expected: FAIL with `AttributeError: module 'price_sidecar' has no attribute 'apply_daily'`.

- [ ] **Step 3: Implement it**

Append to `price_sidecar.py`:

```python
def apply_daily(path: str, gz_path: str) -> int:
    """Fold a day's AllPricesToday into a built sidecar. Returns points written.

    A no-op on an unbuilt sidecar: a partial file must never look like a
    complete one. New uuids are learned as they appear."""
    if not is_ready(path):
        return 0
    db = connect(path)
    try:
        ids = {r["uuid"]: r["card_id"]
               for r in db.execute("SELECT uuid, card_id FROM cards")}
        next_id = max(ids.values(), default=0) + 1
        batch, rows, newest = [], 0, None
        for uuid, src, day, cents in _iter_points(gz_path):
            cid = ids.get(uuid)
            if cid is None:
                cid = next_id
                next_id += 1
                ids[uuid] = cid
                db.execute("INSERT INTO cards (card_id, uuid) VALUES (?,?)",
                           (cid, uuid))
            batch.append((cid, src, day, cents))
            newest = day if newest is None else max(newest, day)
            if len(batch) >= BATCH:
                db.executemany(
                    "INSERT OR REPLACE INTO points (card_id,src,day,cents,agg)"
                    " VALUES (?,?,?,?,0)", batch)
                rows += len(batch)
                batch.clear()
        if batch:
            db.executemany(
                "INSERT OR REPLACE INTO points (card_id,src,day,cents,agg)"
                " VALUES (?,?,?,?,0)", batch)
            rows += len(batch)
        if newest is not None:
            prev = _get_meta(db, "daily_through")
            latest = from_day(newest)
            if prev is None or latest > prev:
                _set_meta(db, "daily_through", latest)
        db.commit()
        return rows
    finally:
        db.close()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_price_sidecar.py -v`
Expected: PASS, 16 tests.

- [ ] **Step 5: Commit**

```bash
git add price_sidecar.py tests/test_price_sidecar.py
git commit -m "sidecar: Fold the nightly AllPricesToday file"
```

---

## Task 5: Read history back out

**Files:**
- Modify: `price_sidecar.py`
- Test: `tests/test_price_sidecar.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_price_sidecar.py`:

```python
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


def test_series_handles_more_uuids_than_one_query_allows(tmp_path):
    """Chunking must not silently drop uuids past SQLite's parameter limit."""
    many = {f"uuid-{i}": TODAY_OBJ for i in range(1200)}
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", many)
    p = str(tmp_path / "side.sqlite")
    price_sidecar.build_from_allprices(p, gz)
    got = list(price_sidecar.series_for_uuids(p, list(many)))
    assert len({u for u, *_ in got}) == 1200
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_price_sidecar.py -k series -v`
Expected: FAIL with `AttributeError: module 'price_sidecar' has no attribute 'series_for_uuids'`.

- [ ] **Step 3: Implement it**

Append to `price_sidecar.py`:

```python
def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def series_for_uuids(path: str, uuids, providers=None, since: str | None = None):
    """Yield (uuid, date_iso, provider, finish, price) for the given uuids.

    Shaped exactly for watchlist_db.upsert_price, so the projection into the
    main database needs no adaptation layer. `since` is exclusive. Yields
    nothing at all when the sidecar is not ready — callers fall back."""
    uuids = list(uuids)
    if not uuids or not is_ready(path):
        return
    want = None
    if providers is not None:
        want = {pack_src(p, f) for p in providers for f in FINISHES}
        want.discard(None)
        if not want:
            return
    floor = to_day(since) if since else None
    db = connect(path)
    try:
        for chunk in _chunks(uuids, 500):
            marks = ",".join("?" * len(chunk))
            sql = (f"SELECT c.uuid AS uuid, p.src AS src, p.day AS day,"
                   f" p.cents AS cents FROM points p"
                   f" JOIN cards c ON c.card_id = p.card_id"
                   f" WHERE c.uuid IN ({marks})")
            args = list(chunk)
            if floor is not None:
                sql += " AND p.day > ?"
                args.append(floor)
            sql += " ORDER BY c.uuid, p.src, p.day"
            for r in db.execute(sql, args):
                if want is not None and r["src"] not in want:
                    continue
                provider, finish = unpack_src(r["src"])
                yield (r["uuid"], from_day(r["day"]), provider, finish,
                       from_cents(r["cents"]))
    finally:
        db.close()
```

Chunking at 500 keeps the `IN (...)` list far below SQLite's default 999-parameter limit, which a watchlist tracking every printing of many cards would otherwise blow through.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_price_sidecar.py -v`
Expected: PASS, 20 tests.

- [ ] **Step 5: Commit**

```bash
git add price_sidecar.py tests/test_price_sidecar.py
git commit -m "sidecar: Read history back as upsert-shaped rows"
```

---

## Task 6: Weekly downsampling

The trickiest part is picking the surviving date. Each week collapses onto the Sunday that ends its ISO week, computed arithmetically from the day-offset:

```
anchor(day) = day + 6 - ((day + 2) % 7)
```

`EPOCH` (2020-01-01) is a Wednesday, hence the `+ 2`. This has been verified against `datetime.isoweekday()` for every day from 2020-01-01 to 2030-12-31, and SQLite's `%` agrees with Python's over the same range. Because the anchor is the week's *last* day, `anchor < cutoff` proves the entire week sits behind the cutoff — that is what stops a partial week from being collapsed early.

**Files:**
- Modify: `price_sidecar.py`
- Test: `tests/test_price_sidecar.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_price_sidecar.py`:

```python
from datetime import date, timedelta


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_price_sidecar.py -k "downsample or anchor" -v`
Expected: FAIL with `AttributeError: module 'price_sidecar' has no attribute 'week_anchor'`.

- [ ] **Step 3: Implement it**

Append to `price_sidecar.py`:

```python
def week_anchor(day: int) -> int:
    """Day-offset of the Sunday ending `day`'s ISO week.

    EPOCH is a Wednesday, which is where the + 2 comes from. Kept in lockstep
    with the identical expression used in downsample's SQL."""
    return day + 6 - ((day + 2) % 7)


_ANCHOR_SQL = "(day + 6 - ((day + 2) % 7))"


def downsample(path: str, keep_daily_days: int = KEEP_DAILY_DAYS,
               today: str | None = None) -> int:
    """Collapse daily rows older than the window into weekly means.

    Returns rows deleted. Only whole weeks are collapsed: the anchor is the
    week's last day, so anchor < cutoff proves every day of that week is
    behind the cutoff. Only agg=0 rows are read as input, so a mean is never
    taken of means and repeat runs are no-ops."""
    if not is_ready(path):
        return 0
    ref = _date.fromisoformat(today) if today else _date.today()
    cutoff = (ref - timedelta(days=keep_daily_days) - EPOCH).days
    db = connect(path)
    try:
        db.execute(
            f"""INSERT OR REPLACE INTO points (card_id, src, day, cents, agg)
                SELECT card_id, src, {_ANCHOR_SQL} AS anchor,
                       CAST(ROUND(AVG(cents)) AS INTEGER), 1
                FROM points
                WHERE agg = 0 AND {_ANCHOR_SQL} < ?
                GROUP BY card_id, src, anchor""", (cutoff,))
        cur = db.execute(
            f"DELETE FROM points WHERE agg = 0 AND {_ANCHOR_SQL} < ?",
            (cutoff,))
        deleted = cur.rowcount
        db.commit()
        if deleted:
            log.info("sidecar downsample: %d daily rows collapsed", deleted)
        return deleted
    finally:
        db.close()
```

The INSERT must run before the DELETE. The Sunday row is itself part of its group, so the INSERT overwrites it with the `agg=1` mean, and the DELETE then skips it because it is no longer `agg=0`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_price_sidecar.py -v`
Expected: PASS, 25 tests.

- [ ] **Step 5: Commit**

```bash
git add price_sidecar.py tests/test_price_sidecar.py
git commit -m "sidecar: Collapse aged daily rows into weekly means"
```

---

## Task 7: Stats for /health

**Files:**
- Modify: `price_sidecar.py`
- Test: `tests/test_price_sidecar.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_price_sidecar.py`:

```python
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


def test_stats_on_missing_file_is_not_ready(tmp_path):
    assert price_sidecar.stats(str(tmp_path / "absent.sqlite")) == {"ready": False}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_price_sidecar.py -k stats -v`
Expected: FAIL with `AttributeError: module 'price_sidecar' has no attribute 'stats'`.

- [ ] **Step 3: Implement it**

Append to `price_sidecar.py`:

```python
def stats(path: str) -> dict:
    """Shape and span of the sidecar, for /health. Counts are full scans, so
    call this on an operator-facing endpoint, not a hot path."""
    if not is_ready(path):
        return {"ready": False}
    db = connect(path)
    try:
        span = db.execute("SELECT COUNT(*) AS n, MIN(day) AS lo, MAX(day) AS hi"
                          " FROM points").fetchone()
        daily = db.execute("SELECT COUNT(*) AS n FROM points WHERE agg=0"
                           ).fetchone()["n"]
        return {
            "ready": True,
            "cards": db.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"],
            "points": span["n"],
            "daily_points": daily,
            "weekly_points": span["n"] - daily,
            "earliest": from_day(span["lo"]) if span["lo"] is not None else None,
            "latest": from_day(span["hi"]) if span["hi"] is not None else None,
            "built_at": _get_meta(db, "built_at"),
            "bytes": os.path.getsize(path),
        }
    finally:
        db.close()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_price_sidecar.py -v`
Expected: PASS, 27 tests.

- [ ] **Step 5: Commit**

```bash
git add price_sidecar.py tests/test_price_sidecar.py
git commit -m "sidecar: Report shape and span for /health"
```

---

## Task 8: Fast path in ensure_history, with fallback

This is the task that makes adds fast. The equivalence test is the safety net for the whole plan: it proves the sidecar path writes what the legacy scan writes.

**Files:**
- Modify: `watchlist_ingest.py` (imports, add `_sidecar_path` and `_project`, rewrite `ensure_history`)
- Test: `tests/test_watchlist_ingest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_watchlist_ingest.py`:

Add `import os` and `import price_sidecar` to the top of
`tests/test_watchlist_ingest.py` — the file currently imports only `gzip`,
`json`, `sqlite3`, `tracemalloc`, `watchlist_db`, and `watchlist_ingest`.

```python
THREE_DP_OBJ = {"paper": {"tcgplayer": {"retail": {
    "normal": {"2026-08-01": 8.0, "2026-08-08": 7.129}}}}}


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
    """Equivalence, at cent precision. The fixture carries a three-decimal
    price so the documented quantization is exercised, not dodged."""
    ap = make_allprintings(tmp_path)
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": THREE_DP_OBJ})
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.commit()
    watchlist_ingest.resolve_watched(db, ap)
    db.commit()

    watchlist_ingest.ingest_prices_file(db, gz)
    legacy = {(r["uuid"], r["date"], r["provider"], r["finish"],
               round(r["price"], 2))
              for r in db.execute("SELECT * FROM prices")}
    db.execute("DELETE FROM prices")
    db.commit()

    side = watchlist_ingest._sidecar_path(str(tmp_path))
    price_sidecar.build_from_allprices(side, gz)
    watchlist_ingest._project(db, side, ["uuid-a"])
    projected = {(r["uuid"], r["date"], r["provider"], r["finish"],
                  round(r["price"], 2))
                 for r in db.execute("SELECT * FROM prices")}

    assert projected == legacy


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_watchlist_ingest.py -k "sidecar or equivalence or fallback" -v`
Expected: FAIL with `AttributeError: module 'watchlist_ingest' has no attribute '_sidecar_path'`.

- [ ] **Step 3: Add the sidecar helpers**

In `watchlist_ingest.py`, add to the imports:

```python
import price_sidecar
```

Then add these two functions just above `ensure_history`:

```python
def _sidecar_path(data_dir: str | None = None) -> str:
    """Where the price sidecar lives. Computed here, not in price_sidecar, so
    that module stays free of any import back into this one."""
    return os.path.join(data_dir or _data_dir(), "price_sidecar.sqlite")


def _project(db, sidecar_path: str, uuids, since: str | None = None) -> int:
    """Copy sidecar history for `uuids` into the main database's prices table.

    The one-way seam: the sidecar is the only writer of `prices`, and nothing
    downstream of `prices` knows the sidecar exists."""
    n = 0
    for uuid, d, provider, finish, price in price_sidecar.series_for_uuids(
            sidecar_path, uuids, since=since):
        watchlist_db.upsert_price(db, uuid, d, provider, finish, price,
                                  commit=False)
        n += 1
    db.commit()
    return n
```

- [ ] **Step 4: Rewrite ensure_history to try the sidecar first**

Replace the body of `ensure_history` from the `missing = {...}` assignment onward (currently `watchlist_ingest.py:190-202`) with:

```python
        missing = {r["uuid"] for r in db.execute(
            """SELECT cu.uuid FROM watchlist_current wc
               JOIN card_uuids cu ON LOWER(cu.card_name)=LOWER(wc.card_name)
               WHERE NOT EXISTS (SELECT 1 FROM prices p WHERE p.uuid=cu.uuid)""")}
        if not missing:
            return 0
        side = _sidecar_path(data_dir)
        if price_sidecar.is_ready(side):
            n = _project(db, side, missing)
            log.info("history fill from sidecar: %d uuid(s), %d rows",
                     len(missing), n)
            return n
        allp = os.path.join(data_dir, "AllPrices.json.gz")
        if not os.path.exists(allp):
            log.info("bootstrap: fetching AllPrices")
            _download(f"{MTGJSON}/AllPrices.json.gz", allp, db)
        n = ingest_prices_file(db, allp, only_uuids=missing)
        log.info("history fill by scan: %d uuid(s), %d rows", len(missing), n)
        return n
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_watchlist_ingest.py -v`
Expected: PASS. Every pre-existing test in the file must still pass — they cover the fallback path, which is unchanged.

- [ ] **Step 6: Commit**

```bash
git add watchlist_ingest.py tests/test_watchlist_ingest.py
git commit -m "ingest: Fill history from the sidecar, falling back to a scan"
```

---

## Task 9: Rewire the nightly ingest

**Files:**
- Modify: `watchlist_ingest.py` (`run_ingest`, currently lines 262-291)
- Test: `tests/test_watchlist_ingest.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_watchlist_ingest.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_watchlist_ingest.py -k run_ingest -v`
Expected: FAIL — `daily_through` is still `2026-08-08` because `run_ingest` does not touch the sidecar yet.

- [ ] **Step 3: Rewrite the daily section of run_ingest**

In `watchlist_ingest.py`, replace the block from the `AllPricesToday` download through `_set_meta(db, "last_ingest", ...)` (currently lines 284-288) with:

```python
        p = _download(f"{MTGJSON}/AllPricesToday.json.gz",
                      os.path.join(data_dir, "AllPricesToday.json.gz"), db)
        side = _sidecar_path(data_dir)
        if price_sidecar.is_ready(side):
            previous = price_sidecar.daily_through(side)
            price_sidecar.apply_daily(side, p)
            # Project only what is new. `previous` is exclusive, so a server
            # that was down for several days catches up in one pass.
            n = _project(db, side, watched_uuids(db), since=previous)
            price_sidecar.downsample(side)
        else:
            n = ingest_prices_file(db, p)
        log.info("daily: %d rows", n)
        _set_meta(db, "last_ingest", date.today().isoformat())
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_watchlist_ingest.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add watchlist_ingest.py tests/test_watchlist_ingest.py
git commit -m "ingest: Route the nightly update through the sidecar"
```

---

## Task 10: Delete the unreachable backfill path

`backfill_cards` and `backfill_entry` are unreachable: `_schedule_backfill(card_names)` accepts `card_names` and never uses it, always calling `ensure_history`. Both do a full `ingest_prices_file` pass anyway, so they are not a faster path worth keeping now that the sidecar exists.

**Files:**
- Modify: `watchlist_ingest.py:207-242` (delete both functions)
- Modify: `server.py:5124`, `server.py:5911-5912`
- Modify: `tests/test_watchlist_ingest.py` (delete the test of the deleted function)

- [ ] **Step 1: Confirm they are unreachable**

Run: `grep -rn "backfill_cards\|backfill_entry" --include=*.py .`
Expected: matches only in `watchlist_ingest.py` (the definitions) and `tests/test_watchlist_ingest.py`. If anything else references them, stop and reassess.

- [ ] **Step 2: Delete the functions**

Remove `backfill_cards` and `backfill_entry` from `watchlist_ingest.py` entirely (currently lines 207-242, ending just before `_needs_backfill`).

- [ ] **Step 3: Delete the test that covered them**

Remove `test_backfill_entry_uses_cached_files_for_one_card` from `tests/test_watchlist_ingest.py`.

- [ ] **Step 4: Drop the vestigial parameter**

In `server.py:5124`, the signature becomes:

```python
def _schedule_backfill() -> bool:
```

And the call site at `server.py:5911-5912` becomes:

```python
        backfilling = (not watchlist_db.entry_price_summary(db, entry)
                       and _schedule_backfill())
```

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q -m "not slow"`
Expected: PASS, with one fewer test than before.

- [ ] **Step 6: Commit**

```bash
git add watchlist_ingest.py server.py tests/test_watchlist_ingest.py
git commit -m "ingest: Remove the unreachable per-card backfill path"
```

---

## Task 11: Server wiring — startup build and /health

The build is scheduled eagerly at startup rather than lazily on first need. The weekly tier can only ever begin from the 90 days `AllPrices` holds on build day, so every day of delay is history permanently lost.

**Files:**
- Modify: `server.py:187-198` (lifespan hook), `server.py:5619-5645` (`/health`)
- Test: `tests/test_http_surface.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_http_surface.py`:

Follow the file's existing shape — the `client()` helper and the `db_path`
fixture, not a hand-built Starlette `Request`:

```python
def test_health_reports_sidecar_state(db_path, monkeypatch):
    """Operators need to see whether the sidecar exists and how deep it goes."""
    monkeypatch.setattr(server.price_sidecar, "stats",
                        lambda _p: {"ready": True, "points": 42,
                                    "earliest": "2026-01-01",
                                    "latest": "2026-08-09"})
    with client() as c:
        body = c.get("/health").json()
    assert body["sidecar"]["ready"] is True
    assert body["sidecar"]["points"] == 42


def test_health_survives_a_missing_sidecar(db_path):
    """No sidecar is a normal state, not an error."""
    with client() as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["sidecar"]["ready"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_http_surface.py -k sidecar -v`
Expected: FAIL with `KeyError: 'sidecar'`.

- [ ] **Step 3: Import the module and report stats**

In `server.py`, add `import price_sidecar` alongside the other local imports (near `import watchlist_ingest`).

In the `/health` handler, add the sidecar block to the returned payload:

```python
    try:
        side = price_sidecar.stats(watchlist_ingest._sidecar_path())
    except Exception:
        side = {"ready": False}
    return JSONResponse({
        "status": "degraded" if stale else "ok",
        "version": VERSION,
        "db": True, "lists": lists, "watched_cards": cards,
        "last_ingest": last, "ingest_stale": stale,
        "sidecar": side,
    })
```

- [ ] **Step 4: Schedule the build at startup**

Add this function next to `watchlist_ingest_loop` in `server.py`:

```python
async def sidecar_build_once():
    """Build the price sidecar if it does not exist yet.

    Eager rather than lazy on purpose: the weekly tier can only start from the
    90 days AllPrices carries on build day, so delay is history that can never
    be recovered."""
    try:
        data_dir = watchlist_ingest._data_dir()
        side = watchlist_ingest._sidecar_path(data_dir)
        if price_sidecar.is_ready(side):
            return
        gz = os.path.join(data_dir, "AllPrices.json.gz")
        if not os.path.exists(gz):
            db = watchlist_db.connect()
            try:
                watchlist_db.init_db(db)
                await asyncio.to_thread(
                    watchlist_ingest._download,
                    f"{watchlist_ingest.MTGJSON}/AllPrices.json.gz", gz, db)
            finally:
                db.close()
        await asyncio.to_thread(price_sidecar.build_from_allprices, side, gz)
    except Exception:
        logging.getLogger("mystic_forge").exception("sidecar build failed")
```

Then in `PassphraseMiddleware.__init__`, add `self._sidecar_task = None`, and in the lifespan hook at `server.py:190-195`:

```python
                if (msg["type"] == "lifespan.startup.complete"
                        and not os.environ.get("MYSTIC_FORGE_NO_INGEST")):
                    self._ingest_task = asyncio.create_task(
                        watchlist_ingest_loop())
                    self._sidecar_task = asyncio.create_task(
                        sidecar_build_once())
                if msg["type"] == "lifespan.shutdown.complete":
                    if self._ingest_task:
                        self._ingest_task.cancel()
                    if self._sidecar_task:
                        self._sidecar_task.cancel()
```

`MYSTIC_FORGE_NO_INGEST` already gates this, so tests never trigger a build.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q -m "not slow"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_http_surface.py
git commit -m "server: Build the sidecar at startup and report it on /health"
```

---

## Task 12: Ship it

**Files:**
- Modify: `VERSION`, `Dockerfile`, `CLAUDE.md`

- [ ] **Step 1: Add the new module to the image**

The Dockerfile copies an explicit file list, not `COPY . .`. A new top-level module that is not added there silently will not exist in the image. Update the `COPY` line:

```dockerfile
COPY VERSION server.py watchlist_db.py watchlist_ingest.py watchlist_pages.py price_sidecar.py watchlist_words.txt og.png rulebook.py MagicCompRules.txt ./
```

- [ ] **Step 2: Verify the module is actually in the image**

Run: `docker compose build && docker compose run --rm mystic-forge python -c "import price_sidecar; print(price_sidecar.SCHEMA_VERSION)"`
Expected: prints `1`. If it raises `ModuleNotFoundError`, the `COPY` line is wrong.

- [ ] **Step 3: Bump the version**

`VERSION` currently reads `1.1.0`. Change it to `1.2.0` — a new capability, no breaking change. It feeds the image tag, the outbound `User-Agent`, and `/health`.

- [ ] **Step 4: Document the sidecar in CLAUDE.md**

Add `price_sidecar.py` to the module list in the Overview section, and note under Architecture that the WATCHLIST section now owns two SQLite files: the main database and the price sidecar, the latter holding all-card price history and being the sole writer of `prices`.

- [ ] **Step 5: Run the full suite one last time**

Run: `python -m pytest -q -m "not slow"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add VERSION Dockerfile CLAUDE.md
git commit -m "release: Ship the persistent price sidecar"
```

- [ ] **Step 7: Hand off the release**

Push to `main`. `ci.yml` runs the tests and `propose-release.yml` opens a PR in the parent `mcp-servers` repo bumping the submodule pin. Then, in the parent repo on that PR branch:

1. Add a changelog entry at `landing/mtg/changelog/index.html` tagged `data-version="<VERSION>"`.
2. Update the teaser in `landing/mtg/index.html` to match.

`scripts/check_release.py` blocks the merge unless the version has a changelog entry, the version increased, and the teaser matches the newest entry. No tool was added or renamed, so the tool-tag check is unaffected.

**After the deploy lands, verify the build actually ran** — this is the one step with no test coverage, because it depends on 141 MB of real MTGJSON data:

```bash
curl -s https://mcp.kautiontape.com/mtg/health | python -m json.tool
```

Expected: `sidecar.ready` is `true` within roughly 30 minutes of deploy, with `points` in the tens of millions and `earliest` about 90 days back. Until it flips to `true`, adds still work — they take the legacy scan path.

---

## Notes for the implementer

**What "done" looks like beyond green tests:** the acceptance criteria in the spec. In particular, criterion 2 — with no sidecar, every current behaviour is preserved exactly — is what the fallback tests in Task 8 exist to prove. If you find yourself changing a pre-existing test's expectations rather than adding new ones, stop and work out why.

**Do not touch** `_envelope`, `_cheapest_latest`, `price_summary`, `price_series`, or anything in `watchlist_pages.py` beyond the two manapool lines in Task 1. The entire design rests on the read path being untouched. A change there means the design has drifted and should be revisited rather than patched.

**Tell the operator about backups.** Once the sidecar is more than ~90 days
old it holds history MTGJSON can no longer replay, so it stops being a
rebuildable cache and becomes real data. It lives in `/data` alongside
`mystic_forge.db` on the persisted `mystic_forge_data` volume, so restarts and
redeploys are safe — but whatever backs up the database should now also cover
`price_sidecar.sqlite`. There is no code change for this; it is a handover
note, and it is easy to forget precisely because nothing breaks when it is
missed.

**One known limitation, deliberately out of scope:** a card MTGJSON has no prices for never satisfies `_ensure_history_for` (`server.py:5677`), so it re-triggers a fill on every page load forever. With a ready sidecar each of those is an indexed lookup returning nothing, so the cost drops from minutes to microseconds — defused, not fixed. Fixing it properly means distinguishing "not fetched yet" from "known to have no prices", which is a separate change.
