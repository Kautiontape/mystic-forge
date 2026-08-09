"""Server-tool tests for the goldfish MCP surface (Tasks 18-19).

The shared server-tool helpers (``MINI_DECK_TEXT``, ``_patch_fetch_minideck``,
``_fake_fetch_for``) live in tests/goldfish/helpers.py — the pinned
consolidation home — for reuse by later server-tool test modules
(test_interactive.py, test_acceptance.py).
"""
import asyncio
import json
import time

import pydantic
import pytest

import server as srv
from mystic_forge.goldfish.cards import AnnotationError, validate_annotations
from mystic_forge.goldfish.odds import odds_at_least
from mystic_forge.goldfish.report import run_code
from mystic_forge.goldfish.runner import AB_BINARY_METRICS, is_binary_ab_metric
from tests.goldfish.helpers import (
    MINI_DECK_TEXT,
    MINI_SCRYFALL,
    _fake_fetch_for,
    _patch_fetch_minideck,
)
from tests.goldfish.test_autoderive import PLAINS, SOL_RING

# ── goldfish_odds ────────────────────────────────────────────────────────────


async def test_goldfish_odds_flat():
    out = await srv.goldfish_odds(srv.GoldfishOddsInput(
        deck_size=99, draws=7, copies=10, min_successes=1))
    expected = odds_at_least(99, 7, 10, 1) * 100
    assert "P(≥1 of 10 copies in top 7 of 99)" in out
    assert f"{expected:.2f}%" in out
    assert "exact hypergeometric" in out


async def test_goldfish_odds_groups():
    out = await srv.goldfish_odds(srv.GoldfishOddsInput.model_validate(
        {"deck_size": 99, "draws": 12,
         "groups": [{"copies": 1, "min_successes": 1},
                    {"copies": 1, "min_successes": 1}]}))
    assert out.count("%") >= 3          # one line per group + the joint result
    assert "Joint" in out
    assert "exact multivariate hypergeometric" in out


async def test_goldfish_odds_both_and_neither_rejected():
    neither = await srv.goldfish_odds(
        srv.GoldfishOddsInput(deck_size=99, draws=7))
    both = await srv.goldfish_odds(srv.GoldfishOddsInput.model_validate(
        {"deck_size": 99, "draws": 7, "copies": 4,
         "groups": [{"copies": 1}]}))
    assert "exactly one" in neither.lower()
    assert "exactly one" in both.lower()


async def test_goldfish_odds_cap_error_passthrough():
    # 8 groups x 60-wide ranges blows odds.py's combination cap; the tool
    # must return the ValueError message, not raise.
    out = await srv.goldfish_odds(srv.GoldfishOddsInput.model_validate(
        {"deck_size": 500, "draws": 100,
         "groups": [{"copies": 60} for _ in range(8)]}))
    assert "too large" in out


async def test_goldfish_odds_draws_exceeding_deck_size():
    out = await srv.goldfish_odds(srv.GoldfishOddsInput(
        deck_size=40, draws=60, copies=4))
    assert "draws" in out.lower() and "deck" in out.lower()


def test_goldfish_odds_nine_groups_rejected_by_model():
    with pytest.raises(pydantic.ValidationError):
        srv.GoldfishOddsInput.model_validate(
            {"deck_size": 99, "draws": 7, "groups": [{"copies": 1}] * 9})


def test_goldfish_odds_group_shape_validated():
    with pytest.raises(pydantic.ValidationError):        # copies required
        srv.GoldfishOddsInput.model_validate(
            {"deck_size": 99, "draws": 7, "groups": [{"min_successes": 1}]})
    with pytest.raises(pydantic.ValidationError):        # copies >= 0
        srv.GoldfishOddsInput.model_validate(
            {"deck_size": 99, "draws": 7, "groups": [{"copies": -1}]})


# ── goldfish_annotate ────────────────────────────────────────────────────────

SWORDS = {
    "name": "Swords to Plowshares", "type_line": "Instant", "mana_cost": "{W}",
    "oracle_text": "Exile target creature. Its controller gains life equal "
                   "to its power.",
    "keywords": []}
PURESTEEL = {
    "name": "Puresteel Paladin", "type_line": "Creature — Human Knight",
    "mana_cost": "{W}{W}", "power": "2", "toughness": "2",
    "oracle_text": "Metalcraft — Equip abilities you activate cost {0} to "
                   "activate as long as you control three or more artifacts.",
    "keywords": []}
BOROS_SIGNET = {
    "name": "Boros Signet", "type_line": "Artifact", "mana_cost": "{2}",
    "oracle_text": "{1}, {T}: Add {R}{W}.", "produced_mana": ["R", "W"],
    "keywords": []}


async def test_goldfish_annotate_three_sections(monkeypatch):
    monkeypatch.setattr(
        srv, "_goldfish_fetch_cards",
        _fake_fetch_for([PLAINS, SOL_RING, SWORDS, PURESTEEL, BOROS_SIGNET]))
    out = await srv.goldfish_annotate(srv.GoldfishAnnotateInput(
        deck="1 Puresteel Paladin\n1 Plains\n1 Sol Ring\n"
             "1 Swords to Plowshares\n1 Boros Signet\n1 Fake Card"))

    # Three sections, correctly populated (singular header for one card).
    assert "## Auto-derived (2 cards)" in out
    assert "## Out of scope (1 card)" in out
    assert "## Needs annotation (2 cards)" in out
    needs = out.split("## Needs annotation")[1]
    assert "Puresteel Paladin" in needs and "Boros Signet" in needs
    assert "Metalcraft — Equip abilities" in needs      # full oracle shown
    # Auto-derived cards don't leak into the gap list (the Signet worked
    # example is the only place its name may reappear).
    assert "Sol Ring" not in needs
    assert "Plains" not in needs

    # Out-of-scope class explanation (D9).
    assert "interaction_removal" in out
    assert "interaction — counted, not simulated" in out

    # Registry cheat-sheet and BOTH worked examples.
    assert "add_mana" in needs and "metalcraft" in needs
    assert "Cloud, Ex-SOLDIER" in needs
    assert '"Boros Signet"' in needs and '"{1}{T}"' in needs
    # Filter/target vocabularies are part of the cheat-sheet.
    assert "instant_or_sorcery" in needs        # spell_cast/cost_reduction filters
    assert "name:<CardName>" in needs           # tutor filter escape hatch
    assert "color:<W|U|B|R|G>" in needs         # cost_reduction color filter
    assert "each_opponent" in needs and "one_opponent" in needs

    # Unrecognized names surface at the top.
    assert "Not recognized by Scryfall" in out
    assert "Fake Card" in out


async def test_goldfish_annotate_minideck_all_auto(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_annotate(
        srv.GoldfishAnnotateInput(deck=MINI_DECK_TEXT))
    assert "Boss" in out.split("\n")[1]                 # commander named up top
    assert "## Auto-derived (6 cards)" in out
    assert "## Needs annotation (0 cards)" in out
    assert "100 cards" in out
    assert "Warning" not in out                         # exactly 100: no size warning


async def test_goldfish_annotate_warns_on_non_100(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_annotate(
        srv.GoldfishAnnotateInput(deck="1 Boss\n2 Plains"))
    assert "Warning" in out and "100" in out


# ── _goldfish_load_deck ──────────────────────────────────────────────────────


async def test_load_deck_first_line_commander():
    names, commander, note = await srv._goldfish_load_deck(
        "1 Boss\n2 Plains\n1 Bear")
    assert commander == "Boss"
    assert names == ["Boss", "Plains", "Plains", "Bear"]
    assert note is None


async def test_load_deck_cmdr_marker_overrides_first_line():
    names, commander, _ = await srv._goldfish_load_deck(
        "2 Plains\n1 Boss *CMDR*\n1 Bear")
    assert commander == "Boss"
    assert names.count("Boss") == 1
    assert all("*CMDR*" not in n for n in names)


async def test_load_deck_single_line_count_pattern():
    names, commander, _ = await srv._goldfish_load_deck("2 Plains")
    assert names == ["Plains", "Plains"]
    assert commander == "Plains"


async def test_load_deck_zero_quantity_deck_rejected():
    with pytest.raises(ValueError, match="no cards"):
        await srv._goldfish_load_deck("0 Sol Ring\n0 Plains")


async def test_load_deck_non_archidekt_url_rejected():
    with pytest.raises(ValueError, match="Only Archidekt URLs"):
        await srv._goldfish_load_deck("https://moxfield.com/decks/abc123")


async def test_annotate_non_archidekt_url_message():
    out = await srv.goldfish_annotate(srv.GoldfishAnnotateInput(
        deck="https://moxfield.com/decks/abc123"))
    assert "Only Archidekt URLs" in out


ARCHIDEKT_PARTNER_DATA = {
    "categories": [
        {"name": "Commander", "isPremier": True, "includedInDeck": True},
    ],
    "cards": [
        {"quantity": 1, "categories": ["Commander"],
         "card": {"oracleCard": {"name": "Partner A"}}},
        {"quantity": 1, "categories": ["Commander"],
         "card": {"oracleCard": {"name": "Partner B"}}},
        {"quantity": 2, "categories": [],
         "card": {"oracleCard": {"name": "Plains"}}},
    ],
}


async def test_load_deck_partner_precon_note(monkeypatch):
    async def fake_get(path, params=None):
        return ARCHIDEKT_PARTNER_DATA

    monkeypatch.setattr(srv, "_archidekt_get", fake_get)
    names, commander, note = await srv._goldfish_load_deck("12345")
    assert commander == "Partner A"
    assert note is not None and "first of 2 premier cards" in note
    assert names.count("Plains") == 2


async def test_annotate_partner_note_in_header(monkeypatch):
    async def fake_get(path, params=None):
        return ARCHIDEKT_PARTNER_DATA

    monkeypatch.setattr(srv, "_archidekt_get", fake_get)
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_annotate(srv.GoldfishAnnotateInput(deck="12345"))
    assert "Commander: Partner A (first of 2 premier cards" in out
    assert "v1 simulates a single commander" in out


# ── _goldfish_fetch_cards cache ──────────────────────────────────────────────


async def test_fetch_cards_second_call_hits_cache(monkeypatch):
    calls = {"n": 0}

    async def fake_post(endpoint, body):
        calls["n"] += 1
        return {"data": [{"name": i["name"]} for i in body["identifiers"]],
                "not_found": []}

    monkeypatch.setattr(srv, "_scryfall_post", fake_post)
    monkeypatch.setattr(srv, "_GOLDFISH_CARD_CACHE", {})
    cards1, nf1 = await srv._goldfish_fetch_cards(["Plains", "Sol Ring"])
    cards2, nf2 = await srv._goldfish_fetch_cards(["Plains", "Sol Ring"])
    assert calls["n"] == 1                              # second call: no HTTP
    assert [c["name"] for c in cards1] == ["Plains", "Sol Ring"]
    assert cards1 == cards2
    assert nf1 == [] and nf2 == []


async def test_fetch_cards_chunks_of_75_and_not_found(monkeypatch):
    batches = []

    async def fake_post(endpoint, body):
        idents = body["identifiers"]
        batches.append(len(idents))
        return {"data": [{"name": i["name"]} for i in idents
                         if i["name"] != "Fake Card"],
                "not_found": [i for i in idents if i["name"] == "Fake Card"]}

    monkeypatch.setattr(srv, "_scryfall_post", fake_post)
    monkeypatch.setattr(srv, "_GOLDFISH_CARD_CACHE", {})
    names = [f"Card {i}" for i in range(79)] + ["Fake Card"]
    cards, not_found = await srv._goldfish_fetch_cards(names)
    assert batches == [75, 5]
    assert len(cards) == 79
    assert not_found == ["Fake Card"]


# ── goldfish/report.run_code (Task 19 stub) ──────────────────────────────────


def test_run_code_stable_and_input_sensitive():
    inputs = {"deck": "1 Plains", "annotations": [], "seed": 42, "n": 100,
              "until_turn": 8, "combos": [], "engine_version": "0.1.0"}
    assert run_code(inputs) == run_code(dict(inputs))          # stable
    assert run_code(inputs) != run_code({**inputs, "seed": 43})
    assert run_code(inputs) != run_code({**inputs, "engine_version": "0.2.0"})


def test_run_code_length_and_charset():
    code = run_code({"deck": "1 Plains", "seed": 1})
    assert len(code) == 13
    assert code.isalnum()
    assert code == code.lower()
    assert "=" not in code


# ── goldfish/runner pre-steps (Task 19) ──────────────────────────────────────


def test_ab_binary_metric_helper():
    assert "commander_cast_by_t4" in AB_BINARY_METRICS
    assert is_binary_ab_metric("commander_cast_by_t5")
    assert is_binary_ab_metric("kill_by_until_turn")
    assert is_binary_ab_metric("cmdr21_by_until_turn")
    assert is_binary_ab_metric("on_curve_through_t5")
    assert is_binary_ab_metric("combo0_assembled_by_t6")
    assert is_binary_ab_metric("combo12_castable_by_t6")
    assert not is_binary_ab_metric("total_damage")
    assert not is_binary_ab_metric("max_cast_chain")
    assert not is_binary_ab_metric("combo0_assembled_by_t5")


# ── goldfish_run ─────────────────────────────────────────────────────────────


def _json_payload(out: str) -> dict:
    return json.loads(out.split("```json")[1].split("```")[0])


async def test_goldfish_run_output_shape(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=20, seed=42, until_turn=6))
    assert "```json" in out                # embedded metrics JSON (D4)
    assert "run_id" in out
    assert "Honesty report" in out
    assert "Commander: Boss" in out        # header
    assert "100 cards" in out
    payload = _json_payload(out)
    assert payload["n"] == 20
    assert payload["seed"] == 42
    cc = payload["metrics"]["commander_cast"]["reached_pct"]
    assert "ci" in cc                      # every proportion carries a CI
    run_id = payload["run_id"]
    assert len(run_id) == 13
    assert f'goldfish_report("{run_id}")' in out       # footer forward ref
    # LRU stored the run under its id.
    stored = srv._GOLDFISH_RUNS[run_id]
    assert stored["kind"] == "run"
    assert stored["result"]["metrics"]["n"] == 20


async def test_goldfish_run_header_commander_note(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    real_load = srv._goldfish_load_deck

    async def fake_load(deck):
        names, commander, _ = await real_load(deck)
        return names, commander, "(first of 2 premier cards — v1 simulates a single commander)"

    monkeypatch.setattr(srv, "_goldfish_load_deck", fake_load)
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=5, until_turn=4))
    assert "Commander: Boss (first of 2 premier cards" in out


def test_goldfish_run_n_capped():
    with pytest.raises(pydantic.ValidationError):
        srv.GoldfishRunInput(deck=MINI_DECK_TEXT, n=50_000)


def test_goldfish_run_mulligan_input_validated():
    with pytest.raises(pydantic.ValidationError, match="mulligan"):
        srv.GoldfishRunInput.model_validate(
            {"deck": MINI_DECK_TEXT, "mulligan": {"bad_field": 1}})
    with pytest.raises(pydantic.ValidationError):        # typed, not name-only
        srv.GoldfishRunInput.model_validate(
            {"deck": MINI_DECK_TEXT, "mulligan": {"min_sources": "three"}})
    ok = srv.GoldfishRunInput.model_validate(
        {"deck": MINI_DECK_TEXT,
         "mulligan": {"min_sources": 2, "free_first": False}})
    assert ok.mulligan.min_sources == 2
    assert ok.mulligan.free_first is False
    assert ok.mulligan.lands_only is None      # unset fields keep engine defaults


def test_goldfish_mulligan_model_mirrors_rules_fields():
    from dataclasses import fields as dc_fields

    from mystic_forge.goldfish.engine import MulliganRules
    assert (set(srv.GoldfishMulliganInput.model_fields)
            == {f.name for f in dc_fields(MulliganRules)})


async def test_goldfish_run_combo_typo_rejected(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=5,
        combos=[{"cards": ["Bear", "Typo Card"], "wins": False}]))
    assert "Typo Card" in out
    assert "not in the deck" in out
    assert "```json" not in out            # rejected before simulating


async def test_goldfish_run_annotation_error_verbatim(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    ann = [{"name": "Bear", "triggers": [{"on": "bogus", "do": "draw"}]}]
    with pytest.raises(AnnotationError) as ei:
        validate_annotations(ann)
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=5, annotations=ann))
    assert out == str(ei.value)            # the self-correction contract


async def test_goldfish_run_annotation_validation_precedes_network(monkeypatch):
    async def no_net(*args, **kw):
        raise AssertionError("network touched before annotation validation")

    monkeypatch.setattr(srv, "_goldfish_load_deck", no_net)
    monkeypatch.setattr(srv, "_goldfish_fetch_cards", no_net)
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=5,
        annotations=[{"name": "Bear", "triggers": [{"on": "bogus", "do": "draw"}]}]))
    assert "invalid on 'bogus'" in out


async def test_goldfish_run_warns_on_unmatched_annotation(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    ann = [{"name": "Puresteel Palladin",          # typo: not a deck card
            "triggers": [{"on": "etb", "do": "draw", "count": 1}]}]
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=5, until_turn=4, annotations=ann))
    assert "not in deck (check spelling)" in out
    assert "Puresteel Palladin" in out
    assert "```json" in out                        # warned, still simulated


async def test_goldfish_run_internal_error_caught(monkeypatch):
    _patch_fetch_minideck(monkeypatch)

    def boom(*args, **kw):
        raise RuntimeError("policy livelock — bug")

    monkeypatch.setattr(srv, "run_batch", boom)
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=5, seed=9, until_turn=4))
    assert "internal simulator error" in out
    assert "seed=9" in out and "n=5" in out and "until_turn=4" in out


PURESTEEL_DECK = ("1 Boss\n20 Plains\n17 Mountain\n26 Bear\n20 Runner\n"
                  "12 Hammer\n4 Puresteel Paladin")


async def test_goldfish_run_annotation_models_needs_annotation_card(monkeypatch):
    fixtures = MINI_SCRYFALL + [PURESTEEL]
    monkeypatch.setattr(srv, "_goldfish_fetch_cards", _fake_fetch_for(fixtures))
    # Without a client annotation, Puresteel is a needs-annotation card:
    # honesty lists it as unrecognized and no trigger of its ever fires.
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=PURESTEEL_DECK, n=20, seed=42, until_turn=6))
    honesty = _json_payload(out)["metrics"]["honesty"]
    assert "Puresteel Paladin" in [e["name"] for e in honesty["unrecognized"]]
    # A client annotation moves it out of unrecognized AND changes the sim:
    # the etb draw trigger shows up in the trigger-fire table.
    ann = [{"name": "Puresteel Paladin",
            "triggers": [{"on": "etb", "do": "draw", "count": 1}]}]
    out2 = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=PURESTEEL_DECK, n=20, seed=42, until_turn=6, annotations=ann))
    payload2 = _json_payload(out2)
    honesty2 = payload2["metrics"]["honesty"]
    assert "Puresteel Paladin" not in [e["name"] for e in honesty2["unrecognized"]]
    fires = payload2["metrics"]["trigger_fires"]
    assert fires.get("Puresteel Paladin|etb|draw", 0) > 0


# ── goldfish_ab ──────────────────────────────────────────────────────────────


async def test_goldfish_ab_scope_banner(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_ab(srv.GoldfishAbInput(
        deck_a=MINI_DECK_TEXT, deck_b=MINI_DECK_TEXT, n=10, seed=1))
    first_paragraph = out.split("\n\n")[0]
    assert "out of scope" in first_paragraph.lower()
    # The two verbatim sentences from spec §Statistics.
    assert "Deltas measure speed and consistency only" in first_paragraph
    assert "do not read results as 'cut your removal'" in first_paragraph
    # Delta table, correlation line, run_id.
    assert "commander_cast_by_t4" in out
    assert "McNemar" in out
    assert "pair correlation" in out.lower()
    assert "run_id" in out
    payload = _json_payload(out)
    assert set(payload) >= {"deltas", "pair_correlation", "until_turn"}
    assert payload["until_turn"] == 10
    assert len(payload["run_id"]) == 13
    assert srv._GOLDFISH_RUNS[payload["run_id"]]["kind"] == "ab"


async def test_goldfish_ab_identical_decks_zero_deltas(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_ab(srv.GoldfishAbInput(
        deck_a=MINI_DECK_TEXT, deck_b=MINI_DECK_TEXT, n=10, seed=7,
        until_turn=6))
    payload = _json_payload(out)
    for name, d in payload["deltas"].items():
        assert d["mean"] == 0.0, name
        assert not d["significant"], name
    assert payload["pair_correlation"] > 0.999
    assert "Changed: 0 cards" in out
    # Both honesty reports, labeled.
    assert "Deck A" in out and "Deck B" in out


async def test_goldfish_ab_banner_accounts_real_one_swap(monkeypatch):
    # One Bear (simulated) swapped for one Swords (interaction_removal):
    # the Counter-diff must classify both sides of the swap.
    monkeypatch.setattr(srv, "_goldfish_fetch_cards",
                        _fake_fetch_for(MINI_SCRYFALL + [SWORDS]))
    deck_b = MINI_DECK_TEXT.replace(
        "30 Bear", "29 Bear\n1 Swords to Plowshares")
    out = await srv.goldfish_ab(srv.GoldfishAbInput(
        deck_a=MINI_DECK_TEXT, deck_b=deck_b, n=5, until_turn=4))
    first_paragraph = out.split("\n\n")[0]
    assert ("Changed: 2 cards — 1 simulated, 1 out of scope "
            "(1 interaction_removal)") in first_paragraph
    assert "Deck A: 0/99 out of scope; Deck B: 1/99" in first_paragraph


async def test_goldfish_ab_errors_name_the_deck(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_ab(srv.GoldfishAbInput(
        deck_a=MINI_DECK_TEXT, deck_b="0 Plains", n=5))
    assert out.startswith("Deck B:")


def test_goldfish_runs_lru_evicts_at_cap():
    srv._GOLDFISH_RUNS.clear()
    try:
        for i in range(srv.GOLDFISH_MAX_RUNS + 3):
            srv._goldfish_store_run(f"id{i}", {"i": i}, {"r": i}, "run")
        assert len(srv._GOLDFISH_RUNS) == srv.GOLDFISH_MAX_RUNS
        assert "id0" not in srv._GOLDFISH_RUNS      # oldest evicted first
        assert "id2" not in srv._GOLDFISH_RUNS
        assert f"id{srv.GOLDFISH_MAX_RUNS + 2}" in srv._GOLDFISH_RUNS
    finally:
        srv._GOLDFISH_RUNS.clear()


# ── concurrency (spec §Concurrency) ──────────────────────────────────────────


async def test_goldfish_runs_do_not_block_other_tools(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    t1 = asyncio.create_task(srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=200, seed=1, until_turn=6)))
    t2 = asyncio.create_task(srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=200, seed=2, until_turn=6)))
    await asyncio.sleep(0.05)              # both sims now hold the semaphore
    start = time.monotonic()
    odds_out = await srv.goldfish_odds(srv.GoldfishOddsInput(
        deck_size=99, draws=7, copies=10))
    elapsed = time.monotonic() - start
    assert "exact hypergeometric" in odds_out
    assert elapsed < 1.0                   # sims in flight never block the loop
    out1, out2 = await asyncio.gather(t1, t2)
    assert "```json" in out1
    assert "```json" in out2
