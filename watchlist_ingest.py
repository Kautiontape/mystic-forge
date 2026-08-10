"""MTGJSON ingest for the price watchlist.

Downloads are cached in DATA_DIR with ETag revalidation (meta table).
AllPrices/AllPricesToday are stream-parsed with ijson — the whole file is
never decoded into memory (spec acceptance criterion)."""

import gzip
import json
import logging
import os
import sqlite3
import time
from datetime import date

import httpx
import ijson

import price_sidecar
import watchlist_db

log = logging.getLogger("mystic_forge.ingest")

MTGJSON = "https://mtgjson.com/api/v5"
PROVIDERS = ("tcgplayer", "cardkingdom", "cardmarket", "manapool")
NTFY_BASE = os.environ.get("MYSTIC_FORGE_NTFY", "https://ntfy.sh")


def ntfy_topic(share_code: str) -> str:
    """Push topic per list. Derived from the share code — subscribing reveals
    only what the share code already grants (read access to buy windows)."""
    return f"mystic-forge-{share_code.lower()}"


def notify_hits(db) -> int:
    """ntfy push for cards that newly entered their buy window.

    State lives in meta (notified:<list_id>) so a hit is announced once, not
    nightly forever. Returns number of messages posted."""
    if os.environ.get("MYSTIC_FORGE_NTFY_OFF"):
        return 0
    sent = 0
    for lst in db.execute("SELECT * FROM lists WHERE superseded_by IS NULL"):
        # State tracks every entry at/below target — bought ones included, so
        # un-marking a purchase doesn't re-announce an old buy window.
        hits = []
        for e in watchlist_db.current_entries(db, lst["id"]):
            if e.get("target_price") is None:
                continue
            s = watchlist_db.entry_price_summary(db, e)
            if s and s["current"] <= e["target_price"]:
                hits.append((e["entry_id"], e["card_name"], s["current"],
                             e["target_price"], bool(e.get("bought_at"))))
        key = f"notified:{lst['id']}"
        prev = set(json.loads(_get_meta(db, key) or "[]"))
        fresh = [h for h in hits if h[0] not in prev and not h[4]]
        if fresh:
            body = " · ".join(f"{n} ${c:.2f} (target ${t:.2f})"
                              for _, n, c, t, _b in fresh)
            try:
                httpx.post(
                    f"{NTFY_BASE}/{ntfy_topic(lst['share_code'])}",
                    content=f"Buy window: {body}",
                    headers={"Title": lst["label"] or "Mystic Forge watchlist",
                             "Tags": "dart"},
                    timeout=10.0)
                sent += 1
            except Exception:
                log.exception("ntfy push failed for list %s", lst["id"])
        _set_meta(db, key, json.dumps([h[0] for h in hits]))
    return sent


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


def ingest_prices_file(db, gz_path: str, only_uuids=None) -> int:
    """Stream a MTGJSON AllPrices/AllPricesToday .gz; upsert watched uuids only
    (or a narrower explicit set for targeted backfills).

    Shape: data.<uuid>.paper.<provider>.retail.<finish>.<date> = price"""
    watched = set(only_uuids) if only_uuids is not None else watched_uuids(db)
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


def _sidecar_path(data_dir: str | None = None) -> str:
    """Where the price sidecar lives. Computed here, not in price_sidecar, so
    that module stays free of any import back into this one."""
    return os.path.join(data_dir or _data_dir(), "price_sidecar.sqlite")


def _project(db, sidecar_path: str, uuids, since: str | None = None) -> int:
    """Copy sidecar history for `uuids` into the main database's prices table.

    The one-way seam: the sidecar is the only writer of `prices`, and nothing
    downstream of `prices` knows the sidecar exists.

    Writes with commit=False and commits once, so a failure part-way leaves
    the transaction uncommitted and `prices` untouched — which is what lets
    callers treat a raised exception as "nothing happened" and fall back."""
    n = 0
    for uuid, d, provider, finish, price in price_sidecar.series_for_uuids(
            sidecar_path, uuids, since=since):
        watchlist_db.upsert_price(db, uuid, d, provider, finish, price,
                                  commit=False)
        n += 1
    db.commit()
    return n


def ensure_history(db_path: str, data_dir: str | None = None) -> int:
    """Guarantee every watched card has its price history.

    How much history, and what it costs, depends on the sidecar: a ready one
    supplies 120 days of dailies plus weekly means beyond that, as an indexed
    read with no download. Without it this falls back to the original path —
    MTGJSON's ~90 days, via a streaming scan of AllPrices, downloading it and
    AllPrintings if they aren't cached yet.

    This is the on-demand path: history is a static backfill, so it must never
    wait for the nightly cycle. Idempotent and self-coalescing — it works on
    "every watched uuid with no prices", so simultaneous adds collapse into one
    pass. Returns price rows written."""
    data_dir = data_dir or _data_dir()
    os.makedirs(data_dir, exist_ok=True)
    db = watchlist_db.connect(db_path)
    try:
        watchlist_db.init_db(db)
        if not db.execute("SELECT 1 FROM watchlist_current LIMIT 1").fetchone():
            return 0
        ap_path = os.path.join(data_dir, "AllPrintings.sqlite")
        if _needs_unresolved(db) and not os.path.exists(ap_path):
            log.info("bootstrap: fetching AllPrintings")
            _download(f"{MTGJSON}/AllPrintings.sqlite", ap_path, db)
        if os.path.exists(ap_path):
            resolve_watched(db, ap_path)
        missing = {r["uuid"] for r in db.execute(
            """SELECT cu.uuid FROM watchlist_current wc
               JOIN card_uuids cu ON LOWER(cu.card_name)=LOWER(wc.card_name)
               WHERE NOT EXISTS (SELECT 1 FROM prices p WHERE p.uuid=cu.uuid)""")}
        if not missing:
            return 0
        # is_ready() only proves the file opens; the read that follows can
        # still fail. Catching it here is what keeps a sick sidecar from being
        # worse than no sidecar: the caller only logs, so an escaping
        # exception would leave `missing` missing and re-fail on every page
        # load forever, instead of falling through to the scan below.
        side = _sidecar_path(data_dir)
        if price_sidecar.is_ready(side):
            try:
                n = _project(db, side, missing)
            except Exception:
                # The scan below runs on this same connection and ends in its
                # own commit, which would otherwise persist whatever rows the
                # failed projection had already staged.
                db.rollback()
                log.exception("sidecar projection failed; falling back to scan")
            else:
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
    finally:
        db.close()


def backfill_cards(db_path: str, data_dir: str | None = None,
                   card_names=None) -> int:
    """Instant history for just-added cards, entirely from the nightly-cached
    MTGJSON files — no downloads. Resolves names against the local
    AllPrintings, then makes ONE streaming pass over the cached AllPrices for
    the union of their uuids (a bulk add costs the same scan as a single add).
    Returns price rows written (0 when nothing is cached yet — the nightly
    ingest covers that case)."""
    names = [card_names] if isinstance(card_names, str) else list(card_names or [])
    if not names:
        return 0
    data_dir = data_dir or _data_dir()
    db = watchlist_db.connect(db_path)
    try:
        watchlist_db.init_db(db)
        ap = os.path.join(data_dir, "AllPrintings.sqlite")
        if os.path.exists(ap):
            resolve_watched(db, ap)
        marks = ",".join("?" * len(names))
        uuids = {r["uuid"] for r in db.execute(
            f"SELECT uuid FROM card_uuids WHERE LOWER(card_name) IN ({marks})",
            [n.lower() for n in names])}
        allp = os.path.join(data_dir, "AllPrices.json.gz")
        if not uuids or not os.path.exists(allp):
            return 0
        n = ingest_prices_file(db, allp, only_uuids=uuids)
        log.info("instant backfill (%d card(s)): %d rows", len(names), n)
        return n
    finally:
        db.close()


def backfill_entry(db_path: str, data_dir: str | None = None,
                   card_name: str | None = None) -> int:
    """Single-card convenience wrapper around backfill_cards."""
    return backfill_cards(db_path, data_dir, [card_name] if card_name else [])


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
            # Nothing watched: do NOT stamp last_ingest, or the first cards
            # added today would wait until tomorrow for any prices.
            return
        ap_path = os.path.join(data_dir, "AllPrintings.sqlite")
        week_old = (not os.path.exists(ap_path)
                    or time.time() - os.path.getmtime(ap_path) > 7 * 86400)
        if week_old or _needs_unresolved(db):
            ap_path = _download(f"{MTGJSON}/AllPrintings.sqlite", ap_path, db)
        resolve_watched(db, ap_path)
        # Read readiness once and branch on the same answer twice, so the two
        # gates below can never disagree and drop the backfill entirely.
        side = _sidecar_path(data_dir)
        ready = price_sidecar.is_ready(side)
        # _needs_backfill alone must NOT gate this. It asks "any watched uuid
        # with no price rows at all?", and a printing MTGJSON simply never
        # prices — a promo, a token — answers yes tonight, tomorrow night and
        # forever, so on that condition alone this downloads and full-scans
        # 1.4 GB every night to accomplish nothing. A ready sidecar holds
        # every card MTGJSON prices, so the projection below covers whatever
        # is missing; the scan stays only as the sidecar-not-ready fallback.
        if not ready and _needs_backfill(db):
            p = _download(f"{MTGJSON}/AllPrices.json.gz",
                          os.path.join(data_dir, "AllPrices.json.gz"), db)
            n = ingest_prices_file(db, p)
            log.info("backfill: %d rows", n)
        p = _download(f"{MTGJSON}/AllPricesToday.json.gz",
                      os.path.join(data_dir, "AllPricesToday.json.gz"), db)
        if ready:
            previous = price_sidecar.daily_through(side)
            # apply, then project, then downsample — the order is load-bearing
            # and must not be reshuffled. Downsampling before the projection
            # would collapse aged dailies into weekly means first, and the
            # projection would then write those means into `prices` as if they
            # were daily observations of a watched card.
            price_sidecar.apply_daily(side, p)
            # Project only what is new. `previous` is exclusive, so a server
            # that was down for several days catches up in one pass.
            # since= is exclusive, so step back a day: re-projecting the
            # watermark date is what catches a same-date revision, which would
            # otherwise land in the sidecar and never reach `prices`.
            # upsert_price is an INSERT OR REPLACE, so the overlap is
            # idempotent and costs one day's rows for watched uuids.
            floor = (price_sidecar.from_day(price_sidecar.to_day(previous) - 1)
                     if previous is not None else None)
            n = _project(db, side, watched_uuids(db), since=floor)
            try:
                price_sidecar.downsample(side)
            except Exception:
                # Retention is housekeeping and the day's prices have already
                # landed. Letting this escape would skip the last_ingest stamp
                # below, so /health would report ingest_stale for a night that
                # actually succeeded.
                log.exception("sidecar downsample failed; the day's prices "
                              "are already applied and projected")
        else:
            n = ingest_prices_file(db, p)
        log.info("daily: %d rows", n)
        _set_meta(db, "last_ingest", date.today().isoformat())
        notify_hits(db)
    finally:
        db.close()
