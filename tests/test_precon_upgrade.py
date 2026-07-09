import httpx
import pytest
import server

# ── Fixtures: real-shaped EDHREC precon JSON (trimmed) ────────────────────────

INDEX = {
    "header": "Precon Upgrade Guide",
    "container": {"json_dict": {"cardlists": [
        {"tag": "setA", "cardviews": [
            {"name": "World Shaper", "url": "/precon/world-shaper"},
            {"name": "Avengers Assemble", "url": "/precon/avengers-assemble"},
            {"name": "Doom Prevails", "url": "/precon/doom-prevails"},
        ]},
    ]}},
}

BASE = {
    "header": "World Shaper Precon",
    "deck": {"commander": ["Szarel, Genesis Shepherd"], "cards": {}},
    "precon_commander_counts": [
        {"value": "Hearthhull, the Worldseed", "count": 12322,
         "href": "/precon/world-shaper/hearthhull-the-worldseed"},
        {"value": "Szarel, Genesis Shepherd", "count": 2764,
         "href": "/precon/world-shaper"},
    ],
    "container": {"json_dict": {"cardlists": [
        {"tag": "cardstoadd", "header": "Cards to Add", "cardviews": [
            {"name": "Icetill Explorer", "inclusion": 1143, "num_decks": 1143,
             "potential_decks": 2737, "synergy": 0.2346},
        ]},
        {"tag": "landstoadd", "header": "Lands to Add", "cardviews": [
            {"name": "Stomping Ground", "inclusion": 593, "num_decks": 593,
             "potential_decks": 2764, "synergy": -0.281},
        ]},
        {"tag": "cardstocut", "header": "Cards to Cut", "cardviews": [
            {"name": "World Breaker", "inclusion": 0, "num_decks": 0,
             "potential_decks": 0, "unpopularity": 0.7152},
        ]},
        {"tag": "landstocut", "header": "Lands to Cut", "cardviews": [
            {"name": "Ancient Tomb", "inclusion": 0, "num_decks": 0,
             "potential_decks": 0, "unpopularity": 0.51},
        ]},
    ]}},
}

SUB = {
    "header": "World Shaper Precon Upgrades for Hearthhull, the Worldseed",
    "deck": {"commander": ["Szarel, Genesis Shepherd"], "cards": {}},
    "precon_commander_counts": BASE["precon_commander_counts"],
    "container": {"json_dict": {"cardlists": [
        {"tag": "cardstoadd", "header": "Cards to Add", "cardviews": [
            {"name": "Cultivate", "inclusion": 900, "num_decks": 900,
             "potential_decks": 1200, "synergy": 0.1},
        ]},
        {"tag": "cardstocut", "header": "Cards to Cut", "cardviews": [
            {"name": "World Breaker", "inclusion": 0, "num_decks": 0,
             "potential_decks": 0, "unpopularity": 0.6},
        ]},
    ]}},
}


def _fake_edhrec_get(monkeypatch):
    async def fake(path):
        if path == "/pages/precon.json":
            return INDEX
        if path == "/pages/precon/world-shaper.json":
            return BASE
        if path == "/pages/precon/world-shaper/hearthhull-the-worldseed.json":
            return SUB
        req = httpx.Request("GET", "http://test")
        raise httpx.HTTPStatusError("not found", request=req,
                                    response=httpx.Response(403, request=req))
    monkeypatch.setattr(server, "_edhrec_get", fake)


# ── Resolution tests ──────────────────────────────────────────────────────────

async def test_resolve_exact_name(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    kind, val = await server._resolve_precon_slug("World Shaper")
    assert kind == "slug"
    assert val == "world-shaper"


async def test_resolve_low_confidence_returns_candidates(monkeypatch):
    _fake_edhrec_get(monkeypatch)
    kind, val = await server._resolve_precon_slug("Zzqx Nonsense Foo")
    assert kind == "candidates"
    assert isinstance(val, list) and len(val) >= 1
