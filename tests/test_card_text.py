import server


ERIETTE = {
    "name": "Eriette of the Charmed Apple",
    "mana_cost": "{1}{W}{B}",
    "type_line": "Legendary Creature — Human Warlock",
    "oracle_text": (
        "At the beginning of your end step, each opponent loses 1 life for "
        "each Aura you control attached to a permanent that player controls."
    ),
    "power": "1", "toughness": "4",
    "color_identity": ["B", "W"],
    "set": "woe", "collector_number": "197",
}

SOL_RING = {
    "name": "Sol Ring",
    "mana_cost": "{1}",
    "type_line": "Artifact",
    "oracle_text": "{T}: Add {C}{C}.",
    "color_identity": [],
    "set": "ltc", "collector_number": "284",
}

DFC = {
    "name": "Gumdrop Poisoner // Tempt with Treats",
    "color_identity": ["B"],
    "set": "woe", "collector_number": "96",
    "card_faces": [
        {"name": "Gumdrop Poisoner", "mana_cost": "{2}{B}",
         "type_line": "Creature — Human Warlock",
         "oracle_text": "Lifelink", "power": "2", "toughness": "2"},
        {"name": "Tempt with Treats", "mana_cost": "{B}",
         "type_line": "Sorcery — Adventure",
         "oracle_text": "Create a Food token."},
    ],
}


def _install_collection(monkeypatch, cards, not_found=None, fail=False):
    """Fake _scryfall_post; returns captured request bodies for inspection."""
    calls: list[dict] = []

    async def fake(endpoint, body):
        assert endpoint == "/cards/collection"
        calls.append(body)
        if fail:
            raise RuntimeError("boom")
        return {"data": cards, "not_found": not_found or []}

    monkeypatch.setattr(server, "_scryfall_post", fake)
    return calls


async def test_returns_text_blocks_in_request_order(monkeypatch):
    _install_collection(monkeypatch, [SOL_RING, ERIETTE])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="Eriette of the Charmed Apple\nSol Ring"))
    assert "each opponent loses 1 life for each Aura" in out
    assert "{T}: Add {C}{C}." in out
    assert "P/T: 1/4" in out
    # Request order, not response order: Eriette was asked for first.
    assert out.index("Eriette") < out.index("Sol Ring")


async def test_duplicate_lines_collapse_to_one_block(monkeypatch):
    _install_collection(monkeypatch, [SOL_RING])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="1 Sol Ring\n1x Sol Ring"))
    assert out.count("{T}: Add {C}{C}.") == 1


async def test_decklist_lines_with_printings_work(monkeypatch):
    _install_collection(monkeypatch, [SOL_RING])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="1x Sol Ring (ltc) 284 *F* [Ramp]"))
    assert "{T}: Add {C}{C}." in out


async def test_dfc_renders_both_faces(monkeypatch):
    _install_collection(monkeypatch, [DFC])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="Gumdrop Poisoner // Tempt with Treats"))
    assert "Lifelink" in out
    assert "Create a Food token." in out


async def test_not_found_reported_with_fuzzy_hint(monkeypatch):
    _install_collection(monkeypatch, [SOL_RING],
                        not_found=[{"name": "Erriette of the Charmed Aple"}])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="Sol Ring\nErriette of the Charmed Aple"))
    assert "## Not found (1)" in out
    assert "Erriette of the Charmed Aple" in out
    assert "scryfall_named" in out


async def test_chunks_at_75_identifiers(monkeypatch):
    # Names must not end in ' <digits>' — the decklist parser reads that as
    # a collector number and strips it, collapsing every line to one name.
    names = [f"Test Card Alpha{i}" for i in range(100)]
    cards = [{"name": n, "oracle_text": f"Text {n}", "color_identity": []}
             for n in names]
    calls = _install_collection(monkeypatch, cards)
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="\n".join(names)))
    assert len(calls) == 2
    assert len(calls[0]["identifiers"]) == 75
    assert len(calls[1]["identifiers"]) == 25
    assert "Text Test Card Alpha99" in out


async def test_total_failure_returns_error(monkeypatch):
    _install_collection(monkeypatch, [], fail=True)
    out = await server.scryfall_card_text(server.CardTextInput(cards="Sol Ring"))
    assert "Unexpected error" in out


async def test_empty_input_message(monkeypatch):
    # Whitespace-only input is rejected by pydantic (str_strip_whitespace +
    # min_length), so the no-entries path needs a comment-only decklist.
    _install_collection(monkeypatch, [])
    out = await server.scryfall_card_text(server.CardTextInput(
        cards="# just a comment"))
    assert out == "No card names found in input."
