import pytest
import server


def test_diff_key_normalizes_case_and_space():
    assert server._diff_key("  Sol   Ring ") == "sol ring"
    assert server._diff_key("SOL RING") == server._diff_key("sol ring")


def test_archidekt_in_deck_cards_excludes_maybeboard():
    data = {
        "categories": [
            {"name": "Commander", "isPremier": True, "includedInDeck": True},
            {"name": "Ramp", "isPremier": False, "includedInDeck": True},
            {"name": "Maybeboard", "isPremier": False, "includedInDeck": False},
        ],
        "cards": [
            {"quantity": 1, "categories": ["Commander"],
             "card": {"oracleCard": {"name": "Test Commander"}}},
            {"quantity": 1, "categories": ["Ramp"],
             "card": {"oracleCard": {"name": "Sol Ring"}}},
            {"quantity": 1, "categories": ["Maybeboard"],
             "card": {"oracleCard": {"name": "Mana Crypt"}}},
            {"quantity": 7, "categories": [],
             "card": {"oracleCard": {"name": "Forest"}}},
        ],
    }
    out = server._archidekt_in_deck_cards(data)
    assert out == {"Test Commander": 1, "Sol Ring": 1, "Forest": 7}
    assert "Mana Crypt" not in out


# ── precon_diff tool ──────────────────────────────────────────────────────────

PRECON = {"data": {
    "name": "Test Precon", "code": "TST", "type": "Commander Deck",
    "commander": [{"name": "Test Commander", "count": 1}],
    "mainBoard": [
        {"name": "Sol Ring", "count": 1},
        {"name": "Arcane Signet", "count": 1},
        {"name": "Cultivate", "count": 1},
        {"name": "Forest", "count": 10},
        {"name": "Mountain", "count": 8},
    ],
    "sideBoard": [],
}}

UPGRADED_LIST = """1 Test Commander
1 Sol Ring
1 Arcane Signet
1 Rampant Growth
12 Forest
6 Mountain"""


def _fake_mtgjson(monkeypatch):
    async def fake(path):
        assert path == "/decks/TestPrecon.json"
        return PRECON
    monkeypatch.setattr(server, "_mtgjson_get", fake)


async def test_diff_added_cut_kept(monkeypatch):
    _fake_mtgjson(monkeypatch)
    out = await server.precon_diff(server.PreconDiffInput(
        file_name="TestPrecon", decklist=UPGRADED_LIST, canonicalize=False))
    assert "## Added (1)" in out
    assert "Rampant Growth" in out
    assert "## Cut (1)" in out
    assert "Cultivate" in out
    assert "Kept: 3" in out
    # Non-basic cut of 1 out of a 22-card precon = 5%.
    assert "Cut: 1 (5% of precon)" in out


async def test_basics_reported_separately(monkeypatch):
    _fake_mtgjson(monkeypatch)
    out = await server.precon_diff(server.PreconDiffInput(
        file_name="TestPrecon", decklist=UPGRADED_LIST, canonicalize=False))
    assert "Basic land changes" in out
    assert "Forest: 10 → 12" in out
    assert "Mountain: 8 → 6" in out
    # Basics must not appear in the meaningful add/cut lists.
    added_cut = out.split("Basic land changes")[0]
    assert "Forest" not in added_cut
    assert "Mountain" not in added_cut


async def test_requires_exactly_one_source(monkeypatch):
    _fake_mtgjson(monkeypatch)
    both = await server.precon_diff(server.PreconDiffInput(
        file_name="TestPrecon", deck="123", decklist=UPGRADED_LIST))
    assert "exactly one" in both.lower()
    neither = await server.precon_diff(server.PreconDiffInput(file_name="TestPrecon"))
    assert "exactly one" in neither.lower()


async def test_canonicalize_flags_unknown(monkeypatch):
    _fake_mtgjson(monkeypatch)

    async def fake_scryfall_post(endpoint, body):
        # Recognize every real card; leave the bogus one unmatched.
        known = {"Test Commander", "Sol Ring", "Arcane Signet", "Cultivate",
                 "Rampant Growth", "Forest", "Mountain"}
        wanted = {i["name"] for i in body["identifiers"]}
        return {"data": [{"name": n} for n in wanted & known],
                "not_found": [{"name": n} for n in wanted - known]}

    monkeypatch.setattr(server, "_scryfall_post", fake_scryfall_post)
    listing = UPGRADED_LIST + "\n1 Notarealcard Xyz"
    out = await server.precon_diff(server.PreconDiffInput(
        file_name="TestPrecon", decklist=listing, canonicalize=True))
    assert "Notarealcard Xyz" in out
    assert "not recognized" in out.lower()
