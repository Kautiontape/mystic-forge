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
