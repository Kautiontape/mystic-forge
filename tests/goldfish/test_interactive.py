"""Interactive goldfish tools (Task 20): goldfish_start/step/state.

The tools are a thin stateful wrapper over the engine: step() takes the
engine's action union verbatim (D10), every response echoes the full
resumable state blob (D3), and legal_actions is response-FORMATTED only —
attach entries are grouped per equipment (Task 8 review finding) while the
engine's own legal_actions stays the pinned ground truth.
"""
import json
import time

import pydantic
import pytest

import server as srv
from mystic_forge.goldfish.engine import legal_actions, new_game
from tests.goldfish.helpers import MINI_DECK_TEXT, _patch_fetch_minideck, mini_cards


@pytest.fixture(autouse=True)
def _fresh_store():
    """Isolate the in-memory game store per test."""
    srv._GOLDFISH_GAMES.clear()
    yield
    srv._GOLDFISH_GAMES.clear()


async def _start(monkeypatch, **kw):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_start(srv.GoldfishStartInput(
        deck=MINI_DECK_TEXT, seed=7, **kw))
    return json.loads(out)


def _inject(g, cards, gid):
    """Plant a hand-built Game in the store (board setups the policy would
    never produce; the tool layer under test is response formatting)."""
    srv._GOLDFISH_GAMES[gid] = {
        "game": g, "cards": cards,
        "meta": {"deck": "1 Boss", "annotations": [], "combos": []},
        "touched": time.time()}


# ── start / step / state round trip ──────────────────────────────────────────


async def test_start_step_state_roundtrip(monkeypatch):
    payload = await _start(monkeypatch)
    gid = payload["game_id"]
    assert payload["state"]["phase"] in ("mulligan", "main1")
    assert payload["state"]["resume"]["deck"] == MINI_DECK_TEXT
    out = await srv.goldfish_step(srv.GoldfishStepInput(game_id=gid))
    stepped = json.loads(out)
    assert stepped["applied"]["reason"]            # policy chose, reason present
    assert stepped["legal_actions"]
    mirrored = json.loads(await srv.goldfish_state(
        srv.GoldfishStateInput(game_id=gid)))
    assert mirrored["game_id"] == gid
    assert mirrored["state"] == stepped["state"]


async def test_illegal_action_rejected_unmutated(monkeypatch):
    payload = await _start(monkeypatch)
    gid = payload["game_id"]
    before = json.loads(await srv.goldfish_state(
        srv.GoldfishStateInput(game_id=gid)))["state"]
    out = json.loads(await srv.goldfish_step(srv.GoldfishStepInput(
        game_id=gid, action={"type": "cast", "card": "Nonexistent"})))
    assert "Nonexistent" in out["error"]
    assert out["state"] == before                  # echoed state is unmutated
    assert out["legal_actions"]
    after = json.loads(await srv.goldfish_state(
        srv.GoldfishStateInput(game_id=gid)))["state"]
    assert before == after


async def test_until_fast_forward_and_resume(monkeypatch):
    payload = await _start(monkeypatch)
    gid = payload["game_id"]
    out = json.loads(await srv.goldfish_step(
        srv.GoldfishStepInput(game_id=gid, until="turn:4")))
    assert out["state"]["turn"] == 4
    assert isinstance(out["applied"], list) and out["applied"]
    assert all(a["action"]["type"] and a["reason"] for a in out["applied"])
    blob = out["state"]
    ahead = json.loads(await srv.goldfish_step(
        srv.GoldfishStepInput(game_id=gid, until="turn:6")))
    srv._GOLDFISH_GAMES.clear()                    # simulate eviction
    out2 = await srv.goldfish_start(srv.GoldfishStartInput(resume_state=blob))
    resumed = json.loads(out2)
    assert resumed["state"]["turn"] == 4
    assert resumed["state"]["log"] == blob["log"]
    # D3: the resumed game replays the SAME future draws — byte-identical log
    replay = json.loads(await srv.goldfish_step(srv.GoldfishStepInput(
        game_id=resumed["game_id"], until="turn:6")))
    assert replay["state"]["log"] == ahead["state"]["log"]


async def test_interactive_mulligan(monkeypatch):
    payload = await _start(monkeypatch, interactive_mulligan=True)
    assert payload["state"]["phase"] == "mulligan"
    kinds = {a["type"] for a in payload["legal_actions"]}
    assert kinds == {"mulligan", "keep"}
    out = json.loads(await srv.goldfish_step(srv.GoldfishStepInput(
        game_id=payload["game_id"], action={"type": "keep", "bottom": []})))
    assert out["state"]["phase"] == "main1"
    assert out["state"]["turn"] == 1
    assert out["applied"]["reason"] is None        # user action: no policy reason


# ── input validation ─────────────────────────────────────────────────────────


def test_start_requires_exactly_one_of_deck_resume():
    with pytest.raises(pydantic.ValidationError):
        srv.GoldfishStartInput()
    with pytest.raises(pydantic.ValidationError):
        srv.GoldfishStartInput(deck=MINI_DECK_TEXT, resume_state={})


def test_step_action_until_mutually_exclusive():
    with pytest.raises(pydantic.ValidationError):
        srv.GoldfishStepInput(game_id="x", action={"type": "pass"}, until="end")


async def test_malformed_resume_blob_clean_errors(monkeypatch):
    payload = await _start(monkeypatch)
    out = json.loads(await srv.goldfish_step(
        srv.GoldfishStepInput(game_id=payload["game_id"], until="turn:3")))
    blob = out["state"]
    assert blob["zones"]["battlefield"]            # lands deployed by turn 3

    dangling = json.loads(json.dumps(blob))
    dangling["zones"]["battlefield"][0]["attached_to"] = "b999"
    res = json.loads(await srv.goldfish_start(
        srv.GoldfishStartInput(resume_state=dangling)))
    assert "b999" in res["error"]

    missing = {k: v for k, v in blob.items() if k != "rng_state"}
    res = json.loads(await srv.goldfish_start(
        srv.GoldfishStartInput(resume_state=missing)))
    assert "rng_state" in res["error"]

    bad_zone = json.loads(json.dumps(blob))
    bad_zone["zones"]["hand"] = [1, 2]
    res = json.loads(await srv.goldfish_start(
        srv.GoldfishStartInput(resume_state=bad_zone)))
    assert "hand" in res["error"]


async def test_resume_unknown_card_in_rebuilt_pool(monkeypatch):
    payload = await _start(monkeypatch)
    blob = json.loads(json.dumps(payload["state"]))
    blob["zones"]["hand"].append("Not A Real Card")
    res = json.loads(await srv.goldfish_start(
        srv.GoldfishStartInput(resume_state=blob)))
    assert "rebuilt pool does not contain" in res["error"]
    assert "Not A Real Card" in res["error"]


def test_resume_keys_match_engine_serialization():
    """Drift guard: the validator's required-key list is exactly the engine's
    to_dict() output (minus display-only land_drops_remaining) plus the
    tool-added resume meta."""
    g = new_game(mini_cards(), ["Plains"] * 60, "Boss", seed=1)
    expected = (set(g.to_dict()) - {"land_drops_remaining"}) | {"resume"}
    assert set(srv._GOLDFISH_RESUME_KEYS) == expected


# ── attach grouping (response formatting only) ───────────────────────────────


def test_attach_grouping_formatter_caps_targets():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 60, "Boss", seed=1)
    g.phase, g.turn = "main1", 3
    eq = g.new_perm("Hammer")
    tokens = [g.new_perm("Token", is_token=True, token_power=i,
                         token_toughness=1) for i in range(10)]
    tokens[0].id = "weird-id"        # non-"b<N>" id: fallback key, no crash
    power_by_id = {t.id: i for i, t in enumerate(tokens)}
    actions = ([{"type": "play_land", "card": "Plains"}]
               + [{"type": "attach", "card": eq.id, "target": t.id}
                  for t in tokens]
               + [{"type": "pass"}])
    grouped = srv._goldfish_group_attach(g, actions)
    assert grouped[0] == {"type": "play_land", "card": "Plains"}
    assert grouped[-1] == {"type": "pass"}
    assert len(grouped) == 3               # land, ONE attach entry, pass
    entry = grouped[1]
    assert entry["card"] == eq.id
    assert len(entry["targets"]) == 8
    assert entry["more_targets"] == 2
    powers = [power_by_id[pid] for pid in entry["targets"]]
    assert powers == sorted(powers, reverse=True)   # descending effective power


async def test_attach_grouping_passthrough_via_state():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 40 + ["Bear"] * 20, "Boss", seed=3)
    g.phase, g.turn = "main1", 4
    ham = g.new_perm("Hammer")
    b1 = g.new_perm("Bear")
    b2 = g.new_perm("Bear")
    _inject(g, cards, "gid-attach")
    out = json.loads(await srv.goldfish_state(
        srv.GoldfishStateInput(game_id="gid-attach")))
    attach = [a for a in out["legal_actions"] if a["type"] == "attach"]
    assert attach == [{"type": "attach", "card": ham.id,
                       "targets": [b1.id, b2.id]}]  # <= 8: no more_targets key
    # every non-attach entry passes through in engine shape, order preserved
    assert ([a for a in out["legal_actions"] if a["type"] != "attach"]
            == [a for a in legal_actions(g) if a["type"] != "attach"])


# ── store lifecycle ──────────────────────────────────────────────────────────


async def test_ttl_eviction(monkeypatch):
    gid = (await _start(monkeypatch))["game_id"]
    srv._GOLDFISH_GAMES[gid]["touched"] = (
        time.time() - srv.GOLDFISH_GAME_TTL - 1)
    out = json.loads(await srv.goldfish_state(
        srv.GoldfishStateInput(game_id=gid)))
    assert "goldfish_start" in out["error"]        # recovery path named
    assert "resume_state" in out["error"]
    assert gid not in srv._GOLDFISH_GAMES


async def test_lru_cap_eviction(monkeypatch):
    monkeypatch.setattr(srv, "GOLDFISH_MAX_GAMES", 2)
    g1 = (await _start(monkeypatch))["game_id"]
    g2 = (await _start(monkeypatch))["game_id"]
    g3 = (await _start(monkeypatch))["game_id"]
    assert g1 not in srv._GOLDFISH_GAMES           # oldest evicted at the cap
    assert g2 in srv._GOLDFISH_GAMES and g3 in srv._GOLDFISH_GAMES
    out = json.loads(await srv.goldfish_step(srv.GoldfishStepInput(game_id=g1)))
    assert "resume_state" in out["error"]
    # an access refreshes LRU order: touch g2, start g4 -> g3 goes, g2 stays
    await srv.goldfish_state(srv.GoldfishStateInput(game_id=g2))
    g4 = (await _start(monkeypatch))["game_id"]
    assert g3 not in srv._GOLDFISH_GAMES
    assert g2 in srv._GOLDFISH_GAMES and g4 in srv._GOLDFISH_GAMES


# ── step response details ────────────────────────────────────────────────────


async def test_log_delta_only_new_lines(monkeypatch):
    payload = await _start(monkeypatch)
    log0 = payload["state"]["log"]
    assert log0                                    # auto-mulligan already logged
    out = json.loads(await srv.goldfish_step(
        srv.GoldfishStepInput(game_id=payload["game_id"])))
    assert out["log_delta"]
    assert out["state"]["log"] == log0 + out["log_delta"]


async def test_until_phase_boundary(monkeypatch):
    payload = await _start(monkeypatch)
    gid = payload["game_id"]
    out = json.loads(await srv.goldfish_step(
        srv.GoldfishStepInput(game_id=gid, until="phase:combat")))
    assert out["state"]["phase"] == "combat"
    assert out["state"]["turn"] == 1
    assert isinstance(out["applied"], list)
    bad = json.loads(await srv.goldfish_step(
        srv.GoldfishStepInput(game_id=gid, until="sometime")))
    assert "error" in bad


async def test_until_end_stops_on_win_or_turn_cap(monkeypatch):
    payload = await _start(monkeypatch)
    out = json.loads(await srv.goldfish_step(
        srv.GoldfishStepInput(game_id=payload["game_id"], until="end")))
    assert out["state"]["won_turn"] is not None or out["state"]["turn"] > 30


async def test_until_already_at_boundary_is_noop(monkeypatch):
    payload = await _start(monkeypatch)              # auto-mulligan -> main1
    out = json.loads(await srv.goldfish_step(srv.GoldfishStepInput(
        game_id=payload["game_id"], until="phase:main1")))
    assert out["applied"] == [] and out["applied_total"] == 0
    assert out["state"] == payload["state"]          # nothing mutated


async def test_until_applied_and_log_delta_caps(monkeypatch):
    payload = await _start(monkeypatch)
    monkeypatch.setattr(srv, "_GOLDFISH_APPLIED_ECHO_CAP", 2)
    monkeypatch.setattr(srv, "_GOLDFISH_LOG_DELTA_CAP", 3)
    out = json.loads(await srv.goldfish_step(srv.GoldfishStepInput(
        game_id=payload["game_id"], until="turn:3")))
    assert out["applied_total"] > 2                  # truncation exercised
    assert len(out["applied"]) == 2
    assert out["log_delta_total"] > 3
    assert len(out["log_delta"]) == 3
    assert out["state"]["log"][-3:] == out["log_delta"]   # the LAST lines


async def test_until_guard_exhaustion_surfaces_error(monkeypatch):
    payload = await _start(monkeypatch)
    gid = payload["game_id"]
    monkeypatch.setattr(srv, "_GOLDFISH_UNTIL_GUARD", 3)
    out = json.loads(await srv.goldfish_step(
        srv.GoldfishStepInput(game_id=gid, until="turn:10")))
    assert "3 steps" in out["error"] and "turn:10" in out["error"]
    assert out["state"] and out["legal_actions"]
    again = json.loads(await srv.goldfish_state(     # game kept, not destroyed
        srv.GoldfishStateInput(game_id=gid)))
    assert again["state"] == out["state"]


async def test_step_after_win_only_pass():
    cards = mini_cards()
    g = new_game(cards, ["Plains"] * 60, "Boss", seed=5)
    g.phase, g.turn, g.won_turn = "main1", 6, 6
    _inject(g, cards, "gid-won")
    out = json.loads(await srv.goldfish_state(
        srv.GoldfishStateInput(game_id="gid-won")))
    assert out["legal_actions"] == [{"type": "pass"}]
    rejected = json.loads(await srv.goldfish_step(srv.GoldfishStepInput(
        game_id="gid-won", action={"type": "play_land", "card": "Plains"})))
    assert "won" in rejected["error"]
    ok = json.loads(await srv.goldfish_step(srv.GoldfishStepInput(
        game_id="gid-won", action={"type": "pass"})))
    assert ok["applied"]["action"] == {"type": "pass"}
