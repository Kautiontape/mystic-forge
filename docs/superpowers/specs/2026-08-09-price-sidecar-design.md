# Persistent price sidecar

**Date:** 2026-08-09
**Status:** approved, ready for implementation plan

## Problem

Adding a card to a watchlist costs a full streaming parse of MTGJSON's
`AllPrices.json.gz` — 141 MB compressed, ~1.4 GB of JSON. `ingest_prices_file`
filters to the uuids it wants, but the filter skips *writes*, not the scan, so
the cost is identical whether one card is missing prices or forty. On a fresh
server the first add also pays two large downloads (`AllPrintings.sqlite`,
641 MB; `AllPrices.json.gz`, 141 MB), but those are cached in the persisted
`/data` volume and paid once. The scan is not — it recurs on every add of a
card that has no price rows yet, taking minutes each time.

A second, latent version of the same bug: `_ensure_history_for`
(`server.py:5677`) fires on every page load when anything is unpriced, and
"unpriced" means "zero price rows." A watched card MTGJSON has no price data
for never clears that condition, so every page view kicks off another full
scan forever. Single-flight stops them overlapping, not repeating.

Separately, price history is capped at what `AllPrices` carries (90 days) for
any newly added card. The `prices` table does accumulate past 90 days via the
nightly `AllPricesToday` pass, but only for cards already being watched.

## Goals

1. An add of a card with cached data completes in well under a second.
2. History accumulates beyond MTGJSON's 90-day window, for **every** card, so
   a card added in future arrives with retroactive depth.
3. The watchlist read path does not change and cannot regress.
4. Add `manapool` as a fourth price provider.

## Non-goals

- Replacing the `prices` table or rewriting any query that reads it.
- Sub-daily price resolution.
- Backfilling history that predates the sidecar's first build. Anything older
  than the 90 days `AllPrices` carries at build time is unrecoverable.

## Measured baseline

Taken from `AllPricesToday.json.gz` on 2026-08-08:

| Fact | Value |
| --- | --- |
| uuids in the file | 111,115 |
| uuids with paper prices | 105,959 |
| price rows for one day, all providers/finishes | 605,842 |
| `AllPrices.json.gz` | 141 MB gz, ~1.4 GB JSON |
| `AllPricesToday.json.gz` | 5.2 MB gz, ~53 MB JSON |
| `AllPrintings.sqlite` | 641 MB |

Provider/finish split for one day:

| provider | normal | foil | etched |
| --- | --- | --- | --- |
| cardmarket | 91,210 | 69,072 | 1,206 |
| tcgplayer | 87,595 | 61,503 | 1,227 |
| manapool | 86,922 | 61,635 | 1,202 |
| cardkingdom | 84,946 | 58,210 | 1,114 |

Derived sizing, all four providers, at ~12–16 bytes per stored row:

- Initial load from `AllPrices` (90 days): ~54.5M rows, **~0.8–1 GB**
- Steady-state daily window (120 days): ~73M rows, **~1.1 GB, constant**
- Weekly tier: ~605,842 rows/week → ~31.5M rows/year, **~440 MB/year**

Roughly 1.5 GB after one year, 2 GB after two.

## Architecture

One-way flow. MTGJSON is the only writer of the sidecar; the sidecar is the
only writer of `prices`; the pages read `prices` exactly as they do today.

```
MTGJSON files
    │  build_from_allprices()  (once)
    │  apply_daily()           (nightly)
    ▼
price_sidecar.sqlite      all cards, tiered retention
    │  series_for_uuids()
    ▼
prices (main db)          watched cards only — schema unchanged
    │
    ▼
watchlist pages           unchanged
```

`prices` is global, keyed `(uuid, date, provider, finish)` with no list
reference, so two lists watching the same card share rows rather than
duplicating them. The multiplier that does apply is printings: a name-only
entry tracks every printing through `uuids_for_entry`, which for a
representative 17-card list is 156 uuids (avg 9.2 printings per card,
measured against Scryfall). That works out to ~100k rows over the 90-day
window and ~135k at the steady-state 120 days — single-digit MB, negligible
next to the sidecar itself.

Tracking all printings is deliberate, not incidental: `_envelope` takes the
per-date minimum across them so the series reflects what a buyer would
actually pay, and stays correct when a reprint changes which printing is
cheapest.

The projection buys total insulation of the read path. `_envelope`,
`_cheapest_latest`, `price_summary`, and `price_series` are not touched, and
neither are their tests.

### Why a separate file rather than tables in the main database

1. **Maintenance isolation.** Downsampling deletes millions of rows. Reclaiming
   that space means `VACUUM`, which takes a write lock on the entire file. In
   the main database every watchlist page would block behind it.
2. **WAL contention.** ~600k inserts a night into the file the pages read
   grows the WAL and forces checkpoints while readers are active. Isolated,
   the main database's WAL stays small.
3. **Page-cache eviction.** SQLite's cache is per-connection and shared across
   the file. A multi-GB, scan-heavy table would evict the small hot tables
   (`watchlist_current`, `events`) that every page load touches.
4. **Backup asymmetry.** The main database is small, precious and worth
   backing up often; the sidecar is gigabytes. Merging them imposes the
   expensive cadence on both.

The honest counter-argument is that past 90 days the sidecar is *also*
irreplaceable, and irreplaceable data usually belongs with the other
irreplaceable data. That is answered by backing it up, not by merging an
append-heavy multi-GB table into a file that page loads read synchronously.
Precious and hot-path are different properties.

The usual argument for a single file — cross-table joins — does not apply,
because the design is a one-way projection rather than a query-time join.

This is also a low-regret decision: folding the sidecar into the main database
later is an `ATTACH` plus `INSERT … SELECT`, with no schema change.

## New module: `price_sidecar.py`

Owns exactly one file, `<DATA_DIR>/price_sidecar.sqlite`, where `DATA_DIR` is
`watchlist_ingest._data_dir()`. Only `watchlist_ingest` calls into this module.

### Schema

```sql
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
```

`WITHOUT ROWID` with that primary key makes the table its own clustered index:
one card's history for one provider/finish is physically contiguous, a lookup
is a single range scan, and there is no secondary index doubling the file size.
Column order in the key is `(card_id, src, day)` because every query is
"everything for this card," never "everything on this date."

Encodings:

- `src` = `provider_idx * 4 + finish_idx`, where `provider_idx` indexes
  `PROVIDERS = ("tcgplayer", "cardkingdom", "cardmarket", "manapool")` and
  `finish_idx` indexes `FINISHES = ("normal", "foil", "etched")`. The
  multiplier is 4, not 3, so a fourth finish can be added later without
  renumbering existing rows.
- `day` = days since the epoch `2020-01-01`, as an integer.
- `cents` = `int(round(float(price) * 100))`.
- `agg` = 0 for an observed daily price, 1 for a weekly mean.

Storing cents quantizes any MTGJSON price carrying more than two decimals.
This is a deliberate, accepted loss — these are retail currency amounts and
the UI formats every one of them to two decimals already (`{cur}{v:.2f}`).
It does mean the sidecar path and the legacy full-scan path can disagree in
the third decimal, which the equivalence test accounts for explicitly.

`meta` holds `schema_version` (starts at `1`), `built_at` (ISO timestamp, set
only on a successful build), and `daily_through` (the newest date applied).
`is_ready()` is `built_at` being present and `schema_version` matching.

Rows whose provider is not in `PROVIDERS` or whose finish is not in `FINISHES`
are skipped at ingest. Only `paper` prices are stored; `mtgo` is ignored, as
it is today.

### API

| Function | Purpose |
| --- | --- |
| `connect(path=None)` | Open with the appropriate PRAGMAs. |
| `build_from_allprices(path, gz_path)` | One-time full load. Returns rows written. |
| `apply_daily(path, gz_path)` | Nightly top-up. Returns rows written. |
| `series_for_uuids(path, uuids, providers=None)` | Yields `(uuid, date_iso, provider, finish, price_float)`. |
| `downsample(path, keep_daily_days=120)` | Collapse aged daily rows to weekly means. Returns rows deleted. |
| `is_ready(path)` | Built, non-corrupt, schema current. |
| `stats(path)` | Row counts, distinct cards, date span, file bytes, `built_at`. |

`series_for_uuids` yields tuples shaped exactly for
`watchlist_db.upsert_price(db, uuid, date, provider, finish, price)` so the
projection into `prices` is a plain loop with no adaptation layer.

### Build

Streams `AllPrices.json.gz` with `ijson.kvitems(f, "data")`, the same approach
`ingest_prices_file` uses today, so peak memory stays flat regardless of file
size. Differences from the current path:

- Writes into `<path>.part`, then `os.replace` onto the final name. A crashed
  or killed build never leaves a half-built sidecar where `is_ready()` could
  see it.
- `PRAGMA journal_mode=OFF`, `PRAGMA synchronous=OFF` while building the
  `.part` file — it is disposable until the rename. The live file uses WAL.
- Batches of 50,000 rows through `executemany`, not per-row `INSERT`.
- uuid → `card_id` assignment held in an in-memory dict during the build
  (~111k entries, a few MB) rather than a query per row.
- Checks for ~2.5 GB of free space on `DATA_DIR` before starting; logs and
  aborts cleanly if short, leaving `is_ready()` false.

Expect this to run on the order of 10–30 minutes. It happens once.

### Downsample

Runs once per nightly ingest, after `apply_daily`.

A week is eligible when its ISO-week Sunday is older than the cutoff
(`today - keep_daily_days`); since Sunday is the last day of an ISO week, that
guarantees the entire week sits behind the cutoff and no partial week is ever
collapsed. For each eligible `(card_id, src, ISO week)` group of `agg=0` rows,
write one row at that week's Sunday with `cents` = the rounded mean of the
group, `agg=1`, and delete the group's other rows.

Rows already marked `agg=1` are never re-read as input, so repeated runs are
idempotent and a mean is never taken of means. This is the invariant the
`agg` column exists to protect.

## Changes to `watchlist_ingest.py`

`PROVIDERS` gains `"manapool"`.

**`ensure_history`** — resolve names against `AllPrintings` as today, then:

1. If `is_ready()`, project the missing uuids out of the sidecar with
   `series_for_uuids` and `upsert_price` them into `prices`. Sub-second.
2. Otherwise fall back to the current behaviour exactly — download
   `AllPrices.json.gz` if absent, full `ingest_prices_file` scan — and
   schedule a sidecar build. The fallback is what guarantees this change can
   never make the product slower than it is today.

**`run_ingest`** — after the existing AllPrintings refresh and `resolve_watched`:

1. Download `AllPricesToday.json.gz` and `apply_daily()` it into the sidecar.
2. Project **only the dates just applied** for watched uuids into `prices`,
   rather than re-walking everything: the range from the previous
   `meta.daily_through` (exclusive) to the new one (inclusive), which covers
   the normal one-day case and also catches up correctly after the server has
   been down for several days. A newly added card gets its full history
   projected by `ensure_history` instead.
3. `downsample()`.
4. `notify_hits()`, unchanged.

The existing `_needs_backfill` full-`AllPrices` branch stays as the
sidecar-not-ready fallback.

This defuses, but does not fix, the perpetual-rescan pathology described
under Problem. A card MTGJSON has no prices for still never satisfies
`_ensure_history_for`, so every page load still triggers a fill — but with a
ready sidecar that fill is an indexed lookup returning nothing, costing
microseconds instead of minutes. Properly fixing it means distinguishing
"not fetched yet" from "known to have no prices," which is out of scope here.

**Dead code:** `backfill_cards` and `backfill_entry` are unreachable —
`_schedule_backfill(card_names)` (`server.py:5124`) accepts `card_names` and
never uses it, always calling `ensure_history`. Both functions do a full
`ingest_prices_file` pass anyway, so they are not a faster path worth wiring
up; the sidecar supersedes them. Delete both, and drop the unused
`card_names` parameter from `_schedule_backfill` along with the argument
passed at `server.py:5912`.

## Changes to `server.py`

- Schedule the sidecar build at startup when `is_ready()` is false, reusing
  the existing single-flight `_history_task` pattern. **Eagerly, not lazily**:
  the weekly tier only accumulates from the day the sidecar first exists, and
  anything older than the 90 days `AllPrices` carries at that moment can never
  be recovered. Delay is permanent data loss.
- `/health` reports `stats()` so drift and staleness are visible.
- `MYSTIC_FORGE_NO_SIDECAR` disables the sidecar entirely and forces the
  fallback path, mirroring the existing `MYSTIC_FORGE_NO_INGEST` escape hatch.
- manapool in the watchlist surfaces: `SHOPS` in `watchlist_pages.py:27`
  (USD, `$`), `shop_names` at `watchlist_pages.py:1005`, the CSV export header
  and rows at `server.py:5735-5754` (new `manapool_usd` column), and the
  `provider` field description at `server.py:5472`.

No release-gate impact: `check_release.py` compares tool names, and no tool is
added or renamed.

## Durability

Past ~90 days the sidecar stops being a cache and becomes the only copy of
that data — MTGJSON cannot replay it. It lives in `/data`, the persisted
`mystic_forge_data` volume, so restarts and redeploys are safe, but volume
loss is now data loss rather than a rebuild. It belongs in whatever backs up
`mystic_forge.db`. `stats()` on `/health` makes silent staleness visible.

## Error handling

- Failed build: `.part` removed, previous sidecar untouched, `is_ready()`
  false, fallback path serves.
- Corrupt sidecar (`sqlite3.DatabaseError` on open, or schema version
  mismatch): treated as not-ready; log and fall back. A rebuild can be forced
  by deleting the file.
- Any sidecar exception on the request path is caught and degrades to the
  existing behaviour. The sidecar must never be able to take a page down.
- `apply_daily` against a not-ready sidecar is a no-op returning 0.
- Disk full mid-build: caught, `.part` removed, logged.

## Testing

Extends `tests/test_watchlist_ingest.py` and adds
`tests/test_price_sidecar.py`, following the existing fixture style
(`make_prices_gz`, `make_allprintings`, tiny synthetic data).

- **Equivalence (the safety test):** on the same fixture, the rows
  `series_for_uuids` projects into `prices` match those `ingest_prices_file`
  writes directly, compared at cent precision (see the quantization note
  under Encodings). This is what proves the read path cannot regress. The
  fixture must include a price with three decimals so the quantization is
  exercised rather than accidentally avoided.
- Encoding round-trips: `src` packing for all provider/finish pairs, `day`
  offsets across a year boundary, cents rounding on prices like `0.005`.
- Unknown provider (e.g. a future MTGJSON addition) and `mtgo` blocks are
  skipped, not crashed on.
- Downsample: 130 days of daily data collapses only rows past the cutoff, one
  row per ISO week carrying the correct mean; a partial boundary week is left
  alone; a second run is a no-op; `agg=1` rows are never re-averaged.
- Atomicity: a build interrupted before rename leaves `is_ready()` false and
  the prior file intact.
- Fallback: with the sidecar absent, corrupt, and disabled by env var,
  `ensure_history` still fills `prices` by the old path.
- Memory ceiling: reuse the existing `tracemalloc` assertion so the build is
  proven to stay streaming.
- manapool: appears in `SHOPS`, the CSV export, and round-trips through
  ingest.

## Acceptance criteria

1. With a ready sidecar, adding a card fills 90+ days of history in under a
   second, with no MTGJSON file access.
2. With no sidecar, every current behaviour is preserved exactly.
3. A card whose history extends past 90 days renders daily points inside the
   120-day window and weekly points beyond it.
4. `downsample` run twice in a row produces no change on the second run.
5. Watchlist pages, and all existing tests, are untouched and passing.
6. manapool is selectable as a fourth shop and appears in the CSV export.
