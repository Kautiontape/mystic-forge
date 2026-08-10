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
    # A WAL-mode database is a three-file unit; os.replace is atomic for only
    # one of them. A stale -wal left by an unclean shutdown would be replayed
    # over the file we just built, silently resurrecting the old database.
    for suffix in ("-wal", "-shm"):
        stale = path + suffix
        if os.path.exists(stale):
            os.remove(stale)
    os.replace(part, path)
    log.info("sidecar built: %d cards, %d points", len(ids), rows)
    return rows


def apply_daily(path: str, gz_path: str) -> int:
    """Fold a day's AllPricesToday into a built sidecar. Returns points written.

    A no-op on an unbuilt sidecar: a partial file must never look like a
    complete one. New uuids are learned as they appear.

    Commits per batch rather than once at the end. The primary key orders
    points by (card_id, src, day), so one card's rows all live on roughly
    the same leaf page; folding in a single new day still touches nearly
    every leaf page in the file, and a single all-at-once transaction would
    let the WAL grow to roughly the size of the whole database. Committing
    incrementally bounds that.

    This trades away the all-or-nothing guarantee build_from_allprices has:
    a crash mid-apply can leave some of the day's rows committed and others
    not. What survives is that this call never advances daily_through() past
    a day it left incomplete -- the watermark write is the last statement --
    and INSERT OR REPLACE makes re-running this function over the *same*
    file converge to the complete, correct state.

    That guarantee is per-invocation, not global. If a crash is followed by
    a run that applies a *different* day, the watermark advances past the
    interrupted day and its missing rows are permanently lost, because
    AllPricesToday only ever carries the latest day. Nothing heals this
    short of deleting the sidecar to force a rebuild, and only inside
    MTGJSON's 90-day window. Callers must not read daily_through() as
    proof that every day beneath it is complete."""
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
                db.commit()
                rows += len(batch)
                batch.clear()
        if batch:
            db.executemany(
                "INSERT OR REPLACE INTO points (card_id,src,day,cents,agg)"
                " VALUES (?,?,?,?,0)", batch)
            db.commit()
            rows += len(batch)
        if newest is not None:
            prev = _get_meta(db, "daily_through")
            latest = from_day(newest)
            if prev is None or latest > prev:
                _set_meta(db, "daily_through", latest)
        db.commit()
        log.info("sidecar daily: %d points, %d cards", rows, len(ids))
        return rows
    finally:
        db.close()
