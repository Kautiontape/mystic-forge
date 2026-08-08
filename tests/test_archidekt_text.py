import server


DECK = {
    "name": "Test Deck",
    "deckFormat": 3,
    "owner": {"username": "shawn"},
    "categories": [
        {"name": "Commander", "isPremier": True, "includedInDeck": True},
        {"name": "Enchantments", "isPremier": False, "includedInDeck": True},
    ],
    "cards": [
        {"quantity": 1, "categories": ["Commander"],
         "card": {"oracleCard": {
             "name": "Eriette of the Charmed Apple",
             "manaCost": "{1}{W}{B}",
             "superTypes": ["Legendary"], "types": ["Creature"],
             "subTypes": ["Human", "Warlock"],
             "power": "1", "toughness": "4", "loyalty": None,
             "text": ("At the beginning of your end step, each opponent "
                      "loses 1 life for each Aura you control attached to a "
                      "permanent that player controls."),
             "faces": [],
         }}},
        {"quantity": 1, "categories": ["Enchantments"],
         "card": {"oracleCard": {
             "name": "Gumdrop Poisoner // Tempt with Treats",
             "manaCost": "{2}{B} // {B}",
             "superTypes": [], "types": [], "subTypes": [],
             "power": "", "toughness": "", "loyalty": None,
             "text": "",
             "faces": [
                 {"name": "Gumdrop Poisoner", "manaCost": "{2}{B}",
                  "superTypes": [], "types": ["Creature"],
                  "subTypes": ["Human", "Warlock"],
                  "power": "2", "toughness": "2", "loyalty": None,
                  "text": "Lifelink"},
                 {"name": "Tempt with Treats", "manaCost": "{B}",
                  "superTypes": [], "types": ["Sorcery"],
                  "subTypes": ["Adventure"],
                  "power": "", "toughness": "", "loyalty": None,
                  "text": "Create a Food token."},
             ],
         }}},
    ],
}


def _install_deck(monkeypatch):
    async def fake(path, params=None):
        return DECK
    monkeypatch.setattr(server, "_archidekt_get", fake)


async def test_default_output_has_no_text(monkeypatch):
    _install_deck(monkeypatch)
    out = await server.archidekt_deck(server.ArchidektDeckInput(deck="123"))
    assert "1 [CMDR] Eriette of the Charmed Apple" in out
    assert "loses 1 life" not in out
    assert "{1}{W}{B}" not in out


async def test_include_text_renders_mana_type_pt_text(monkeypatch):
    _install_deck(monkeypatch)
    out = await server.archidekt_deck(
        server.ArchidektDeckInput(deck="123", include_text=True))
    assert "1 [CMDR] Eriette of the Charmed Apple {1}{W}{B}" in out
    assert "Legendary Creature — Human Warlock 1/4" in out
    assert "loses 1 life for each Aura" in out


async def test_include_text_renders_faces(monkeypatch):
    _install_deck(monkeypatch)
    out = await server.archidekt_deck(
        server.ArchidektDeckInput(deck="123", include_text=True))
    assert "Gumdrop Poisoner {2}{B}" in out
    assert "Lifelink" in out
    assert "Sorcery — Adventure" in out
    assert "Create a Food token." in out


async def test_missing_fields_degrade_to_bare_line(monkeypatch):
    bare = {
        "name": "Bare Deck", "deckFormat": 3, "owner": {"username": "s"},
        "categories": [{"name": "Lands", "isPremier": False,
                        "includedInDeck": True}],
        "cards": [{"quantity": 7, "categories": ["Lands"],
                   "card": {"oracleCard": {"name": "Wastes"}}}],
    }

    async def fake(path, params=None):
        return bare
    monkeypatch.setattr(server, "_archidekt_get", fake)
    out = await server.archidekt_deck(
        server.ArchidektDeckInput(deck="123", include_text=True))
    assert "7 Wastes" in out
