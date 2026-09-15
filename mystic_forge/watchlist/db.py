"""SQLite storage for the multi-list price watchlist (spec 2026-08-08).

Event-sourced: every mutation appends to `events`; `watchlist_current` is a
materialized fold kept in the same transaction. `prices` is global data shared
by all lists. No user table — a list IS the identity, named by a passphrase
stored only as a SHA-256 hash.
"""

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

DB_PATH = os.environ.get("MYSTIC_FORGE_DB", "mystic_forge.db")

# Price providers the ingest tracks. The USD three form the default "cheapest
# across markets" basis; Cardmarket is EUR and only ever stands on its own.
ALL_SHOPS = ("tcgplayer", "cardkingdom", "cardmarket", "manapool")
USD_SHOPS = ("tcgplayer", "cardkingdom", "manapool")
SHOP_CURRENCY = {"tcgplayer": "$", "cardkingdom": "$", "cardmarket": "€",
                 "manapool": "$"}
SHOP_NAMES = {"tcgplayer": "TCGplayer", "cardkingdom": "Card Kingdom",
              "cardmarket": "Cardmarket", "manapool": "Mana Pool"}
# A target is either a fixed number or a rule that follows the historic low
# (target = lowest price seen before today, less target_pct percent).
TARGET_MODES = ("fixed", "low")
_WORDS_FILE = Path(__file__).parent.parent / "data" / "watchlist_words.txt"
_SHARE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O/1/I/L/U confusables

SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
  id INTEGER PRIMARY KEY,
  passphrase_hash TEXT NOT NULL UNIQUE,
  share_code TEXT NOT NULL UNIQUE,
  label TEXT,
  created_at TEXT NOT NULL,
  cloned_from_list INTEGER,
  cloned_from_seq INTEGER,
  superseded_by INTEGER
);
CREATE TABLE IF NOT EXISTS events (
  list_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  action TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (list_id, seq)
);
CREATE TABLE IF NOT EXISTS watchlist_current (
  list_id INTEGER NOT NULL,
  entry_id INTEGER NOT NULL,
  card_name TEXT NOT NULL,
  set_code TEXT,
  collector_number TEXT,
  uuid TEXT,
  target_price REAL,
  note TEXT,
  added_at TEXT NOT NULL,
  bought_at TEXT,
  target_mode TEXT NOT NULL DEFAULT 'fixed',
  target_pct REAL NOT NULL DEFAULT 0,
  shop TEXT,
  PRIMARY KEY (list_id, entry_id)
);
CREATE TABLE IF NOT EXISTS prices (
  uuid TEXT NOT NULL,
  date TEXT NOT NULL,
  provider TEXT NOT NULL,
  finish TEXT NOT NULL,
  price REAL NOT NULL,
  PRIMARY KEY (uuid, date, provider, finish)
);
CREATE TABLE IF NOT EXISTS card_uuids (
  card_name TEXT NOT NULL,
  uuid TEXT NOT NULL,
  set_code TEXT,
  collector_number TEXT,
  scryfall_id TEXT,
  PRIMARY KEY (card_name, uuid)
);
CREATE TABLE IF NOT EXISTS mtgstocks_prints (
  card_name TEXT NOT NULL,
  set_code TEXT NOT NULL,
  print_id INTEGER,
  slug TEXT,
  checked_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'ingest',
  PRIMARY KEY (card_name, set_code)
);
-- Viewers' browsers resolve print ids the server cannot reach and report
-- them back. One vote per voter per printing: a voter changing their mind
-- replaces their own row rather than stacking another.
CREATE TABLE IF NOT EXISTS mtgstocks_votes (
  card_name TEXT NOT NULL,
  set_code TEXT NOT NULL,
  voter TEXT NOT NULL,
  print_id INTEGER NOT NULL,
  slug TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (card_name, set_code, voter)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- Covering: every board query walks a printing's history at one shop and
-- needs the price of each row. Rows sit in the table in ingest order, so a
-- card's history is scattered over the whole file and each row was a
-- random page read on a cold cache. Carrying price makes it sequential:
-- 1.4s -> 0.4s for a 33-card board with nothing cached.
CREATE INDEX IF NOT EXISTS idx_prices_pfudp
  ON prices(provider, finish, uuid, date, price);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    # 30s, not the 5s default: init_db builds a new index across 1.5M price
    # rows on the first start after a schema change (~20s in production),
    # and a page request arriving meanwhile should wait, not 500.
    db = sqlite3.connect(path or DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    # superseded by idx_prices_pfudp (same prefix, plus price)
    db.execute("DROP INDEX IF EXISTS idx_prices_pfud")
    # 1.3.1's (provider, date) index made "prices through" instant but, with
    # no ANALYZE stats, the planner also chose it for a single-shop board's
    # envelopes -- ordered by date, so a scan of every row at that shop, per
    # card. Nothing needs it now; see pages.board_through.
    db.execute("DROP INDEX IF EXISTS idx_prices_pd")
    cols = [r[1] for r in db.execute("PRAGMA table_info(watchlist_current)")]
    if "bought_at" not in cols:  # migration for pre-"bought" databases
        db.execute("ALTER TABLE watchlist_current ADD COLUMN bought_at TEXT")
    if "target_mode" not in cols:  # migration for fixed-target-only databases
        db.execute("ALTER TABLE watchlist_current ADD COLUMN target_mode TEXT"
                   " NOT NULL DEFAULT 'fixed'")
        db.execute("ALTER TABLE watchlist_current ADD COLUMN target_pct REAL"
                   " NOT NULL DEFAULT 0")
    if "shop" not in cols:         # migration for pre-per-card-shop databases
        db.execute("ALTER TABLE watchlist_current ADD COLUMN shop TEXT")
    cols = [r[1] for r in db.execute("PRAGMA table_info(mtgstocks_prints)")]
    if "source" not in cols:     # migration for pre-vote databases
        db.execute("ALTER TABLE mtgstocks_prints ADD COLUMN source TEXT"
                   " NOT NULL DEFAULT 'ingest'")
    db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _words() -> list[str]:
    return _WORDS_FILE.read_text().split()


def mint_passphrase() -> str:
    words = _words()
    picks = [secrets.choice(words) for _ in range(4)]
    return "-".join(picks) + f"-{secrets.randbelow(100):02d}"


def mint_share_code() -> str:
    return "SC-" + "".join(secrets.choice(_SHARE_ALPHABET) for _ in range(6))


def hash_passphrase(passphrase: str) -> str:
    return hashlib.sha256(passphrase.strip().lower().encode()).hexdigest()


def get_list_by_passphrase(db, passphrase: str):
    return db.execute("SELECT * FROM lists WHERE passphrase_hash=?",
                      (hash_passphrase(passphrase),)).fetchone()


def get_list_by_share(db, share_code: str):
    return db.execute("SELECT * FROM lists WHERE share_code=?",
                      (share_code.strip().upper(),)).fetchone()


def get_list(db, list_id: int):
    return db.execute("SELECT * FROM lists WHERE id=?", (list_id,)).fetchone()


def create_list(db, label: str | None = None,
                cloned_from_list: int | None = None,
                cloned_from_seq: int | None = None) -> tuple[int, str, str]:
    """Returns (list_id, passphrase, share_code). Passphrase shown once, here."""
    passphrase = mint_passphrase()
    while True:  # share codes are short; retry the rare collision
        share_code = mint_share_code()
        try:
            cur = db.execute(
                "INSERT INTO lists (passphrase_hash, share_code, label, created_at,"
                " cloned_from_list, cloned_from_seq) VALUES (?,?,?,?,?,?)",
                (hash_passphrase(passphrase), share_code, label, _now(),
                 cloned_from_list, cloned_from_seq))
            break
        except sqlite3.IntegrityError:
            continue
    list_id = cur.lastrowid
    append_event(db, list_id, "create", {"label": label})
    db.commit()
    return list_id, passphrase, share_code


def append_event(db, list_id: int, action: str, payload: dict) -> int:
    row = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE list_id=?",
                     (list_id,)).fetchone()
    seq = row[0]
    db.execute("INSERT INTO events (list_id, seq, ts, action, payload_json)"
               " VALUES (?,?,?,?,?)",
               (list_id, seq, _now(), action, json.dumps(payload)))
    return seq


class NotFound(Exception):
    pass


def current_entries(db, list_id: int) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT * FROM watchlist_current WHERE list_id=? ORDER BY entry_id",
        (list_id,))]


def _find_entry(db, list_id: int, entry_id=None, name=None,
                set_code=None, collector_number=None):
    if entry_id is not None:
        return db.execute(
            "SELECT * FROM watchlist_current WHERE list_id=? AND entry_id=?",
            (list_id, entry_id)).fetchone()
    q = "SELECT * FROM watchlist_current WHERE list_id=? AND LOWER(card_name)=LOWER(?)"
    args = [list_id, name]
    if set_code:
        q += " AND LOWER(set_code)=LOWER(?)"
        args.append(set_code)
    if collector_number:
        q += " AND collector_number=?"
        args.append(collector_number)
    return db.execute(q, args).fetchone()


def _norm_target(target_price, target_mode, target_pct):
    """Canonical (price, mode, pct) triple. A 'low' rule carries no fixed
    price; a fixed target carries no percentage."""
    mode = target_mode if target_mode in TARGET_MODES else "fixed"
    pct = float(target_pct or 0)
    if mode == "low":
        return None, "low", max(0.0, min(pct, 99.0))
    return target_price, "fixed", 0.0


def _write_target(db, list_id: int, entry, target_price, target_mode,
                  target_pct) -> int:
    """Append a set_target event and materialize it. Returns the seq."""
    price, mode, pct = _norm_target(target_price, target_mode, target_pct)
    seq = append_event(db, list_id, "set_target",
                       {"entry_id": entry["entry_id"],
                        "card_name": entry["card_name"],
                        "target_price": price, "target_mode": mode,
                        "target_pct": pct})
    db.execute("UPDATE watchlist_current SET target_price=?, target_mode=?,"
               " target_pct=? WHERE list_id=? AND entry_id=?",
               (price, mode, pct, list_id, entry["entry_id"]))
    return seq


def _target_differs(entry, target_price, target_mode, target_pct) -> bool:
    price, mode, pct = _norm_target(target_price, target_mode, target_pct)
    return (price != entry["target_price"]
            or mode != (entry["target_mode"] or "fixed")
            or pct != float(entry["target_pct"] or 0))


def add_card(db, list_id: int, card_name: str, set_code: str | None = None,
             collector_number: str | None = None, target_price: float | None = None,
             note: str | None = None, target_mode: str | None = None,
             target_pct: float | None = None,
             shop: str | None = None) -> tuple[int, dict]:
    """Append add (or set_target/set_note/set_shop for an existing entry) and
    materialize.

    `target_mode='low'` makes the target follow the historic low (less
    `target_pct` percent) instead of a fixed `target_price`. `shop` pins the
    card's price basis to one market; None means cheapest across USD shops.

    Returns (last_seq, entry_dict)."""
    existing = _find_entry(db, list_id, name=card_name, set_code=set_code,
                           collector_number=collector_number)
    if existing:
        seq = existing["entry_id"]
        eid = existing["entry_id"]
        wants_target = target_price is not None or target_mode == "low"
        if wants_target and _target_differs(existing, target_price,
                                            target_mode, target_pct):
            seq = _write_target(db, list_id, existing, target_price,
                                target_mode, target_pct)
        if note is not None and note != existing["note"]:
            seq = append_event(db, list_id, "set_note",
                              {"entry_id": eid, "card_name": existing["card_name"],
                               "note": note})
            db.execute("UPDATE watchlist_current SET note=?"
                       " WHERE list_id=? AND entry_id=?", (note, list_id, eid))
        if shop is not None and shop != existing["shop"]:
            seq = _write_shop(db, list_id, existing, shop)
        db.commit()
        return seq, dict(_find_entry(db, list_id, entry_id=eid))

    added_at = _now()
    price, mode, pct = _norm_target(target_price, target_mode, target_pct)
    shop = shop if shop in ALL_SHOPS else None
    payload = {"card_name": card_name, "set_code": set_code,
               "collector_number": collector_number,
               "target_price": price, "target_mode": mode, "target_pct": pct,
               "shop": shop, "note": note, "added_at": added_at}
    seq = append_event(db, list_id, "add", payload)
    db.execute(
        "INSERT INTO watchlist_current (list_id, entry_id, card_name, set_code,"
        " collector_number, target_price, target_mode, target_pct, shop, note,"
        " added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (list_id, seq, card_name, set_code, collector_number,
         price, mode, pct, shop, note, added_at))
    db.commit()
    return seq, dict(_find_entry(db, list_id, entry_id=seq))


def _write_shop(db, list_id: int, entry, shop) -> int:
    """Append a set_shop event (None = back to cheapest USD) and materialize."""
    shop = shop if shop in ALL_SHOPS else None
    seq = append_event(db, list_id, "set_shop",
                       {"entry_id": entry["entry_id"],
                        "card_name": entry["card_name"], "shop": shop})
    db.execute("UPDATE watchlist_current SET shop=? WHERE list_id=? AND entry_id=?",
               (shop, list_id, entry["entry_id"]))
    return seq


def remove_entry(db, list_id: int, entry_id: int | None = None,
                 name: str | None = None, set_code: str | None = None,
                 collector_number: str | None = None) -> dict:
    row = _find_entry(db, list_id, entry_id=entry_id, name=name,
                      set_code=set_code, collector_number=collector_number)
    if row is None:
        raise NotFound(f"No watchlist entry matching "
                       f"{'#' + str(entry_id) if entry_id else name!r}")
    append_event(db, list_id, "remove",
                 {"entry_id": row["entry_id"], "card_name": row["card_name"]})
    db.execute("DELETE FROM watchlist_current WHERE list_id=? AND entry_id=?",
               (list_id, row["entry_id"]))
    db.commit()
    return dict(row)


def state_at(db, list_id: int, seq: int | None = None) -> dict[int, dict]:
    """Pure fold of the event chain up to (and including) seq."""
    q = "SELECT * FROM events WHERE list_id=? ORDER BY seq"
    entries: dict[int, dict] = {}
    for ev in db.execute(q, (list_id,)):
        if seq is not None and ev["seq"] > seq:
            break
        payload = json.loads(ev["payload_json"])
        if ev["action"] == "add":
            entries[ev["seq"]] = {"entry_id": ev["seq"], "target_mode": "fixed",
                                  "target_pct": 0.0, "shop": None, **payload}
        elif ev["action"] == "remove":
            entries.pop(payload["entry_id"], None)
        elif ev["action"] == "set_target":
            # pre-rule payloads carry only a price: they are fixed targets
            e = entries[payload["entry_id"]]
            e["target_price"] = payload["target_price"]
            e["target_mode"] = payload.get("target_mode", "fixed")
            e["target_pct"] = payload.get("target_pct", 0.0)
        elif ev["action"] == "set_shop":
            entries[payload["entry_id"]]["shop"] = payload.get("shop")
        elif ev["action"] == "set_note":
            entries[payload["entry_id"]]["note"] = payload["note"]
        elif ev["action"] == "bought":
            entries[payload["entry_id"]]["bought_at"] = payload["date"]
        elif ev["action"] == "unbought":
            entries[payload["entry_id"]]["bought_at"] = None
        # create / clone_init / set_label carry no entry state
    return entries


def replay_state(db, list_id: int) -> dict[int, dict]:
    return state_at(db, list_id, None)


def clone_list(db, source_list_id: int, at_seq: int | None = None,
               recovery: bool = False) -> tuple[int, str, str]:
    """Mint a new list seeded from source state as of at_seq (default: latest).

    recovery=True marks the source superseded by the new list (spec: cloning
    your OWN list is recovery; cloning via share code is a fork)."""
    source = get_list(db, source_list_id)
    if source is None:
        raise NotFound(f"No list #{source_list_id}")
    if at_seq is None:
        at_seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM events"
                            " WHERE list_id=?", (source_list_id,)).fetchone()[0]
    snapshot = state_at(db, source_list_id, at_seq)
    new_id, passphrase, share_code = create_list(
        db, label=source["label"],
        cloned_from_list=source_list_id, cloned_from_seq=at_seq)
    append_event(db, new_id, "clone_init",
                 {"source_list": source_list_id, "source_seq": at_seq,
                  "source_share_code": source["share_code"],
                  "recovery": recovery})
    db.commit()
    for entry in sorted(snapshot.values(), key=lambda e: e["entry_id"]):
        add_card(db, new_id, entry["card_name"],
                 set_code=entry.get("set_code"),
                 collector_number=entry.get("collector_number"),
                 target_price=entry.get("target_price"),
                 target_mode=entry.get("target_mode"),
                 target_pct=entry.get("target_pct"),
                 shop=entry.get("shop"),
                 note=entry.get("note"))
    if recovery:
        db.execute("UPDATE lists SET superseded_by=? WHERE id=?",
                   (new_id, source_list_id))
        db.commit()
    return new_id, passphrase, share_code


def upsert_price(db, uuid: str, date: str, provider: str, finish: str,
                 price: float, commit: bool = True) -> None:
    db.execute("INSERT OR REPLACE INTO prices (uuid, date, provider, finish,"
               " price) VALUES (?,?,?,?,?)", (uuid, date, provider, finish, price))
    if commit:
        db.commit()


def _providers(provider) -> tuple:
    """A provider argument may name one shop or a group of them."""
    if isinstance(provider, str):
        return (provider,)
    return tuple(provider)


def _cheapest_on(db, uuids, providers, finish, date, price):
    """The (uuid, provider) that set the envelope's price on `date`."""
    marks = ",".join("?" * len(uuids))
    pmarks = ",".join("?" * len(providers))
    row = db.execute(
        f"SELECT uuid, provider FROM prices WHERE date=? AND finish=?"
        f" AND provider IN ({pmarks}) AND uuid IN ({marks})"
        f" ORDER BY price ASC, provider ASC LIMIT 1",
        [date, finish, *providers, *uuids]).fetchone()
    if row is None:                      # cannot happen for an envelope row
        return None, None
    return row["uuid"], row["provider"]


# A board render asks for the same card's envelope up to three times (basis
# summary, 'low' target reference, sparkline). Inside envelope_memo() the
# first answer is reused; outside it nothing is cached, so the ingest loop
# and the tools always read fresh. Context-local, so concurrent renders in
# different threads never share a memo.
_ENV_MEMO: ContextVar[dict | None] = ContextVar("envelope_memo", default=None)


@contextmanager
def envelope_memo():
    """Reuse envelopes for the duration of one render."""
    token = _ENV_MEMO.set({})
    try:
        yield
    finally:
        _ENV_MEMO.reset(token)


def _envelope(db, uuids, provider, finish):
    """Per-date minimum across printings — and across the given shops, when
    `provider` names several: the price a buyer actually pays.

    Tracking one uuid would silently rewrite history when a reprint changes
    which printing is cheapest; the envelope keeps deltas honest."""
    if not uuids:
        return []
    providers = _providers(provider)
    memo = _ENV_MEMO.get()
    key = (tuple(uuids), providers, finish)
    if memo is not None and key in memo:
        return memo[key]
    marks = ",".join("?" * len(uuids))
    pmarks = ",".join("?" * len(providers))
    env = db.execute(
        f"SELECT date, MIN(price) AS price FROM prices"
        f" WHERE provider IN ({pmarks}) AND finish=? AND uuid IN ({marks})"
        f" GROUP BY date ORDER BY date",
        [*providers, finish, *uuids]).fetchall()
    if memo is not None:
        memo[key] = env
    return env


def envelope_low(env, exclude_latest: bool = False):
    """(price, date) of the lowest point on an envelope, or None.

    With `exclude_latest` the newest date is left out — the reference a
    'low' target rule measures today's price against, so that a brand-new
    low can be recognised as one (today can't undercut itself)."""
    rows = env[:-1] if exclude_latest else env
    if not rows:
        return None
    best = min(rows, key=lambda r: (r["price"], r["date"]))
    return best["price"], best["date"]


def price_summary(db, uuids, provider="tcgplayer",
                  finish: str = "normal", today: str | None = None):
    """Cheapest-available price + 7d/30d deltas on the min-across-printings
    envelope, or None if no data. `uuid` is today's cheapest printing and
    `provider` the shop that set it (meaningful when several were given).
    `low` is the lowest point ever recorded on this envelope."""
    uuids = list(uuids)
    providers = _providers(provider)
    env = _envelope(db, uuids, providers, finish)
    if not env:
        return None
    current, date = env[-1]["price"], env[-1]["date"]
    uuid, shop = _cheapest_on(db, uuids, providers, finish, date, current)
    today = today or _date.today().isoformat()
    low = envelope_low(env)
    out = {"uuid": uuid, "provider": shop, "current": current,
           "date": date, "d7": None, "d30": None,
           "low": low[0] if low else None, "low_date": low[1] if low else None}
    for key, days in (("d7", 7), ("d30", 30)):
        ref_date = (_date.fromisoformat(today) - timedelta(days=days)).isoformat()
        ref = None
        for row in env:
            if row["date"] <= ref_date:
                ref = row["price"]
            else:
                break
        if ref is not None:
            out[key] = round(current - ref, 2)
    return out


def price_series(db, uuids, days: int = 90, provider="tcgplayer",
                 finish: str = "normal", today: str | None = None):
    uuids = list(uuids)
    providers = _providers(provider)
    env = _envelope(db, uuids, providers, finish)
    if not env:
        return None
    today = today or _date.today().isoformat()
    start = (_date.fromisoformat(today) - timedelta(days=days)).isoformat()
    uuid, shop = _cheapest_on(db, uuids, providers, finish,
                              env[-1]["date"], env[-1]["price"])
    return {"uuid": uuid, "provider": shop, "finish": finish,
            "points": [(r["date"], r["price"]) for r in env
                       if r["date"] >= start]}


def latest_by_shop(db, uuids, finish: str = "normal") -> dict:
    """Newest price at every shop that has one: {provider: (price, date)},
    cheapest printing per shop. One query per card, for the modal's
    cross-market row."""
    uuids = list(uuids)
    if not uuids:
        return {}
    marks = ",".join("?" * len(uuids))
    pmarks = ",".join("?" * len(ALL_SHOPS))
    # Two steps, both answered from the (provider, finish, uuid, date) index:
    # the newest date per shop never touches a price, and the cheapest price
    # on that date is a handful of point reads. The one-query form -- a
    # window over every historical row of every printing -- read a price for
    # each of them, and was a quarter of a board's render time.
    newest = db.execute(
        f"SELECT provider, MAX(date) AS date FROM prices"
        f" WHERE provider IN ({pmarks}) AND finish=? AND uuid IN ({marks})"
        f" GROUP BY provider", [*ALL_SHOPS, finish, *uuids]).fetchall()
    out = {}
    for n in newest:
        price = db.execute(
            f"SELECT MIN(price) FROM prices WHERE provider=? AND finish=?"
            f" AND uuid IN ({marks}) AND date=?",
            [n["provider"], finish, *uuids, n["date"]]).fetchone()[0]
        out[n["provider"]] = (price, n["date"])
    return out


def scryfall_id_for(db, entry: dict, uuid: str | None = None):
    """A Scryfall id to show the card's face: the given printing's, else
    the pinned printing's, else any printing of the name."""
    if uuid:
        row = db.execute("SELECT scryfall_id FROM card_uuids WHERE uuid=?"
                         " AND scryfall_id IS NOT NULL", (uuid,)).fetchone()
        if row:
            return row["scryfall_id"]
    for u in uuids_for_entry(db, entry):
        row = db.execute("SELECT scryfall_id FROM card_uuids WHERE uuid=?"
                         " AND scryfall_id IS NOT NULL", (u,)).fetchone()
        if row:
            return row["scryfall_id"]
    return None


def reference_low(db, uuids, provider="tcgplayer", finish: str = "normal"):
    """The lowest envelope price before the newest date — what a 'low'
    target rule is measured against. None with fewer than two points."""
    env = _envelope(db, list(uuids), provider, finish)
    low = envelope_low(env, exclude_latest=True)
    return low


def uuids_for_entry(db, entry: dict) -> list[str]:
    """MTGJSON uuids an entry tracks: its pinned printing, else all printings."""
    if entry.get("uuid"):
        return [entry["uuid"]]
    q = "SELECT uuid FROM card_uuids WHERE LOWER(card_name)=LOWER(?)"
    args = [entry["card_name"]]
    if entry.get("set_code"):
        q += " AND LOWER(set_code)=LOWER(?)"
        args.append(entry["set_code"])
    if entry.get("collector_number"):
        q += " AND collector_number=?"
        args.append(entry["collector_number"])
    return [r["uuid"] for r in db.execute(q, args)]


def entry_price_summary(db, entry: dict, provider="tcgplayer",
                        today: str | None = None):
    """Price summary for an entry's tracked printings at one shop (or the
    cheapest across several, when `provider` is a tuple).

    Prefers normal finish; falls back to foil so foil-only collector
    printings still show a price. Adds a 'finish' key to the result."""
    uuids = uuids_for_entry(db, entry)
    if not uuids:
        return None
    for finish in ("normal", "foil"):
        s = price_summary(db, uuids, provider=provider, finish=finish,
                          today=today)
        if s is not None:
            s["finish"] = finish
            return s
    return None


def entry_shops(entry: dict) -> tuple:
    """The shops an entry's price basis is drawn from: its pinned shop, else
    the cheapest across the USD markets."""
    shop = entry.get("shop")
    return (shop,) if shop in ALL_SHOPS else USD_SHOPS


def entry_currency(entry: dict) -> str:
    """Currency symbol of the entry's basis (and therefore of its target)."""
    return SHOP_CURRENCY.get(entry.get("shop") or "", "$")


def basis_summary(db, entry: dict, today: str | None = None):
    """The summary hit state is judged on: the entry's pinned shop, else the
    cheapest across USD shops. Every consumer of "is this at target" —
    board, tools, push alerts — must read through here so they agree."""
    return entry_price_summary(db, entry, provider=entry_shops(entry),
                               today=today)


def effective_target(db, entry: dict, summary=None):
    """The number an entry's price is compared against right now.

    fixed → the stored target_price. low → the lowest basis price recorded
    before the newest one, less target_pct percent (None until there are two
    points of history)."""
    mode = entry.get("target_mode") or "fixed"
    if mode != "low":
        return entry.get("target_price")
    uuids = uuids_for_entry(db, entry)
    if not uuids:
        return None
    finish = summary.get("finish", "normal") if summary else "normal"
    low = reference_low(db, uuids, entry_shops(entry), finish)
    if low is None:
        return None
    pct = float(entry.get("target_pct") or 0)
    return round(low[0] * (1 - pct / 100), 2)


def is_hit(entry: dict, summary, target) -> bool:
    """At/below target on its basis. Bought cards never count."""
    return bool(summary and target is not None and not entry.get("bought_at")
                and summary["current"] <= target)


def set_entry_target(db, list_id: int, entry_id: int, target_price,
                     target_mode: str | None = None,
                     target_pct: float | None = None):
    """Set (or clear, with None) an entry's target; appends a set_target event.

    target_mode='low' installs a historic-low rule (target_pct percent under
    it) in place of a fixed price."""
    row = _find_entry(db, list_id, entry_id=entry_id)
    if row is None:
        raise NotFound(f"No entry #{entry_id}")
    _write_target(db, list_id, row, target_price, target_mode, target_pct)
    db.commit()
    return dict(_find_entry(db, list_id, entry_id=entry_id))


def set_entry_shop(db, list_id: int, entry_id: int, shop):
    """Pin an entry's price basis to one shop (None = cheapest USD)."""
    row = _find_entry(db, list_id, entry_id=entry_id)
    if row is None:
        raise NotFound(f"No entry #{entry_id}")
    if shop is not None and shop not in ALL_SHOPS:
        raise ValueError(f"unknown shop {shop!r}")
    _write_shop(db, list_id, row, shop)
    db.commit()
    return dict(_find_entry(db, list_id, entry_id=entry_id))


def set_bought(db, list_id: int, entry_id: int, bought: bool = True) -> dict:
    """Mark an entry bought (kept, muted, chart-annotated) or un-mark it."""
    row = _find_entry(db, list_id, entry_id=entry_id)
    if row is None:
        raise NotFound(f"No entry #{entry_id}")
    if bought:
        date = _now()[:10]
        append_event(db, list_id, "bought",
                     {"entry_id": entry_id, "card_name": row["card_name"],
                      "date": date})
        db.execute("UPDATE watchlist_current SET bought_at=?"
                   " WHERE list_id=? AND entry_id=?", (date, list_id, entry_id))
    else:
        append_event(db, list_id, "unbought",
                     {"entry_id": entry_id, "card_name": row["card_name"]})
        db.execute("UPDATE watchlist_current SET bought_at=NULL"
                   " WHERE list_id=? AND entry_id=?", (list_id, entry_id))
    db.commit()
    return dict(_find_entry(db, list_id, entry_id=entry_id))


def set_entry_note(db, list_id: int, entry_id: int, note) -> dict:
    """Set (or clear, with None) an entry's note; appends a set_note event."""
    row = _find_entry(db, list_id, entry_id=entry_id)
    if row is None:
        raise NotFound(f"No entry #{entry_id}")
    append_event(db, list_id, "set_note",
                 {"entry_id": entry_id, "card_name": row["card_name"],
                  "note": note})
    db.execute("UPDATE watchlist_current SET note=?"
               " WHERE list_id=? AND entry_id=?", (note, list_id, entry_id))
    db.commit()
    return dict(_find_entry(db, list_id, entry_id=entry_id))


def set_label(db, list_id: int, label):
    """Rename a list; recorded as a set_label event (ignored by entry replay)."""
    append_event(db, list_id, "set_label", {"label": label})
    db.execute("UPDATE lists SET label=? WHERE id=?", (label, list_id))
    db.commit()
