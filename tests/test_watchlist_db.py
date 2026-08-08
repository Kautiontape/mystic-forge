import re
import sqlite3

import watchlist_db


def test_init_db_creates_tables(db):
    names = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"lists", "events", "watchlist_current", "prices", "meta",
            "card_uuids"} <= names


def test_mint_passphrase_format():
    pp = watchlist_db.mint_passphrase()
    parts = pp.split("-")
    assert len(parts) == 5                     # 4 words + 2-digit number
    assert re.fullmatch(r"\d{2}", parts[-1])
    assert all(re.fullmatch(r"[a-z]+", w) for w in parts[:4])


def test_mint_share_code_format():
    sc = watchlist_db.mint_share_code()
    assert re.fullmatch(r"SC-[A-Z2-9]{6}", sc)


def test_create_list_roundtrip(db):
    list_id, pp, sc = watchlist_db.create_list(db, label="my deck")
    row = watchlist_db.get_list_by_passphrase(db, pp)
    assert row["id"] == list_id
    assert row["label"] == "my deck"
    assert row["share_code"] == sc
    assert watchlist_db.get_list_by_share(db, sc)["id"] == list_id
    # passphrase never stored in the clear
    raw = db.execute("SELECT passphrase_hash FROM lists").fetchone()[0]
    assert pp not in raw and len(raw) == 64


def test_wrong_passphrase_returns_none(db):
    watchlist_db.create_list(db)
    assert watchlist_db.get_list_by_passphrase(db, "nope-nope-nope-nope-00") is None


def test_create_records_create_event(db):
    list_id, _, _ = watchlist_db.create_list(db, label="x")
    ev = db.execute("SELECT * FROM events WHERE list_id=?", (list_id,)).fetchone()
    assert ev["seq"] == 1 and ev["action"] == "create"
