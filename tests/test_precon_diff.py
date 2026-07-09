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
