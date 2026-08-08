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


def test_watch_page_shows_list_and_history(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db, label="My Deck")
    watchlist_db.add_card(db, list_id, "Sol Ring", note="<script>")
    db.close()
    with client() as c:
        r = c.get(f"/w/{pp}")
    assert r.status_code == 200
    assert "My Deck" in r.text and "Sol Ring" in r.text
    assert "<script>" not in r.text          # escaped
    assert "&lt;script&gt;" in r.text


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
