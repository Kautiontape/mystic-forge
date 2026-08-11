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
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

DB_PATH = os.environ.get("MYSTIC_FORGE_DB", "mystic_forge.db")
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
CREATE INDEX IF NOT EXISTS idx_prices_pfud
  ON prices(provider, finish, uuid, date);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    cols = [r[1] for r in db.execute("PRAGMA table_info(watchlist_current)")]
    if "bought_at" not in cols:  # migration for pre-"bought" databases
        db.execute("ALTER TABLE watchlist_current ADD COLUMN bought_at TEXT")
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


def add_card(db, list_id: int, card_name: str, set_code: str | None = None,
             collector_number: str | None = None, target_price: float | None = None,
             note: str | None = None) -> tuple[int, dict]:
    """Append add (or set_target/set_note for an existing entry) and materialize.

    Returns (last_seq, entry_dict)."""
    existing = _find_entry(db, list_id, name=card_name, set_code=set_code,
                           collector_number=collector_number)
    if existing:
        seq = existing["entry_id"]
        eid = existing["entry_id"]
        if target_price is not None and target_price != existing["target_price"]:
            seq = append_event(db, list_id, "set_target",
                              {"entry_id": eid, "card_name": existing["card_name"],
                               "target_price": target_price})
            db.execute("UPDATE watchlist_current SET target_price=?"
                       " WHERE list_id=? AND entry_id=?",
                       (target_price, list_id, eid))
        if note is not None and note != existing["note"]:
            seq = append_event(db, list_id, "set_note",
                              {"entry_id": eid, "card_name": existing["card_name"],
                               "note": note})
            db.execute("UPDATE watchlist_current SET note=?"
                       " WHERE list_id=? AND entry_id=?", (note, list_id, eid))
        db.commit()
        return seq, dict(_find_entry(db, list_id, entry_id=eid))

    added_at = _now()
    payload = {"card_name": card_name, "set_code": set_code,
               "collector_number": collector_number,
               "target_price": target_price, "note": note, "added_at": added_at}
    seq = append_event(db, list_id, "add", payload)
    db.execute(
        "INSERT INTO watchlist_current (list_id, entry_id, card_name, set_code,"
        " collector_number, target_price, note, added_at) VALUES (?,?,?,?,?,?,?,?)",
        (list_id, seq, card_name, set_code, collector_number,
         target_price, note, added_at))
    db.commit()
    return seq, dict(_find_entry(db, list_id, entry_id=seq))


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
            entries[ev["seq"]] = {"entry_id": ev["seq"], **payload}
        elif ev["action"] == "remove":
            entries.pop(payload["entry_id"], None)
        elif ev["action"] == "set_target":
            entries[payload["entry_id"]]["target_price"] = payload["target_price"]
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


def _cheapest_latest(db, uuids, provider, finish):
    """(uuid, price, date) with the lowest most-recent price, else None."""
    if not uuids:
        return None
    marks = ",".join("?" * len(uuids))
    row = db.execute(
        f"""SELECT uuid, price, date FROM (
              SELECT uuid, price, date,
                     ROW_NUMBER() OVER (PARTITION BY uuid ORDER BY date DESC) rn
              FROM prices
              WHERE provider=? AND finish=? AND uuid IN ({marks}))
            WHERE rn=1 ORDER BY price ASC LIMIT 1""",
        [provider, finish, *uuids]).fetchone()
    return (row["uuid"], row["price"], row["date"]) if row else None


def _envelope(db, uuids, provider, finish):
    """Per-date minimum across printings: the price a buyer actually pays.

    Tracking one uuid would silently rewrite history when a reprint changes
    which printing is cheapest; the envelope keeps deltas honest."""
    if not uuids:
        return []
    marks = ",".join("?" * len(uuids))
    return db.execute(
        f"SELECT date, MIN(price) AS price FROM prices"
        f" WHERE provider=? AND finish=? AND uuid IN ({marks})"
        f" GROUP BY date ORDER BY date",
        [provider, finish, *uuids]).fetchall()


def price_summary(db, uuids, provider: str = "tcgplayer",
                  finish: str = "normal", today: str | None = None):
    """Cheapest-available price + 7d/30d deltas on the min-across-printings
    envelope, or None if no data. `uuid` is today's cheapest printing."""
    uuids = list(uuids)
    env = _envelope(db, uuids, provider, finish)
    if not env:
        return None
    current, date = env[-1]["price"], env[-1]["date"]
    best = _cheapest_latest(db, uuids, provider, finish)
    today = today or _date.today().isoformat()
    out = {"uuid": best[0] if best else None, "current": current,
           "date": date, "d7": None, "d30": None}
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


def price_series(db, uuids, days: int = 90, provider: str = "tcgplayer",
                 finish: str = "normal", today: str | None = None):
    uuids = list(uuids)
    env = _envelope(db, uuids, provider, finish)
    if not env:
        return None
    today = today or _date.today().isoformat()
    start = (_date.fromisoformat(today) - timedelta(days=days)).isoformat()
    best = _cheapest_latest(db, uuids, provider, finish)
    return {"uuid": best[0] if best else None, "provider": provider,
            "finish": finish,
            "points": [(r["date"], r["price"]) for r in env
                       if r["date"] >= start]}


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


def entry_price_summary(db, entry: dict, provider: str = "tcgplayer",
                        today: str | None = None):
    """Price summary for an entry's tracked printings.

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


def set_entry_target(db, list_id: int, entry_id: int, target_price):
    """Set (or clear, with None) an entry's target; appends a set_target event."""
    row = _find_entry(db, list_id, entry_id=entry_id)
    if row is None:
        raise NotFound(f"No entry #{entry_id}")
    append_event(db, list_id, "set_target",
                 {"entry_id": entry_id, "card_name": row["card_name"],
                  "target_price": target_price})
    db.execute("UPDATE watchlist_current SET target_price=?"
               " WHERE list_id=? AND entry_id=?", (target_price, list_id, entry_id))
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
