"""Server-rendered watchlist pages — the "arcane ledger".

Catppuccin Latte (day) / Macchiato (night), serif display over mono data.
Two views per list: the main board (cards + sparklines) and a separate
history view (revision chain + fork/restore), Google-Docs style.
Single-series price charts: blue line; status deltas always carry a glyph +
ink-token text (never color alone). All assets inline — no external requests.
"""

import json
import urllib.parse
from datetime import datetime, timezone
from html import escape as esc

import watchlist_db

EVENTS_PER_PAGE = 15
CARDS_PER_PAGE = 24

SHOPS = {"tcgplayer": "$", "cardkingdom": "$", "cardmarket": "€"}

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
  --hitbg:#40a02b14;--hitglow:#40a02b33;
}
[data-theme=macchiato]{
  --base:#24273a;--mantle:#1e2030;--crust:#181926;--surface0:#363a4f;
  --surface1:#494d64;--text:#cad3f5;--sub:#a5adcb;--overlay:#6e738d;
  --blue:#8aadf4;--lavender:#b7bdf8;--mauve:#c6a0f6;--peach:#f5a97f;
  --teal:#8bd5ca;--green:#a6da95;--red:#ed8796;--yellow:#eed49d;
  --card:#1e2030cc;--glow1:#c6a0f61a;--glow2:#f5a97f14;--shadow:#00000055;
  --hitbg:#a6da9518;--hitglow:#a6da9540;
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
header.masthead{display:flex;align-items:center;gap:.7rem;margin-bottom:.4rem}
h1{font-size:clamp(1.6rem,4vw,2.4rem);font-weight:600;letter-spacing:.01em}
h1 .rune{color:var(--mauve)}
.iconbtn{background:none;border:none;color:var(--sub);cursor:pointer;font-size:1.05rem;
  padding:.25rem;border-radius:.4rem;line-height:1}
.iconbtn:hover{background:var(--mantle);color:var(--text)}
#theme{margin-left:auto;font-size:1.2rem}
.meta{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;color:var(--sub);
  font-size:.85rem;margin-bottom:1.4rem}
.chip{font-family:var(--font-data);font-size:.75rem;background:var(--mantle);
  border:1px solid var(--surface0);border-radius:999px;padding:.15rem .6rem;
  color:var(--sub);text-decoration:none;display:inline-block}
button.chip{cursor:pointer}
.chip:hover{border-color:var(--overlay);color:var(--text)}
.shops{display:inline-flex;border:1px solid var(--surface0);border-radius:999px;overflow:hidden}
.shops a{font-family:var(--font-data);font-size:.72rem;padding:.18rem .6rem;
  color:var(--sub);text-decoration:none}
.shops a.on{background:var(--mauve);color:var(--base)}
.shops a:not(.on):hover{background:var(--mantle)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
  gap:.8rem;margin-bottom:1.6rem}
.stat{background:var(--card);border:1px solid var(--surface0);border-radius:.8rem;
  padding:.7rem .9rem;backdrop-filter:blur(6px)}
.stat b{display:block;font-family:var(--font-data);font-size:1.25rem;font-weight:600}
.stat span{font-size:.72rem;color:var(--sub);text-transform:uppercase;letter-spacing:.08em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(15.5rem,1fr));gap:.9rem}
.card{background:var(--card);border:1px solid var(--surface0);border-radius:1rem;
  padding:.9rem 1rem .7rem;cursor:pointer;backdrop-filter:blur(6px);
  box-shadow:0 1px 2px var(--shadow);transition:transform .18s,box-shadow .18s,border-color .18s;
  animation:rise .5s both;position:relative}
.card:hover{transform:translateY(-3px);box-shadow:0 8px 24px var(--shadow);border-color:var(--lavender)}
.card.hit{border-color:var(--green);background:linear-gradient(var(--hitbg),var(--hitbg)),var(--card);
  box-shadow:0 0 0 1px var(--green),0 0 18px var(--hitglow)}
.card.hit:hover{box-shadow:0 0 0 1px var(--green),0 8px 26px var(--hitglow)}
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
  padding:1.1rem 1.2rem;backdrop-filter:blur(6px);max-width:44rem;margin:0 auto}
.rev{display:flex;gap:.55rem;align-items:baseline;padding:.45rem .3rem;border-radius:.5rem;
  cursor:pointer;font-size:.85rem;border-bottom:1px dashed var(--surface0)}
.rev:hover{background:var(--mantle)}
.rev .n{font-family:var(--font-data);color:var(--mauve);min-width:2.6rem}
.rev .a{font-family:var(--font-data);font-size:.7rem;color:var(--sub);
  text-transform:uppercase;min-width:5rem}
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
.sites{display:flex;gap:.45rem;flex-wrap:wrap;margin:.7rem 0 .2rem}
.sites a{font-family:var(--font-data);font-size:.72rem;border:1px solid var(--surface1);
  border-radius:.45rem;padding:.2rem .55rem;color:var(--sub);text-decoration:none}
.sites a:hover{border-color:var(--lavender);color:var(--text)}
.sites a::after{content:" ↗";color:var(--overlay)}
.tgtedit{display:flex;gap:.5rem;align-items:center;margin-top:.7rem;flex-wrap:wrap}
.tgtedit label{font-size:.8rem;color:var(--sub)}
.tgtedit input{font-family:var(--font-data);font-size:.85rem;width:6.5rem;
  background:var(--mantle);color:var(--text);border:1px solid var(--surface1);
  border-radius:.45rem;padding:.3rem .5rem}
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

_JS = """
const KEY=%(key)s, EDITABLE=%(editable)s, CPAD=%(cpad)d, CW=%(cw)d, CH=%(ch)d, CUR=%(cur)s;
const themeBtn=document.getElementById('theme');
function themeGlyph(){themeBtn.textContent=
  document.documentElement.dataset.theme==='latte'?'\\u{1F319}':'\\u2600\\uFE0F';
  themeBtn.title='switch to '+(document.documentElement.dataset.theme==='latte'?'macchiato':'latte');}
themeBtn.onclick=()=>{const h=document.documentElement;
  h.dataset.theme=h.dataset.theme==='latte'?'macchiato':'latte';
  localStorage.setItem('mf-theme',h.dataset.theme);themeGlyph();};
themeGlyph();
document.querySelectorAll('[data-copy]').forEach(c=>c.onclick=e=>{
  e.stopPropagation();navigator.clipboard.writeText(c.dataset.copy);
  if(!c.dataset.orig)c.dataset.orig=c.textContent;
  c.textContent='copied \\u2713';clearTimeout(c._t);
  c._t=setTimeout(()=>{c.textContent=c.dataset.orig},900);
});
document.querySelectorAll('dialog').forEach(d=>
  d.addEventListener('click',e=>{if(e.target===d)d.close()}));
const renameBtn=document.getElementById('rename');
if(renameBtn)renameBtn.onclick=async()=>{
  const label=prompt('Rename this list:',renameBtn.dataset.label||'');
  if(label===null)return;
  const r=await fetch('/api/rename',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:KEY,label})});
  if(r.ok)location.reload();
};
// ── card detail modal ──
const cardDlg=document.getElementById('cardDlg');
if(cardDlg)document.querySelectorAll('.card[data-name]').forEach(card=>{
  card.onclick=()=>{
    document.getElementById('cardTitle').textContent=card.dataset.name;
    document.getElementById('cardSub').textContent=card.dataset.sub;
    document.getElementById('chartHost').innerHTML=card.dataset.chart||'';
    document.getElementById('snapHost').innerHTML=card.dataset.tail||'';
    document.getElementById('siteHost').innerHTML=card.dataset.sites||'';
    const te=document.getElementById('tgtEdit');
    if(te){te.dataset.entry=card.dataset.entry;
      document.getElementById('tgtInput').value=card.dataset.target||'';}
    const pts=card.dataset.pts?JSON.parse(card.dataset.pts):[];
    if(pts.length)armCrosshair(pts);
    cardDlg.showModal();
  };
});
function armCrosshair(pts){
  const svg=document.querySelector('#chartHost svg');if(!svg)return;
  const tip=document.getElementById('tip');
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
    tip.textContent=pts[i][0]+' \\u00b7 '+CUR+pts[i][1].toFixed(2);
  };
  svg.onmouseleave=()=>{tip.style.display='none';
    vline.setAttribute('x1',-9);vline.setAttribute('x2',-9);dot.setAttribute('cx',-9)};
}
const tgtSave=document.getElementById('tgtSave');
if(tgtSave)tgtSave.onclick=async()=>{
  const v=document.getElementById('tgtInput').value.trim();
  const r=await fetch('/api/target',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:KEY,entry_id:+document.getElementById('tgtEdit').dataset.entry,
                         target_price:v===''?null:+v})});
  if(r.ok)location.reload();else alert('Could not save target');
};
// ── revision modal (history view) ──
const revDlg=document.getElementById('revDlg');
if(revDlg){
  document.querySelectorAll('.rev').forEach(r=>r.onclick=async()=>{
    const seq=r.dataset.seq;
    document.getElementById('revTitle').textContent='Revision #'+seq;
    document.getElementById('revBody').innerHTML='<p class=sub>consulting the ledger\\u2026</p>';
    document.getElementById('forkOut').innerHTML='';
    revDlg.showModal();
    const res=await fetch('/api/revision/'+encodeURIComponent(KEY)+'/'+seq);
    if(!res.ok){document.getElementById('revBody').textContent='Could not read revision.';return}
    const d=await res.json();
    let h='<table class=snap><tr><th>Card</th><th>Printing</th><th>Target</th><th>Note</th></tr>';
    if(!d.entries.length)h+='<tr><td colspan=4><i>empty at this revision</i></td></tr>';
    for(const e of d.entries){
      h+=`<tr><td>${e.card_name}</td><td class=num>${e.set_code?e.set_code+' #'+e.collector_number:'cheapest'}</td>`+
         `<td class=num>${e.target_price!=null?'$'+e.target_price.toFixed(2):'\\u2014'}</td><td>${e.note||''}</td></tr>`;
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
    out.innerHTML=`<div class=secret>\\u26a0 shown once \\u2014 passphrase: <b>${d.passphrase}</b><br>`+
      `page: <a href="${d.page}">${d.page}</a> \\u00b7 share: ${d.share_code}</div>`+
      (mode==='recover'?'<p class=sub>The current list is now marked superseded.</p>':'');
  }
  document.getElementById('forkBtn').onclick=e=>doFork('fork',e.target.dataset.seq);
  const recBtn=document.getElementById('recoverBtn');
  if(recBtn)recBtn.onclick=e=>doFork('recover',e.target.dataset.seq);
}
document.querySelectorAll('dialog .close').forEach(b=>b.onclick=()=>b.closest('dialog').close());
"""


def _pts(db, entry, shop):
    s = watchlist_db.entry_price_summary(db, entry, provider=shop)
    if not s:
        return None, []
    series = watchlist_db.price_series(
        db, watchlist_db.uuids_for_entry(db, entry), days=90, provider=shop,
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


def _big_svg(points, name, cur):
    if len(points) < 2:
        return "<p class=nodata>Not enough history yet.</p>"
    xy, lo, hi = _coords(points, CW, CH, CPAD)
    pl = " ".join(f"{x},{y}" for x, y in xy)
    grid = "".join(f'<line class="gridline" x1="{CPAD}" y1="{y}" x2="{CW - CPAD}" y2="{y}"/>'
                   for y in (CPAD, CH / 2, CH - CPAD))
    return (
        f'<svg viewBox="0 0 {CW} {CH}" style="width:100%;height:auto" role="img">'
        f'<title>{esc(name)} — 90 day price history</title>{grid}'
        f'<text class="axis" x="{CPAD}" y="{CPAD - 6}">{cur}{hi:.2f}</text>'
        f'<text class="axis" x="{CPAD}" y="{CH - CPAD + 14}">{cur}{lo:.2f}</text>'
        f'<text class="axis" x="{CW - CPAD}" y="{CPAD - 6}" text-anchor="end">now {cur}{points[-1][1]:.2f}</text>'
        f'<text class="axis" x="{CPAD}" y="{CH - 6}">{esc(points[0][0])}</text>'
        f'<text class="axis" x="{CW - CPAD}" y="{CH - 6}" text-anchor="end">{esc(points[-1][0])}</text>'
        f'<polyline points="{pl}" fill="none" stroke="var(--blue)" stroke-width="2" stroke-linejoin="round"/>'
        f'</svg>')


def _delta(v, label, cur):
    if v is None:
        return f'<span><span class="lbl">{label}</span> <span class="fl">·—</span></span>'
    cls, glyph = ("dn", "▼") if v < 0 else ("up", "▲") if v > 0 else ("fl", "·")
    return (f'<span title="change vs {label} ago"><span class="lbl">{label}</span> '
            f'<span class="{cls}">{glyph}</span>{cur}{abs(v):.2f}</span>')


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


def _tail_table(points, cur):
    rows = "".join(
        f'<tr><td class="num">{esc(d)}</td><td class="num">{cur}{v:.2f}</td></tr>'
        for d, v in points[-10:][::-1])
    return ('<table class="snap"><tr><th>Date</th><th>Price</th></tr>'
            + rows + "</table>") if rows else ""


def _site_links(entry) -> str:
    """External hop-out badges. Pinned printings deep-link where possible."""
    name = entry["card_name"]
    q = urllib.parse.quote(name)
    slug = "".join(ch for ch in name.lower().replace(" ", "-") if ch.isalnum() or ch == "-")
    if entry.get("set_code") and entry.get("collector_number"):
        scry = (f"https://scryfall.com/card/{entry['set_code'].lower()}/"
                f"{urllib.parse.quote(entry['collector_number'])}")
    else:
        scry = f"https://scryfall.com/search?q={urllib.parse.quote(f'!\"{name}\"')}"
    links = [
        ("Scryfall", scry),
        ("EDHREC", f"https://edhrec.com/cards/{slug}"),
        ("MTGStocks", f"https://www.mtgstocks.com/search?query={q}"),
        ("TCGplayer", f"https://www.tcgplayer.com/search/magic/product?q={q}"),
    ]
    return '<div class="sites">' + "".join(
        f'<a href="{esc(u)}" target="_blank" rel="noopener">{n}</a>'
        for n, u in links) + "</div>"


def _card_html(db, entry, idx, shop, cur):
    s, points = _pts(db, entry, shop)
    name = esc(entry["card_name"])
    badge = (f'<span class="badge">{esc(entry["set_code"])} '
             f'#{esc(entry["collector_number"] or "")}</span>'
             if entry.get("set_code") else "")
    note = f'<p class="note">{esc(entry["note"] or "")}</p>'
    hit = bool(s and entry.get("target_price") is not None
               and s["current"] <= entry["target_price"])
    if s:
        foil = ' <small>(foil)</small>' if s.get("finish") == "foil" else ""
        price = f'<div class="price">{cur}{s["current"]:.2f}{foil}</div>'
        deltas = (f'<div class="deltas">{_delta(s["d7"], "7d", cur)}'
                  f'{_delta(s["d30"], "30d", cur)}</div>')
    else:
        price = '<div class="nodata">awaiting first ingest…</div>'
        deltas = ""
    target = ""
    if entry.get("target_price") is not None:
        if hit:
            target = (f'<div class="target hit">🎯 at target '
                      f'{cur}{entry["target_price"]:.2f} — buy window</div>')
        else:
            gap = f' · {cur}{s["current"] - entry["target_price"]:.2f} above' if s else ""
            target = f'<div class="target">target {cur}{entry["target_price"]:.2f}{gap}</div>'
    spark = _spark_svg(points, entry["card_name"]) if points else ""
    sub = (f'{entry["set_code"]} #{entry["collector_number"]}'
           if entry.get("set_code") else "cheapest printing") + f" · {shop}"
    data = (f' data-name="{name}" data-sub="{esc(sub)}"'
            f' data-entry="{entry["entry_id"]}"'
            f' data-target="{entry["target_price"] if entry.get("target_price") is not None else ""}"'
            f' data-sites="{esc(_site_links(entry))}"'
            f' data-pts="{esc(json.dumps(points))}"'
            f' data-chart="{esc(_big_svg(points, entry["card_name"], cur))}"'
            f' data-tail="{esc(_tail_table(points, cur))}"')
    return (f'<article class="card{" hit" if hit else ""}" '
            f'style="animation-delay:{idx * 45}ms"{data}>'
            f'<h3>{name}{badge}</h3>{note}{price}{deltas}{target}{spark}</article>')


def _pager(base, param, page, total, per, keep=""):
    pages = max(1, -(-total // per))
    if pages == 1:
        return ""
    prev = (f'<a href="{base}?{param}={page - 1}{keep}">‹ newer</a>'
            if page > 1 else "<span>‹</span>")
    nxt = (f'<a href="{base}?{param}={page + 1}{keep}">older ›</a>'
           if page < pages else "<span>›</span>")
    return f'<nav class="pager">{prev}<span>{page}/{pages}</span>{nxt}</nav>'


def _shell(row, editable, body, dialogs, shop="tcgplayer"):
    key = row["_key"]
    title = esc(row["label"] or "Watchlist")
    cur = SHOPS.get(shop, "$")
    js = _JS % {"key": json.dumps(key), "editable": json.dumps(editable),
                "cpad": CPAD, "cw": CW, "ch": CH, "cur": json.dumps(cur)}
    rename = (f'<button class="iconbtn" id="rename" title="Rename list" '
              f'data-label="{esc(row["label"] or "")}">✎</button>' if editable else "")
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
<header class="masthead"><h1><span class="rune">✦</span> {title}</h1>{rename}
<button class="iconbtn" id="theme"></button></header>
{body}
<footer>forged in the Mystic Forge · <a href="/health">health</a></footer>
</div>
{dialogs}
<script>{js}</script>
</body></html>"""


def render_main(db, row, editable: bool, cp: int = 1, shop: str = "tcgplayer") -> str:
    """The board: stat tiles + card grid. History lives in its own view."""
    key = row["_key"]
    shop = shop if shop in SHOPS else "tcgplayer"
    cur = SHOPS[shop]
    base = f"/w/{esc(key)}" if editable else f"/s/{esc(key)}"
    entries = watchlist_db.current_entries(db, row["id"])
    summaries = [(e, watchlist_db.entry_price_summary(db, e, provider=shop))
                 for e in entries]
    total_val = sum(s["current"] for _, s in summaries if s)
    net7 = sum(s["d7"] for _, s in summaries if s and s["d7"] is not None)
    hits = sum(1 for e, s in summaries
               if s and e["target_price"] is not None and s["current"] <= e["target_price"])
    last = db.execute("SELECT value FROM meta WHERE key='last_ingest'").fetchone()
    last = last["value"] if last else "never"

    page_cards = entries[(cp - 1) * CARDS_PER_PAGE: cp * CARDS_PER_PAGE]
    cards = "".join(_card_html(db, e, i, shop, cur)
                    for i, e in enumerate(page_cards)) or \
        '<p class="nodata">Nothing watched yet — ask Claude to <code>watchlist_add</code> a card.</p>'

    shop_links = "".join(
        f'<a href="{base}?shop={s}" class="{"on" if s == shop else ""}">{s}</a>'
        for s in SHOPS)
    share = (f'<button class="chip" data-copy="{esc(row["share_code"])}" '
             f'title="Copy the read-only share code">share {esc(row["share_code"])} ⧉</button>')
    ro = '' if editable else '<span class="chip">read-only</span>'
    superseded = ('<p class="note">⚠ superseded by a recovery clone — this copy '
                  'is historical.</p>' if row["superseded_by"] else "")

    tgt_edit = ""
    if editable:
        tgt_edit = ('<div class="tgtedit" id="tgtEdit"><label for="tgtInput">'
                    'target price</label><input id="tgtInput" type="number" '
                    'step="0.01" min="0" placeholder="none">'
                    '<button class="act" id="tgtSave">Save</button></div>')
    dialogs = (f'<dialog id="cardDlg"><h3 id="cardTitle"></h3><p class="sub" id="cardSub"></p>'
               f'<div class="chart-wrap"><div id="chartHost"></div><div class="tip" id="tip"></div></div>'
               f'<div id="siteHost"></div>{tgt_edit}<div id="snapHost"></div>'
               f'<div class="btnrow"><button class="act close">Close</button></div></dialog>')

    body = f"""
<div class="meta">{ro}{share}
<a class="chip" href="{base}/history" title="Every change ever made, and time travel">⟲ history</a>
<span class="shops">{shop_links}</span>
<span title="A falling price (▼) means it's getting cheaper to buy">▼ = cheaper</span>
<span>prices as of {esc(last)}</span></div>
{superseded}
<div class="stats">
<div class="stat"><b>{cur}{total_val:.2f}</b><span>list total</span></div>
<div class="stat"><b>{"▼" if net7 < 0 else "▲" if net7 > 0 else "·"}{cur}{abs(net7):.2f}</b><span>7-day net</span></div>
<div class="stat"><b>{hits}</b><span>at target</span></div>
<div class="stat"><b>{len(entries)}</b><span>cards</span></div>
</div>
<div class="grid">{cards}</div>
{_pager(base, "cp", cp, len(entries), CARDS_PER_PAGE, keep=f"&shop={shop}")}"""
    return _shell(row, editable, body, dialogs, shop)


def render_history(db, row, editable: bool, hp: int = 1) -> str:
    """The stashed-away revision view: full chain, revision modal, fork/restore."""
    key = row["_key"]
    base = f"/w/{esc(key)}" if editable else f"/s/{esc(key)}"
    total_ev = db.execute("SELECT COUNT(*) FROM events WHERE list_id=?",
                          (row["id"],)).fetchone()[0]
    events = db.execute(
        "SELECT * FROM events WHERE list_id=? ORDER BY seq DESC LIMIT ? OFFSET ?",
        (row["id"], EVENTS_PER_PAGE, (hp - 1) * EVENTS_PER_PAGE)).fetchall()
    # set_target/set_note/remove payloads carry only entry_id — resolve the
    # card name through the add event that minted that entry (entry_id == seq).
    adds = {ev["seq"]: json.loads(ev["payload_json"]).get("card_name", "")
            for ev in db.execute(
                "SELECT seq, payload_json FROM events WHERE list_id=?"
                " AND action='add'", (row["id"],))}

    def _detail(ev, payload):
        d = payload.get("card_name") or payload.get("label") or ""
        if not d and payload.get("entry_id") in adds:
            d = adds[payload["entry_id"]]
            if ev["action"] == "set_target":
                tp = payload.get("target_price")
                d += " → no target" if tp is None else f" → ${tp:.2f}"
        return d

    revs = "".join(
        f'<div class="rev" data-seq="{ev["seq"]}"><span class="n">#{ev["seq"]}</span>'
        f'<span class="a">{esc(ev["action"].replace("_", " "))}</span>'
        f'<span class="d">{esc(_detail(ev, json.loads(ev["payload_json"])))}</span>'
        f'<span class="t">{_ago(ev["ts"])}</span></div>'
        for ev in events)
    recover_btn = ('<button class="act" id="recoverBtn">Restore here (supersede)</button>'
                   if editable else "")
    dialogs = (f'<dialog id="revDlg"><h3 id="revTitle"></h3>'
               f'<p class="sub">The list as it stood at this revision.</p>'
               f'<div id="revBody"></div><div id="forkOut"></div>'
               f'<div class="btnrow"><button class="act primary" id="forkBtn">⑂ Fork this revision</button>'
               f'{recover_btn}<button class="act close">Close</button></dialog>')
    body = f"""
<div class="meta"><a class="chip" href="{base}">← back to the board</a>
<span>{total_ev} revisions · append-only · click one to inspect or time-travel</span></div>
<div class="rail">{revs}
{_pager(base + "/history", "hp", hp, total_ev, EVENTS_PER_PAGE)}</div>"""
    return _shell(row, editable, body, dialogs)
