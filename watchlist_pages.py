"""Server-rendered watchlist pages — the "arcane ledger".

Catppuccin Latte (day) / Macchiato (night), serif display over mono data.
Single-series price charts: blue line, status deltas always carry a glyph +
ink-token text (never color alone); dark palette validated for contrast
against the Macchiato surface. All assets inline — no external requests.
"""

import json
from datetime import datetime, timezone
from html import escape as esc

import watchlist_db

EVENTS_PER_PAGE = 12
CARDS_PER_PAGE = 24

# Chart geometry shared with the inline JS crosshair (keep in sync there).
CW, CH, CPAD = 640, 220, 34
SW, SH = 240, 56

_CSS = """
:root{
  --font-display:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --font-data:ui-monospace,"Cascadia Code","JetBrains Mono","Fira Code",Menlo,monospace;
}
[data-theme=latte]{
  --base:#eff1f5;--mantle:#e6e9ef;--crust:#dce0e8;--surface0:#ccd0da;
  --surface1:#bcc0cc;--text:#4c4f69;--sub:#6c6f85;--overlay:#9ca0b0;
  --blue:#1e66f5;--lavender:#7287fd;--mauve:#8839ef;--peach:#fe640b;
  --teal:#179299;--green:#40a02b;--red:#d20f39;--yellow:#df8e1d;
  --card:#ffffffcc;--glow1:#8839ef14;--glow2:#fe640b12;--shadow:#4c4f6922;
}
[data-theme=macchiato]{
  --base:#24273a;--mantle:#1e2030;--crust:#181926;--surface0:#363a4f;
  --surface1:#494d64;--text:#cad3f5;--sub:#a5adcb;--overlay:#6e738d;
  --blue:#8aadf4;--lavender:#b7bdf8;--mauve:#c6a0f6;--peach:#f5a97f;
  --teal:#8bd5ca;--green:#a6da95;--red:#ed8796;--yellow:#eed49d;
  --card:#1e2030cc;--glow1:#c6a0f61a;--glow2:#f5a97f14;--shadow:#00000055;
}
*{box-sizing:border-box;margin:0}
body{
  font-family:var(--font-display);color:var(--text);background:var(--base);
  background-image:radial-gradient(60rem 40rem at 85% -10%,var(--glow1),transparent 60%),
                   radial-gradient(50rem 34rem at -10% 100%,var(--glow2),transparent 55%);
  background-attachment:fixed;min-height:100vh;padding-bottom:4rem;
  transition:background-color .3s,color .3s;
}
body::before{content:"";position:fixed;inset:0 0 auto 0;height:3px;z-index:5;
  background:linear-gradient(90deg,var(--mauve),var(--peach),var(--teal))}
.wrap{max-width:72rem;margin:0 auto;padding:2.2rem 1.2rem 0}
header.masthead{display:flex;flex-wrap:wrap;align-items:baseline;gap:.6rem 1rem;margin-bottom:.4rem}
h1{font-size:clamp(1.6rem,4vw,2.4rem);font-weight:600;letter-spacing:.01em}
h1 .rune{color:var(--mauve)}
.meta{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;color:var(--sub);
  font-size:.85rem;margin-bottom:1.4rem}
.chip{font-family:var(--font-data);font-size:.75rem;background:var(--mantle);
  border:1px solid var(--surface0);border-radius:999px;padding:.15rem .6rem;cursor:pointer}
.chip:hover{border-color:var(--overlay)}
.spacer{flex:1}
#theme{margin-left:auto;background:var(--mantle);border:1px solid var(--surface0);
  color:var(--text);border-radius:999px;padding:.3rem .8rem;cursor:pointer;font-size:.9rem}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
  gap:.8rem;margin-bottom:1.6rem}
.stat{background:var(--card);border:1px solid var(--surface0);border-radius:.8rem;
  padding:.7rem .9rem;backdrop-filter:blur(6px)}
.stat b{display:block;font-family:var(--font-data);font-size:1.25rem;font-weight:600}
.stat span{font-size:.72rem;color:var(--sub);text-transform:uppercase;letter-spacing:.08em}
.cols{display:grid;grid-template-columns:1fr 19rem;gap:1.4rem;align-items:start}
@media(max-width:56rem){.cols{grid-template-columns:1fr}}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(15.5rem,1fr));gap:.9rem}
.card{background:var(--card);border:1px solid var(--surface0);border-radius:1rem;
  padding:.9rem 1rem .7rem;cursor:pointer;backdrop-filter:blur(6px);
  box-shadow:0 1px 2px var(--shadow);transition:transform .18s,box-shadow .18s,border-color .18s;
  animation:rise .5s both;position:relative}
.card:hover{transform:translateY(-3px);box-shadow:0 8px 24px var(--shadow);border-color:var(--lavender)}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
.card h3{font-size:1.02rem;font-weight:600;line-height:1.25}
.badge{font-family:var(--font-data);font-size:.66rem;color:var(--sub);
  border:1px solid var(--surface1);border-radius:.35rem;padding:.05rem .35rem;
  vertical-align:2px;margin-left:.35rem;white-space:nowrap}
.note{font-style:italic;color:var(--sub);font-size:.78rem;margin:.15rem 0 .4rem;min-height:1em}
.price{font-family:var(--font-data);font-size:1.5rem;font-weight:600;letter-spacing:-.01em}
.price small{font-size:.7rem;color:var(--peach);font-weight:400}
.deltas{display:flex;gap:.7rem;font-family:var(--font-data);font-size:.75rem;
  color:var(--text);margin:.15rem 0 .35rem}
.deltas .lbl{color:var(--overlay)}
.dn{color:var(--green)}.up{color:var(--red)}.fl{color:var(--overlay)}
.target{font-size:.74rem;color:var(--sub);font-family:var(--font-data)}
.target.hit{color:var(--green);font-weight:600}
.spark{width:100%;height:auto;display:block;margin-top:.45rem}
.spark polyline{stroke-dasharray:600;stroke-dashoffset:600;animation:draw 1.1s .15s forwards ease-out}
@keyframes draw{to{stroke-dashoffset:0}}
.nodata{color:var(--overlay);font-size:.75rem;font-style:italic;margin-top:.6rem}
.rail{background:var(--card);border:1px solid var(--surface0);border-radius:1rem;
  padding:1rem;backdrop-filter:blur(6px)}
.rail h2{font-size:1rem;letter-spacing:.06em;text-transform:uppercase;
  color:var(--sub);font-weight:600;margin-bottom:.6rem}
.rev{display:flex;gap:.55rem;align-items:baseline;padding:.42rem .3rem;border-radius:.5rem;
  cursor:pointer;font-size:.82rem;border-bottom:1px dashed var(--surface0)}
.rev:hover{background:var(--mantle)}
.rev .n{font-family:var(--font-data);color:var(--mauve);min-width:2.4rem}
.rev .a{font-family:var(--font-data);font-size:.7rem;color:var(--sub);
  text-transform:uppercase;min-width:4.6rem}
.rev .d{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rev .t{color:var(--overlay);font-size:.7rem;white-space:nowrap}
.pager{display:flex;justify-content:center;gap:.8rem;margin-top:.7rem;
  font-family:var(--font-data);font-size:.8rem}
.pager a{color:var(--blue);text-decoration:none}.pager span{color:var(--sub)}
dialog{border:1px solid var(--surface1);border-radius:1rem;background:var(--base);
  color:var(--text);max-width:44rem;width:92vw;padding:1.3rem;margin:auto;
  max-height:90vh;overflow:auto;box-shadow:0 20px 60px var(--shadow)}
dialog::backdrop{background:#0006;backdrop-filter:blur(3px)}
dialog h3{font-size:1.2rem;margin-bottom:.2rem}
dialog .sub{color:var(--sub);font-size:.8rem;margin-bottom:.8rem}
.chart-wrap{position:relative}
.tip{position:absolute;pointer-events:none;background:var(--crust);border:1px solid var(--surface1);
  border-radius:.5rem;padding:.25rem .55rem;font-family:var(--font-data);font-size:.72rem;
  transform:translate(-50%,-115%);white-space:nowrap;display:none}
table.snap{width:100%;border-collapse:collapse;font-size:.84rem;margin:.5rem 0}
table.snap th{text-align:left;color:var(--sub);font-size:.7rem;text-transform:uppercase;
  letter-spacing:.06em;padding:.3rem .5rem;border-bottom:1px solid var(--surface1)}
table.snap td{padding:.32rem .5rem;border-bottom:1px solid var(--surface0)}
table.snap td.num{font-family:var(--font-data)}
.btnrow{display:flex;gap:.6rem;margin-top:.9rem;flex-wrap:wrap}
button.act{font-family:inherit;font-size:.88rem;border-radius:.6rem;cursor:pointer;
  padding:.45rem 1rem;border:1px solid var(--surface1);background:var(--mantle);color:var(--text)}
button.act.primary{background:var(--mauve);border-color:var(--mauve);color:var(--base)}
button.act:hover{filter:brightness(1.08)}
.secret{font-family:var(--font-data);background:var(--mantle);border:1px dashed var(--peach);
  border-radius:.5rem;padding:.5rem .7rem;margin:.5rem 0;word-break:break-all}
footer{margin-top:2.5rem;text-align:center;color:var(--overlay);font-size:.75rem}
footer a{color:var(--sub)}
.axis{font-family:var(--font-data);font-size:10px;fill:var(--sub)}
.gridline{stroke:var(--surface0);stroke-width:1}
"""

_JS_TMPL = """
const KEY=%(key)s, EDITABLE=%(editable)s, CPAD=%(cpad)d, CW=%(cw)d, CH=%(ch)d;
document.getElementById('theme').onclick=()=>{
  const h=document.documentElement;
  const next=h.dataset.theme==='latte'?'macchiato':'latte';
  h.dataset.theme=next;localStorage.setItem('mf-theme',next);setThemeLabel();
};
function setThemeLabel(){
  document.getElementById('theme').textContent=
    document.documentElement.dataset.theme==='latte'?'\\u{1F319} macchiato':'\\u2600\\uFE0F latte';
}
setThemeLabel();
document.querySelectorAll('.chip[data-copy]').forEach(c=>c.onclick=e=>{
  e.stopPropagation();navigator.clipboard.writeText(c.dataset.copy);
  const t=c.textContent;c.textContent='copied ✓';setTimeout(()=>c.textContent=t,900);
});
// ── card detail modal with crosshair chart ──
const cardDlg=document.getElementById('cardDlg');
document.querySelectorAll('.card[data-pts]').forEach(card=>{
  card.onclick=()=>{
    document.getElementById('cardTitle').textContent=card.dataset.name;
    document.getElementById('cardSub').textContent=card.dataset.sub;
    document.getElementById('chartHost').innerHTML=card.dataset.chart||'';
    document.getElementById('snapHost').innerHTML=card.dataset.tail||'';
    armCrosshair(JSON.parse(card.dataset.pts));
    cardDlg.showModal();
  };
});
function armCrosshair(pts){
  const svg=document.querySelector('#chartHost svg');if(!svg||!pts.length)return;
  const wrap=svg.parentElement,tip=document.getElementById('tip');
  const ns='http://www.w3.org/2000/svg';
  const vline=document.createElementNS(ns,'line');
  vline.setAttribute('stroke','var(--overlay)');vline.setAttribute('stroke-dasharray','3 3');
  const dot=document.createElementNS(ns,'circle');
  dot.setAttribute('r','4');dot.setAttribute('fill','var(--blue)');
  dot.setAttribute('stroke','var(--base)');dot.setAttribute('stroke-width','2');
  svg.append(vline,dot);
  const lo=Math.min(...pts.map(p=>p[1])),hi=Math.max(...pts.map(p=>p[1]));
  const pad=(hi-lo)||1;
  svg.onmousemove=e=>{
    const r=svg.getBoundingClientRect();
    const fx=Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));
    const i=Math.round(fx*(pts.length-1));
    const x=CPAD+(CW-2*CPAD)*(pts.length>1?i/(pts.length-1):.5);
    const y=CH-CPAD-(CH-2*CPAD)*((pts[i][1]-lo)/pad);
    vline.setAttribute('x1',x);vline.setAttribute('x2',x);
    vline.setAttribute('y1',CPAD);vline.setAttribute('y2',CH-CPAD);
    dot.setAttribute('cx',x);dot.setAttribute('cy',y);
    tip.style.display='block';
    tip.style.left=(x/CW*r.width)+'px';tip.style.top=(y/CH*r.height)+'px';
    tip.textContent=pts[i][0]+' · $'+pts[i][1].toFixed(2);
  };
  svg.onmouseleave=()=>{tip.style.display='none';
    vline.setAttribute('x1',-9);vline.setAttribute('x2',-9);dot.setAttribute('cx',-9)};
}
// ── revision modal ──
const revDlg=document.getElementById('revDlg');
document.querySelectorAll('.rev').forEach(r=>r.onclick=async()=>{
  const seq=r.dataset.seq;
  document.getElementById('revTitle').textContent='Revision #'+seq;
  document.getElementById('revBody').innerHTML='<p class=sub>consulting the ledger…</p>';
  document.getElementById('forkOut').innerHTML='';
  revDlg.showModal();
  const res=await fetch('/api/revision/'+encodeURIComponent(KEY)+'/'+seq);
  if(!res.ok){document.getElementById('revBody').textContent='Could not read revision.';return}
  const d=await res.json();
  let h='<table class=snap><tr><th>Card</th><th>Printing</th><th>Target</th><th>Note</th></tr>';
  if(!d.entries.length)h+='<tr><td colspan=4><i>empty at this revision</i></td></tr>';
  for(const e of d.entries){
    h+=`<tr><td>${e.card_name}</td><td class=num>${e.set_code?e.set_code+' #'+e.collector_number:'cheapest'}</td>`+
       `<td class=num>${e.target_price!=null?'$'+e.target_price.toFixed(2):'—'}</td><td>${e.note||''}</td></tr>`;
  }
  document.getElementById('revBody').innerHTML=h+'</table>';
  document.getElementById('forkBtn').dataset.seq=seq;
  const rec=document.getElementById('recoverBtn');
  if(rec)rec.dataset.seq=seq;
});
async function doFork(mode,seq){
  const res=await fetch('/api/fork',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:KEY,at_seq:+seq,mode})});
  const out=document.getElementById('forkOut');
  if(!res.ok){out.textContent='Fork failed: '+await res.text();return}
  const d=await res.json();
  out.innerHTML=`<div class=secret>⚠ shown once — passphrase: <b>${d.passphrase}</b><br>`+
    `page: <a href="${d.page}">${d.page}</a> · share: ${d.share_code}</div>`+
    (mode==='recover'?'<p class=sub>The current list is now marked superseded.</p>':'');
}
document.getElementById('forkBtn').onclick=e=>doFork('fork',e.target.dataset.seq);
const recBtn=document.getElementById('recoverBtn');
if(recBtn)recBtn.onclick=e=>doFork('recover',e.target.dataset.seq);
document.querySelectorAll('dialog .close').forEach(b=>b.onclick=()=>b.closest('dialog').close());
"""


def _pts(db, entry):
    s = watchlist_db.entry_price_summary(db, entry)
    if not s:
        return None, []
    series = watchlist_db.price_series(
        db, watchlist_db.uuids_for_entry(db, entry), days=90,
        finish=s.get("finish", "normal"))
    return s, (series["points"] if series else [])


def _coords(points, w, h, pad):
    lo = min(p[1] for p in points)
    hi = max(p[1] for p in points)
    rng = (hi - lo) or 1.0
    n = len(points)
    out = []
    for i, (_, v) in enumerate(points):
        x = pad + (w - 2 * pad) * (i / (n - 1) if n > 1 else 0.5)
        y = h - pad - (h - 2 * pad) * ((v - lo) / rng)
        out.append((round(x, 1), round(y, 1)))
    return out, lo, hi


def _spark_svg(points, name):
    if len(points) < 2:
        return ""
    xy, _, _ = _coords(points, SW, SH, 4)
    pl = " ".join(f"{x},{y}" for x, y in xy)
    area = f"M4,{SH - 2} L" + " L".join(f"{x},{y}" for x, y in xy) + f" L{SW - 4},{SH - 2} Z"
    ex, ey = xy[-1]
    return (
        f'<svg class="spark" viewBox="0 0 {SW} {SH}" role="img">'
        f'<title>{esc(name)} — 90 day price trend</title>'
        f'<path d="{area}" fill="var(--blue)" opacity=".12"/>'
        f'<polyline points="{pl}" fill="none" stroke="var(--blue)" stroke-width="2" stroke-linejoin="round"/>'
        f'<circle cx="{ex}" cy="{ey}" r="3" fill="var(--blue)" stroke="var(--base)" stroke-width="2"/>'
        f'</svg>')


def _big_svg(points, name):
    if len(points) < 2:
        return "<p class=nodata>Not enough history yet.</p>"
    xy, lo, hi = _coords(points, CW, CH, CPAD)
    pl = " ".join(f"{x},{y}" for x, y in xy)
    gy = [CPAD, CH / 2, CH - CPAD]
    grid = "".join(f'<line class="gridline" x1="{CPAD}" y1="{y}" x2="{CW - CPAD}" y2="{y}"/>'
                   for y in gy)
    cur = points[-1][1]
    return (
        f'<svg viewBox="0 0 {CW} {CH}" style="width:100%;height:auto" role="img">'
        f'<title>{esc(name)} — 90 day price history</title>{grid}'
        f'<text class="axis" x="{CPAD}" y="{CPAD - 6}">${hi:.2f}</text>'
        f'<text class="axis" x="{CPAD}" y="{CH - CPAD + 14}">${lo:.2f}</text>'
        f'<text class="axis" x="{CW - CPAD}" y="{CPAD - 6}" text-anchor="end">now ${cur:.2f}</text>'
        f'<text class="axis" x="{CPAD}" y="{CH - 6}">{esc(points[0][0])}</text>'
        f'<text class="axis" x="{CW - CPAD}" y="{CH - 6}" text-anchor="end">{esc(points[-1][0])}</text>'
        f'<polyline points="{pl}" fill="none" stroke="var(--blue)" stroke-width="2" stroke-linejoin="round"/>'
        f'</svg>')


def _delta(v, label):
    if v is None:
        return f'<span><span class="lbl">{label}</span> <span class="fl">·—</span></span>'
    cls, glyph = ("dn", "▼") if v < 0 else ("up", "▲") if v > 0 else ("fl", "·")
    return (f'<span><span class="lbl">{label}</span> '
            f'<span class="{cls}">{glyph}</span>{abs(v):.2f}</span>')


def _ago(ts):
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ts
    d = datetime.now(timezone.utc) - then
    if d.days > 0:
        return f"{d.days}d ago"
    if d.seconds >= 3600:
        return f"{d.seconds // 3600}h ago"
    return f"{max(1, d.seconds // 60)}m ago"


def _tail_table(points):
    rows = "".join(
        f'<tr><td class="num">{esc(d)}</td><td class="num">${v:.2f}</td></tr>'
        for d, v in points[-10:][::-1])
    return ('<table class="snap"><tr><th>Date</th><th>Price</th></tr>'
            + rows + "</table>") if rows else ""


def _card_html(db, entry, idx):
    s, points = _pts(db, entry)
    name = esc(entry["card_name"])
    badge = (f'<span class="badge">{esc(entry["set_code"])} '
             f'#{esc(entry["collector_number"] or "")}</span>'
             if entry.get("set_code") else "")
    note = f'<p class="note">{esc(entry["note"] or "")}</p>'
    if s:
        foil = ' <small>(foil)</small>' if s.get("finish") == "foil" else ""
        price = f'<div class="price">${s["current"]:.2f}{foil}</div>'
        deltas = (f'<div class="deltas">{_delta(s["d7"], "7d")}'
                  f'{_delta(s["d30"], "30d")}</div>')
    else:
        price = '<div class="nodata">awaiting first ingest…</div>'
        deltas = ""
    target = ""
    if entry.get("target_price") is not None:
        if s and s["current"] <= entry["target_price"]:
            target = f'<div class="target hit">🎯 at target ${entry["target_price"]:.2f}</div>'
        else:
            gap = f' · ${s["current"] - entry["target_price"]:.2f} above' if s else ""
            target = f'<div class="target">target ${entry["target_price"]:.2f}{gap}</div>'
    spark = _spark_svg(points, entry["card_name"]) if points else ""
    sub = (f'{entry["set_code"]} #{entry["collector_number"]}'
           if entry.get("set_code") else "cheapest printing") + " · tcgplayer"
    data = (f' data-pts="{esc(json.dumps(points))}"'
            f' data-name="{name}" data-sub="{esc(sub)}"'
            f' data-chart="{esc(_big_svg(points, entry["card_name"]))}"'
            f' data-tail="{esc(_tail_table(points))}"') if points else ""
    return (f'<article class="card" style="animation-delay:{idx * 45}ms"{data}>'
            f'<h3>{name}{badge}</h3>{note}{price}{deltas}{target}{spark}</article>')


def _pager(base, param, page, total, per):
    pages = max(1, -(-total // per))
    if pages == 1:
        return ""
    prev = f'<a href="{base}?{param}={page - 1}">‹ newer</a>' if page > 1 else "<span>‹</span>"
    nxt = f'<a href="{base}?{param}={page + 1}">older ›</a>' if page < pages else "<span>›</span>"
    return f'<nav class="pager">{prev}<span>{page}/{pages}</span>{nxt}</nav>'


def render_page(db, row, editable: bool, hp: int = 1, cp: int = 1) -> str:
    """Full HTML for /w (editable=True, key=passphrase) or /s (share code)."""
    key = row["_key"]  # set by caller: passphrase or share code — NEVER both
    entries = watchlist_db.current_entries(db, row["id"])
    total_ev = db.execute("SELECT COUNT(*) FROM events WHERE list_id=?",
                          (row["id"],)).fetchone()[0]
    events = db.execute(
        "SELECT * FROM events WHERE list_id=? ORDER BY seq DESC LIMIT ? OFFSET ?",
        (row["id"], EVENTS_PER_PAGE, (hp - 1) * EVENTS_PER_PAGE)).fetchall()

    summaries = [(e, watchlist_db.entry_price_summary(db, e)) for e in entries]
    total_val = sum(s["current"] for _, s in summaries if s)
    net7 = sum(s["d7"] for _, s in summaries if s and s["d7"] is not None)
    hits = sum(1 for e, s in summaries
               if s and e["target_price"] is not None and s["current"] <= e["target_price"])
    last_ingest = db.execute(
        "SELECT value FROM meta WHERE key='last_ingest'").fetchone()
    last_ingest = last_ingest["value"] if last_ingest else "never"

    page_cards = entries[(cp - 1) * CARDS_PER_PAGE: cp * CARDS_PER_PAGE]
    cards = "".join(_card_html(db, e, i) for i, e in enumerate(page_cards)) or \
        '<p class="nodata">Nothing watched yet — ask Claude to <code>watchlist_add</code> a card.</p>'

    revs = "".join(
        f'<div class="rev" data-seq="{ev["seq"]}"><span class="n">#{ev["seq"]}</span>'
        f'<span class="a">{esc(ev["action"])}</span>'
        f'<span class="d">{esc(json.loads(ev["payload_json"]).get("card_name") or json.loads(ev["payload_json"]).get("label") or "")}</span>'
        f'<span class="t">{_ago(ev["ts"])}</span></div>'
        for ev in events)

    title = esc(row["label"] or "Watchlist")
    superseded = ('<p class="note">⚠ superseded by a recovery clone — this copy '
                  'is historical.</p>' if row["superseded_by"] else "")
    share_chip = (f'<span class="chip" data-copy="{esc(row["share_code"])}">share '
                  f'{esc(row["share_code"])} ⧉</span>')
    mode_chip = ('<span class="chip">✎ your ledger</span>' if editable
                 else '<span class="chip">👁 read-only</span>')
    recover_btn = ('<button class="act" id="recoverBtn">Restore here (supersede)</button>'
                   if editable else "")
    js = _JS_TMPL % {"key": json.dumps(key), "editable": json.dumps(editable),
                     "cpad": CPAD, "cw": CW, "ch": CH}
    base = f"/w/{esc(key)}" if editable else f"/s/{esc(key)}"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ctext x='8' y='13' font-size='14' text-anchor='middle' fill='%238839ef'%3E✦%3C/text%3E%3C/svg%3E">
<title>{title} · Mystic Forge</title>
<script>document.documentElement.dataset.theme=
  localStorage.getItem('mf-theme')||
  (matchMedia('(prefers-color-scheme: dark)').matches?'macchiato':'latte');</script>
<style>{_CSS}</style></head><body>
<div class="wrap">
<header class="masthead"><h1><span class="rune">✦</span> {title}</h1>
<button id="theme">☾</button></header>
<div class="meta">{mode_chip}{share_chip}
<span>prices as of {esc(last_ingest)}</span><span>· ▼ favorable for buyers</span></div>
{superseded}
<div class="stats">
<div class="stat"><b>${total_val:.2f}</b><span>list total</span></div>
<div class="stat"><b>{"▼" if net7 < 0 else "▲" if net7 > 0 else "·"}${abs(net7):.2f}</b><span>7-day net</span></div>
<div class="stat"><b>{hits}</b><span>at target</span></div>
<div class="stat"><b>{len(entries)}</b><span>cards</span></div>
</div>
<div class="cols">
<section><div class="grid">{cards}</div>
{_pager(base, "cp", cp, len(entries), CARDS_PER_PAGE)}</section>
<aside class="rail"><h2>Ledger</h2>{revs}
{_pager(base, "hp", hp, total_ev, EVENTS_PER_PAGE)}</aside>
</div>
<footer>forged in the Mystic Forge · revision history is append-only ·
<a href="/health">health</a></footer>
</div>
<dialog id="cardDlg"><h3 id="cardTitle"></h3><p class="sub" id="cardSub"></p>
<div class="chart-wrap"><div id="chartHost"></div><div class="tip" id="tip"></div></div>
<div id="snapHost"></div>
<div class="btnrow"><button class="act close">Close</button></div></dialog>
<dialog id="revDlg"><h3 id="revTitle"></h3>
<p class="sub">The list as it stood at this revision.</p>
<div id="revBody"></div><div id="forkOut"></div>
<div class="btnrow"><button class="act primary" id="forkBtn">⑂ Fork this revision</button>
{recover_btn}<button class="act close">Close</button></div></dialog>
<script>{js}</script>
</body></html>"""
