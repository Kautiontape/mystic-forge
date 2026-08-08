"""Self-contained HTML run reports (spec §Run reports, D11).

``render_report`` turns a stored run/AB result into a single HTML document
with zero JavaScript and zero external requests (no ``<script>``, no URLs,
no ``xmlns`` — artifact-CSP-proof by construction). Light/dark theming via
a single ``<style>`` block with ``prefers-color-scheme`` overrides plus a
``data-theme`` stamp escape hatch, following the project's dataviz palette
(series blue, muted ink tokens, hairline grid).

Purity contract: this module never reads the clock. The report date comes
from ``inputs["generated_at"]``, stamped by the caller (the server) at
render time; it is presentation metadata and is stripped before the run
code is recomputed for display.

Every number in the report is read from the metrics/deltas dicts produced
by ``goldfish.metrics``/``goldfish.runner`` — nothing is recomputed. The
one presentation transform: %-reached-by-turn curves accumulate the stored
per-turn histogram counts over the stored ``n`` (the same division
``metrics._prop`` performs; no statistic is re-derived from records).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from html import escape
from typing import Any

ENGINE_LABEL = "goldfish"


def run_code(inputs: dict) -> str:
    """Short base32 code content-addressing a run's canonical inputs.

    Callers hash the RESOLVED library in its GIVEN order plus the
    commander — the exact list handed to the engine — together with
    annotations, combos, n, seed, until_turn and engine_version, NOT the
    raw deck string/URL. Resolution already drops Scryfall-unrecognized
    names and the command-zone commander copy, so raw-text cosmetics
    (count grouping, Archidekt category metadata) don't move the code —
    but ORDER does: the engine shuffles the library as given, so hashing
    anything order-insensitive would let one code stand for two different
    game sequences and break the byte-identical claim below.
    Presentation-only fields (``generated_at``) are excluded by callers.

    Identical inputs -> the same 13-char code and (per the determinism
    criterion) byte-identical results, so the code doubles as a
    reproducibility claim anyone can verify by re-running."""
    canon = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canon.encode()).digest()
    return base64.b32encode(digest[:8]).decode().rstrip("=").lower()


# ── Formatting helpers ───────────────────────────────────────────────────────


def _esc(s: Any) -> str:
    """Escape text-node content. quote=False keeps apostrophes/quotes
    verbatim (the A/B scope banner must appear exactly as composed)."""
    return escape(str(s), quote=False)


def _pct(p: dict) -> str:
    return f"{p['value'] * 100:.1f}%"


def _ci(p: dict) -> str:
    lo, hi = p["ci"]
    return f"{lo * 100:.1f}–{hi * 100:.1f}%"


# ── SVG chart helpers (640×240, dataviz mark specs) ──────────────────────────

_W, _H = 640, 240
_ML, _MR, _MT, _MB = 46, 16, 30, 36


def _nice_max(v: float) -> float:
    """Smallest of {2,4,10}·10^k ≥ v — halves stay clean tick values."""
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    for mult in (2.0, 4.0, 10.0):
        cand = mult * 10 ** exp
        if cand >= v:
            return cand
    return 2.0 * 10 ** (exp + 1)


def _fmt_num(v: float) -> str:
    return f"{v:g}"


def _svg_frame(cls: str, title: str) -> str:
    return (f'<svg class="{cls}" viewBox="0 0 {_W} {_H}" width="{_W}" '
            f'height="{_H}" role="img">'
            f'<text class="ttl" x="{_ML}" y="18">{_esc(title)}</text>')


def _y_grid(ticks: list[tuple[float, str]]) -> str:
    """Hairline gridlines + muted tick labels; the y=0 line is the axis."""
    out = []
    for y, label in ticks:
        cls = "axisline" if label in ("0", "0%") else "gridline"
        out.append(f'<line class="{cls}" x1="{_ML}" y1="{y:.1f}" '
                   f'x2="{_W - _MR}" y2="{y:.1f}"/>')
        out.append(f'<text class="ax" x="{_ML - 6}" y="{y + 4:.1f}" '
                   f'text-anchor="end">{label}</text>')
    return "".join(out)


def _svg_hist(histogram: dict, title: str,
              x_title: str = "Turn", y_title: str = "Games") -> str:
    """Bar chart of an integer-keyed count histogram: thin bars (≤24px),
    4px rounded data-end, square baseline, 2px surface gaps by slot air;
    single series (no legend — the title names it); the tallest bar carries
    the one direct label."""
    counts = {int(k): int(v) for k, v in histogram.items()}
    parts = [_svg_frame("hist", title)]
    if not counts:
        parts.append(f'<text class="ax" x="{_W / 2:.0f}" y="{_H / 2:.0f}" '
                     f'text-anchor="middle">no games reached this event'
                     f'</text></svg>')
        return "".join(parts)
    cats = list(range(min(counts), max(counts) + 1))
    ymax = _nice_max(max(counts.values()))
    base = _H - _MB
    plot_w = _W - _ML - _MR
    plot_h = base - (_MT + 12)     # headroom keeps cap labels off the title

    def sy(v: float) -> float:
        return base - (v / ymax) * plot_h

    parts.append(_y_grid([(sy(0), "0"),
                          (sy(ymax / 2), _fmt_num(ymax / 2)),
                          (sy(ymax), _fmt_num(ymax))]))
    slot = plot_w / len(cats)
    bar_w = min(24.0, max(2.0, slot - 4.0))
    tallest = max(counts, key=lambda c: (counts[c], -c))
    label_step = 1 if len(cats) <= 16 else 2
    for i, cat in enumerate(cats):
        cx = _ML + i * slot + slot / 2
        if i % label_step == 0:
            parts.append(f'<text class="ax" x="{cx:.1f}" y="{base + 14}" '
                         f'text-anchor="middle">{cat}</text>')
        v = counts.get(cat, 0)
        if v <= 0:
            continue
        x0, x1 = cx - bar_w / 2, cx + bar_w / 2
        y0 = sy(v)
        r = min(4.0, bar_w / 2, base - y0)
        parts.append(
            f'<path class="bar" d="M{x0:.1f} {base} L{x0:.1f} {y0 + r:.1f} '
            f'Q{x0:.1f} {y0:.1f} {x0 + r:.1f} {y0:.1f} '
            f'L{x1 - r:.1f} {y0:.1f} Q{x1:.1f} {y0:.1f} '
            f'{x1:.1f} {y0 + r:.1f} L{x1:.1f} {base} Z">'
            f'<title>{x_title} {cat}: {v} {y_title.lower()}</title></path>')
        if cat == tallest:
            parts.append(f'<text class="lbl" x="{cx:.1f}" y="{y0 - 6:.1f}" '
                         f'text-anchor="middle">{v}</text>')
    parts.append(f'<text class="ax" x="{_ML + plot_w / 2:.0f}" y="{_H - 6}" '
                 f'text-anchor="middle">{_esc(x_title)}</text>')
    parts.append(f'<text class="ax" x="14" y="{_MT + plot_h / 2:.0f}" '
                 f'text-anchor="middle" transform="rotate(-90 14 '
                 f'{_MT + plot_h / 2:.0f})">{_esc(y_title)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_reached_curve(pct_by_turn: list[float], title: str) -> str:
    """Cumulative %-reached line: 2px round-capped stroke, ≥8px end marker
    with a 2px surface ring, the endpoint carrying the one direct label."""
    parts = [_svg_frame("curve", title)]
    if not pct_by_turn:
        parts.append(f'<text class="ax" x="{_W / 2:.0f}" y="{_H / 2:.0f}" '
                     f'text-anchor="middle">no data</text></svg>')
        return "".join(parts)
    base = _H - _MB
    plot_w = _W - _ML - _MR
    plot_h = base - (_MT + 12)     # headroom for the endpoint label
    turns = len(pct_by_turn)

    def sx(i: int) -> float:
        return _ML + (i * plot_w / (turns - 1) if turns > 1 else plot_w / 2)

    def sy(v: float) -> float:
        return base - (v / 100.0) * plot_h

    parts.append(_y_grid([(sy(t), f"{t:g}%") for t in (0, 25, 50, 75, 100)]))
    label_step = 1 if turns <= 12 else 2
    for i in range(turns):
        if i % label_step == 0:
            parts.append(f'<text class="ax" x="{sx(i):.1f}" y="{base + 14}" '
                         f'text-anchor="middle">{i + 1}</text>')
    pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}"
                   for i, v in enumerate(pct_by_turn))
    parts.append(f'<polyline class="line" points="{pts}"/>')
    ex, ey = sx(turns - 1), sy(pct_by_turn[-1])
    parts.append(f'<circle class="dot" cx="{ex:.1f}" cy="{ey:.1f}" r="4">'
                 f'<title>turn {turns}: {pct_by_turn[-1]:.1f}%</title>'
                 f'</circle>')
    # Headroom above guarantees ey ≥ _MT + 12, so ey - 8 clears the title.
    parts.append(f'<text class="lbl" x="{ex - 8:.1f}" y="{ey - 8:.1f}" '
                 f'text-anchor="end">{pct_by_turn[-1]:.0f}%</text>')
    parts.append(f'<text class="ax" x="{_ML + plot_w / 2:.0f}" y="{_H - 6}" '
                 f'text-anchor="middle">Turn</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_whisker(mean: float, lo: float, hi: float) -> str:
    """Small CI whisker for an A/B delta row: zero reference, 2px interval
    stroke, mean dot with a 2px surface ring. Domain symmetric around 0 so
    the sign reads at a glance."""
    w, h, pad = 220, 24, 10
    cy = h / 2
    dom = max(abs(lo), abs(hi), abs(mean), 1e-12) * 1.1

    def x(v: float) -> float:
        return w / 2 + (v / dom) * (w / 2 - pad)

    return (f'<svg class="whisker" viewBox="0 0 {w} {h}" width="{w}" '
            f'height="{h}" role="img">'
            f'<line class="zero" x1="{w / 2}" y1="3" x2="{w / 2}" '
            f'y2="{h - 3}"/>'
            f'<line class="range" x1="{x(lo):.1f}" y1="{cy}" '
            f'x2="{x(hi):.1f}" y2="{cy}"/>'
            f'<circle class="dot" cx="{x(mean):.1f}" cy="{cy}" r="4">'
            f'<title>Δ {mean:+.4f} [{lo:+.4f}, {hi:+.4f}]</title></circle>'
            f'</svg>')


def _cum_pct(histogram: dict, n: int, until_turn: int) -> list[float]:
    """%-reached-by-turn from the stored histogram counts and n (the same
    count/n division the metrics layer uses — nothing re-derived)."""
    total = 0
    out: list[float] = []
    for t in range(1, until_turn + 1):
        total += int(histogram.get(str(t), 0))
        out.append(100.0 * total / n if n else 0.0)
    return out


# ── HTML sections ────────────────────────────────────────────────────────────


def _fingerprint(rows: list[tuple[str, str]]) -> str:
    body = "".join(f'<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>'
                   for k, v in rows)
    return f'<table class="fp"><tbody>{body}</tbody></table>'


_REPRO_LINE = (
    "Re-run with these inputs reproduces this report: the run code "
    "content-addresses the resolved deck list, annotations, combos, n, "
    "seed, until_turn and engine version — identical inputs give the same "
    "code and byte-identical results.")


def _tile(label: str, p: dict) -> str:
    return (f'<div class="tile"><div class="tlabel">{_esc(label)}</div>'
            f'<div class="tval">{_pct(p)}</div>'
            f'<div class="tci">95% CI {_ci(p)}</div></div>')


def _stat_tiles(m: dict, until_turn: int) -> str:
    tiles = [
        _tile(f"Commander cast by T{until_turn}",
              m["commander_cast"]["reached_pct"]),
        _tile("Commander cast by T4", m["commander_cast"]["pct_by_turn"]["4"]),
        _tile(f"Kill (40 damage) by T{until_turn}", m["kill"]["reached_pct"]),
        _tile("Games mulliganed", m["mulligans"]["pct_mulliganed"]),
        _tile("On-curve through T5", m["on_curve"]["pct_all_drops_through_t5"]),
        _tile("4+-spell turn by T6", m["casts"]["pct_4plus_turn_by_t6"]),
    ]
    return '<div class="tiles">' + "".join(tiles) + "</div>"


def _trigger_table(fires: dict) -> str:
    if not fires:
        return ('<p class="note">(no annotated triggers fired — nothing to '
                "tabulate)</p>")
    rows = sorted(fires.items(), key=lambda kv: (-kv[1], kv[0]))
    body = []
    for key, avg in rows[:15]:
        card, event, verb = str(key).rsplit("|", 2)
        body.append(f'<tr><td>{_esc(card)}</td>'
                    f'<td>{_esc(event)} → {_esc(verb)}</td>'
                    f'<td class="num">{avg:.2f}</td></tr>')
    out = ('<table class="data"><thead><tr><th>Card</th><th>Trigger</th>'
           '<th class="num">Avg fires/game</th></tr></thead>'
           f'<tbody>{"".join(body)}</tbody></table>')
    if len(rows) > 15:
        out += (f'<p class="note">…and {len(rows) - 15} more annotated '
                "triggers below the top 15.</p>")
    return out


def _honesty_section(honesty: dict | None, approx: dict, heading: str) -> str:
    parts = [f"<h2>{_esc(heading)}</h2>"]
    if honesty is not None:
        def drawn(entries: list[dict]) -> str:
            return ", ".join(
                f"{_esc(e['name'])} (drawn "
                f"{e['drawn_pct']['value'] * 100:.0f}%)" for e in entries)

        oos = honesty["out_of_scope"]
        n_oos = sum(len(v) for v in oos.values())
        parts.append(f"<h3>Out of scope — {n_oos}</h3>"
                     "<p class=\"note\">Inert during simulation, counted by "
                     "class; the sim cannot value these.</p>")
        if n_oos:
            items = "".join(f"<li><strong>{_esc(cls)}</strong>: "
                            f"{drawn(oos[cls])}</li>" for cls in sorted(oos))
            parts.append(f"<ul>{items}</ul>")
        else:
            parts.append("<p>(none)</p>")
        unrec = honesty["unrecognized"]
        parts.append(f"<h3>Unrecognized — {len(unrec)}</h3>"
                     "<p class=\"note\">Candidates for annotation (see "
                     "goldfish_annotate).</p>")
        parts.append(f"<p>{drawn(unrec)}</p>" if unrec else "<p>(none)</p>")
        low = honesty["modeled_low_impact"]
        parts.append("<h3>Modeled, low-impact</h3>"
                     "<p class=\"note\">Drawn but never fired — only this "
                     "group supports "
                     "'the sim agrees this card underperforms'.</p>")
        parts.append("<p>" + ", ".join(_esc(x) for x in low) + "</p>"
                     if low else "<p>(none)</p>")
    by_kind: dict[str, set[str]] = {}
    for name, notes in approx.items():
        for note in notes:
            by_kind.setdefault(str(note).split(":", 1)[0], set()).add(name)
    if by_kind:
        parts.append("<h3>Standing approximations</h3>"
                     "<p class=\"note\">v1 model shortcuts affecting these "
                     "cards.</p>")
        items = "".join(f"<li><strong>{_esc(kind)}</strong>: "
                        f"{', '.join(_esc(x) for x in sorted(by_kind[kind]))}"
                        f"</li>" for kind in sorted(by_kind))
        parts.append(f"<ul>{items}</ul>")
    return "".join(parts)


# ── Style: dataviz reference palette, light + selected dark steps ────────────

_CSS = """
:root { color-scheme: light dark; }
body {
  --surface-1: #fcfcfb; --page: #f9f9f7;
  --ink-1: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7;
  --series-1: #2a78d6;
  --ring: rgba(11,11,11,0.10);
  margin: 0; padding: 24px; background: var(--page); color: var(--ink-1);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) body {
    --surface-1: #1a1a19; --page: #0d0d0d;
    --ink-1: #ffffff; --ink-2: #c3c2b7;
    --grid: #2c2c2a; --axis: #383835;
    --series-1: #3987e5;
    --ring: rgba(255,255,255,0.10);
  }
}
:root[data-theme="dark"] body {
  --surface-1: #1a1a19; --page: #0d0d0d;
  --ink-1: #ffffff; --ink-2: #c3c2b7;
  --grid: #2c2c2a; --axis: #383835;
  --series-1: #3987e5;
  --ring: rgba(255,255,255,0.10);
}
main { max-width: 760px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 12px; }
h2 { font-size: 17px; margin: 28px 0 8px; }
h3 { font-size: 14px; margin: 18px 0 4px; }
.banner {
  background: var(--surface-1); border: 1px solid var(--ring);
  border-left: 3px solid var(--axis); border-radius: 6px;
  padding: 12px 14px; margin: 0 0 20px; color: var(--ink-2); font-size: 14px;
}
table.fp { border-collapse: collapse; margin: 12px 0; }
table.fp th { text-align: left; padding: 3px 16px 3px 0; font-weight: 600;
  color: var(--ink-2); white-space: nowrap; }
table.fp td { padding: 3px 0; }
.repro, .note { color: var(--ink-2); font-size: 13px; }
.tiles { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }
.tile { background: var(--surface-1); border: 1px solid var(--ring);
  border-radius: 8px; padding: 12px 16px; flex: 1 1 150px; }
.tile .tlabel { font-size: 13px; color: var(--ink-2); }
.tile .tval { font-size: 26px; font-weight: 600; }
.tile .tci { font-size: 12px; color: var(--muted); }
figure { margin: 20px 0; background: var(--surface-1);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px;
  overflow-x: auto; }
.tablewrap { overflow-x: auto; }
table.data { border-collapse: collapse; width: 100%; font-size: 14px; }
table.data th { text-align: left; padding: 6px 12px 6px 0;
  border-bottom: 1px solid var(--axis); color: var(--ink-2);
  font-weight: 600; }
table.data td { padding: 5px 12px 5px 0;
  border-bottom: 1px solid var(--grid); }
table.data .num { text-align: right;
  font-variant-numeric: tabular-nums; }
svg { display: block; max-width: 100%; height: auto; }
table.data svg { display: inline-block; vertical-align: middle; }
svg text { font: 11px system-ui, -apple-system, "Segoe UI", sans-serif;
  fill: var(--muted); font-variant-numeric: tabular-nums; }
svg .ttl { font-size: 13px; font-weight: 600; fill: var(--ink-1); }
svg .lbl { font-size: 12px; font-weight: 600; fill: var(--ink-2); }
svg .bar { fill: var(--series-1); }
svg .line { fill: none; stroke: var(--series-1); stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round; }
svg .dot { fill: var(--series-1); stroke: var(--surface-1);
  stroke-width: 2; }
svg .range { stroke: var(--series-1); stroke-width: 2;
  stroke-linecap: round; }
svg .gridline { stroke: var(--grid); stroke-width: 1; }
svg .axisline, svg .zero { stroke: var(--axis); stroke-width: 1; }
"""


def _document(title: str, body: str) -> str:
    return ('<!doctype html>\n<html lang="en">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, '
            'initial-scale=1">\n'
            f"<title>{_esc(title)}</title>\n"
            f"<style>{_CSS}</style>\n</head>\n<body>\n<main>\n"
            f"{body}\n</main>\n</body>\n</html>\n")


# ── Renderers ────────────────────────────────────────────────────────────────


def render_report(result: dict, inputs: dict, kind: str = "run") -> str:
    """Render a stored run into a self-contained HTML report (D11).

    ``result`` is the (records-stripped) runner output — ``metrics`` for
    kind="run"; ``deltas``/``pair_correlation``/``metrics_a``/``metrics_b``
    (plus the server-composed ``scope_banner``) for kind="ab". ``inputs``
    is the run-code input dict, optionally carrying the caller-stamped
    ``generated_at``. Every figure is read from those dicts verbatim."""
    code = run_code({k: v for k, v in inputs.items() if k != "generated_at"})
    if kind == "ab":
        return _render_ab(result, inputs, code)
    return _render_run(result, inputs, code)


def _common_rows(inputs: dict, code: str) -> list[tuple[str, str]]:
    return [
        ("Games", str(inputs.get("n", "?"))),
        ("Seed", str(inputs.get("seed", "?"))),
        ("Simulated through", f"turn {inputs.get('until_turn', '?')}"),
        ("Engine version",
         f"{ENGINE_LABEL} {inputs.get('engine_version', '?')}"),
        ("Generated", str(inputs.get("generated_at", "(not stamped)"))),
        ("Run code", code),
    ]


def _render_run(result: dict, inputs: dict, code: str) -> str:
    m = result["metrics"]
    until_turn = int(inputs.get("until_turn", m.get("until_turn", 10)))
    n = int(m.get("n", inputs.get("n", 0)))
    commander = str(inputs.get("commander", "(unknown)"))
    note = result.get("commander_note")
    deck_n = len(inputs.get("library", ())) + 1
    rows = [("Deck", f"{deck_n} cards including commander"),
            ("Commander", f"{commander} {note}" if note else commander),
            *_common_rows(inputs, code)]
    cc = m["commander_cast"]
    body = [
        "<h1>Goldfish run report</h1>",
        _fingerprint(rows),
        f'<p class="repro">{_esc(_REPRO_LINE)}</p>',
        "<h2>Headline</h2>",
        _stat_tiles(m, until_turn),
        "<h2>Charts</h2>",
        "<figure>" + _svg_hist(cc["histogram"], "Commander cast turn")
        + "</figure>",
        "<figure>" + _svg_reached_curve(
            _cum_pct(cc["histogram"], n, until_turn),
            "Commander cast — cumulative % of games") + "</figure>",
        "<figure>" + _svg_reached_curve(
            _cum_pct(m["kill"]["histogram"], n, until_turn),
            "Kill (40 damage) — cumulative % of games") + "</figure>",
        "<figure>" + _svg_hist(
            m["casts"]["distribution"],
            "Casts in a turn — distribution over all simulated turns",
            x_title="Casts in a turn", y_title="Turns") + "</figure>",
        "<h2>Trigger fires</h2>",
        '<div class="tablewrap">' + _trigger_table(m["trigger_fires"])
        + "</div>",
        _honesty_section(m.get("honesty"),
                         result.get("approx_by_card", {}),
                         "Honesty report"),
    ]
    return _document(f"Goldfish run {code}", "\n".join(body))


def _render_ab(result: dict, inputs: dict, code: str) -> str:
    deltas = result["deltas"]
    banner = result.get("scope_banner")
    deck_a = (f"{len(inputs.get('library_a', ())) + 1} cards — "
              f"{inputs.get('commander_a', '(unknown)')}")
    deck_b = (f"{len(inputs.get('library_b', ())) + 1} cards — "
              f"{inputs.get('commander_b', '(unknown)')}")
    rows = [("Deck A", deck_a), ("Deck B", deck_b),
            *_common_rows(inputs, code)]
    rows[2] = ("Games", f"{inputs.get('n', '?')} (paired)")
    delta_rows = []
    for name, d in deltas.items():
        sig = "YES" if d["significant"] else "no"
        if d.get("mcnemar_p") is not None:
            sig += f" (McNemar p={d['mcnemar_p']:.3g})"
        delta_rows.append(
            f"<tr><td>{_esc(name)}</td>"
            f'<td>{_svg_whisker(d["mean"], d["ci_low"], d["ci_high"])}</td>'
            f'<td class="num">{d["mean"]:+.4f}</td>'
            f'<td class="num">[{d["ci_low"]:+.4f}, {d["ci_high"]:+.4f}]</td>'
            f"<td>{_esc(sig)}</td></tr>")
    corr = result.get("pair_correlation")
    corr_text = (f"Pair correlation: {corr:.3f} (per-game total damage "
                 "between arms; ~1.0 = pairing held, near 0 = degraded "
                 "pairing)." if corr is not None else "")
    body = [
        (f'<p class="banner">{_esc(banner)}</p>' if banner else ""),
        "<h1>Goldfish A/B report</h1>",
        _fingerprint(rows),
        f'<p class="repro">{_esc(_REPRO_LINE)}</p>',
        "<h2>Paired deltas (A − B)</h2>",
        ('<div class="tablewrap"><table class="data"><thead><tr>'
         "<th>Metric</th><th>95% CI whisker</th>"
         '<th class="num">Δ mean</th><th class="num">95% CI</th>'
         "<th>Significant</th></tr></thead>"
         f'<tbody>{"".join(delta_rows)}</tbody></table></div>'),
        f'<p>{_esc(corr_text)}</p>' if corr_text else "",
        (f'<p class="note">Caveat: {_esc(result["caveat"])}</p>'
         if result.get("caveat") else ""),
        _honesty_section(result["metrics_a"].get("honesty"),
                         result.get("approx_by_card_a", {}),
                         "Honesty report — Deck A"),
        _honesty_section(result["metrics_b"].get("honesty"),
                         result.get("approx_by_card_b", {}),
                         "Honesty report — Deck B"),
    ]
    return _document(f"Goldfish A/B {code}", "\n".join(b for b in body if b))
