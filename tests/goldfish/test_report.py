"""Task 21 — HTML run reports, resolved-library run codes, hosted route.

Report rendering is pure (report.py never reads the clock — the caller
stamps inputs["generated_at"]); the rendered HTML is self-contained: no
<script>, no external URLs (asserted as zero occurrences of "http" — the
hosted Report: link lives in the TOOL output, never in the HTML).
"""
import json
import os
from types import SimpleNamespace

import pytest

import server as srv
from goldfish import ENGINE_VERSION
from goldfish import metrics as gmetrics
from goldfish.report import render_report, run_code
from goldfish.runner import run_ab, run_batch
from tests.goldfish.helpers import (
    MINI_DECK_TEXT,
    _patch_fetch_minideck,
    mini_cards,
    small_deck,
)

_AB_BANNER = (
    "Changed: 3 cards — 3 simulated, 0 out of scope. "
    "Deck A: 0/39 out of scope; Deck B: 0/39. Deltas measure speed and "
    "consistency only; the sim cannot value interaction, and converting "
    "out-of-scope cards to simulable ones inflates deltas — do not read "
    "results as 'cut your removal'.")


def _payload(out: str) -> dict:
    return json.loads(out.split("```json")[1].split("```")[0])


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sample_run():
    """(result, inputs, html) for a real run_batch on the mini deck."""
    cards = mini_cards()
    deck = small_deck()
    result = run_batch(cards, deck, "Boss", n=30, seed=7, until_turn=6)
    scopes: dict = {name: None for name in cards}
    scopes["Hammer"] = "interaction_removal"
    scopes["Mystery Elk"] = "unrecognized"
    result["metrics"] = gmetrics.aggregate(result["records"], 6, scopes)
    # 17 synthetic trigger rows exercise the top-15 cap + count note.
    result["metrics"]["trigger_fires"] = {
        f"Card{i:02d}|etb|draw": float(20 - i) for i in range(17)}
    result["approx_by_card"] = {"Hammer": ["equip: greedy chain undercount"]}
    del result["records"]
    inputs = {"library": sorted(deck), "commander": "Boss",
              "annotations": [], "combos": [], "n": 30, "seed": 7,
              "until_turn": 6, "opponents": 1, "mulligan": {},
              "engine_version": ENGINE_VERSION,
              "generated_at": "2026-08-08 14:00 UTC"}
    return result, inputs, render_report(result, inputs, kind="run")


@pytest.fixture(scope="module")
def sample_ab():
    """(result, inputs, html) for a real run_ab of two mini-deck variants."""
    cards = mini_cards()
    deck_a = small_deck()
    deck_b = ["Plains"] * 15 + ["Mountain"] * 15 + ["Bear"] * 2 + ["Runner"] * 7
    result = run_ab(cards, deck_a, "Boss", cards, deck_b, "Boss",
                    n=20, seed=3, until_turn=6)
    scopes: dict = {name: None for name in cards}
    result["metrics_a"] = gmetrics.aggregate(result["records_a"], 6, scopes)
    result["metrics_b"] = gmetrics.aggregate(result["records_b"], 6, scopes)
    result["scope_banner"] = _AB_BANNER
    result["approx_by_card_a"] = {"Hammer": ["equip: greedy chain undercount"]}
    result["approx_by_card_b"] = {}
    del result["records_a"], result["records_b"]
    inputs = {"library_a": sorted(deck_a), "library_b": sorted(deck_b),
              "commander_a": "Boss", "commander_b": "Boss",
              "annotations": [], "annotations_a": [], "annotations_b": [],
              "combos": [], "n": 20, "seed": 3, "until_turn": 6,
              "allow_different_commanders": False,
              "engine_version": ENGINE_VERSION,
              "generated_at": "2026-08-08 14:05 UTC"}
    return result, inputs, render_report(result, inputs, kind="ab")


# ── run_code: resolved-library semantics (Task-19 deferred item) ─────────────


async def test_run_code_order_insensitive_card_sensitive(monkeypatch):
    """Reordering the decklist keeps the run code; a card swap changes it."""
    _patch_fetch_minideck(monkeypatch)
    reordered = "1 Boss\n12 Hammer\n30 Bear\n20 Runner\n17 Mountain\n20 Plains"
    swapped = "1 Boss\n20 Plains\n17 Mountain\n30 Bear\n21 Runner\n11 Hammer"
    ids = []
    for deck in (MINI_DECK_TEXT, reordered, swapped):
        out = await srv.goldfish_run(srv.GoldfishRunInput(
            deck=deck, n=2, seed=5, until_turn=2))
        ids.append(_payload(out)["run_id"])
    assert ids[0] == ids[1]        # same multiset, different order → same code
    assert ids[0] != ids[2]        # one-card swap → different code


def test_run_code_shape_and_generated_at_excluded():
    inputs = {"library": ["Bear", "Plains"], "commander": "Boss", "seed": 1}
    code = run_code(inputs)
    assert len(code) == 13 and code.isalnum() and code == code.lower()
    # generated_at is presentation metadata: render must not let it drift
    # the printed code (render strips it before hashing).
    html = render_report(
        {"metrics": _minimal_metrics()},
        {**inputs, "annotations": [], "combos": [], "n": 1, "seed": 1,
         "until_turn": 2, "engine_version": "0.1.0",
         "generated_at": "2026-08-08 00:00 UTC"})
    stripped = run_code({**inputs, "annotations": [], "combos": [], "n": 1,
                         "seed": 1, "until_turn": 2,
                         "engine_version": "0.1.0"})
    assert stripped in html


def _minimal_metrics() -> dict:
    cards = mini_cards()
    result = run_batch(cards, small_deck(), "Boss", n=2, seed=1, until_turn=2)
    return result["metrics"]


# ── render_report: run kind ──────────────────────────────────────────────────


def test_report_self_contained(sample_run):
    _result, _inputs, html = sample_run
    assert html.startswith("<!doctype html>")
    assert "<script" not in html.lower()
    assert "http" not in html.lower()      # zero external URLs, no xmlns
    assert "<svg" in html


def test_report_fingerprint_and_repro_line(sample_run):
    _result, inputs, html = sample_run
    low = html.lower()
    for key in ("seed", "run code", "engine"):
        assert key in low
    assert "Boss" in html
    assert "40 cards" in html              # 39 library + commander
    assert ENGINE_VERSION in html
    assert "2026-08-08 14:00 UTC" in html  # caller-stamped, not clock-read
    code = run_code({k: v for k, v in inputs.items() if k != "generated_at"})
    assert code in html
    assert "re-run with these inputs reproduces this report" in low


def test_report_dark_mode_block(sample_run):
    _result, _inputs, html = sample_run
    assert html.count("<style") == 1
    assert "@media (prefers-color-scheme: dark)" in html


def test_report_numbers_match_metrics(sample_run):
    """Spot-check: rendered numbers are read from the metrics dict."""
    result, _inputs, html = sample_run
    m = result["metrics"]
    cc = m["commander_cast"]["reached_pct"]
    assert f"{cc['value'] * 100:.1f}%" in html
    lo, hi = m["kill"]["reached_pct"]["ci"]
    assert f"{lo * 100:.1f}–{hi * 100:.1f}%" in html
    mull = m["mulligans"]["pct_mulliganed"]
    assert f"{mull['value'] * 100:.1f}%" in html
    assert f"{m['trigger_fires']['Card03|etb|draw']:.2f}" in html


def test_report_charts_tiles_tables_honesty(sample_run):
    _result, _inputs, html = sample_run
    assert html.count('class="hist"') == 2     # cast-turn + casts distribution
    assert html.count('class="curve"') == 2    # commander-cast + kill reached
    assert html.count('class="tile"') >= 5     # stat tiles with CI ranges
    assert "95% CI" in html
    # Trigger table: top 15 of 17, with the remainder counted.
    assert "Card00" in html and "Card14" in html
    assert "Card15" not in html and "Card16" not in html
    assert "2 more" in html
    # Honesty: three-way split + standing notes.
    assert "Honesty report" in html
    assert "interaction_removal" in html
    assert "Mystery Elk" in html
    assert "Unrecognized" in html
    assert "low-impact" in html
    assert "Standing approximations" in html and "equip" in html


# ── render_report: ab kind ───────────────────────────────────────────────────


def test_ab_report_banner_verbatim_at_top(sample_ab):
    result, _inputs, html = sample_ab
    assert result["scope_banner"] in html                  # verbatim
    assert html.index(result["scope_banner"]) < html.index("<h1")


def test_ab_report_whiskers_deltas_correlation(sample_ab):
    result, _inputs, html = sample_ab
    assert html.count('class="whisker"') == len(result["deltas"])
    d = result["deltas"]["total_damage"]
    assert f"{d['mean']:+.4f}" in html
    assert f"{result['pair_correlation']:.3f}" in html
    assert "McNemar" in html               # binary metrics carry exact p
    assert "Deck A" in html and "Deck B" in html
    assert "Honesty report — Deck A" in html
    assert "Honesty report — Deck B" in html
    assert "<script" not in html.lower() and "http" not in html.lower()
    assert "@media (prefers-color-scheme: dark)" in html


# ── goldfish_report tool ─────────────────────────────────────────────────────


async def test_goldfish_report_round_trip(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=5, seed=9, until_turn=4))
    run_id = _payload(out)["run_id"]
    entry = srv._GOLDFISH_RUNS[run_id]
    assert entry["html"] is None                       # rendered lazily
    assert "records" not in entry["result"]            # trimmed at store time
    html1 = await srv.goldfish_report(srv.GoldfishReportInput(run_id=run_id))
    assert html1.startswith("<!doctype html>")
    assert run_id in html1 and "<svg" in html1
    assert "UTC" in html1                              # server-stamped date
    assert entry["html"] == html1                      # cached in the entry
    html2 = await srv.goldfish_report(srv.GoldfishReportInput(run_id=run_id))
    assert html2 == html1


async def test_goldfish_report_unknown_id():
    srv._GOLDFISH_RUNS.clear()
    try:
        srv._goldfish_store_run("liverun123456", {"n": 1}, {"metrics": {}},
                                "run")
        out = await srv.goldfish_report(
            srv.GoldfishReportInput(run_id="nosuchrun1234"))
        assert "nosuchrun1234" in out
        assert "liverun123456" in out                  # lists live run_ids
        assert "recreate" in out.lower()               # reruns recreate them
    finally:
        srv._GOLDFISH_RUNS.clear()


def test_goldfish_runs_lru_read_refresh():
    srv._GOLDFISH_RUNS.clear()
    try:
        srv._goldfish_store_run("aaa", {}, {}, "run")
        srv._goldfish_store_run("bbb", {}, {}, "run")
        assert srv._goldfish_get_run("aaa") is not None    # read refreshes
        assert srv._goldfish_get_run("zzz") is None
        for i in range(srv.GOLDFISH_MAX_RUNS - 1):
            srv._goldfish_store_run(f"id{i}", {}, {}, "run")
        # 51 inserts against a 50-cap: the unread "bbb" is evicted first.
        assert "aaa" in srv._GOLDFISH_RUNS
        assert "bbb" not in srv._GOLDFISH_RUNS
    finally:
        srv._GOLDFISH_RUNS.clear()


def test_store_drops_records():
    srv._GOLDFISH_RUNS.clear()
    try:
        srv._goldfish_store_run(
            "ccc", {"n": 1},
            {"metrics": {"n": 1}, "records": [1], "records_a": [2],
             "records_b": [3]}, "run")
        entry = srv._GOLDFISH_RUNS["ccc"]
        for key in ("records", "records_a", "records_b"):
            assert key not in entry["result"]
        assert entry["result"]["metrics"] == {"n": 1}
        assert entry["html"] is None
    finally:
        srv._GOLDFISH_RUNS.clear()


# ── Hosted mode ──────────────────────────────────────────────────────────────


async def test_hosted_mode_persists_and_links(monkeypatch, tmp_path):
    _patch_fetch_minideck(monkeypatch)
    monkeypatch.setattr(srv, "GOLDFISH_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "GOLDFISH_PUBLIC_BASE_URL",
                        "https://reports.example")
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=5, seed=11, until_turn=4))
    run_id = _payload(out)["run_id"]
    assert f"Report: https://reports.example/goldfish/r/{run_id}" in out
    disk = (tmp_path / f"{run_id}.html").read_text()
    assert "<svg" in disk
    assert "http" not in disk.lower()      # the URL lives in the tool output
    assert srv._GOLDFISH_RUNS[run_id]["html"] == disk


def test_hosted_cleanup_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "GOLDFISH_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "GOLDFISH_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(srv, "GOLDFISH_REPORT_FILE_CAP", 2)
    codes = ["aaaaaaaaaaaa1", "bbbbbbbbbbbb2"]
    for i, code in enumerate(codes):
        url = srv._goldfish_persist_report(code, {"html": f"<p>{code}</p>"})
        assert url is None                 # base URL unset → no report_url
        os.utime(tmp_path / f"{code}.html", (1000 + i, 1000 + i))
    srv._goldfish_persist_report("cccccccccccc3", {"html": "<p>c</p>"})
    assert not (tmp_path / "aaaaaaaaaaaa1.html").exists()   # oldest pruned
    assert (tmp_path / "bbbbbbbbbbbb2.html").exists()
    assert (tmp_path / "cccccccccccc3.html").exists()


async def test_route_handler_404_and_200(monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "GOLDFISH_REPORT_DIR", str(tmp_path))
    code = "abc123def456g"

    def req(c):
        return SimpleNamespace(path_params={"code": c})

    assert (await srv._goldfish_report_page(req("../etc/passwd"))
            ).status_code == 404           # bad shape
    assert (await srv._goldfish_report_page(req("ABCDEFGHIJKLM"))
            ).status_code == 404           # uppercase rejected
    assert (await srv._goldfish_report_page(req(code))
            ).status_code == 404           # valid shape, unknown code
    (tmp_path / f"{code}.html").write_text("<p>hosted report</p>")
    ok = await srv._goldfish_report_page(req(code))
    assert ok.status_code == 200
    assert b"<p>hosted report</p>" in ok.body
    assert "text/html" in ok.headers["content-type"]


async def test_unconfigured_no_persist_no_url(monkeypatch):
    _patch_fetch_minideck(monkeypatch)
    monkeypatch.setattr(srv, "GOLDFISH_REPORT_DIR", None)
    monkeypatch.setattr(srv, "GOLDFISH_PUBLIC_BASE_URL",
                        "https://reports.example")
    assert srv._goldfish_persist_report("a" * 13, {"html": "<p></p>"}) is None
    out = await srv.goldfish_run(srv.GoldfishRunInput(
        deck=MINI_DECK_TEXT, n=3, seed=2, until_turn=3))
    assert "https://reports.example" not in out        # no report_url
    run_id = _payload(out)["run_id"]
    html = await srv.goldfish_report(srv.GoldfishReportInput(run_id=run_id))
    assert "<svg" in html                  # LRU delivery still works
