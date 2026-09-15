import re

import pytest

from mystic_forge.watchlist import db as watchlist_db


def test_init_db_creates_tables(db):
    names = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"lists", "events", "watchlist_current", "prices", "meta",
            "card_uuids"} <= names


def test_single_shop_envelope_is_a_seek_not_a_shop_scan(db):
    """A one-shop board asks for envelopes with `provider IN (?)`. 1.3.1's
    (provider, date) index tempted the planner into satisfying the GROUP BY
    from it and scanning every row at that shop -- 30s a page. The plan
    must be the fully constrained covering index, with no other index on
    prices left around to lure it."""
    names = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='prices'")}
    assert names == {"sqlite_autoindex_prices_1", "idx_prices_pfudp"}, names
    plan = " ".join(r[3] for r in db.execute(
        "EXPLAIN QUERY PLAN SELECT date, MIN(price) FROM prices"
        " WHERE provider IN (?) AND finish=? AND uuid IN (?, ?)"
        " GROUP BY date ORDER BY date", ("cardkingdom", "normal", "u1", "u2")))
    assert "COVERING INDEX idx_prices_pfudp (provider=? AND finish=? AND uuid=?)" in plan, plan


def test_envelope_is_answered_from_a_covering_index(db):
    """A printing's history at one shop must come straight off the index:
    rows live in the table in ingest order, so a card's history is a random
    page read per row on a cold cache without `price` in the index."""
    plan = " ".join(r[3] for r in db.execute(
        "EXPLAIN QUERY PLAN SELECT date, MIN(price) FROM prices"
        " WHERE provider IN (?, ?) AND finish=? AND uuid IN (?, ?)"
        " GROUP BY date", ("tcgplayer", "cardkingdom", "normal", "u1", "u2")))
    assert "COVERING INDEX idx_prices_pfudp" in plan, plan


def test_init_db_drops_the_superseded_price_index(db):
    db.execute("CREATE INDEX IF NOT EXISTS idx_prices_pfud"
               " ON prices(provider, finish, uuid, date)")
    watchlist_db.init_db(db)
    names = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_prices_pfud" not in names and "idx_prices_pfudp" in names


def test_init_db_drops_the_planner_trap_index(db):
    db.execute("CREATE INDEX IF NOT EXISTS idx_prices_pd ON prices(provider, date)")
    watchlist_db.init_db(db)
    names = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_prices_pd" not in names


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


# ── target rules, per-card shop, cheapest-across-markets ─────────────────────

def _priced(db, name, rows):
    """rows: [(date, provider, price)] for one uuid named after the card."""
    u = "u-" + name.lower().replace(" ", "-")
    db.execute("INSERT OR IGNORE INTO card_uuids (card_name, uuid) VALUES (?,?)",
               (name, u))
    for date, provider, price in rows:
        watchlist_db.upsert_price(db, u, date, provider, "normal", price)
    return u


def test_basis_is_cheapest_usd_shop_unless_pinned(db):
    list_id, _, _ = watchlist_db.create_list(db)
    _, e = watchlist_db.add_card(db, list_id, "Sol Ring")
    _priced(db, "Sol Ring", [("2026-09-14", "tcgplayer", 1.50),
                             ("2026-09-14", "manapool", 1.10),
                             ("2026-09-14", "cardkingdom", 1.99),
                             ("2026-09-14", "cardmarket", 0.50)])   # EUR: excluded
    s = watchlist_db.basis_summary(db, e)
    assert (s["current"], s["provider"]) == (1.10, "manapool")
    _, e = watchlist_db.add_card(db, list_id, "Sol Ring", shop="tcgplayer")
    assert e["shop"] == "tcgplayer"
    s = watchlist_db.basis_summary(db, e)
    assert (s["current"], s["provider"]) == (1.50, "tcgplayer")
    assert watchlist_db.entry_currency(e) == "$"
    e = watchlist_db.set_entry_shop(db, list_id, e["entry_id"], "cardmarket")
    assert watchlist_db.basis_summary(db, e)["current"] == 0.50
    assert watchlist_db.entry_currency(e) == "€"
    e = watchlist_db.set_entry_shop(db, list_id, e["entry_id"], None)
    assert e["shop"] is None
    with pytest.raises(ValueError):
        watchlist_db.set_entry_shop(db, list_id, e["entry_id"], "amazon")


def test_multi_shop_envelope_is_per_date_minimum(db):
    list_id, _, _ = watchlist_db.create_list(db)
    _, e = watchlist_db.add_card(db, list_id, "Cultivate")
    u = _priced(db, "Cultivate", [("2026-09-12", "tcgplayer", 2.0),
                                  ("2026-09-12", "manapool", 3.0),
                                  ("2026-09-13", "tcgplayer", 2.5),
                                  ("2026-09-13", "manapool", 1.0),
                                  ("2026-09-14", "cardkingdom", 1.8)])
    series = watchlist_db.price_series(db, [u], provider=watchlist_db.USD_SHOPS,
                                       today="2026-09-14")
    assert series["points"] == [("2026-09-12", 2.0), ("2026-09-13", 1.0),
                                ("2026-09-14", 1.8)]
    assert series["provider"] == "cardkingdom"
    by_shop = watchlist_db.latest_by_shop(db, [u])
    assert by_shop == {"tcgplayer": (2.5, "2026-09-13"),
                       "manapool": (1.0, "2026-09-13"),
                       "cardkingdom": (1.8, "2026-09-14")}


def test_low_rule_follows_the_historic_low(db):
    list_id, _, _ = watchlist_db.create_list(db)
    _, e = watchlist_db.add_card(db, list_id, "Rhystic Study",
                                 target_mode="low", target_pct=10)
    assert (e["target_mode"], e["target_pct"], e["target_price"]) == ("low", 10.0, None)
    # one point: nothing to measure against yet
    _priced(db, "Rhystic Study", [("2026-09-10", "tcgplayer", 40.0)])
    s = watchlist_db.basis_summary(db, e)
    assert watchlist_db.effective_target(db, e, s) is None
    # prior low 40 → 10% under is 36; today 35 is a buy window
    _priced(db, "Rhystic Study", [("2026-09-11", "tcgplayer", 42.0),
                                  ("2026-09-12", "tcgplayer", 35.0)])
    s = watchlist_db.basis_summary(db, e)
    assert watchlist_db.effective_target(db, e, s) == 36.0
    assert watchlist_db.is_hit(e, s, 36.0)
    assert (s["low"], s["low_date"]) == (35.0, "2026-09-12")
    # the rule moved with it: next day the reference low is 35
    _priced(db, "Rhystic Study", [("2026-09-13", "tcgplayer", 34.0)])
    s = watchlist_db.basis_summary(db, e)
    assert watchlist_db.effective_target(db, e, s) == 31.5
    assert not watchlist_db.is_hit(e, s, 31.5)
    # match mode (0%): at or under the prior low counts
    e = watchlist_db.set_entry_target(db, list_id, e["entry_id"], None,
                                      target_mode="low", target_pct=0)
    assert watchlist_db.effective_target(db, e, s) == 35.0
    assert watchlist_db.is_hit(e, s, 35.0)
    # back to a fixed number clears the rule
    e = watchlist_db.set_entry_target(db, list_id, e["entry_id"], 30.0)
    assert (e["target_mode"], e["target_pct"], e["target_price"]) == ("fixed", 0.0, 30.0)


def test_rule_and_shop_survive_replay_and_clone(db):
    list_id, _, _ = watchlist_db.create_list(db)
    seq, e = watchlist_db.add_card(db, list_id, "Sol Ring", target_mode="low",
                                   target_pct=5, shop="manapool", note="n")
    watchlist_db.add_card(db, list_id, "Cultivate", target_price=2.0)
    state = watchlist_db.replay_state(db, list_id)
    assert state[seq]["target_mode"] == "low" and state[seq]["target_pct"] == 5.0
    assert state[seq]["shop"] == "manapool"
    # pre-rule events (no mode keys) replay as fixed targets
    db.execute("INSERT INTO events (list_id, seq, ts, action, payload_json)"
               " VALUES (?,?,?,?,?)",
               (list_id, 99, "2026-01-01T00:00:00Z", "set_target",
                '{"entry_id": %d, "target_price": 7.0}' % seq))
    db.commit()
    state = watchlist_db.replay_state(db, list_id)
    assert (state[seq]["target_mode"], state[seq]["target_price"]) == ("fixed", 7.0)
    new_id, _, _ = watchlist_db.clone_list(db, list_id)
    cloned = {x["card_name"]: x for x in watchlist_db.current_entries(db, new_id)}
    assert cloned["Sol Ring"]["target_mode"] == "fixed"   # the latest state
    assert cloned["Sol Ring"]["shop"] == "manapool" and cloned["Sol Ring"]["note"] == "n"
    assert cloned["Cultivate"]["target_price"] == 2.0


def test_add_card_updates_rule_and_shop_on_existing_entry(db):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=2.0)
    _, e = watchlist_db.add_card(db, list_id, "Sol Ring", target_mode="low",
                                 target_pct=20, shop="cardkingdom")
    assert (e["target_mode"], e["target_pct"], e["target_price"], e["shop"]) == \
        ("low", 20.0, None, "cardkingdom")
    actions = [r["action"] for r in db.execute(
        "SELECT action FROM events WHERE list_id=? ORDER BY seq", (list_id,))]
    assert actions == ["create", "add", "set_target", "set_shop"]
    # nothing changed → no new events
    watchlist_db.add_card(db, list_id, "Sol Ring", target_mode="low",
                          target_pct=20, shop="cardkingdom")
    assert db.execute("SELECT COUNT(*) FROM events WHERE list_id=?",
                      (list_id,)).fetchone()[0] == 4


def test_init_db_migrates_a_pre_rule_database(tmp_path):
    """Production tables predate target_mode/target_pct/shop: init_db must
    add them in place and leave existing rows readable and hit-able."""
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
    CREATE TABLE lists (id INTEGER PRIMARY KEY, passphrase_hash TEXT NOT NULL UNIQUE,
      share_code TEXT NOT NULL UNIQUE, label TEXT, created_at TEXT NOT NULL,
      cloned_from_list INTEGER, cloned_from_seq INTEGER, superseded_by INTEGER);
    CREATE TABLE events (list_id INTEGER NOT NULL, seq INTEGER NOT NULL, ts TEXT NOT NULL,
      action TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (list_id, seq));
    CREATE TABLE watchlist_current (list_id INTEGER NOT NULL, entry_id INTEGER NOT NULL,
      card_name TEXT NOT NULL, set_code TEXT, collector_number TEXT, uuid TEXT,
      target_price REAL, note TEXT, added_at TEXT NOT NULL, bought_at TEXT,
      PRIMARY KEY (list_id, entry_id));
    INSERT INTO lists VALUES (1, 'h', 'SC-OLD001', 'Old', '2026-01-01T00:00:00Z', NULL, NULL, NULL);
    INSERT INTO events VALUES (1, 1, '2026-01-01T00:00:00Z', 'create', '{"label": "Old"}');
    INSERT INTO events VALUES (1, 2, '2026-01-01T00:00:00Z', 'add',
      '{"card_name": "Sol Ring", "set_code": null, "collector_number": null, "target_price": 2.0, "note": null, "added_at": "2026-01-01T00:00:00Z"}');
    INSERT INTO watchlist_current VALUES (1, 2, 'Sol Ring', NULL, NULL, NULL, 2.0, NULL, '2026-01-01T00:00:00Z', NULL);
    """)
    old.commit()
    old.close()
    db = watchlist_db.connect(path)
    watchlist_db.init_db(db)
    cols = {r[1] for r in db.execute("PRAGMA table_info(watchlist_current)")}
    assert {"target_mode", "target_pct", "shop"} <= cols
    e = watchlist_db.current_entries(db, 1)[0]
    assert (e["target_mode"], e["target_pct"], e["shop"], e["target_price"]) == \
        ("fixed", 0.0, None, 2.0)
    assert watchlist_db.replay_state(db, 1)[2]["target_mode"] == "fixed"
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-09-14", "manapool", "normal", 1.5)
    s = watchlist_db.basis_summary(db, e)
    assert s["current"] == 1.5 and s["provider"] == "manapool"
    assert watchlist_db.is_hit(e, s, watchlist_db.effective_target(db, e, s))
    # and the new fields write into the migrated table
    e = watchlist_db.set_entry_target(db, 1, 2, None, target_mode="low", target_pct=10)
    assert (e["target_mode"], e["target_pct"]) == ("low", 10.0)
    db.close()
