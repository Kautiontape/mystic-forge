import asyncio
import datetime
import logging
import os
import time

import pytest
from starlette.testclient import TestClient

import mystic_forge
import server
from mystic_forge.watchlist import db as watchlist_db
from mystic_forge.watchlist import ingest as watchlist_ingest
from mystic_forge.watchlist import mtgstocks


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


def _voting_fixture(db_path, card="Bitterblossom"):
    db = watchlist_db.connect(db_path)
    list_id, pp, sc = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, card)
    db.close()
    return pp, sc


def test_the_page_ships_the_browser_side_resolver(db_path):
    """The server cannot reach MTGStocks from this host, so the page carries
    the lookup and posts the answer back."""
    pp, _ = _voting_fixture(db_path)
    page = client().get(f"/w/{pp}").text
    assert "https://api.mtgstocks.com" in page
    assert "/search/autocomplete/" in page
    assert "/api/mtgstocks" in page


def test_a_card_carries_its_set_for_the_browser_to_pin(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Bitterblossom", set_code="MOR",
                          collector_number="62")
    db.close()
    assert 'data-set="MOR"' in client().get(f"/w/{pp}").text


def test_api_mtgstocks_accepts_a_vote_and_the_badge_appears(db_path):
    pp, _ = _voting_fixture(db_path)
    with client() as c:
        r = c.post("/api/mtgstocks",
                   json={"key": pp, "card_name": "Bitterblossom",
                         "print_id": 97426, "slug": "97426-bitterblossom"},
                   headers={"CF-Connecting-IP": "203.0.113.7"})
        assert r.status_code == 200
        assert r.json()["url"] == \
            "https://www.mtgstocks.com/prints/97426-bitterblossom"
        assert "/prints/97426-bitterblossom" in c.get(f"/w/{pp}").text


def test_api_mtgstocks_accepts_votes_from_share_pages_too(db_path):
    """The one write a read-only page may make: it names a global fact about
    a card, not anything about the list, and it is validated on arrival."""
    pp, sc = _voting_fixture(db_path)
    with client() as c:
        r = c.post("/api/mtgstocks",
                   json={"key": sc, "card_name": "Bitterblossom",
                         "print_id": 97426, "slug": "97426-bitterblossom"},
                   headers={"CF-Connecting-IP": "203.0.113.8"})
        assert r.status_code == 200
        assert "/prints/97426-bitterblossom" in c.get(f"/s/{sc}").text


def test_api_mtgstocks_rejects_a_slug_naming_another_card(db_path):
    pp, _ = _voting_fixture(db_path)
    with client() as c:
        r = c.post("/api/mtgstocks",
                   json={"key": pp, "card_name": "Bitterblossom",
                         "print_id": 3, "slug": "3-black-lotus"},
                   headers={"CF-Connecting-IP": "203.0.113.9"})
        assert r.status_code == 400
        assert "/prints/3-" not in c.get(f"/w/{pp}").text


def test_api_mtgstocks_rejects_a_card_that_is_not_on_the_list(db_path):
    pp, _ = _voting_fixture(db_path)
    with client() as c:
        r = c.post("/api/mtgstocks",
                   json={"key": pp, "card_name": "Black Lotus",
                         "print_id": 3, "slug": "3-black-lotus"},
                   headers={"CF-Connecting-IP": "203.0.113.10"})
        assert r.status_code == 404


def test_api_mtgstocks_rejects_an_unknown_key(db_path):
    _voting_fixture(db_path)
    with client() as c:
        r = c.post("/api/mtgstocks",
                   json={"key": "not-a-list", "card_name": "Bitterblossom",
                         "print_id": 97426, "slug": "97426-bitterblossom"})
        assert r.status_code == 404


def test_api_mtgstocks_counts_one_address_as_one_voter(db_path):
    """Ten posts from one address must not outvote one from another."""
    pp, _ = _voting_fixture(db_path)
    with client() as c:
        c.post("/api/mtgstocks",
               json={"key": pp, "card_name": "Bitterblossom",
                     "print_id": 97426, "slug": "97426-bitterblossom"},
               headers={"CF-Connecting-IP": "203.0.113.1"})
        for _ in range(10):
            c.post("/api/mtgstocks",
                   json={"key": pp, "card_name": "Bitterblossom",
                         "print_id": 2901, "slug": "2901-bitterblossom"},
                   headers={"CF-Connecting-IP": "203.0.113.2"})
        assert "/prints/97426-bitterblossom" in c.get(f"/w/{pp}").text
    db = watchlist_db.connect(db_path)
    votes = db.execute("SELECT voter FROM mtgstocks_votes").fetchall()
    db.close()
    assert len(votes) == 2
    assert not any("203.0.113" in v["voter"] for v in votes)   # hashed, not kept


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
    assert "<b>1</b><span>buy windows</span>" in page
    assert 'class="verdict' not in page          # the BUY/WAIT banner is gone


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


def _sorting_fixture(db_path):
    """Four cards with distinct signals + one bought.

    Alpha  $10, target 12 → hit  (distance -2)
    Beta   $30, target 31 → near (distance -? no: +... 30-31=-1 hit!) —
           use target 28 → distance +2 (near miss)
    Gamma  $5,  no target, d7 -4 (biggest drop), d30 -1
    Delta  $50, no target, d7 +1, d30 -20 (biggest 30d drop)
    Omega  bought, $1
    """
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    prices = {"Alpha": (10, 12.0), "Beta": (30, 28.0),
              "Gamma": (5, None), "Delta": (50, None), "Omega": (1, None)}
    # dates relative to today: the deltas are measured from the real clock
    today = datetime.date.today()
    d = lambda days: (today - datetime.timedelta(days=days)).isoformat()  # noqa: E731
    for name, (cur, tgt) in prices.items():
        seq, _ = watchlist_db.add_card(db, list_id, name, target_price=tgt)
        u = f"u-{name}"
        db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES (?,?)",
                   (name, u))
        d7ref = {"Gamma": cur + 4, "Delta": cur - 1}.get(name, cur)
        d30ref = {"Gamma": cur + 1, "Delta": cur + 20}.get(name, cur)
        watchlist_db.upsert_price(db, u, d(30), "tcgplayer", "normal", d30ref)
        watchlist_db.upsert_price(db, u, d(7), "tcgplayer", "normal", d7ref)
        watchlist_db.upsert_price(db, u, d(0), "tcgplayer", "normal", cur)
        if name == "Omega":
            watchlist_db.set_bought(db, list_id, seq)
    db.close()
    return pp


def _order(page, names):
    return sorted(names, key=lambda n: page.index(f"<h3>{n}"))


def test_sort_default_is_distance_to_target_bought_last(db_path):
    pp = _sorting_fixture(db_path)
    with client() as c:
        page = c.get(f"/w/{pp}").text
    order = _order(page, ["Alpha", "Beta", "Gamma", "Delta", "Omega"])
    assert order[0] == "Alpha"            # hit: below target
    assert order[1] == "Beta"             # nearest above target
    assert order[-1] == "Omega"           # bought always last
    assert 'class="sortbar"' in page and ">near target</a>" in page


def test_sort_variants(db_path):
    pp = _sorting_fixture(db_path)
    with client() as c:
        cheap = c.get(f"/w/{pp}?sort=price").text
        d7 = c.get(f"/w/{pp}?sort=d7").text
        d30 = c.get(f"/w/{pp}?sort=d30").text
        age = c.get(f"/w/{pp}?sort=age").text
        bogus = c.get(f"/w/{pp}?sort=banana").text
    live = ["Alpha", "Beta", "Gamma", "Delta"]
    assert _order(cheap, live)[0] == "Gamma"          # $5 cheapest
    assert _order(cheap, live + ["Omega"])[-1] == "Omega"
    assert _order(d7, live)[0] == "Gamma"             # -4 biggest 7d drop
    assert _order(d30, live)[0] == "Delta"            # -20 biggest 30d drop
    assert _order(age, live)[0] == "Delta"            # newest add first
    assert _order(bogus, live)[0] == "Alpha"          # unknown → default


def test_bought_toggle_hides_and_shows(db_path):
    pp = _sorting_fixture(db_path)
    with client() as c:
        shown = c.get(f"/w/{pp}").text
        hidden = c.get(f"/w/{pp}?bought=hide").text
    assert "<h3>Omega" in shown and "hide bought (1)" in shown
    assert "<h3>Omega" not in hidden and "show bought (1)" in hidden
    assert "bought=hide" in hidden        # state preserved in view links


def test_sort_state_survives_shop_switch(db_path):
    pp = _sorting_fixture(db_path)
    with client() as c:
        page = c.get(f"/w/{pp}?sort=d7&bought=hide").text
    assert f"/w/{pp}?sort=d7&shop=cardkingdom&bought=hide" in page


async def test_board_streams_its_head_before_reading_prices(db_path):
    """The head -- title, theme, OG tags -- goes out as its own chunk before
    any card is rendered, so the page paints (and unfurlers get their tags)
    while the prices are still being read. TestClient collects the whole
    body, so the chunks are read off the response's iterator directly."""
    from starlette.requests import Request
    db = watchlist_db.connect(db_path)
    _, pp, sc = watchlist_db.create_list(db, label="Streamed")
    db.close()
    for key, editable in ((pp, True), (sc, False)):
        row = dict(watchlist_db.get_list_by_passphrase(
            watchlist_db.connect(db_path), pp))
        row["_key"] = key
        request = Request({"type": "http", "method": "GET", "path": "/",
                           "query_string": b"sort=price", "headers": []})
        resp = server._stream_board(request, row, editable, False)
        assert resp.media_type == "text/html"
        chunks = [ch async for ch in resp.body_iterator]
        assert len(chunks) == 2
        head, body = chunks
        assert 'og:title" content="Streamed · a Magic price watchlist"' in head
        assert 'id="wait"' in head and 'class="grid"' not in head
        assert 'class="subtitle"' in body and 'class="subtitle"' not in head
        assert 'class="grid"' in body and body.rstrip().endswith("</html>")
        assert 'class="on">cheapest</a>' in body     # query params reach the body
    with client() as c:
        r = c.get(f"/w/{pp}")
        assert r.status_code == 200 and "content-length" not in r.headers
        assert r.text.count("<!doctype html>") == 1


def test_og_preview_tags_static_and_served(db_path):
    """Link previews get a real card; nothing perishable in the description."""
    db = watchlist_db.connect(db_path)
    _, pp, sc = watchlist_db.create_list(db, label="Cloud Voltron")
    db.close()
    with client() as c:
        share = c.get(f"/s/{sc}").text
        img = c.get("/og.png")
    assert 'og:title" content="Cloud Voltron · a Magic price watchlist"' in share
    assert 'og:image" content="' in share and "/og.png" in share
    assert 'twitter:card" content="summary_large_image"' in share
    import re
    desc = re.search(r'og:description" content="([^"]*)"', share, re.S).group(1)
    assert "$" not in desc and "202" not in desc      # no prices, no dates
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_pager_numbered_and_sort_agnostic(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    for i in range(30 * 5):                       # 150 cards → 7 pages of 24
        watchlist_db.add_card(db, list_id, f"Bulk Card {i:03d}")
    db.close()
    with client() as c:
        p1 = c.get(f"/w/{pp}").text
        p4 = c.get(f"/w/{pp}?cp=4").text
    assert "older ›" not in p1 and "‹ newer" not in p1  # sort-agnostic labels
    assert "Next ›" in p1 and 'class="pnum dis">‹ Prev' in p1
    assert 'class="pnum cur" aria-current="page">4' in p4
    assert p4.count('class="gap"') == 2                # 1 … 3 4 5 … 7
    assert f'href="{server.PUBLIC_PREFIX}/w/{pp}?cp=7"' in p4  # last page reachable


def test_api_note_set_clear_and_share_forbidden(db_path):
    list_id, pp, sc, seq = _seeded_list(db_path)
    with client() as c:
        r = c.post("/api/note", json={"key": pp, "entry_id": seq,
                                      "note": "Cloud deck, batch 2"})
        assert r.status_code == 200 and r.json()["note"] == "Cloud deck, batch 2"
        page = c.get(f"/w/{pp}").text
        assert "Cloud deck, batch 2" in page
        assert 'data-note="Cloud deck, batch 2"' in page
        r = c.post("/api/note", json={"key": pp, "entry_id": seq, "note": ""})
        assert r.status_code == 200 and r.json()["note"] is None
        assert c.post("/api/note", json={"key": sc, "entry_id": seq,
                                         "note": "x"}).status_code == 403
    db = watchlist_db.connect(db_path)
    actions = [x["action"] for x in db.execute(
        "SELECT action FROM events WHERE list_id=? ORDER BY seq", (list_id,))]
    db.close()
    assert actions == ["create", "add", "set_note", "set_note"]


def test_note_editor_in_modals_editable_only(db_path):
    list_id, pp, sc, seq = _seeded_list(db_path)
    with client() as c:
        own = c.get(f"/w/{pp}").text
        share = c.get(f"/s/{sc}").text
    assert 'id="noteInput"' in own and 'id="noteInput"' not in share
    assert "addNote" in own                      # add-card flow asks for a note


def test_api_add_accepts_note(db_path, monkeypatch):
    import server as srv

    async def fake(endpoint, params=None):
        return {"name": "Cultivate", "prices": {"usd": "1.00"}}
    monkeypatch.setattr(srv, "_scryfall_get", fake)
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    db.close()
    with client() as c:
        r = c.post("/api/add", json={"key": pp, "name": "Cultivate",
                                     "note": "ramp package"})
    assert r.status_code == 200
    db = watchlist_db.connect(db_path)
    assert watchlist_db.current_entries(db, list_id)[0]["note"] == "ramp package"
    db.close()


def test_filter_normalizes_names(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sram, Senior Edificer")
    watchlist_db.add_card(db, list_id, "Sword of Hearth and Home")
    db.close()
    with client() as c:
        hit = c.get(f"/w/{pp}?q=sram senior").text     # comma+case ignored
        miss = c.get(f"/w/{pp}?q=zzz").text
        clear = c.get(f"/w/{pp}").text
    assert "<h3>Sram" in hit and "<h3>Sword" not in hit
    assert "<article" not in miss and "No cards match" in miss
    assert "<h3>Sram" in clear and "<h3>Sword" in clear
    assert 'id="filter"' in clear
    assert 'value="sram senior"' in hit               # box keeps the query


def test_filter_state_preserved_in_view_links(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sram, Senior Edificer")
    db.close()
    with client() as c:
        page = c.get(f"/w/{pp}?q=sram&sort=price").text
    assert "sort=price" in page and "q=sram" in page   # both survive in links


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
    assert "<b>0</b><span>buy windows</span>" in page   # bought: no window
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
    # MTGStocks only has per-printing URLs, so its badge needs a resolved id.
    mtgstocks.remember(db, "Sol Ring", "C21", 5210, "sol-ring")
    db.close()
    with client() as c:
        r = c.get(f"/w/{pp}").text
    assert "scryfall.com/card/c21/263" in r
    assert "edhrec.com/cards/sol-ring" in r
    assert "mtgstocks.com/prints/5210-sol-ring" in r and "tcgplayer.com" in r


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


# ═══════════════════════════════════════════════════════════════════════════════
# SIDECAR WIRING — /health reporting and the one-shot startup build
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _no_inherited_sidecar_stats():
    """/health caches stats() in a module global, and a cache that outlived
    its test would answer the next one with the wrong sidecar."""
    server._sidecar_stats_cache = None
    yield
    server._sidecar_stats_cache = None


def _sidecar_env(db_path, monkeypatch):
    """Point the sidecar helpers at this test's tmp dir and return that path,
    whatever the ambient environment happens to say."""
    monkeypatch.delenv("MYSTIC_FORGE_DATA", raising=False)
    monkeypatch.delenv("MYSTIC_FORGE_NO_SIDECAR", raising=False)
    return watchlist_ingest._sidecar_path(os.path.dirname(db_path))


def test_health_reports_sidecar_state(db_path, monkeypatch):
    """Operators need to see whether the sidecar exists and how deep it goes."""
    monkeypatch.setattr(server.price_sidecar, "stats",
                        lambda _p: {"ready": True, "points": 42,
                                    "earliest": "2026-01-01",
                                    "latest": "2026-08-09"})
    with client() as c:
        body = c.get("/health").json()
    assert body["sidecar"]["ready"] is True
    assert body["sidecar"]["points"] == 42


def test_health_survives_a_missing_sidecar(db_path, monkeypatch):
    """No sidecar is a normal state, not an error."""
    _sidecar_env(db_path, monkeypatch)
    with client() as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["sidecar"]["ready"] is False


def test_health_names_why_the_sidecar_is_not_ready(db_path, monkeypatch):
    """"absent" and "corrupt" are the difference between a first boot and
    history that has just been thrown away. They are byte-identical in
    `ready`, and /health is where an operator gets to tell them apart."""
    side = _sidecar_env(db_path, monkeypatch)
    with client() as c:
        assert c.get("/health").json()["sidecar"]["reason"] == "absent"
    server._sidecar_stats_cache = None
    with open(side, "wb") as f:
        f.write(b"this is not a database")
    with client() as c:
        assert c.get("/health").json()["sidecar"]["reason"] == "corrupt"


def test_health_runs_sidecar_stats_off_the_event_loop(db_path, monkeypatch):
    """stats() is three full table scans -- ~3.2s on a 54M-row sidecar. Called
    inline it blocks the loop for that long on the one endpoint an uptime
    monitor polls every 10-30s, stalling every in-flight MCP request with it."""
    where = {}

    def probe(_p):
        try:
            asyncio.get_running_loop()
            where["on_loop"] = True
        except RuntimeError:
            where["on_loop"] = False
        return {"ready": True, "points": 1}

    monkeypatch.setattr(server.price_sidecar, "stats", probe)
    with client() as c:
        assert c.get("/health").json()["sidecar"]["points"] == 1
    assert where == {"on_loop": False}


def _counting_stats(calls):
    def stats(_p):
        calls.append(1)
        return {"ready": True, "points": len(calls)}
    return stats


def test_health_caches_sidecar_stats_within_the_ttl(db_path, monkeypatch):
    """Off the loop is not enough on its own: a monitor polling every 10s
    would otherwise keep a worker thread scanning a gigabyte continuously."""
    calls = []
    monkeypatch.setattr(server.price_sidecar, "stats", _counting_stats(calls))
    with client() as c:
        first = c.get("/health").json()["sidecar"]
        second = c.get("/health").json()["sidecar"]
    assert len(calls) == 1
    assert first == second == {"ready": True, "points": 1}


def test_health_recomputes_sidecar_stats_once_the_ttl_lapses(db_path,
                                                             monkeypatch):
    """Cached, not frozen -- a build landing has to become visible."""
    monkeypatch.setattr(server, "_SIDECAR_STATS_TTL", 0.0)
    calls = []
    monkeypatch.setattr(server.price_sidecar, "stats", _counting_stats(calls))
    with client() as c:
        assert c.get("/health").json()["sidecar"]["points"] == 1
        assert c.get("/health").json()["sidecar"]["points"] == 2
    assert len(calls) == 2


def test_health_survives_stats_blowing_up(db_path, monkeypatch):
    """A sick sidecar must not take the healthcheck down with it."""
    def boom(_p):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(server.price_sidecar, "stats", boom)
    with client() as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["sidecar"] == {"ready": False}


def test_health_answers_while_a_build_is_in_flight(db_path, monkeypatch):
    """A build is 10-30 minutes during which the .part file exists and the
    real path does not, and the healthcheck still has to answer."""
    side = _sidecar_env(db_path, monkeypatch)
    with open(f"{side}.part.{os.getpid()}", "wb") as f:
        f.write(b"half a database")
    with client() as c:
        body = c.get("/health").json()
    assert body["sidecar"] == {"ready": False, "reason": "absent"}
    assert body["status"] in ("ok", "degraded")


# ── Startup scheduling ───────────────────────────────────────────────────────


class _LifespanApp:
    """Minimal ASGI app that emits the lifespan messages it is handed."""

    def __init__(self, *types):
        self.types = types

    async def __call__(self, scope, receive, send):
        for t in self.types:
            await send({"type": t})


def run_lifespan(*types):
    """Drive PassphraseMiddleware's lifespan hook.

    Returns the middleware and which of its tasks were cancelled. That second
    value has to be sampled from inside the loop: asyncio.run cancels every
    task still pending when it tears the loop down, so a `.cancelled()` read
    after it returns is true whether or not the shutdown hook did anything."""
    mw = server.PassphraseMiddleware(_LifespanApp(*types))

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(_msg):
        pass

    async def go():
        await mw({"type": "lifespan"}, receive, send)
        for _ in range(3):        # let the scheduled tasks run, or be cancelled
            await asyncio.sleep(0)
        return mw, {name: task is not None and task.cancelled()
                    for name, task in (("ingest", mw._ingest_task),
                                       ("sidecar", mw._sidecar_task))}

    return asyncio.run(go())


def _record_startups(monkeypatch, started, body=None):
    async def run(name):
        started.append(name)
        if body is not None:
            await body()

    monkeypatch.setattr(server, "watchlist_ingest_loop",
                        lambda: run("ingest"))
    monkeypatch.setattr(server, "sidecar_build_once", lambda: run("sidecar"))


def test_startup_schedules_the_sidecar_build(monkeypatch):
    """Eager, not lazy: the weekly tier can only ever begin from the 90 days
    AllPrices holds on build day, so a day of delay is a day of history that
    can never be recovered."""
    monkeypatch.delenv("MYSTIC_FORGE_NO_INGEST", raising=False)
    started = []
    _record_startups(monkeypatch, started)
    mw, _ = run_lifespan("lifespan.startup.complete")
    assert started == ["ingest", "sidecar"]
    assert mw._sidecar_task is not None


def test_a_second_startup_event_cannot_start_a_second_build(monkeypatch):
    """The build downloads 141 MB and runs for 10-30 minutes; twice over is
    twice the bandwidth racing for the same output path."""
    monkeypatch.delenv("MYSTIC_FORGE_NO_INGEST", raising=False)
    started = []
    _record_startups(monkeypatch, started)
    run_lifespan("lifespan.startup.complete", "lifespan.startup.complete")
    assert started.count("sidecar") == 1
    assert started.count("ingest") == 1


def test_no_ingest_env_var_keeps_startup_inert():
    """conftest sets MYSTIC_FORGE_NO_INGEST for the whole suite, and that is
    the only thing standing between the test run and a 141 MB download."""
    mw, _ = run_lifespan("lifespan.startup.complete")
    assert mw._sidecar_task is None and mw._ingest_task is None


def test_shutdown_cancels_the_sidecar_build(monkeypatch):
    monkeypatch.delenv("MYSTIC_FORGE_NO_INGEST", raising=False)
    started = []
    _record_startups(monkeypatch, started, body=asyncio.Event().wait)
    _, cancelled = run_lifespan("lifespan.startup.complete",
                                "lifespan.shutdown.complete")
    assert cancelled == {"ingest": True, "sidecar": True}


# ── The one-shot build itself ────────────────────────────────────────────────


def _stub_build(calls, delay=0.0):
    def build(path, gz):
        calls.append((path, gz))
        time.sleep(delay)
        return 0
    return build


async def test_sidecar_build_runs_once_even_when_scheduled_twice(db_path,
                                                                 monkeypatch):
    """141 MB and 10-30 minutes of CPU. Per-pid .part names keep two builds
    from publishing each other's half-finished file, but nothing stops them
    from both doing the work."""
    side = _sidecar_env(db_path, monkeypatch)
    open(os.path.join(os.path.dirname(db_path), "AllPrices.json.gz"), "wb").close()
    calls = []
    monkeypatch.setattr(server.price_sidecar, "is_ready", lambda _p: False)
    monkeypatch.setattr(server.price_sidecar, "build_from_allprices",
                        _stub_build(calls, delay=0.05))
    await asyncio.gather(server.sidecar_build_once(),
                         server.sidecar_build_once())
    assert [c[0] for c in calls] == [side]


async def test_sidecar_build_skips_a_sidecar_that_is_already_ready(db_path,
                                                                  monkeypatch):
    _sidecar_env(db_path, monkeypatch)
    calls = []
    monkeypatch.setattr(server.price_sidecar, "is_ready", lambda _p: True)
    monkeypatch.setattr(server.price_sidecar, "build_from_allprices",
                        _stub_build(calls))
    await server.sidecar_build_once()
    assert calls == []


async def test_sidecar_build_respects_the_disable_switch(db_path, monkeypatch):
    """MYSTIC_FORGE_NO_SIDECAR means "do not use it", which is not the same
    as "rebuild it" -- and a rebuild moves whatever is there aside."""
    _sidecar_env(db_path, monkeypatch)
    monkeypatch.setenv("MYSTIC_FORGE_NO_SIDECAR", "1")
    calls, fetched = [], []
    monkeypatch.setattr(server.price_sidecar, "build_from_allprices",
                        _stub_build(calls))
    # Stubbed rather than left live: without the check this reaches for
    # AllPrices, and a test that can pull 141 MB off MTGJSON when the code
    # regresses is a worse outcome than the regression.
    monkeypatch.setattr(watchlist_ingest, "_download",
                        lambda url, dest, _db: fetched.append(url))
    await server.sidecar_build_once()
    assert calls == [] and fetched == []


async def test_sidecar_build_downloads_allprices_when_it_is_absent(db_path,
                                                                   monkeypatch):
    side = _sidecar_env(db_path, monkeypatch)
    gz = os.path.join(os.path.dirname(db_path), "AllPrices.json.gz")
    fetched = []

    def fake_download(url, dest, _db):
        fetched.append(url)
        open(dest, "wb").close()
        return dest

    calls = []
    monkeypatch.setattr(server.price_sidecar, "is_ready", lambda _p: False)
    monkeypatch.setattr(watchlist_ingest, "_download", fake_download)
    monkeypatch.setattr(server.price_sidecar, "build_from_allprices",
                        _stub_build(calls))
    await server.sidecar_build_once()
    assert fetched == [f"{watchlist_ingest.MTGJSON}/AllPrices.json.gz"]
    assert calls == [(side, gz)]


async def test_sidecar_build_survives_a_disk_too_small_to_hold_it(db_path,
                                                                  monkeypatch,
                                                                  caplog):
    """build_from_allprices raises OSError rather than filling the volume.
    The server has to finish starting anyway -- every other tool still works
    without a sidecar, and the page path falls back to the legacy scan."""
    _sidecar_env(db_path, monkeypatch)
    open(os.path.join(os.path.dirname(db_path), "AllPrices.json.gz"), "wb").close()

    def no_room(_path, _gz):
        raise OSError("not enough free space")

    monkeypatch.setattr(server.price_sidecar, "is_ready", lambda _p: False)
    monkeypatch.setattr(server.price_sidecar, "build_from_allprices", no_room)
    with caplog.at_level(logging.ERROR):
        await server.sidecar_build_once()          # must not raise
    assert "sidecar build failed" in caplog.text


async def test_sidecar_build_warns_before_replacing_an_unusable_sidecar(
        db_path, monkeypatch, caplog):
    """Past ~90 days the file being replaced is the only copy of its history.
    An operator gets one chance to notice that in the deploy log."""
    side = _sidecar_env(db_path, monkeypatch)
    with open(side, "wb") as f:
        f.write(b"this is not a database")
    open(os.path.join(os.path.dirname(db_path), "AllPrices.json.gz"), "wb").close()
    monkeypatch.setattr(server.price_sidecar, "build_from_allprices",
                        _stub_build([]))
    with caplog.at_level(logging.WARNING):
        await server.sidecar_build_once()
    assert "corrupt" in caplog.text and "superseded" in caplog.text


# ── The reprojection startup pass ───────────────────────────────────────────


def _record_reprojection(monkeypatch, calls, body=None):
    def reproject(db_path, data_dir=None):
        calls.append((db_path, data_dir))
        return body() if body is not None else 0
    monkeypatch.setattr(watchlist_ingest, "reproject_if_stale", reproject)
    return calls


async def test_startup_reprojects_a_sidecar_that_was_already_ready(db_path,
                                                                   monkeypatch):
    """The 1.2.0 field failure. The sidecar was built days earlier, so the
    build returned early — and everything that only happens after a build,
    the projection of a newly tracked provider included, never happened."""
    _sidecar_env(db_path, monkeypatch)
    calls = _record_reprojection(monkeypatch, [])
    monkeypatch.setattr(server.price_sidecar, "is_ready", lambda _p: True)
    monkeypatch.setattr(server.price_sidecar, "build_from_allprices",
                        _stub_build([]))
    await server.sidecar_build_once()
    assert [c[0] for c in calls] == [db_path]


async def test_startup_reprojects_after_a_build_it_just_finished(db_path,
                                                                 monkeypatch):
    """The other half: a first build has to be delivered too."""
    side = _sidecar_env(db_path, monkeypatch)
    open(os.path.join(os.path.dirname(db_path), "AllPrices.json.gz"), "wb").close()
    built, calls = [], _record_reprojection(monkeypatch, [])
    monkeypatch.setattr(server.price_sidecar, "is_ready", lambda _p: False)
    monkeypatch.setattr(server.price_sidecar, "build_from_allprices",
                        _stub_build(built))
    await server.sidecar_build_once()
    assert [c[0] for c in built] == [side]
    assert [c[0] for c in calls] == [db_path]


async def test_startup_does_not_reproject_when_the_sidecar_is_disabled(
        db_path, monkeypatch):
    """MYSTIC_FORGE_NO_SIDECAR means "do not use it"; reading it to project
    from is using it."""
    _sidecar_env(db_path, monkeypatch)
    monkeypatch.setenv("MYSTIC_FORGE_NO_SIDECAR", "1")
    calls = _record_reprojection(monkeypatch, [])
    await server.sidecar_build_once()
    assert calls == []


async def test_startup_does_not_reproject_when_the_build_failed(db_path,
                                                                monkeypatch,
                                                                caplog):
    """Nothing landed, so there is nothing new to deliver — and the failure
    must still be logged and swallowed rather than reaching startup."""
    _sidecar_env(db_path, monkeypatch)
    open(os.path.join(os.path.dirname(db_path), "AllPrices.json.gz"), "wb").close()
    calls = _record_reprojection(monkeypatch, [])

    def no_room(_path, _gz):
        raise OSError("not enough free space")
    monkeypatch.setattr(server.price_sidecar, "is_ready", lambda _p: False)
    monkeypatch.setattr(server.price_sidecar, "build_from_allprices", no_room)
    with caplog.at_level(logging.ERROR):
        await server.sidecar_build_once()
    assert calls == []
    assert "sidecar build failed" in caplog.text


async def test_reprojection_runs_off_the_event_loop(db_path, monkeypatch):
    """~1.2s of SQLite writes for 1,000 watched cards. Inline it stalls every
    in-flight request on a server that is still starting up."""
    where = {}

    def probe(_db_path, data_dir=None):
        try:
            asyncio.get_running_loop()
            where["on_loop"] = True
        except RuntimeError:
            where["on_loop"] = False
        return 0

    _sidecar_env(db_path, monkeypatch)
    monkeypatch.setattr(server.price_sidecar, "is_ready", lambda _p: True)
    monkeypatch.setattr(watchlist_ingest, "reproject_if_stale", probe)
    await server.sidecar_build_once()
    assert where == {"on_loop": False}


# ── The nightly loop's guard ────────────────────────────────────────────────


async def _one_ingest_tick(monkeypatch):
    """Run exactly one iteration of the ingest loop and report what it ran.

    to_thread is stubbed rather than the ingest itself, so the whole tick is
    synchronous up to the hour-long sleep the loop then parks on."""
    ran = []

    async def fake_to_thread(fn, *a, **kw):
        ran.append(fn)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    task = asyncio.create_task(server.watchlist_ingest_loop())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return ran


def _stamp(db_path, **meta):
    db = watchlist_db.connect(db_path)
    for key, value in meta.items():
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
    db.commit()
    db.close()


async def test_ingest_loop_fires_when_the_running_version_changed(db_path,
                                                                  monkeypatch):
    """The 1.2.0 shared cause: last_ingest was stamped today by the *old*
    build, so the date guard alone kept the new one from ever running an
    ingest — and the MTGStocks resolve and the projection live only there."""
    _stamp(db_path, last_ingest=datetime.date.today().isoformat(),
           last_ingest_version="1.1.0")
    assert await _one_ingest_tick(monkeypatch) == [watchlist_ingest.run_ingest]


async def test_ingest_loop_stays_quiet_when_the_date_and_version_both_match(
        db_path, monkeypatch):
    """And it must settle: one forced run, not one every hour forever."""
    _stamp(db_path, last_ingest=datetime.date.today().isoformat(),
           last_ingest_version=server.VERSION)
    assert await _one_ingest_tick(monkeypatch) == []


async def test_ingest_loop_still_fires_on_a_new_day(db_path, monkeypatch):
    _stamp(db_path, last_ingest="2020-01-01", last_ingest_version=server.VERSION)
    assert await _one_ingest_tick(monkeypatch) == [watchlist_ingest.run_ingest]


async def test_ingest_loop_fires_when_nothing_was_ever_stamped(db_path,
                                                               monkeypatch):
    assert await _one_ingest_tick(monkeypatch) == [watchlist_ingest.run_ingest]


def test_the_version_helper_agrees_with_the_server_constant():
    """Two readers of one file. If they ever disagree the loop would force an
    ingest every hour, on every deploy, forever."""
    assert mystic_forge.version() == server.VERSION


# ── 1.3.0: markets dropdown, target rules, export/import, the forge ──────────

def _market_fixture(db_path, **card_kwargs):
    """Sol Ring at three USD shops and Cardmarket; cheapest USD is Mana Pool."""
    db = watchlist_db.connect(db_path)
    list_id, pp, sc = watchlist_db.create_list(db, label="Markets")
    seq, _ = watchlist_db.add_card(db, list_id, "Sol Ring", **card_kwargs)
    db.execute("INSERT INTO card_uuids (card_name, uuid, scryfall_id) VALUES"
               " ('Sol Ring','u1','abc-123')")
    for prov, price in (("tcgplayer", 1.50), ("cardkingdom", 1.99),
                        ("manapool", 1.10), ("cardmarket", 0.90)):
        watchlist_db.upsert_price(db, "u1", "2026-09-13", prov, "normal", price + 0.2)
        watchlist_db.upsert_price(db, "u1", "2026-09-14", prov, "normal", price)
    db.close()
    return list_id, pp, sc, seq


def test_board_defaults_to_cheapest_usd_market_with_a_dropdown(db_path):
    _, pp, _, _ = _market_fixture(db_path)
    with client() as c:
        page = c.get(f"/w/{pp}").text
    assert 'id="shopSel"' in page and "All markets" in page
    assert '<option value="all"' in page and '<option value="cardmarket"' in page
    assert "$1.10" in page and "via Mana Pool" in page     # cheapest USD, tagged
    assert "€0.90" not in page                              # EUR never mixed in
    assert 'data-basis="manapool"' in page
    assert 'class="verdict' not in page                     # BUY banner retired


def test_dropdown_shop_shows_that_market_and_keeps_the_basis(db_path):
    _, pp, _, _ = _market_fixture(db_path, target_price=1.20)
    with client() as c:
        default = c.get(f"/w/{pp}").text
        ck = c.get(f"/w/{pp}?shop=cardkingdom").text
        cm = c.get(f"/w/{pp}?shop=cardmarket").text
    assert default.count('class="card hit"') == 1           # 1.10 ≤ 1.20
    assert ck.count('class="card hit"') == 1 and "$1.99" in ck   # display CK, hit stays
    assert cm.count('class="card hit"') == 1 and "€0.90" in cm
    assert "target $1.20 (USD)" in cm                        # basis currency named
    assert "judge the cheapest USD market" in cm


def test_pinned_shop_is_the_card_basis(db_path):
    _, pp, _, seq = _market_fixture(db_path, target_price=1.60, shop="tcgplayer")
    with client() as c:
        page = c.get(f"/w/{pp}").text
    assert "$1.50" in page and "📌 TCGplayer" in page
    assert page.count('class="card hit"') == 1               # 1.50 ≤ 1.60 at TCG
    assert 'data-shop="tcgplayer"' in page


def test_api_shop_pins_and_clears(db_path):
    _, pp, sc, seq = _market_fixture(db_path)
    with client() as c:
        r = c.post("/api/shop", json={"key": pp, "entry_id": seq, "shop": "cardkingdom"})
        assert r.status_code == 200 and r.json()["shop"] == "cardkingdom"
        assert "$1.99" in c.get(f"/w/{pp}").text
        r = c.post("/api/shop", json={"key": pp, "entry_id": seq, "shop": None})
        assert r.json()["shop"] is None
        assert c.post("/api/shop", json={"key": pp, "entry_id": seq,
                                         "shop": "amazon"}).status_code == 400
        assert c.post("/api/shop", json={"key": sc, "entry_id": seq,
                                         "shop": "tcgplayer"}).status_code == 403


def test_api_target_installs_a_follow_the_low_rule(db_path):
    list_id, pp, _, seq = _market_fixture(db_path)
    with client() as c:
        r = c.post("/api/target", json={"key": pp, "entry_id": seq,
                                        "target_mode": "low", "target_pct": 10})
        assert r.status_code == 200
        body = r.json()
        assert body["target_mode"] == "low" and body["target_pct"] == 10
        # prior low on the USD basis is 1.30 (Mana Pool yesterday) → 1.17
        assert body["effective"] == 1.17
        page = c.get(f"/w/{pp}").text
        assert "historic low −10% (now $1.17)" in page
        assert 'data-mode="low"' in page and 'data-pct="10"' in page
        assert c.post("/api/target", json={"key": pp, "entry_id": seq,
                                           "target_mode": "banana"}).status_code == 400
        assert c.post("/api/target", json={"key": pp, "entry_id": seq,
                                           "target_mode": "low",
                                           "target_pct": 150}).status_code == 400
        # back to fixed
        r = c.post("/api/target", json={"key": pp, "entry_id": seq,
                                        "target_price": 2.0})
        assert r.json()["target_mode"] == "fixed" and r.json()["effective"] == 2.0
    hist = client().get(f"/w/{pp}/history").text
    assert "→ historic low −10%" in hist and "→ $2.00" in hist


def test_low_rule_lights_the_card_on_a_new_low(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_mode="low")
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','u1')")
    watchlist_db.upsert_price(db, "u1", "2026-09-12", "tcgplayer", "normal", 2.0)
    watchlist_db.upsert_price(db, "u1", "2026-09-13", "tcgplayer", "normal", 1.8)
    watchlist_db.upsert_price(db, "u1", "2026-09-14", "tcgplayer", "normal", 1.7)
    db.close()
    page = client().get(f"/w/{pp}").text
    assert page.count('class="card hit"') == 1 and "buy window" in page
    assert "<b>1</b><span>buy windows</span>" in page


def test_api_card_serves_one_market_for_the_modal(db_path):
    _, pp, sc, seq = _market_fixture(db_path)
    with client() as c:
        r = c.get(f"/api/card/{sc}/{seq}?shop=cardmarket")     # share pages too
        assert r.status_code == 200
        d = r.json()
        assert d["cur"] == "€" and d["current"] == 0.90 and d["low"] == 0.90
        assert d["pts"] == [["2026-09-13", 1.1], ["2026-09-14", 0.9]]
        assert "<svg" in d["chart"] and "2026-09-14" in d["tail"]
        basis = c.get(f"/api/card/{pp}/{seq}").json()
        assert basis["provider"] == "manapool" and basis["cur"] == "$"
        assert c.get(f"/api/card/{pp}/{seq}?shop=ebay").status_code == 400
        assert c.get(f"/api/card/{pp}/999").status_code == 404


def test_modal_carries_image_markets_and_the_new_layout(db_path):
    _, pp, sc, seq = _market_fixture(db_path)
    with client() as c:
        own = c.get(f"/w/{pp}").text
        share = c.get(f"/s/{sc}").text
    assert 'data-img="https://api.scryfall.com/cards/abc-123?format=image' in own
    assert '&quot;manapool&quot;: {&quot;price&quot;: 1.1' in own   # data-shops JSON
    assert 'id="cardImg"' in own and 'id="shopRow"' in own and 'id="kLow"' in own
    assert '<details class="histbox">' in own                       # history folded away
    assert 'id="followLow"' in own and 'id="basisSel"' in own       # target editor
    assert 'id="scBeat"' in own and "Match historic low" in own
    assert 'class="act ghost" id="removeBtn"' in own                # remove: far left
    assert own.index('id="removeBtn"') < own.index('id="tgtSave"') < own.index('id="boughtBtn"')
    assert 'id="followLow"' not in share and 'id="shopRow"' in share
    # Card Kingdom and Mana Pool badges join the hop-outs
    assert "cardkingdom.com/catalog/search" in own and "manapool.com/card/sol-ring" in own


def test_image_falls_back_to_a_name_lookup(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sram, Senior Edificer")
    db.close()
    page = client().get(f"/w/{pp}").text
    assert ("api.scryfall.com/cards/named?exact=Sram%2C%20Senior%20Edificer"
            "&amp;format=image") in page


def test_export_dialog_and_data_on_both_pages(db_path):
    list_id, pp, sc, seq = _market_fixture(db_path, target_price=5.0)
    db = watchlist_db.connect(db_path)
    watchlist_db.add_card(db, list_id, "Cultivate", set_code="CMM",
                          collector_number="264", target_mode="low", target_pct=10)
    db.close()
    with client() as c:
        own = c.get(f"/w/{pp}").text
        share = c.get(f"/s/{sc}").text
    for page in (own, share):
        assert 'id="exportBtn"' in page and 'id="exportDlg"' in page
        assert 'id="xTcg"' in page and 'id="xCk"' in page and 'id="xMp"' in page
        assert "tcgplayer.com/massentry?productline=Magic&c=" in page
        assert "cardkingdom.com/builder?c=" in page and "manapool.com/add-deck" in page
        assert '"name": "Sol Ring", "set": "", "cn": "", "hit": true' in page
        assert '"spec": "5.00"' in page
        assert '"name": "Cultivate", "set": "CMM", "cn": "264", "hit": false' in page
        assert '"spec": "low-10%"' in page
    assert 'id="importBtn"' in own and 'id="importDlg"' in own
    assert 'id="importBtn"' not in share and 'id="importDlg"' not in share
    assert own.index('id="addCard"') < own.index('id="exportBtn"') < own.index('id="importBtn"')


def test_export_button_hidden_on_an_empty_list(db_path):
    db = watchlist_db.connect(db_path)
    _, pp, _ = watchlist_db.create_list(db)
    db.close()
    assert 'id="exportBtn"' not in client().get(f"/w/{pp}").text


def _fake_collection(monkeypatch, known):
    """A /cards/collection fake keyed on name or set+number."""
    async def _post(endpoint, body):
        data = []
        for ident in body["identifiers"]:
            if "name" in ident:
                card = known.get(ident["name"].lower())
            else:
                card = known.get((ident["set"].lower(), ident["collector_number"]))
            if card:
                data.append(card)
        return {"data": data, "not_found": []}

    async def _get(endpoint, params=None):
        import httpx
        req = httpx.Request("GET", "x://x")
        raise httpx.HTTPStatusError("404", request=req,
                                    response=httpx.Response(404, request=req))
    monkeypatch.setattr(server, "_scryfall_post", _post)
    monkeypatch.setattr(server, "_scryfall_get", _get)


def test_api_import_parses_targets_and_pins_on_request(db_path, monkeypatch):
    sol = {"name": "Sol Ring", "set": "cmm", "collector_number": "464",
           "prices": {"usd": "2.00"}}
    _fake_collection(monkeypatch, {"sol ring": sol, ("cmm", "464"): sol,
                                   "rhystic study": {"name": "Rhystic Study",
                                                     "prices": {"usd": "40"}},
                                   "cultivate": {"name": "Cultivate", "prices": {}}})
    db = watchlist_db.connect(db_path)
    list_id, pp, sc = watchlist_db.create_list(db)
    db.close()
    text = ("1 Sol Ring (CMM) 464 @ -25%\n1x Rhystic Study @ low-10%\n"
            "Cultivate @ 3\nNonexistent Card\n")
    with client() as c:
        assert c.post("/api/import", json={"key": sc, "decklist": text}).status_code == 403
        assert c.post("/api/import", json={"key": pp, "decklist": text,
                                           "target": "banana"}).status_code == 400
        r = c.post("/api/import", json={"key": pp, "decklist": text, "note": "deck"})
        assert r.status_code == 200
        d = r.json()
        assert sorted(d["added"]) == ["Cultivate", "Rhystic Study", "Sol Ring"]
        assert d["skipped"] == ["Nonexistent Card"]
    db = watchlist_db.connect(db_path)
    got = {e["card_name"]: e for e in watchlist_db.current_entries(db, list_id)}
    assert got["Sol Ring"]["set_code"] is None            # pins are opt-in
    assert got["Sol Ring"]["target_price"] == 1.5         # 25% under $2.00
    assert (got["Rhystic Study"]["target_mode"], got["Rhystic Study"]["target_pct"]) == ("low", 10.0)
    assert got["Cultivate"]["target_price"] == 3.0
    assert all(e["note"] == "deck" for e in got.values())
    db.close()
    with client() as c:
        r = c.post("/api/import", json={"key": pp, "decklist": "1 Sol Ring (CMM) 464",
                                        "pin_printings": True})
        assert r.json()["added"] == ["Sol Ring"]           # a pinned copy is a new entry
    db = watchlist_db.connect(db_path)
    pinned = [e for e in watchlist_db.current_entries(db, list_id) if e["set_code"]]
    assert pinned and pinned[0]["set_code"] == "CMM" and pinned[0]["collector_number"] == "464"
    db.close()


def test_forge_page_and_api_create(db_path, monkeypatch):
    _fake_collection(monkeypatch, {"sol ring": {"name": "Sol Ring", "prices": {"usd": "2"}}})
    with client() as c:
        for path in ("/w/new", "/w/"):
            r = c.get(path)
            assert r.status_code == 200, path
            assert 'id="forgeGo"' in r.text and 'id="newCards"' in r.text
        r = c.post("/api/create", json={"label": "Forged", "decklist": "1 Sol Ring @ low",
                                        "note": "seed"})
        assert r.status_code == 200
        d = r.json()
        assert d["passphrase"] and d["share_code"].startswith("SC-")
        assert d["page"].endswith(f"/w/{d['passphrase']}")
        assert d["share"].endswith(f"/s/{d['share_code']}")
        assert d["import"]["added"] == ["Sol Ring"]
        board = c.get(f"/w/{d['passphrase']}").text
        assert "Forged" in board and "Sol Ring" in board and "historic low" in board
        # an empty forge is fine too
        assert c.post("/api/create", json={}).json()["import"] is None
        assert c.post("/api/create", json={"target": "??"}).status_code == 400


def test_api_create_is_throttled_like_the_tool(db_path, monkeypatch):
    monkeypatch.setattr(server, "MINT_LIMIT", 2)
    server._mint_log.clear()
    with client() as c:
        assert c.post("/api/create", json={}).status_code == 200
        assert c.post("/api/create", json={}).status_code == 200
        assert c.post("/api/create", json={}).status_code == 429


def test_api_add_accepts_a_target_rule(db_path, monkeypatch):
    async def _get(endpoint, params=None):
        return {"name": "Sol Ring", "prices": {"usd": "4.00"}}
    monkeypatch.setattr(server, "_scryfall_get", _get)
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db)
    db.close()
    with client() as c:
        assert c.post("/api/add", json={"key": pp, "name": "Sol Ring",
                                        "target": "low-5%"}).status_code == 200
        assert c.post("/api/add", json={"key": pp, "name": "Cultivate",
                                        "target": "-50%"}).status_code == 200
        assert c.post("/api/add", json={"key": pp, "name": "Zzz",
                                        "target": "nope"}).status_code == 400
    db = watchlist_db.connect(db_path)
    got = {e["card_name"]: e for e in watchlist_db.current_entries(db, list_id)}
    assert (got["Sol Ring"]["target_mode"], got["Sol Ring"]["target_pct"]) == ("low", 5.0)
    assert got["Cultivate"]["target_price"] == 2.0
    db.close()


def test_csv_export_reports_the_basis_and_the_rule(db_path):
    _, pp, _, _ = _market_fixture(db_path, target_mode="low", target_pct=10)
    with client() as c:
        csv_text = c.get(f"/w/{pp}/export.csv").text
    head, row = csv_text.strip().splitlines()[:2]
    assert head.startswith("card,set_code,collector_number,price,currency,shop,")
    cols = dict(zip(head.split(","), row.split(",")))
    assert cols["price"] == "1.1" and cols["shop"] == "manapool"
    assert cols["target_rule"] == "low-10%" and cols["target"] == "1.17"
    assert cols["low"] == "1.1" and cols["low_date"] == "2026-09-14"


def test_mint_throttle_ignores_a_spoofed_forwarded_header(db_path, monkeypatch):
    """The first X-Forwarded-For entry is caller-written; rotating it must
    not hand out fresh throttle buckets (the last hop is what the proxy
    actually accepted)."""
    monkeypatch.setattr(server, "MINT_LIMIT", 2)
    server._mint_log.clear()
    with client() as c:
        for n in range(2):
            r = c.post("/api/create", json={},
                       headers={"x-forwarded-for": f"10.0.0.{n}, 203.0.113.9"})
            assert r.status_code == 200
        r = c.post("/api/create", json={},
                   headers={"x-forwarded-for": "10.0.0.99, 203.0.113.9"})
        assert r.status_code == 429
        # a different accepted peer is a different bucket
        assert c.post("/api/create", json={},
                      headers={"cf-connecting-ip": "198.51.100.7"}).status_code == 200
