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
    assert f"/s/{pp}" not in r.text           # passphrase never leaks in share hints


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


def test_history_resolves_entry_names_for_target_events(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    seq, _ = watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_db.set_entry_target(db, list_id, seq, 2.5)
    db.close()
    with client() as c:
        r = c.get(f"/w/{pp}/history").text
    assert "target set" in r
    assert "Sol Ring → $2.50" in r


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


def test_target_basis_is_shop_independent(db_path):
    """Targets are USD/tcgplayer-basis: hit state and the buy-windows tile
    must not change when the display shop changes (persona: the Optimizer)."""
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Rhystic Study", target_price=70.0)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES"
               " ('Rhystic Study','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "tcgplayer", "normal", 65.0)
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "cardmarket", "normal", 80.0)
    db.close()
    with client() as c:
        tcg = c.get(f"/w/{pp}").text
        cm = c.get(f"/w/{pp}?shop=cardmarket").text
    for page in (tcg, cm):
        assert page.count('class="card hit"') == 1     # hit on USD basis
        assert "target $70.00" in page                 # never relabeled €
    assert "€80.00" in cm                              # display price is CM's


def test_hits_sort_before_misses(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Miss One", target_price=1.0)
    watchlist_db.add_card(db, list_id, "Hit One", target_price=10.0)
    db.executemany("INSERT INTO card_uuids (card_name, uuid) VALUES (?,?)",
                   [("Miss One", "m1"), ("Hit One", "h1")])
    watchlist_db.upsert_price(db, "m1", "2026-08-08", "tcgplayer", "normal", 5.0)
    watchlist_db.upsert_price(db, "h1", "2026-08-08", "tcgplayer", "normal", 5.0)
    db.close()
    with client() as c:
        page = c.get(f"/w/{pp}").text
    assert page.index("Hit One") < page.index("Miss One")
    assert 'class="verdict verdict--buy"' in page and "BUY" in page


def test_freshness_shows_price_date_not_ingest_date(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-08-07", "tcgplayer", "normal", 7.0)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest','2026-08-08')")
    db.commit()
    db.close()
    with client() as c:
        page = c.get(f"/w/{pp}").text
    assert "prices through 2026-08-07" in page


def test_share_page_offers_claim_and_owner_page_does_not(db_path):
    db = watchlist_db.connect(db_path)
    _, pp, sc = watchlist_db.create_list(db)
    db.close()
    with client() as c:
        share = c.get(f"/s/{sc}").text
        own = c.get(f"/w/{pp}").text
    assert "Make my own copy" in share
    assert "Make my own copy" not in own
    assert 'id="renameDlg"' in own and 'id="renameDlg"' not in share
    assert "prompt(" not in own                # bespoke dialog, not browser chrome


def test_revision_modal_js_escapes_interpolations(db_path):
    """The revision modal builds innerHTML from API data; every user-supplied
    field must round-trip through the X() escaper (stored-XSS regression)."""
    db = watchlist_db.connect(db_path)
    _, pp, _ = watchlist_db.create_list(db)
    db.close()
    with client() as c:
        page = c.get(f"/w/{pp}/history").text
    assert "X(e.card_name)" in page and "X(e.note" in page
    assert "X(e.set_code)" in page


def test_shop_survives_history_roundtrip(db_path):
    db = watchlist_db.connect(db_path)
    _, pp, _ = watchlist_db.create_list(db)
    db.close()
    with client() as c:
        board = c.get(f"/w/{pp}?shop=cardmarket").text
        hist = c.get(f"/w/{pp}/history?shop=cardmarket").text
    import server as srv
    px = srv.PUBLIC_PREFIX                       # gateway prefix, e.g. "/mtg"
    assert f"{px}/w/{pp}/history?shop=cardmarket" in board
    assert f'href="{px}/w/{pp}?shop=cardmarket"' in hist


def test_mint_throttle_limits_new_lists(db_path, monkeypatch):
    """Spec mitigation: a public no-auth URL can't be farmed for lists."""
    import server as srv
    monkeypatch.setattr(srv, "MINT_LIMIT", 2)
    monkeypatch.setattr(srv, "_mint_log", {})
    db = watchlist_db.connect(db_path)
    _, _, sc = watchlist_db.create_list(db)
    db.close()
    codes = []
    with client() as c:
        for _ in range(3):
            r = c.post("/api/fork", json={"key": sc})
            codes.append(r.status_code)
    assert codes == [200, 200, 429]


async def test_mint_throttle_applies_to_the_mcp_tool(db_path, monkeypatch):
    import server as srv
    monkeypatch.setattr(srv, "MINT_LIMIT", 1)
    monkeypatch.setattr(srv, "_mint_log", {})
    first = await srv.watchlist_create(srv.WatchlistCreateInput(label="a"))
    second = await srv.watchlist_create(srv.WatchlistCreateInput(label="b"))
    assert "Passphrase" in first
    assert "Too many" in second


def test_csv_export(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=10.0)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-08-01", "tcgplayer", "normal", 8.0)
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "tcgplayer", "normal", 7.0)
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "cardmarket", "normal", 5.0)
    db.close()
    with client() as c:
        r = c.get(f"/w/{pp}/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("card,set_code")
    assert lines[1].startswith("Sol Ring,")
    assert "7.0" in lines[1] and "5.0" in lines[1] and "2026-08-08" in lines[1]
    with client() as c:
        assert c.get("/w/bogus-bogus-bogus-bogus-00/export.csv").status_code == 404


def test_modal_chart_draws_target_line(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=7.5)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-08-01", "tcgplayer", "normal", 8.0)
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "tcgplayer", "normal", 7.0)
    db.close()
    with client() as c:
        page = c.get(f"/w/{pp}").text
    assert "targetline" in page and "target $7.50" in page


def test_cards_are_keyboard_accessible(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.close()
    with client() as c:
        page = c.get(f"/w/{pp}").text
    assert "keyable(card)" in page and ":focus-visible" in page


def _seeded_list(db_path, **card_kwargs):
    db = watchlist_db.connect(db_path)
    list_id, pp, sc = watchlist_db.create_list(db)
    seq, _ = watchlist_db.add_card(db, list_id, "Sol Ring", **card_kwargs)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-08-08", "tcgplayer", "normal", 7.0)
    db.close()
    return list_id, pp, sc, seq


def test_api_bought_mutes_card_and_leaves_math(db_path):
    list_id, pp, sc, seq = _seeded_list(db_path, target_price=10.0)
    with client() as c:
        r = c.post("/api/bought", json={"key": pp, "entry_id": seq})
        assert r.status_code == 200 and r.json()["bought_at"]
        assert c.post("/api/bought",
                      json={"key": sc, "entry_id": seq}).status_code == 403
        page = c.get(f"/w/{pp}").text
    assert 'class="card bought"' in page
    assert "✓ bought" in page
    assert "<b>0</b><span>buy windows</span>" in page   # bought leaves the math
    assert 'class="verdict"' not in page or "BUY" not in page
    with client() as c:                                  # and it's reversible
        c.post("/api/bought", json={"key": pp, "entry_id": seq, "bought": False})
        page = c.get(f"/w/{pp}").text
    assert 'class="card hit"' in page


def test_bought_replay_and_history(db_path):
    list_id, pp, sc, seq = _seeded_list(db_path)
    db = watchlist_db.connect(db_path)
    watchlist_db.set_bought(db, list_id, seq)
    replayed = watchlist_db.replay_state(db, list_id)
    assert replayed[seq]["bought_at"] is not None
    db.close()
    with client() as c:
        hist = c.get(f"/w/{pp}/history").text
    assert ">bought</span>" in hist or "bought</span>" in hist


def test_bought_sorts_last(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    s1, _ = watchlist_db.add_card(db, list_id, "Bought One")
    watchlist_db.add_card(db, list_id, "Active One")
    watchlist_db.set_bought(db, list_id, s1)
    db.close()
    with client() as c:
        page = c.get(f"/w/{pp}").text
    assert page.index("Active One") < page.index("Bought One")


def test_api_remove_from_page(db_path):
    list_id, pp, sc, seq = _seeded_list(db_path)
    with client() as c:
        assert c.post("/api/remove",
                      json={"key": sc, "entry_id": seq}).status_code == 403
        r = c.post("/api/remove", json={"key": pp, "entry_id": seq})
        assert r.status_code == 200 and r.json()["removed"] == "Sol Ring"
        assert c.post("/api/remove",
                      json={"key": pp, "entry_id": 999}).status_code == 404


def test_api_resolve_and_add_flow(db_path, monkeypatch):
    import server as srv

    async def fake_scryfall(endpoint, params=None):
        import httpx
        if endpoint == "/cards/c21/263":
            return {"name": "Sol Ring", "set": "c21", "collector_number": "263",
                    "prices": {"usd": "2.50"}}
        if endpoint == "/cards/named":
            return {"name": "Rhystic Study", "prices": {"usd": "40.00"}}
        req = httpx.Request("GET", "x://x")
        raise httpx.HTTPStatusError("404", request=req,
                                    response=httpx.Response(404, request=req))
    monkeypatch.setattr(srv, "_scryfall_get", fake_scryfall)

    db = watchlist_db.connect(db_path)
    list_id, pp, sc = watchlist_db.create_list(db)
    db.close()
    with client() as c:
        r = c.post("/api/resolve", json={
            "key": pp, "query": "https://scryfall.com/card/c21/263/sol-ring"})
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == "Sol Ring" and d["set_code"] == "C21"
        assert "scryfall.com" in d["sites"]
        r = c.post("/api/resolve", json={"key": pp, "query": "rhystic stud"})
        assert r.json()["name"] == "Rhystic Study"
        r = c.post("/api/add", json={"key": pp, "name": "Sol Ring",
                                     "set_code": "C21",
                                     "collector_number": "263",
                                     "target_price": 2.0})
        assert r.status_code == 200
        assert c.post("/api/add", json={"key": sc, "name": "X"}).status_code == 403
    db = watchlist_db.connect(db_path)
    entries = watchlist_db.current_entries(db, list_id)
    db.close()
    assert entries[0]["set_code"] == "C21" and entries[0]["target_price"] == 2.0


def test_board_ui_affordances(db_path):
    list_id, pp, sc, seq = _seeded_list(db_path, target_price=2.5)
    with client() as c:
        own = c.get(f"/w/{pp}").text
        share = c.get(f"/s/{sc}").text
    assert 'id="addCard"' in own and 'id="addCard"' not in share
    assert 'id="boughtBtn"' in own and 'id="boughtBtn"' not in share
    assert 'id="removeBtn"' in own and "Really remove?" in own
    assert own.count('class="xclose"') >= 3          # × on every dialog
    assert 'data-target="2.50"' in own               # two-decimal target
    assert 'id="alerts"' in own and "mystic-forge-sc-" in own.lower()
    assert 'class="mright"' in own                   # history demoted to right


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
