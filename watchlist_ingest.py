"""MTGJSON ingest for the price watchlist.

Downloads are cached in DATA_DIR with ETag revalidation (meta table).
AllPrices/AllPricesToday are stream-parsed with ijson — the whole file is
never decoded into memory (spec acceptance criterion)."""

import gzip
import logging
import os
import sqlite3
import time
from datetime import date

import httpx
import ijson

import watchlist_db

log = logging.getLogger("mystic_forge.ingest")

MTGJSON = "https://mtgjson.com/api/v5"
PROVIDERS = ("tcgplayer", "cardkingdom", "cardmarket")


def _data_dir() -> str:
    return os.environ.get(
        "MYSTIC_FORGE_DATA",
        os.path.dirname(os.path.abspath(watchlist_db.DB_PATH)))


def _get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
               (key, value))
    db.commit()


def _download(url: str, dest: str, db) -> str:
    """Download url to dest unless the cached copy's ETag still matches."""
    etag_key = f"etag:{url}"
    headers = {}
    old_etag = _get_meta(db, etag_key)
    if old_etag and os.path.exists(dest):
        headers["If-None-Match"] = old_etag
    with httpx.stream("GET", url, headers=headers, timeout=300.0,
                      follow_redirects=True) as resp:
        if resp.status_code == 304:
            return dest
        resp.raise_for_status()
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
        os.replace(tmp, dest)
        if resp.headers.get("etag"):
            _set_meta(db, etag_key, resp.headers["etag"])
    return dest


def resolve_watched(db, allprintings_path: str) -> int:
    """Fill card_uuids for watched names that lack resolution. Returns rows added."""
    pending = db.execute(
        """SELECT DISTINCT card_name FROM watchlist_current wc
           WHERE NOT EXISTS (SELECT 1 FROM card_uuids cu
                             WHERE LOWER(cu.card_name)=LOWER(wc.card_name))"""
    ).fetchall()
    if not pending:
        return 0
    ap = sqlite3.connect(allprintings_path)
    ap.row_factory = sqlite3.Row
    added = 0
    for row in pending:
        # scryfallId lives in cardIdentifiers, not cards (real MTGJSON schema)
        for c in ap.execute(
                "SELECT c.name, c.uuid, c.setCode, c.number, ci.scryfallId"
                " FROM cards c LEFT JOIN cardIdentifiers ci ON ci.uuid = c.uuid"
                " WHERE LOWER(c.name)=LOWER(?)", (row["card_name"],)):
            db.execute(
                "INSERT OR IGNORE INTO card_uuids (card_name, uuid, set_code,"
                " collector_number, scryfall_id) VALUES (?,?,?,?,?)",
                (c["name"], c["uuid"], c["setCode"], c["number"],
                 c["scryfallId"]))
            added += 1
    ap.close()
    db.commit()
    return added


def watched_uuids(db) -> set[str]:
    return {r["uuid"] for r in db.execute(
        """SELECT DISTINCT cu.uuid FROM watchlist_current wc
           JOIN card_uuids cu ON LOWER(cu.card_name)=LOWER(wc.card_name)""")}


def ingest_prices_file(db, gz_path: str) -> int:
    """Stream a MTGJSON AllPrices/AllPricesToday .gz; upsert watched uuids only.

    Shape: data.<uuid>.paper.<provider>.retail.<finish>.<date> = price"""
    watched = watched_uuids(db)
    rows = 0
    with gzip.open(gz_path, "rb") as f:
        for uuid, obj in ijson.kvitems(f, "data"):
            if uuid not in watched:
                continue
            paper = (obj or {}).get("paper", {})
            for provider in PROVIDERS:
                retail = (paper.get(provider) or {}).get("retail", {})
                for finish, series in retail.items():
                    for d, price in (series or {}).items():
                        watchlist_db.upsert_price(db, uuid, d, provider, finish,
                                                  float(price), commit=False)
                        rows += 1
    db.commit()
    return rows


def _needs_backfill(db) -> bool:
    """Any watched uuid with no price rows at all?"""
    return db.execute(
        """SELECT 1 FROM watchlist_current wc
           JOIN card_uuids cu ON LOWER(cu.card_name)=LOWER(wc.card_name)
           WHERE NOT EXISTS (SELECT 1 FROM prices p WHERE p.uuid=cu.uuid)
           LIMIT 1""").fetchone() is not None


def _needs_unresolved(db) -> bool:
    return db.execute(
        """SELECT 1 FROM watchlist_current wc
           WHERE NOT EXISTS (SELECT 1 FROM card_uuids cu
                             WHERE LOWER(cu.card_name)=LOWER(wc.card_name))
           LIMIT 1""").fetchone() is not None


def run_ingest(db_path: str, data_dir: str | None = None) -> None:
    """Synchronous nightly ingest (call via asyncio.to_thread). Idempotent."""
    data_dir = data_dir or _data_dir()
    os.makedirs(data_dir, exist_ok=True)
    db = watchlist_db.connect(db_path)
    try:
        watchlist_db.init_db(db)
        if not db.execute("SELECT 1 FROM watchlist_current LIMIT 1").fetchone():
            _set_meta(db, "last_ingest", date.today().isoformat())
            return
        ap_path = os.path.join(data_dir, "AllPrintings.sqlite")
        week_old = (not os.path.exists(ap_path)
                    or time.time() - os.path.getmtime(ap_path) > 7 * 86400)
        if week_old or _needs_unresolved(db):
            ap_path = _download(f"{MTGJSON}/AllPrintings.sqlite", ap_path, db)
        resolve_watched(db, ap_path)
        if _needs_backfill(db):
            p = _download(f"{MTGJSON}/AllPrices.json.gz",
                          os.path.join(data_dir, "AllPrices.json.gz"), db)
            n = ingest_prices_file(db, p)
            log.info("backfill: %d rows", n)
        p = _download(f"{MTGJSON}/AllPricesToday.json.gz",
                      os.path.join(data_dir, "AllPricesToday.json.gz"), db)
        n = ingest_prices_file(db, p)
        log.info("daily: %d rows", n)
        _set_meta(db, "last_ingest", date.today().isoformat())
    finally:
        db.close()
