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
# Must stay comfortably above MTGJSON's ~90-day AllPrices window. That gap is
# the only reason an ingested row can never be older than downsample's cutoff.
# Lower this below ~90 -- or point apply_daily at a full AllPrices rather than
# AllPricesToday -- and daily rows start landing in weeks that have already
# collapsed, where downsample can only discard them (see there).
BATCH = 50_000
DOWNSAMPLE_CARDS = 2_000  # card_ids per downsample transaction; bounds the WAL
BUILD_HEADROOM = 2_500_000_000     # bytes of free space a full build needs
READ_CHUNK = 500          # uuids per read query; well under SQLite's 32766 parameter cap

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


def unpack_src(src: int):
    """Decode a packed src, or None if it names a slot this build reserves.

    Mirrors pack_src, which already returns None for unknowns. A newer build
    may write a provider or finish this one has no name for; reading such a
    row must skip it, never raise."""
    p, f = divmod(src, FINISH_SLOTS)
    if p >= len(PROVIDERS) or f >= len(FINISHES):
        return None
    return PROVIDERS[p], FINISHES[f]


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
        batch, rows, newest, oldest = [], 0, None, None
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
            oldest = day if oldest is None else min(oldest, day)
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
        # Detecting this in downsample would cost a second full scan and could
        # only report "some rows were dropped"; here every day is already in
        # hand, so it costs one min() per point and names the offending date.
        if (oldest is not None and newest is not None
                and oldest < newest - KEEP_DAILY_DAYS):
            log.warning(
                "sidecar daily: %s is older than the %d-day daily window "
                "ending %s -- downsample will discard points landing in weeks "
                "it has already collapsed. KEEP_DAILY_DAYS must stay above the "
                "span this file covers.",
                from_day(oldest), KEEP_DAILY_DAYS, from_day(newest))
        db.commit()
        log.info("sidecar daily: %d points, %d cards", rows, len(ids))
        return rows
    finally:
        db.close()


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def series_for_uuids(path: str, uuids, providers=None, since: str | None = None):
    """Yield (uuid, date_iso, provider, finish, price) for the given uuids.

    Shaped exactly for watchlist_db.upsert_price, so the projection into the
    main database needs no adaptation layer. `since` is exclusive. Yields
    nothing at all when the sidecar is not ready — callers fall back.

    Holds a read connection to `path` open for as long as the generator is
    alive; callers should consume it fully (a full `for` loop, or `list(...)`)
    rather than abandoning it partway with a `break`."""
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
        for chunk in _chunks(uuids, READ_CHUNK):
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
                pair = unpack_src(r["src"])
                if pair is None:
                    continue
                provider, finish = pair
                yield (r["uuid"], from_day(r["day"]), provider, finish,
                       from_cents(r["cents"]))
    finally:
        db.close()


def week_anchor(day: int) -> int:
    """Day-offset of the Sunday ending `day`'s ISO week.

    EPOCH is a Wednesday, which is where the + 2 comes from.

    Kept in lockstep with _ANCHOR_SQL, which carries an extra `+ 7) % 7` this
    does not need: Python's % floors, SQLite's truncates toward zero, so the
    two forms agree only while `day + 2` is non-negative. A pre-EPOCH date --
    which nothing in the ingest path rejects, and which to_day happily turns
    into a negative offset -- makes the un-normalized SQL land a whole week
    late while still falling on a Sunday, so nothing about the result looks
    wrong. Normalizing the SQL is what "lockstep" means here."""
    return day + 6 - ((day + 2) % 7)


_ANCHOR_SQL = "(day + 6 - (((day + 2) % 7 + 7) % 7))"


def downsample(path: str, keep_daily_days: int = KEEP_DAILY_DAYS,
               today: str | None = None) -> int:
    """Collapse daily rows older than the window into weekly means.

    Returns rows deleted. Only whole weeks are collapsed: the anchor is the
    week's last day, so anchor < cutoff proves every day of that week is
    behind the cutoff. Only agg=0 rows are read as input, so a mean is never
    taken of means and repeat runs are no-ops.

    A week that already carries an agg=1 row is skipped entirely, so a late
    daily row can never overwrite an established weekly mean with itself.
    That late row is still deleted along with the rest of its cohort.
    Reweighting the stored mean to absorb it is not merely expensive but
    unsound: that needs to know whether the day is already inside the mean,
    which a count cannot answer -- re-applying an already-included day would
    double-count it -- and knowing it would mean storing the set of included
    days, which defeats the compression. Leaving the row instead would strand
    an agg=0 point behind the cutoff permanently. Discarding one late day is
    the smaller loss. apply_daily warns when it sees such a day, which is
    where the condition is detectable for free.

    Wall-clock time bounds the cutoff only as far as the data allows: see the
    watermark check below. Passing today= is a test seam and bypasses it.

    Work is committed per DOWNSAMPLE_CARDS-wide card_id range rather than all
    at once. Rows behind the cutoff are scattered across nearly every leaf
    page, so a single transaction copies essentially the whole database into
    the WAL -- measured at 100% of file size for one week's worth of rows.
    Chunking trades this function's all-or-nothing guarantee for a bounded
    WAL. That is safe because each card is collapsed independently of every
    other: the aggregate groups by card_id and the guard above is correlated
    on it, so a crash leaves whole cards done and the rest untouched, and the
    next run finishes them without computing any value differently."""
    if not is_ready(path):
        return 0
    db = connect(path)
    try:
        if today:
            ref = _date.fromisoformat(today)
        else:
            ref = datetime.now(timezone.utc).date()
            # The wall clock is an untrusted input, and this is the only
            # irreversible operation in the module. A container without NTP,
            # an NTP step after a long drift, a VM restored from a snapshot
            # and a dead RTC battery all present as a jump forward, and one
            # run with a fast clock collapses the entire daily window --
            # unrecoverably, since past ~90 days MTGJSON cannot replay it.
            # daily_through is what the data itself says, so let it bound the
            # cutoff: a clock jump becomes a no-op, and a long ingest outage
            # stops collapsing rather than racing ahead of the data.
            watermark = _get_meta(db, "daily_through")
            if watermark:
                ref = min(ref, _date.fromisoformat(watermark))
        cutoff = (ref - timedelta(days=keep_daily_days) - EPOCH).days
        # Two queries, not one: SQLite optimizes a lone MIN() or MAX() over the
        # leading primary-key column into a seek, but MIN(x), MAX(x) together
        # into a full table scan.
        lo = db.execute("SELECT MIN(card_id) FROM points").fetchone()[0]
        hi = db.execute("SELECT MAX(card_id) FROM points").fetchone()[0]
        if lo is None:
            return 0
        deleted = 0
        for start in range(lo, hi + 1, DOWNSAMPLE_CARDS):
            end = min(start + DOWNSAMPLE_CARDS - 1, hi)
            # INSERT before DELETE: the Sunday is itself part of its group, so
            # this overwrites it with the agg=1 mean and the DELETE then skips
            # it, because it is no longer agg=0.
            db.execute(
                f"""INSERT OR REPLACE INTO points (card_id, src, day, cents, agg)
                    SELECT card_id, src, {_ANCHOR_SQL} AS anchor,
                           CAST(ROUND(AVG(cents)) AS INTEGER), 1
                    FROM points
                    WHERE agg = 0 AND card_id BETWEEN ? AND ?
                      AND {_ANCHOR_SQL} < ?
                    GROUP BY card_id, src, anchor
                    HAVING NOT EXISTS (
                        SELECT 1 FROM points w
                        WHERE w.card_id = points.card_id
                          AND w.src = points.src
                          AND w.day = anchor AND w.agg = 1)""",
                (start, end, cutoff))
            cur = db.execute(
                f"DELETE FROM points WHERE agg = 0 AND card_id BETWEEN ? AND ?"
                f" AND {_ANCHOR_SQL} < ?", (start, end, cutoff))
            deleted += cur.rowcount
            db.commit()
        if deleted:
            log.info("sidecar downsample: %d daily rows collapsed", deleted)
        return deleted
    finally:
        db.close()
