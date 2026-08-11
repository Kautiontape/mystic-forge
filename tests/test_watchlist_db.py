import re

import pytest

from mystic_forge.watchlist import db as watchlist_db


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


def test_add_card_materializes_current(db):
    list_id, _, _ = watchlist_db.create_list(db)
    seq, entry = watchlist_db.add_card(db, list_id, "Sol Ring",
                                       target_price=1.5, note="Cloud deck")
    row = db.execute("SELECT * FROM watchlist_current WHERE list_id=?",
                     (list_id,)).fetchone()
    assert row["entry_id"] == seq == entry["entry_id"]
    assert row["card_name"] == "Sol Ring"
    assert row["target_price"] == 1.5
    assert row["note"] == "Cloud deck"


def test_add_same_name_twice_updates_instead_of_duplicating(db):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=2.0)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=1.0, note="hi")
    rows = db.execute("SELECT * FROM watchlist_current WHERE list_id=?",
                      (list_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["target_price"] == 1.0 and rows[0]["note"] == "hi"
    actions = [r["action"] for r in db.execute(
        "SELECT action FROM events WHERE list_id=? ORDER BY seq", (list_id,))]
    assert actions == ["create", "add", "set_target", "set_note"]


def test_two_printings_of_same_card_coexist(db):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", set_code="C21",
                          collector_number="263")
    watchlist_db.add_card(db, list_id, "Sol Ring", set_code="LTC",
                          collector_number="284")
    rows = db.execute("SELECT * FROM watchlist_current WHERE list_id=?",
                      (list_id,)).fetchall()
    assert len(rows) == 2


def test_remove_by_name_and_by_entry_id(db):
    list_id, _, _ = watchlist_db.create_list(db)
    seq1, _ = watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_db.add_card(db, list_id, "Cultivate")
    removed = watchlist_db.remove_entry(db, list_id, entry_id=seq1)
    assert removed["card_name"] == "Sol Ring"
    removed = watchlist_db.remove_entry(db, list_id, name="cultivate")
    assert removed["card_name"] == "Cultivate"
    assert db.execute("SELECT COUNT(*) FROM watchlist_current WHERE list_id=?",
                      (list_id,)).fetchone()[0] == 0


def test_remove_missing_raises(db):
    list_id, _, _ = watchlist_db.create_list(db)
    with pytest.raises(watchlist_db.NotFound):
        watchlist_db.remove_entry(db, list_id, name="Ghost Card")


def test_lists_are_isolated(db):
    a, _, _ = watchlist_db.create_list(db)
    b, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, a, "Sol Ring")
    assert watchlist_db.current_entries(db, b) == []
    with pytest.raises(watchlist_db.NotFound):
        watchlist_db.remove_entry(db, b, name="Sol Ring")


def test_replay_reproduces_current(db):
    """Spec acceptance: replaying events reproduces watchlist_current exactly."""
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=2.0)
    s2, _ = watchlist_db.add_card(db, list_id, "Cultivate", note="ramp")
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=1.0)
    watchlist_db.remove_entry(db, list_id, entry_id=s2)
    replayed = watchlist_db.replay_state(db, list_id)
    current = {r["entry_id"]: dict(r) for r in
               db.execute("SELECT * FROM watchlist_current WHERE list_id=?",
                          (list_id,))}
    assert set(replayed) == set(current)
    for eid, entry in replayed.items():
        for col in ("card_name", "set_code", "collector_number",
                    "target_price", "note", "added_at"):
            assert entry[col] == current[eid][col], f"{col} diverged"


def test_clone_at_seq_matches_source_state(db):
    src, _, _ = watchlist_db.create_list(db, label="orig")
    watchlist_db.add_card(db, src, "Sol Ring", target_price=2.0)
    s_cult, _ = watchlist_db.add_card(db, src, "Cultivate")
    at = db.execute("SELECT MAX(seq) FROM events WHERE list_id=?",
                    (src,)).fetchone()[0]
    watchlist_db.remove_entry(db, src, entry_id=s_cult)      # after `at`
    new_id, pp, sc = watchlist_db.clone_list(db, src, at_seq=at, recovery=False)
    names = sorted(e["card_name"] for e in watchlist_db.current_entries(db, new_id))
    assert names == ["Cultivate", "Sol Ring"]
    targets = {e["card_name"]: e["target_price"]
               for e in watchlist_db.current_entries(db, new_id)}
    assert targets["Sol Ring"] == 2.0
    row = watchlist_db.get_list(db, new_id)
    assert row["cloned_from_list"] == src and row["cloned_from_seq"] == at
    assert row["label"] == "orig"


def test_clone_defaults_to_latest(db):
    src, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, src, "Sol Ring")
    new_id, _, _ = watchlist_db.clone_list(db, src, at_seq=None, recovery=False)
    assert [e["card_name"] for e in watchlist_db.current_entries(db, new_id)] \
        == ["Sol Ring"]


def test_recovery_clone_supersedes_source_but_fork_does_not(db):
    src, _, _ = watchlist_db.create_list(db)
    fork_id, _, _ = watchlist_db.clone_list(db, src, recovery=False)
    assert watchlist_db.get_list(db, src)["superseded_by"] is None
    rec_id, _, _ = watchlist_db.clone_list(db, src, recovery=True)
    assert watchlist_db.get_list(db, src)["superseded_by"] == rec_id


def test_clone_history_starts_with_clone_init_then_adds(db):
    src, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, src, "Sol Ring")
    new_id, _, _ = watchlist_db.clone_list(db, src, recovery=False)
    actions = [r["action"] for r in db.execute(
        "SELECT action FROM events WHERE list_id=? ORDER BY seq", (new_id,))]
    assert actions == ["create", "clone_init", "add"]
    # replay invariant holds for clones too
    replayed = watchlist_db.replay_state(db, new_id)
    assert [e["card_name"] for e in replayed.values()] == ["Sol Ring"]
