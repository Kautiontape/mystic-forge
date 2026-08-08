from starlette.testclient import TestClient

import server
import watchlist_db


def client():
    # StreamableHTTPSessionManager.run() is once-per-instance; each TestClient
    # runs a fresh lifespan, so force a fresh manager (prod builds once).
    server.mcp._session_manager = None
    return TestClient(server.build_app())


def test_health_ok(db_path):
    with client() as c:
        r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "last_ingest" in body and "lists" in body


def test_health_reports_stale_ingest(db_path):
    db = watchlist_db.connect(db_path)
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest','2020-01-01')")
    db.commit()
    db.close()
    with client() as c:
        body = c.get("/health").json()
    assert body["ingest_stale"] is True and body["status"] == "degraded"


def test_watch_page_shows_list(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db, label="My Deck")
    watchlist_db.add_card(db, list_id, "Sol Ring", note="<script>alert(1)</script>")
    db.close()
    with client() as c:
        r = c.get(f"/w/{pp}")
    assert r.status_code == 200
    assert "My Deck" in r.text and "Sol Ring" in r.text
    assert "<script>alert(1)" not in r.text   # note is escaped, never executable
    assert "&lt;script&gt;alert(1)" in r.text
    assert 'class="rev"' not in r.text        # history lives in its own view
    assert "/history" in r.text               # ...linked from the board


def test_watch_page_404_on_bad_passphrase(db_path):
    with client() as c:
        assert c.get("/w/bogus-bogus-bogus-bogus-00").status_code == 404


def test_share_page_readonly_no_passphrase_leak(db_path):
    db = watchlist_db.connect(db_path)
    _, pp, sc = watchlist_db.create_list(db, label="Shared")
    db.close()
    with client() as c:
        r = c.get(f"/s/{sc}")
    assert r.status_code == 200
    assert "Shared" in r.text
    assert pp not in r.text                  # passphrase never on share page
    assert 'id="recoverBtn"' not in r.text   # no recovery button on share page


def test_page_has_sparkline_and_themes(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-08-07", "tcgplayer", "normal", 8.0)
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "tcgplayer", "normal", 7.0)
    db.close()
    with client() as c:
        r = c.get(f"/w/{pp}")
    assert '<svg class="spark"' in r.text
    assert "macchiato" in r.text and "latte" in r.text
    assert "$7.00" in r.text


def test_history_view_pagination(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    for i in range(20):                       # 21 events with create
        watchlist_db.add_card(db, list_id, f"Card {i}")
    db.close()
    with client() as c:
        p1 = c.get(f"/w/{pp}/history").text
        p2 = c.get(f"/w/{pp}/history?hp=2").text
    assert p1.count('class="rev"') == 15      # EVENTS_PER_PAGE
    assert p2.count('class="rev"') == 6
    assert "#21" in p1 and "#1</span>" not in p1
    assert "#1</span>" in p2


def test_share_history_view_has_no_restore(db_path):
    db = watchlist_db.connect(db_path)
    _, _, sc = watchlist_db.create_list(db)
    db.close()
    with client() as c:
        r = c.get(f"/s/{sc}/history")
    assert r.status_code == 200
    assert 'id="forkBtn"' in r.text
    assert 'id="recoverBtn"' not in r.text


def test_shop_toggle_switches_provider_and_currency(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "tcgplayer", "normal", 7.0)
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "cardmarket", "normal", 5.5)
    db.close()
    with client() as c:
        default = c.get(f"/w/{pp}").text
        cm = c.get(f"/w/{pp}?shop=cardmarket").text
        bogus = c.get(f"/w/{pp}?shop=amazon").text
    assert "$7.00" in default
    assert "€5.50" in cm and "$7.00" not in cm
    assert "$7.00" in bogus                    # unknown shop falls back


def test_target_hit_lights_up_card(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=10.0)  # above price
    watchlist_db.add_card(db, list_id, "Cultivate", target_price=1.0)  # below price
    db.executemany("INSERT INTO card_uuids (card_name, uuid) VALUES (?,?)",
                   [("Sol Ring", "u1"), ("Cultivate", "u2")])
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "tcgplayer", "normal", 7.0)
    watchlist_db.upsert_price(db, "u2", "2026-08-08", "tcgplayer", "normal", 2.0)
    db.close()
    with client() as c:
        r = c.get(f"/w/{pp}").text
    assert r.count('class="card hit"') == 1
    assert "buy window" in r


def test_api_target_set_clear_and_share_forbidden(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, sc = watchlist_db.create_list(db)
    seq, _ = watchlist_db.add_card(db, list_id, "Sol Ring")
    db.close()
    with client() as c:
        r = c.post("/api/target", json={"key": pp, "entry_id": seq,
                                        "target_price": 12.5})
        assert r.status_code == 200 and r.json()["target_price"] == 12.5
        r = c.post("/api/target", json={"key": pp, "entry_id": seq,
                                        "target_price": None})
        assert r.status_code == 200 and r.json()["target_price"] is None
        r = c.post("/api/target", json={"key": sc, "entry_id": seq,
                                        "target_price": 5})
        assert r.status_code == 403
    db = watchlist_db.connect(db_path)
    actions = [x["action"] for x in db.execute(
        "SELECT action FROM events WHERE list_id=? ORDER BY seq", (list_id,))]
    db.close()
    assert actions == ["create", "add", "set_target", "set_target"]


def test_api_rename_and_share_forbidden(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, sc = watchlist_db.create_list(db, label="Old Name")
    db.close()
    with client() as c:
        r = c.post("/api/rename", json={"key": pp, "label": "New Name"})
        assert r.status_code == 200
        assert "New Name" in c.get(f"/w/{pp}").text
        assert c.post("/api/rename", json={"key": sc, "label": "x"}).status_code == 403
    db = watchlist_db.connect(db_path)
    assert watchlist_db.get_list(db, list_id)["label"] == "New Name"
    # rename is in the event chain but replay of entries is unaffected
    assert watchlist_db.replay_state(db, list_id) == {}
    db.close()


def test_card_modal_payload_has_site_links(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", set_code="C21",
                          collector_number="263")
    db.close()
    with client() as c:
        r = c.get(f"/w/{pp}").text
    assert "scryfall.com/card/c21/263" in r
    assert "edhrec.com/cards/sol-ring" in r
    assert "mtgstocks.com" in r and "tcgplayer.com" in r


def test_api_revision_snapshot(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, sc = watchlist_db.create_list(db)
    s1, _ = watchlist_db.add_card(db, list_id, "Sol Ring", target_price=5.0)
    watchlist_db.add_card(db, list_id, "Cultivate")
    db.close()
    with client() as c:
        r = c.get(f"/api/revision/{sc}/{s1}")      # share code can read
    assert r.status_code == 200
    d = r.json()
    assert [e["card_name"] for e in d["entries"]] == ["Sol Ring"]
    with client() as c:
        assert c.get("/api/revision/SC-NOPE99/1").status_code == 404


def test_api_fork_and_recover(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, sc = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.close()

    with client() as c:
        r = c.post("/api/fork", json={"key": sc})            # share → fork ok
    assert r.status_code == 200
    body = r.json()
    assert body["passphrase"].count("-") == 4
    db = watchlist_db.connect(db_path)
    assert watchlist_db.get_list(db, list_id)["superseded_by"] is None
    db.close()

    with client() as c:                                       # share → recover forbidden
        r = c.post("/api/fork", json={"key": sc, "mode": "recover"})
    assert r.status_code == 403

    with client() as c:                                       # passphrase → recover ok
        r = c.post("/api/fork", json={"key": pp, "mode": "recover"})
    assert r.status_code == 200
    db = watchlist_db.connect(db_path)
    assert watchlist_db.get_list(db, list_id)["superseded_by"] is not None
    db.close()
