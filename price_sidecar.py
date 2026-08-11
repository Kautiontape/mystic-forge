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
import pathlib
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
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
STALE_PART_SECONDS = 6 * 3600
# How old an abandoned .part sibling must be before a build deletes it. A full
# build takes 10-30 minutes, so anything younger than this may well belong to a
# live process; deleting that is the very hazard per-pid part names exist to
# avoid (see build_from_allprices).
SUPERSEDED_SUFFIX = ".superseded"
# Where a rebuild parks the sidecar it is replacing (see _supersede).

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
    """Open the sidecar read-write.

    Warning: this creates an empty database if `path` does not exist, and an
    empty database is not a sidecar -- is_ready() reports it `corrupt`. Every
    caller inside this module gates on is_ready() first; anything outside it
    should do the same rather than relying on the open to fail."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def pack_src(provider: str, finish: str) -> int | None:
    """Packed provider/finish code, or None if either is unknown upstream."""
    p = _PROV_IDX.get(provider)
    f = _FIN_IDX.get(finish)
    if p is None or f is None:
        return None
    return p * FINISH_SLOTS + f


def unpack_src(src: int) -> tuple[str, str] | None:
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
    """Quantize to cents. Deliberately lossy past two decimals — see the spec.

    Python's round() is half-to-even; the ROUND() downsample uses to average a
    week is SQLite's, which is half-away-from-zero. The two therefore disagree
    by one cent on a mean landing exactly on .5 -- unreachable for a whole
    seven-day week, reachable for a partial boundary week. Left alone
    deliberately: it is inside the quantization loss the spec already accepts,
    and unifying it would mean either rounding weekly means in Python (a row
    at a time, over tens of millions) or reimplementing banker's rounding in
    SQL."""
    return int(round(float(price) * 100))


def from_cents(cents: int) -> float:
    return cents / 100.0


def _get_meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(db: sqlite3.Connection, key: str, value) -> None:
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
               (key, str(value)))


def _readiness(path: str) -> str | None:
    """None when the sidecar is usable, else why not.

    One of "disabled" (the env-var escape hatch), "absent" (no completed
    build at that path -- either no file, or one carrying no built_at),
    "corrupt" (it will not open, or holds no meta table), or "schema" (a
    complete build of a version this code does not read).

    Corrupt and schema warn; absent and disabled do not. The distinction is
    the point: the system's response to not-ready is a full rebuild, which
    permanently discards the weekly tier -- and past ~90 days MTGJSON cannot
    replay it. A first boot losing nothing and a SCHEMA_VERSION bump throwing
    away years of weekly means must not look alike in the log."""
    if os.environ.get("MYSTIC_FORGE_NO_SIDECAR"):
        return "disabled"
    if not path or not os.path.exists(path):
        return "absent"
    try:
        # as_uri() percent-encodes; interpolating the raw path would let a `?`
        # or `#` in it truncate the URI, dropping mode=ro and creating a stray
        # database at the truncated name.
        uri = pathlib.Path(path).absolute().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        db.row_factory = sqlite3.Row
        try:
            version = _get_meta(db, "schema_version")
            built = _get_meta(db, "built_at")
        finally:
            db.close()
    except sqlite3.DatabaseError as e:
        log.warning("sidecar at %s will not open (%s); falling back to the "
                    "full-scan path. Delete it to force a rebuild -- note "
                    "that a rebuild recovers only MTGJSON's ~90-day window, "
                    "not the weekly tier.", path, e)
        return "corrupt"
    if not built:
        return "absent"
    if version != str(SCHEMA_VERSION):
        log.warning("sidecar at %s was built for schema version %s, but this "
                    "build reads version %s; treating it as not ready. "
                    "Rebuilding discards every weekly mean it holds, which "
                    "MTGJSON cannot replay.", path, version, SCHEMA_VERSION)
        return "schema"
    return None


def is_ready(path: str) -> bool:
    """A completed build of the current schema, openable and uncorrupted."""
    return _readiness(path) is None


def daily_through(path: str) -> str | None:
    """Newest date applied, as an ISO string, or None."""
    if not is_ready(path):
        return None
    db = connect(path)
    try:
        return _get_meta(db, "daily_through")
    finally:
        db.close()


def _iter_points(gz_path: str,
                 counts: dict | None = None) -> Iterator[tuple[str, int, int, int]]:
    """Yield (uuid, src, day, cents) from an MTGJSON prices .gz.

    Shape: data.<uuid>.paper.<provider>.retail.<finish>.<date> = price.
    Streams with ijson, so peak memory is independent of file size.

    Tolerance is narrow and deliberate. An unknown provider or finish is
    skipped, and so is an unparseable date or price -- MTGJSON adds providers
    without warning, and one bad value must not lose a night's ingest. A
    structurally wrong document is not covered: `{"paper": "oops"}` raises,
    because a shape change that broad means the assumptions behind every
    other line here no longer hold and failing loudly is the safer answer.

    `counts`, if given, is updated in place: counts["kept"][src] += 1 per
    point yielded (keyed by the packed src), and
    counts["skipped"][(provider, finish)] += len(series) for every
    provider/finish pack_src does not recognize. Plain dict/Counter
    increments only, bounded by the number of distinct provider/finish
    pairs — this runs inside a loop over tens of millions of points."""
    with gzip.open(gz_path, "rb") as f:
        for uuid, obj in ijson.kvitems(f, "data"):
            paper = (obj or {}).get("paper") or {}
            for provider, pdata in paper.items():
                retail = (pdata or {}).get("retail") or {}
                for finish, series in retail.items():
                    src = pack_src(provider, finish)
                    if src is None:
                        if counts is not None:
                            counts["skipped"][(provider, finish)] += \
                                len(series or {})
                        continue
                    kept = counts["kept"] if counts is not None else None
                    for d, price in (series or {}).items():
                        try:
                            day, cents = to_day(d), to_cents(price)
                        except (ValueError, TypeError):
                            continue
                        if kept is not None:
                            kept[src] += 1
                        yield uuid, src, day, cents


def _free_bytes(directory: str) -> int:
    st = os.statvfs(directory)
    return st.f_bavail * st.f_frsize


def _next_card_id(ids: dict[str, int]) -> int:
    """First free card_id, given the uuid -> card_id map already in hand.

    max()+1, never len()+1: a hole in the sequence -- a deleted card, or any
    future build that resumes rather than starting from empty -- makes
    len()+1 collide with a live id. build_from_allprices starts from an empty
    dict, where the two agree, so sharing this is alignment against that
    future hazard rather than a fix for a live bug. Computed once and
    incremented by the caller, so it stays O(1) per new uuid across the ~111k
    of them a full build sees."""
    return max(ids.values(), default=0) + 1


def _sweep_stale_parts(path: str, directory: str) -> None:
    """Delete abandoned .part siblings, leaving anything recent alone.

    A build that is killed leaks its part file; without this they accumulate
    at roughly a gigabyte each. Age is the only signal available here -- there
    is no lock and no registry of live builds -- so the threshold is set well
    past a build's runtime and nothing younger is touched."""
    prefix = os.path.basename(path) + ".part."
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        stale = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(stale) > STALE_PART_SECONDS:
                os.remove(stale)
                log.info("sidecar: removed abandoned build file %s", stale)
        except OSError:
            continue


def _supersede(path: str) -> None:
    """Move any existing sidecar aside, and clear the way for os.replace.

    A rebuild is triggered by is_ready() being false, and that is also the
    answer for a file that is merely corrupt or built for another
    SCHEMA_VERSION. Past ~90 days this file holds the only copy of its
    history -- MTGJSON cannot replay it -- so publishing over it would turn a
    transient corruption or a mistaken version bump into the permanent loss of
    every weekly mean. A rename costs one inode and makes it recoverable by
    hand; the operator decides what to do with the copy, which is a decision
    this code is in no position to make for them.

    Only the most recent copy is kept: os.replace overwrites the previous one
    rather than stacking gigabyte-sized generations until the volume fills.

    The -wal/-shm companions move with the file they belong to. Deleting
    them -- all the swap had to do when it only needed them out of
    os.replace's way -- would strip every un-checkpointed transaction off the
    copy being preserved, and a backup that will not open is not a backup. Any
    companion the moved file does not have is deleted at the destination, or
    the previous generation's -wal would be replayed into this one.

    Best-effort, not atomic: no filesystem can rename three files as a unit,
    so a crash mid-way can leave the copy without a companion. The live path
    is unaffected either way -- os.replace below is still the only thing that
    publishes."""
    if not os.path.exists(path):
        for suffix in ("-wal", "-shm"):    # orphans; nothing to preserve
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        return
    dest = path + SUPERSEDED_SUFFIX
    os.replace(path, dest)
    for suffix in ("-wal", "-shm"):
        src, old = path + suffix, dest + suffix
        if os.path.exists(src):
            os.replace(src, old)
        elif os.path.exists(old):
            os.remove(old)
    log.warning("sidecar: kept the previous %s as %s before rebuilding. Past "
                "MTGJSON's ~90-day window that copy is the only record of its "
                "weekly tier; verify the new build before deleting it.",
                path, dest)


def build_from_allprices(path: str, gz_path: str) -> int:
    """Full load from AllPrices.json.gz. Returns points written.

    Builds into a private <path>.part.<pid> and atomically renames, so an
    interrupted build can never leave a file that is_ready() would accept. Any
    sidecar already at `path` is moved aside, not overwritten -- see
    _supersede, which is where the reasoning about that lives.

    The pid in that name is what keeps two concurrent builds from destroying
    each other. With one shared `.part`, B's cleanup unlinks A's in-progress
    file, B builds a fresh one under the same name, and A's os.replace then
    publishes B's half-finished database as ready -- reproduced. Per-build
    names make the two independent; each still races only on the final
    os.replace, where last writer wins and every candidate is complete.

    Known limitation: there is no cross-process mutual exclusion between
    build, apply_daily and downsample. Nothing stops a nightly apply_daily
    from running against a sidecar a second process is replacing underneath
    it. An advisory lock over the whole file is the real answer and is a
    larger design change than this."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # Sweep before the headroom check, not after: a leaked part file is about
    # the size of a whole sidecar, so two of them can hold the free-space
    # check below permanently short of the space they are themselves wasting.
    _sweep_stale_parts(path, directory)
    if _free_bytes(directory) < BUILD_HEADROOM:
        raise OSError(f"not enough free space in {directory} to build the "
                      f"sidecar ({BUILD_HEADROOM} bytes needed)")
    part = f"{path}.part.{os.getpid()}"
    if os.path.exists(part):
        os.remove(part)
    db = sqlite3.connect(part)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(SCHEMA)
        db.execute("PRAGMA journal_mode=OFF")     # disposable until the rename
        db.execute("PRAGMA synchronous=OFF")
        ids: dict[str, int] = {}
        next_id = _next_card_id(ids)
        batch: list[tuple] = []
        rows = 0
        counts = {"kept": Counter(), "skipped": Counter()}
        for uuid, src, day, cents in _iter_points(gz_path, counts):
            cid = ids.get(uuid)
            if cid is None:
                cid = next_id
                next_id += 1
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
    # one of them. Whatever is at `path` has to be cleared out of the way first
    # -- a surviving -wal would otherwise be replayed over the file we just
    # built, silently resurrecting the old database -- and _supersede does that
    # by moving the whole unit aside rather than deleting it.
    _supersede(path)
    os.replace(part, path)
    log.info("sidecar built: %d cards, %d points; kept %s; skipped %s",
              len(ids), rows,
              {unpack_src(s): n for s, n in counts["kept"].items()},
              dict(counts["skipped"]))
    return rows


_APPLY_SQL = (
    "INSERT INTO points (card_id,src,day,cents,agg) VALUES (?,?,?,?,0)"
    " ON CONFLICT(card_id,src,day) DO UPDATE SET cents=excluded.cents"
    " WHERE points.agg = 0")
# Not INSERT OR REPLACE. A weekly mean is stored at its week's Sunday, so a
# late daily row for that Sunday collides with it on the primary key -- and a
# REPLACE would overwrite the mean before downsample's guard could protect it,
# after which the next downsample relabels the survivor agg=1 and the loss
# becomes undetectable. The WHERE clause makes an agg=1 row untouchable while
# still letting a revised price land on an existing agg=0 day.


def apply_daily(path: str, gz_path: str) -> int:
    """Fold a day's AllPricesToday into a built sidecar. Returns points written.

    A no-op on an unbuilt sidecar: a partial file must never look like a
    complete one. New uuids are learned as they appear.

    "Points written" counts points read out of the file, which is one more
    than the rows that change whenever a point is dropped for colliding with
    an established weekly mean. Restating it exactly would cost a per-row
    changes() call across ~600k rows a night to report a number nothing
    branches on.

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
    and the upsert below makes re-running this function over the *same* file
    converge to the complete, correct state.

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
        next_id = _next_card_id(ids)
        batch, rows, newest, oldest = [], 0, None, None
        counts = {"kept": Counter(), "skipped": Counter()}
        for uuid, src, day, cents in _iter_points(gz_path, counts):
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
                db.executemany(_APPLY_SQL, batch)
                db.commit()
                rows += len(batch)
                batch.clear()
        if batch:
            db.executemany(_APPLY_SQL, batch)
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
        # build_from_allprices runs once, possibly years before this does;
        # this is the path that sees a provider MTGJSON added in between, and
        # dropping one silently every night forever is the failure to catch.
        if counts["skipped"]:
            log.warning(
                "sidecar daily: skipped %s -- provider/finish pairs this "
                "build has no slot for. Add them to PROVIDERS/FINISHES or "
                "these points are dropped every night.",
                dict(counts["skipped"]))
        db.commit()
        log.info("sidecar daily: %d points, %d cards; kept %s; skipped %s",
                 rows, len(ids),
                 {unpack_src(s): n for s, n in counts["kept"].items()},
                 dict(counts["skipped"]))
        return rows
    finally:
        db.close()


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def series_for_uuids(
        path: str, uuids, providers=None, since: str | None = None
) -> Iterator[tuple[str, str, str, str, float]]:
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

    A week that already carries an agg=1 row is skipped entirely, so nothing
    here recomputes an established weekly mean. That guard alone is not
    enough, though, and this docstring used to claim it was: the mean lives
    at its week's Sunday, so a late daily row *for that Sunday* shares its
    primary key, and an INSERT OR REPLACE in apply_daily destroyed the mean
    before this ran -- leaving one row for the guard to find nothing wrong
    with. apply_daily now refuses to overwrite an agg=1 row (see _APPLY_SQL),
    which is where that case actually has to be stopped. The residual is
    exact: a late row on any of the week's other six days is accepted by
    apply_daily and then deleted here with the rest of its cohort, unmerged.
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
            #
            # No watermark means the data cannot say how far it runs, so
            # nothing may be collapsed on its authority -- falling through to
            # the raw clock here would restore exactly the hazard above, on
            # the one irreversible operation in the module. It is reachable:
            # a build whose points are all unparseable writes built_at but
            # not daily_through, which is_ready() accepts.
            watermark = _get_meta(db, "daily_through")
            if not watermark:
                log.warning(
                    "sidecar at %s has no daily_through watermark; refusing "
                    "to collapse anything, since only the wall clock would "
                    "bound the cutoff and collapsing cannot be undone.", path)
                return 0
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


def stats(path: str) -> dict:
    """Shape and span of the sidecar, for /health. Counts are full scans, so
    call this on an operator-facing endpoint, not a hot path.

    `bytes` sums the main file with any live -wal/-shm companions. A WAL-mode
    database is a three-file unit, and a concurrent reader -- another stats()
    call included -- can hold a checkpoint back long enough for the -wal to
    reach a meaningful fraction of the main file's size; reporting the main
    file alone would understate what the volume actually holds.

    `daily_through` is the stored watermark, reported alongside the computed
    `latest` rather than instead of it. The two agree under normal operation,
    so publishing both costs nothing and turns any divergence between them
    into a free signal of a partial write.

    When not ready, `reason` names which of the four states it is (see
    _readiness) beside the unchanged `ready` key. Without it a truncated file
    and a never-created one are byte-identical here, while the response to
    them differs entirely: one means rebuild and lose the weekly tier, the
    other means a build that was always going to happen."""
    reason = _readiness(path)
    if reason is not None:
        return {"ready": False, "reason": reason}
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
            "daily_through": _get_meta(db, "daily_through"),
            "built_at": _get_meta(db, "built_at"),
            "bytes": sum(os.path.getsize(path + suffix)
                        for suffix in ("", "-wal", "-shm")
                        if os.path.exists(path + suffix)),
        }
    finally:
        db.close()
