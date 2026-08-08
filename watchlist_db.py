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
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("MYSTIC_FORGE_DB", "mystic_forge.db")
_WORDS_FILE = Path(__file__).parent / "watchlist_words.txt"
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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
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
                              {"entry_id": eid, "target_price": target_price})
            db.execute("UPDATE watchlist_current SET target_price=?"
                       " WHERE list_id=? AND entry_id=?",
                       (target_price, list_id, eid))
        if note is not None and note != existing["note"]:
            seq = append_event(db, list_id, "set_note",
                              {"entry_id": eid, "note": note})
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
                 name: str | None = None) -> dict:
    row = _find_entry(db, list_id, entry_id=entry_id, name=name)
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
        # create / clone_init carry no state
    return entries


def replay_state(db, list_id: int) -> dict[int, dict]:
    return state_at(db, list_id, None)
