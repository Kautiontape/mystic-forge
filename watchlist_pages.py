"""Server-rendered watchlist pages — the "arcane ledger".

Catppuccin Latte (day) / Macchiato (night), serif display over mono data.
Two views per list: the main board (cards + sparklines + verdict) and a
separate history view (revision chain + fork/restore), Google-Docs style.

Semantics that keep the numbers honest (persona-review driven):
- Unpinned cards chart the min-across-printings envelope (watchlist_db).
- Targets are USD/tcgplayer-basis: hit state is computed against tcgplayer
  regardless of the display shop, so "at target" never changes meaning.
- The freshness line shows the newest PRICE date, not the ingest date.
All assets inline — no external requests. Revision-modal content is escaped
client-side (X()) because it round-trips through innerHTML.
"""

import json
import urllib.parse
from datetime import datetime, timezone
from html import escape as esc

import watchlist_db
import watchlist_ingest

EVENTS_PER_PAGE = 15
CARDS_PER_PAGE = 24

SHOPS = {"tcgplayer": "$", "cardkingdom": "$", "cardmarket": "€"}

# Public path prefix stripped by the gateway (set from PUBLIC_BASE by server.py).
# Every emitted link and fetch must include it; empty when served at the root.
PREFIX = ""

# Chart geometry shared with the inline JS crosshair (keep in sync there).
CW, CH, CPAD = 640, 220, 34
SW, SH = 240, 56

_CSS = """
:root{
  --font-display:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --font-ui:"Avenir Next","Seravek","Segoe UI",system-ui,Cantarell,sans-serif;
  --font-data:ui-monospace,"Cascadia Code","JetBrains Mono","Fira Code",Menlo,monospace;
}
@view-transition{navigation:auto}   /* crossfade instead of flash on navigation */
[data-theme=latte]{
  --base:#eff1f5;--mantle:#e6e9ef;--crust:#dce0e8;--surface0:#ccd0da;
  --surface1:#bcc0cc;--text:#4c4f69;--sub:#6c6f85;--overlay:#9ca0b0;
  --blue:#1e66f5;--lavender:#7287fd;--mauve:#8839ef;--peach:#fe640b;
  --teal:#179299;--green:#40a02b;--red:#d20f39;--yellow:#df8e1d;
  --card:#ffffffcc;--glow1:#8839ef14;--glow2:#fe640b12;--shadow:#4c4f6922;
  --hitbg:#40a02b14;--hitglow:#40a02b33;
  --green-text:#317717;--yellow-text:#8f6400;--peach-text:#b34605;
}
[data-theme=macchiato]{
  --base:#24273a;--mantle:#1e2030;--crust:#181926;--surface0:#363a4f;
  --surface1:#494d64;--text:#cad3f5;--sub:#a5adcb;--overlay:#6e738d;
  --blue:#8aadf4;--lavender:#b7bdf8;--mauve:#c6a0f6;--peach:#f5a97f;
  --teal:#8bd5ca;--green:#a6da95;--red:#ed8796;--yellow:#eed49d;
  --card:#1e2030cc;--glow1:#c6a0f61a;--glow2:#f5a97f14;--shadow:#00000055;
  --hitbg:#a6da9518;--hitglow:#a6da9540;
  --green-text:var(--green);--yellow-text:var(--yellow);--peach-text:var(--peach);
}
*{box-sizing:border-box;margin:0}
html{background:var(--base)}   /* real navigations never flash white */
body{
  font-family:var(--font-ui);color:var(--text);background:var(--base);
  background-image:radial-gradient(60rem 40rem at 85% -10%,var(--glow1),transparent 60%),
                   radial-gradient(50rem 34rem at -10% 100%,var(--glow2),transparent 55%);
  background-attachment:fixed;min-height:100vh;padding-bottom:4rem;
  transition:background-color .3s,color .3s;
}
body::before{content:"";position:fixed;inset:0 0 auto 0;height:3px;z-index:5;
  background:linear-gradient(90deg,var(--mauve),var(--peach),var(--teal))}
.wrap{max-width:72rem;margin:0 auto;padding:2.2rem 1.2rem 0}
header.masthead{display:block;margin-bottom:.1rem}
h1{font-family:var(--font-display);font-size:clamp(1.6rem,4vw,2.4rem);
  font-weight:600;letter-spacing:.01em;line-height:1.08;text-wrap:balance}
h1.long{font-size:clamp(1.3rem,3.4vw,1.9rem)}   /* long titles step down */
h1 .rune{color:var(--mauve)}
h1 .iconbtn{font-size:1.05rem;vertical-align:.35rem;margin-left:.2rem}
.iconbtn{background:none;border:none;color:var(--sub);cursor:pointer;font-size:1.05rem;
  padding:.25rem;border-radius:.4rem;line-height:1}
.iconbtn:hover{background:var(--mantle);color:var(--text)}
.mright{float:right;display:flex;gap:.2rem;align-items:center;
  margin:.35rem 0 .35rem .8rem}   /* quiet top-right cluster; title flows around */
#theme{font-size:1.2rem}
.subtitle{color:var(--sub);font-size:.85rem;margin:.35rem 0 1.2rem}
.actions{display:flex;align-items:center;gap:.9rem;flex-wrap:wrap;margin-bottom:1.3rem}
.mla{margin-left:auto}
.textlink{font-family:var(--font-ui);font-size:.85rem;background:none;border:none;
  color:var(--sub);cursor:pointer;text-decoration:none;padding:.25rem .4rem;
  border-radius:.4rem;display:inline-flex;align-items:center;gap:.25rem}
.textlink:hover{color:var(--text);text-decoration:underline}
.chip{font-family:var(--font-data);font-size:.78rem;background:var(--mantle);
  border:1px solid var(--surface0);border-radius:.45rem;padding:.2rem .55rem;
  color:var(--text);text-decoration:none;display:inline-flex;align-items:center;gap:.25rem}
button.chip{cursor:pointer}
button.chip:hover{border-color:var(--overlay)}
.shopgrp{display:inline-flex;align-items:center;gap:.4rem}
.shoplbl{font-size:.8rem;color:var(--sub)}
.pagehead{font-size:1.1rem;letter-spacing:.05em;text-transform:uppercase;
  color:var(--sub);text-align:center;margin-bottom:.6rem}
.sortbar{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;
  margin:-.5rem 0 .9rem}
.shops{display:inline-flex;gap:.1rem}           /* view tabs, not buttons */
.shops a{font-family:var(--font-ui);font-size:.82rem;padding:.3rem .55rem;
  color:var(--sub);text-decoration:none;border-bottom:2px solid transparent}
.shops a.on{color:var(--text);border-bottom-color:var(--mauve);font-weight:600}
.shops a:not(.on):hover{color:var(--text)}
.verdict{font-family:var(--font-data);font-size:.9rem;background:var(--card);
  border:none;border-left:3px solid var(--surface1);
  border-radius:.5rem;padding:.65rem .95rem;margin-bottom:1rem;
  backdrop-filter:blur(6px);line-height:1.7}
.verdict--buy{border-left-color:var(--green)}
.verdict--wait{border-left-color:var(--yellow)}
.verdict .buy{color:var(--green-text);font-weight:600}
.verdict .wait{color:var(--yellow-text);font-weight:600}
.verdict .quiet{color:var(--sub)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
  gap:.8rem;margin-bottom:1.6rem}
.stat{background:var(--card);border:none;border-radius:.7rem;
  padding:.65rem .85rem;backdrop-filter:blur(6px)}
.stat b{display:block;font-family:var(--font-data);font-size:1.4rem;font-weight:600}
.stat span{font-size:.72rem;color:var(--sub);text-transform:uppercase;letter-spacing:.09em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(15.5rem,1fr));gap:.9rem}
.card{background:var(--card);border:1px solid var(--surface0);border-radius:1rem;
  padding:.9rem 1rem .7rem;cursor:pointer;backdrop-filter:blur(6px);
  box-shadow:0 1px 2px var(--shadow);transition:transform .18s,box-shadow .18s,border-color .18s;
  animation:rise .5s both;position:relative}
.card:hover{transform:translateY(-3px);box-shadow:0 8px 24px var(--shadow);border-color:var(--lavender)}
.card:focus-visible,.rev:focus-visible,.chip:focus-visible,.iconbtn:focus-visible{
  outline:2px solid var(--lavender);outline-offset:2px}
.card.hit{border-color:var(--green);background:linear-gradient(var(--hitbg),var(--hitbg)),var(--card);
  box-shadow:0 0 0 1px var(--green),0 0 18px var(--hitglow)}
.card.hit:hover{box-shadow:0 0 0 1px var(--green),0 8px 26px var(--hitglow)}
.card.bought{opacity:.72;filter:saturate(.45);border-color:var(--surface1)}
.card.bought .price{color:var(--sub)}
.boughtnote{font-size:.75rem;color:var(--lavender);font-family:var(--font-data)}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
.card h3{font-family:var(--font-display);font-size:1.05rem;font-weight:600;line-height:1.25}
dialog h3{font-family:var(--font-display)}
.badge{font-family:var(--font-data);font-size:.7rem;color:var(--sub);
  border:1px solid var(--surface1);border-radius:.35rem;padding:.05rem .35rem;
  vertical-align:2px;margin-left:.35rem;white-space:nowrap}
.note{font-family:var(--font-display);font-style:italic;color:var(--sub);
  font-size:.78rem;margin:.15rem 0 .4rem;min-height:1em}
.price{font-family:var(--font-data);font-size:1.5rem;font-weight:600;letter-spacing:-.01em}
.price small{font-size:.7rem;color:var(--peach-text);font-weight:400}
.deltas{display:flex;gap:.7rem;font-family:var(--font-data);font-size:.75rem;
  color:var(--text);margin:.15rem 0 .35rem;flex-wrap:wrap}
.deltas .lbl{color:var(--sub)}
.deltas .pct{color:var(--sub)}
.dn{color:var(--green-text)}.up{color:var(--red)}.fl{color:var(--sub)}
.target{font-size:.75rem;color:var(--sub);font-family:var(--font-data)}
.target.hit{color:var(--green-text);font-weight:600}
.spark{width:100%;height:auto;display:block;margin-top:.45rem}
.spark polyline{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.1s .15s forwards ease-out}
@keyframes draw{to{stroke-dashoffset:0}}
.nodata{color:var(--sub);font-size:.78rem;font-style:italic;margin-top:.6rem}
.skel{background:linear-gradient(90deg,transparent,var(--surface0),transparent)
  0 0/200% 100%;animation:shimmer 1.6s linear infinite;border-radius:.3rem;
  padding:.15rem .4rem;display:inline-block}
@keyframes shimmer{to{background-position:-200% 0}}
.rail{background:var(--card);border:1px solid var(--surface0);border-radius:1rem;
  padding:1.1rem 1.2rem;backdrop-filter:blur(6px);max-width:44rem;margin:0 auto}
.rev{display:flex;gap:.55rem;align-items:baseline;padding:.45rem .3rem;border-radius:.5rem;
  cursor:pointer;font-size:.85rem;border-bottom:1px dashed var(--surface0)}
.rev:hover{background:var(--mantle)}
.rev .n{font-family:var(--font-data);color:var(--mauve);min-width:2.6rem}
.rev .a{font-family:var(--font-data);font-size:.72rem;color:var(--sub);min-width:5.4rem}
.rev .dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;margin-right:.3rem}
.rev .d{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rev .t{color:var(--sub);font-size:.72rem;white-space:nowrap}
.pager{display:flex;justify-content:center;gap:.8rem;margin-top:.7rem;
  font-family:var(--font-data);font-size:.8rem}
.pager a{color:var(--blue);text-decoration:none;padding:.3rem .5rem}
.pager span{color:var(--sub)}
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
.sites a::after{content:" ↗";color:var(--sub)}
.tgtedit{display:flex;gap:.5rem;align-items:center;margin-top:.7rem;flex-wrap:wrap}
.tgtedit label{font-size:.8rem;color:var(--sub)}
.tgtedit input,#renameInput{font-family:var(--font-data);font-size:.85rem;
  background:var(--mantle);color:var(--text);border:1px solid var(--surface1);
  border-radius:.45rem;padding:.3rem .5rem}
.tgtedit input{width:6.5rem}
#renameInput{width:100%;margin:.5rem 0}
.err{color:var(--red);font-family:var(--font-data);font-size:.75rem}
table.snap{width:100%;border-collapse:collapse;font-size:.84rem;margin:.5rem 0}
table.snap th{text-align:left;color:var(--sub);font-size:.72rem;text-transform:uppercase;
  letter-spacing:.06em;padding:.3rem .5rem;border-bottom:1px solid var(--surface1)}
table.snap td{padding:.32rem .5rem;border-bottom:1px solid var(--surface0)}
table.snap td.num{font-family:var(--font-data)}
.btnrow{display:flex;gap:.6rem;margin-top:.9rem;flex-wrap:wrap}
button.act{font-family:inherit;font-size:.88rem;border-radius:.6rem;cursor:pointer;
  padding:.45rem 1rem;border:1px solid var(--surface1);background:var(--mantle);color:var(--text)}
button.act.primary{background:var(--mauve);border-color:var(--mauve);color:var(--base)}
button.act.danger{color:var(--red);border-color:var(--red)}
button.act:hover{filter:brightness(1.08)}
.xclose{position:absolute;top:.55rem;right:.75rem;background:none;border:none;
  color:var(--sub);font-size:1.15rem;cursor:pointer;line-height:1;padding:.2rem .4rem;
  border-radius:.4rem}
.xclose:hover{background:var(--mantle);color:var(--text)}
dialog{position:relative}
.modalend{display:flex;gap:.6rem;margin-top:.9rem;justify-content:flex-start;
  align-items:center;flex-wrap:wrap;position:sticky;bottom:-1.3rem;
  background:var(--base);padding:.6rem 0;z-index:2}
.modalend #removeBtn{margin-left:auto}
#addInput{font-family:var(--font-data);font-size:.85rem;width:100%;margin:.5rem 0;
  background:var(--mantle);color:var(--text);border:1px solid var(--surface1);
  border-radius:.45rem;padding:.35rem .5rem}
.secret{font-family:var(--font-data);background:var(--mantle);border:1px dashed var(--peach);
  border-radius:.5rem;padding:.5rem .7rem;margin:.5rem 0;word-break:break-all}
footer{margin-top:2.5rem;text-align:center;color:var(--sub);font-size:.75rem}
footer a{color:var(--sub)}
.axis{font-family:var(--font-data);font-size:11px;fill:var(--sub)}
.gridline{stroke:var(--surface0);stroke-width:1}
.targetline{stroke:var(--peach);stroke-width:1.5;stroke-dasharray:5 4}
@media(max-width:40rem){
  .wrap{padding:1.3rem .9rem 0}
  h1{font-size:1.6rem}                         /* full width; pencil rides inside */
  .iconbtn{font-size:1.35rem;padding:.45rem .6rem}
  .mright{float:none;justify-content:flex-end;margin:0 0 .3rem}
  .actions{gap:.6rem}
  .shopgrp{width:100%;margin-left:0}
  .shops{display:flex;flex:1}
  .shops a{flex:1;text-align:center;justify-content:center}
  /* 44px-rule tap targets: height, not font inflation */
  .chip,.shops a,.pager a,.textlink{min-height:2.75rem;display:inline-flex;align-items:center}
  .shops a{display:flex}
  button.act{min-height:2.75rem;padding:.65rem 1.1rem}
  .xclose{padding:.6rem .8rem}
  .rev{padding:.75rem .3rem;flex-wrap:wrap}
  .rev .d{flex:1 1 100%;white-space:normal;order:9;padding-left:1.15rem}
  .pager a{padding:.6rem .8rem}
  .stats{grid-template-columns:repeat(2,1fr);gap:.6rem}
  .stat{padding:.55rem .7rem}
  .stat b{font-size:1.1rem}
  #addInput,#renameInput,.tgtedit input{font-size:1rem}  /* no iOS zoom-on-focus */
  .tgtedit input{width:8rem;padding:.5rem .6rem}
  .axis{font-size:20px}                        /* ≈10px rendered at phone width */
  .badge,.deltas,.rev .a,.sites a,.tip,.boughtnote{font-size:.8rem}
  dialog{width:100vw;max-width:100vw;margin:auto 0 0;border-radius:1rem 1rem 0 0;
    max-height:92dvh;padding:1rem}
  .card::after{content:"›";position:absolute;top:.8rem;right:.9rem;
    color:var(--sub);font-size:1.1rem}
}
"""

_JS = r"""
const KEY=%(key)s, EDITABLE=%(editable)s, CPAD=%(cpad)d, CW=%(cw)d, CH=%(ch)d, CUR=%(cur)s;
const P=%(prefix)s;                       // gateway path prefix, e.g. '/mtg'
const U=path=>P+path;                     // build a browser-correct URL
const X=s=>String(s??'').replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// ── flashless navigation: every internal link and mutation morphs in place ──
async function morphNavigate(url,push=true){
  try{
    const r=await fetch(url);
    if(!r.ok)throw 0;
    const doc=new DOMParser().parseFromString(await r.text(),'text/html');
    const nw=doc.querySelector('.wrap');
    if(!nw)throw 0;
    const apply=()=>{
      document.querySelector('.wrap').replaceWith(nw);
      document.querySelectorAll('body > dialog').forEach(d=>d.remove());
      doc.querySelectorAll('body > dialog').forEach(d=>document.body.append(d));
      document.title=doc.title;
      wire();wireDialogs();
    };
    if(document.startViewTransition)document.startViewTransition(apply);
    else apply();
    if(push&&url!==location.href)history.pushState({},'',url);
  }catch(e){location.href=url}
}
const refresh=()=>morphNavigate(location.href,false);
window.onpopstate=()=>morphNavigate(location.href,false);
const copyable=c=>c.onclick=e=>{
  e.stopPropagation();
  navigator.clipboard.writeText(c.dataset.copy.startsWith('/')
    ?location.origin+c.dataset.copy:c.dataset.copy);
  if(!c.style.minWidth){c.style.minWidth=c.getBoundingClientRect().width+'px';
    c.style.textAlign='center'}
  if(!c.dataset.orig)c.dataset.orig=c.textContent;
  c.textContent='copied \u2713';clearTimeout(c._t);
  c._t=setTimeout(()=>{c.textContent=c.dataset.orig},900);
};
const enterClicks=(inputId,btnId)=>{const i=document.getElementById(inputId);
  if(i)i.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();
    document.getElementById(btnId).click()}}};
const keyable=el=>{el.setAttribute('tabindex','0');el.setAttribute('role','button');
  el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();el.click()}}};
const themeGlyph=()=>{const b=document.getElementById('theme');if(!b)return;
  b.textContent=document.documentElement.dataset.theme==='latte'?'\u{1F319}':'\u2600\uFE0F';
  b.title='switch to '+(document.documentElement.dataset.theme==='latte'?'macchiato':'latte');};
let pending=null;
// ── wire(): bindings inside .wrap — rerun after every morph ──
function wire(){
  const themeBtn=document.getElementById('theme');
  if(themeBtn)themeBtn.onclick=()=>{const h=document.documentElement;
    h.dataset.theme=h.dataset.theme==='latte'?'macchiato':'latte';
    localStorage.setItem('mf-theme',h.dataset.theme);
    document.querySelector('meta[name=theme-color]').content=
      h.dataset.theme==='macchiato'?'#24273a':'#eff1f5';
    themeGlyph();};
  themeGlyph();
  document.querySelectorAll('[data-copy]').forEach(copyable);
  // internal links (shop tabs, pager, history/back chips) morph, never navigate
  document.querySelectorAll('.wrap a[href^="/"]').forEach(a=>
    a.onclick=e=>{e.preventDefault();morphNavigate(a.getAttribute('href'))});
  const renameBtn=document.getElementById('rename');
  if(renameBtn)renameBtn.onclick=()=>{
    document.getElementById('renameInput').value=renameBtn.dataset.label||'';
    document.getElementById('renameErr').textContent='';
    document.getElementById('renameDlg').showModal();};
  const claimBtn=document.getElementById('claim');
  if(claimBtn)claimBtn.onclick=claimFlow;
  const addBtn=document.getElementById('addCard');
  if(addBtn)addBtn.onclick=()=>{document.getElementById('addPreview').innerHTML='';
    document.getElementById('addErr').textContent='';
    document.getElementById('addGo').style.display='none';
    document.getElementById('addInput').value='';
    document.getElementById('addDlg').showModal();
    document.getElementById('addInput').focus();};
  const alertsBtn=document.getElementById('alerts');
  if(alertsBtn)alertsBtn.onclick=()=>document.getElementById('alertsDlg').showModal();
  if(document.getElementById('cardDlg'))
    document.querySelectorAll('.card[data-name]').forEach(wireCard);
  if(document.getElementById('revDlg'))
    document.querySelectorAll('.rev').forEach(wireRev);
}
// ── wireDialogs(): bindings inside <dialog>s — rerun when dialogs are swapped ──
function wireDialogs(){
  document.querySelectorAll('dialog').forEach(d=>
    d.onclick=e=>{if(e.target===d)d.close()});
  document.querySelectorAll('dialog .xclose,dialog .close').forEach(b=>
    b.onclick=()=>b.closest('dialog').close());
  const renameSave=document.getElementById('renameSave');
  if(renameSave){renameSave.onclick=async()=>{
    const r=await fetch(U('/api/rename'),{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:KEY,label:document.getElementById('renameInput').value})});
    if(r.ok){document.getElementById('renameDlg').close();refresh()}
    else document.getElementById('renameErr').textContent='could not rename';
  };enterClicks('renameInput','renameSave');}
  const addLookup=document.getElementById('addLookup');
  if(addLookup){
    addLookup.onclick=async()=>{
      const q=document.getElementById('addInput').value.trim();if(!q)return;
      document.getElementById('addErr').textContent='';
      document.getElementById('addPreview').innerHTML='<p class=sub>consulting Scryfall\u2026</p>';
      const r=await fetch(U('/api/resolve'),{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:KEY,query:q})});
      if(!r.ok){document.getElementById('addPreview').innerHTML='';
        document.getElementById('addErr').textContent=(await r.json()).error||'not found';return}
      pending=await r.json();
      const printing=pending.set_code?` <span class=badge>${X(pending.set_code.toUpperCase())} #${X(pending.collector_number)}</span>`:'';
      document.getElementById('addPreview').innerHTML=
        `<h3>${X(pending.name)}${printing}</h3>`+
        (pending.usd?`<div class=price>$${(+pending.usd).toFixed(2)}</div>`:'')+
        (pending.chart||'<p class=nodata>Local history arrives after tonight\u2019s ingest.</p>')+
        (pending.sites||'')+
        `<div class=tgtedit><label>target price ($)</label>`+
        `<input id=addTarget type=number step=0.01 min=0 placeholder=none></div>`;
      document.getElementById('addGo').style.display='';
    };
    enterClicks('addInput','addLookup');
    document.getElementById('addGo').onclick=async()=>{
      const t=document.getElementById('addTarget');
      const r=await fetch(U('/api/add'),{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:KEY,name:pending.name,set_code:pending.set_code,
          collector_number:pending.collector_number,
          target_price:t&&t.value.trim()!==''?+t.value:null})});
      if(!r.ok){document.getElementById('addErr').textContent='could not add';return}
      const d=await r.json();
      if(d.backfilling)document.getElementById('addPreview').innerHTML+=
        '<p class=sub>added \u2713 \u2014 pulling 90 days of history from the '+
        'cached price data; it appears within a minute or two.</p>';
      setTimeout(()=>{document.getElementById('addDlg').close();refresh()},
                 d.backfilling?1500:0);
    };
  }
  const boughtBtn=document.getElementById('boughtBtn');
  if(boughtBtn)boughtBtn.onclick=async()=>{
    const r=await fetch(U('/api/bought'),{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:KEY,entry_id:+boughtBtn.dataset.entry,
                           bought:!boughtBtn.dataset.bought})});
    if(r.ok){document.getElementById('cardDlg').close();refresh()}
  };
  const removeBtn=document.getElementById('removeBtn');
  if(removeBtn){
    removeBtn.onclick=()=>{removeBtn.style.display='none';
      document.getElementById('rmConfirm').style.display='';};
    document.getElementById('rmNo').onclick=()=>{
      document.getElementById('rmConfirm').style.display='none';
      removeBtn.style.display='';};
    document.getElementById('rmYes').onclick=async()=>{
      const r=await fetch(U('/api/remove'),{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:KEY,entry_id:+removeBtn.dataset.entry})});
      if(r.ok){document.getElementById('cardDlg').close();refresh()}
    };
  }
  const tgtSave=document.getElementById('tgtSave');
  if(tgtSave){tgtSave.onclick=async()=>{
    const v=document.getElementById('tgtInput').value.trim();
    const r=await fetch(U('/api/target'),{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:KEY,entry_id:+document.getElementById('tgtEdit').dataset.entry,
                           target_price:v===''?null:+v})});
    if(r.ok){document.getElementById('cardDlg').close();refresh()}
    else document.getElementById('tgtErr').textContent='could not save target';
  };enterClicks('tgtInput','tgtSave');}
  const forkBtn=document.getElementById('forkBtn');
  if(forkBtn){
    forkBtn.onclick=e=>doFork('fork',e.target.dataset.seq);
    const recBtn=document.getElementById('recoverBtn');
    if(recBtn)recBtn.onclick=e=>doFork('recover',e.target.dataset.seq);
  }
}
async function claimFlow(){
  const dlg=document.getElementById('claimDlg');dlg.showModal();
  const out=document.getElementById('claimOut');
  out.innerHTML='<p class=sub>minting your copy\u2026</p>';
  const r=await fetch(U('/api/fork'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:KEY,mode:'fork'})});
  if(!r.ok){out.innerHTML='<p class=err>could not create a copy</p>';return}
  const d=await r.json();
  out.innerHTML=`<div class=secret>Your key (screenshot this \u2014 it is shown once):<br>`+
    `<b>${d.passphrase}</b></div>`+
    `<p class=sub>That key is how you edit your list \u2014 open `+
    `<a href="${d.page}">your page</a> or tell it to Claude in chat. `+
    `Your copy starts with everything on this board and is yours alone.</p>`;
}
function wireCard(card){
  keyable(card);
  card.onclick=()=>{
    const cardDlg=document.getElementById('cardDlg');
    document.getElementById('cardTitle').textContent=card.dataset.name;
    document.getElementById('cardSub').textContent=card.dataset.sub;
    document.getElementById('chartHost').innerHTML=card.dataset.chart||'';
    document.getElementById('snapHost').innerHTML=card.dataset.tail||'';
    document.getElementById('siteHost').innerHTML=card.dataset.sites||'';
    const te=document.getElementById('tgtEdit');
    if(te){te.dataset.entry=card.dataset.entry;
      document.getElementById('tgtInput').value=card.dataset.target||'';
      document.getElementById('tgtErr').textContent='';}
    const bb=document.getElementById('boughtBtn'),rb=document.getElementById('removeBtn');
    if(bb){bb.dataset.entry=card.dataset.entry;
      bb.textContent=card.dataset.bought?'Not bought after all':'Bought \u2713';
      bb.dataset.bought=card.dataset.bought||'';}
    if(rb){rb.dataset.entry=card.dataset.entry;
      document.getElementById('rmConfirm').style.display='none';rb.style.display='';}
    const pts=card.dataset.pts?JSON.parse(card.dataset.pts):[];
    if(pts.length)armCrosshair(pts);
    cardDlg.showModal();
  };
}
function wireRev(r){
  keyable(r);r.onclick=async()=>{
    const seq=r.dataset.seq,revDlg=document.getElementById('revDlg');
    document.getElementById('revTitle').textContent='Revision #'+seq;
    document.getElementById('revBody').innerHTML='<p class=sub>consulting the ledger\u2026</p>';
    document.getElementById('forkOut').innerHTML='';
    revDlg.showModal();
    const res=await fetch(U('/api/revision/'+encodeURIComponent(KEY)+'/'+seq));
    if(!res.ok){document.getElementById('revBody').textContent='Could not read revision.';return}
    const d=await res.json();
    let h='<table class=snap><tr><th>Card</th><th>Printing</th><th>Target</th><th>Note</th></tr>';
    if(!d.entries.length)h+='<tr><td colspan=4><i>empty at this revision</i></td></tr>';
    for(const e of d.entries){
      h+=`<tr><td>${X(e.card_name)}</td><td class=num>${e.set_code?X(e.set_code)+' #'+X(e.collector_number):'cheapest'}</td>`+
         `<td class=num>${e.target_price!=null?'$'+(+e.target_price).toFixed(2):'\u2014'}</td><td>${X(e.note||'')}</td></tr>`;
    }
    document.getElementById('revBody').innerHTML=h+'</table>';
    document.getElementById('forkBtn').dataset.seq=seq;
    const rec=document.getElementById('recoverBtn');
    if(rec)rec.dataset.seq=seq;
  };
}
async function doFork(mode,seq){
  const res=await fetch(U('/api/fork'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:KEY,at_seq:+seq,mode})});
  const out=document.getElementById('forkOut');
  if(!res.ok){out.textContent='Fork failed: '+await res.text();return}
  const d=await res.json();
  out.innerHTML=`<div class=secret>\u26a0 shown once \u2014 passphrase: <b>${d.passphrase}</b><br>`+
    `page: <a href="${d.page}">${d.page}</a> \u00b7 share: ${d.share_code}</div>`+
    (mode==='recover'?'<p class=sub>The current list is now marked superseded.</p>':'');
}
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
  const show=e=>{
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
    tip.textContent=pts[i][0]+' \u00b7 '+CUR+pts[i][1].toFixed(2);
  };
  svg.onpointermove=show;svg.onpointerdown=show;
  svg.onpointerleave=()=>{tip.style.display='none';
    vline.setAttribute('x1',-9);vline.setAttribute('x2',-9);dot.setAttribute('cx',-9)};
}
// While history is being fetched, poll quietly until prices land.
if(document.querySelector('.skel')){
  let tries=0;
  const poll=setInterval(async()=>{
    if(++tries>60){clearInterval(poll);return}          // give up after ~5 min
    const r=await fetch(location.href,{cache:'no-store'}).catch(()=>null);
    if(r&&r.ok&&!(await r.text()).includes('class="nodata skel"')){
      clearInterval(poll);refresh();
    }
  },5000);
}
wire();wireDialogs();
"""


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


def _spark_svg(points, name, color="var(--blue)"):
    if len(points) < 2:
        return ""
    xy, _, _ = _coords(points, SW, SH, 4)
    pl = " ".join(f"{x},{y}" for x, y in xy)
    area = f"M4,{SH - 2} L" + " L".join(f"{x},{y}" for x, y in xy) + f" L{SW - 4},{SH - 2} Z"
    ex, ey = xy[-1]
    return (
        f'<svg class="spark" viewBox="0 0 {SW} {SH}" role="img">'
        f'<title>{esc(name)} — 90 day price trend</title>'
        f'<path d="{area}" fill="{color}" opacity=".12"/>'
        f'<polyline points="{pl}" pathLength="1" fill="none" stroke="{color}"'
        f' stroke-width="2" stroke-linejoin="round"/>'
        f'<circle cx="{ex}" cy="{ey}" r="3" fill="{color}" stroke="var(--base)" stroke-width="2"/>'
        f'</svg>')


def _big_svg(points, name, cur, target=None, bought_at=None):
    if len(points) < 2:
        return "<p class=nodata>Not enough history yet.</p>"
    xy, lo, hi = _coords(points, CW, CH, CPAD)
    pl = " ".join(f"{x},{y}" for x, y in xy)
    grid = "".join(f'<line class="gridline" x1="{CPAD}" y1="{y}" x2="{CW - CPAD}" y2="{y}"/>'
                   for y in (CPAD, CH / 2, CH - CPAD))
    bline = ""
    if bought_at and points[0][0] <= bought_at <= points[-1][0]:
        # nearest point index for the purchase date → vertical marker
        bi = max(i for i, (d, _) in enumerate(points) if d <= bought_at)
        bx = xy[bi][0]
        bline = (f'<line x1="{bx}" y1="{CPAD}" x2="{bx}" y2="{CH - CPAD}"'
                 f' stroke="var(--lavender)" stroke-width="1.5" stroke-dasharray="2 4"/>'
                 f'<text class="axis" x="{bx + 4}" y="{CPAD + 12}"'
                 f' fill="var(--lavender)">bought {esc(bought_at)}</text>')
    tline = ""
    if target is not None and lo <= target <= hi:
        rng = (hi - lo) or 1.0
        ty = round(CH - CPAD - (CH - 2 * CPAD) * ((target - lo) / rng), 1)
        tline = (f'<line class="targetline" x1="{CPAD}" y1="{ty}"'
                 f' x2="{CW - CPAD}" y2="{ty}"/>'
                 f'<text class="axis" x="{CW - CPAD}" y="{ty - 5}"'
                 f' text-anchor="end" fill="var(--peach-text)">target {cur}{target:.2f}</text>')
    return (
        f'<svg viewBox="0 0 {CW} {CH}" style="width:100%;height:auto;touch-action:pan-y" role="img">'
        f'<title>{esc(name)} — 90 day price history</title>{grid}{tline}{bline}'
        f'<text class="axis" x="{CPAD}" y="{CPAD - 6}">{cur}{hi:.2f}</text>'
        f'<text class="axis" x="{CPAD}" y="{CH - CPAD + 14}">{cur}{lo:.2f}</text>'
        f'<text class="axis" x="{CW - CPAD}" y="{CPAD - 6}" text-anchor="end">now {cur}{points[-1][1]:.2f}</text>'
        f'<text class="axis" x="{CPAD}" y="{CH - 6}">{esc(points[0][0])}</text>'
        f'<text class="axis" x="{CW - CPAD}" y="{CH - 6}" text-anchor="end">{esc(points[-1][0])}</text>'
        f'<polyline points="{pl}" fill="none" stroke="var(--blue)" stroke-width="2" stroke-linejoin="round"/>'
        f'</svg>')


def _delta_pct(v, ref_now, label, cur):
    """Delta chip with percentage vs the price `label` days ago."""
    if v is None:
        return f'<span><span class="lbl">{label}</span> <span class="fl">·—</span></span>'
    cls, glyph = ("dn", "▼") if v < 0 else ("up", "▲") if v > 0 else ("fl", "·")
    then = ref_now - v
    pct = f' <span class="pct">{abs(v) / then * 100:.1f}%</span>' if then else ""
    return (f'<span title="change vs {label} ago"><span class="lbl">{label}</span> '
            f'<span class="{cls}">{glyph}</span>{cur}{abs(v):.2f}{pct}</span>')


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


def _hit(entry, s) -> bool:
    return bool(s and entry.get("target_price") is not None
                and s["current"] <= entry["target_price"])


def _card_html(db, entry, s, hit, idx, shop, cur, filling=False):
    """One board card. `s` is the DISPLAY-shop summary; `hit` was computed on
    the tcgplayer basis (targets are USD) so it never flips with the shop."""
    points = []
    if s:
        series = watchlist_db.price_series(
            db, watchlist_db.uuids_for_entry(db, entry), days=90, provider=shop,
            finish=s.get("finish", "normal"))
        points = series["points"] if series else []
    name = esc(entry["card_name"])
    bought_at = entry.get("bought_at")
    badge = (f'<span class="badge">{esc(entry["set_code"])} '
             f'#{esc(entry["collector_number"] or "")}</span>'
             if entry.get("set_code") else "")
    note = f'<p class="note">{esc(entry["note"] or "")}</p>'
    if s:
        foil = ' <small>(foil)</small>' if s.get("finish") == "foil" else ""
        price = f'<div class="price">{cur}{s["current"]:.2f}{foil}</div>'
        deltas = (f'<div class="deltas">{_delta_pct(s["d7"], s["current"], "7d", cur)}'
                  f'{_delta_pct(s["d30"], s["current"], "30d", cur)}</div>')
    else:
        price = ('<div class="nodata skel">fetching price history…</div>'
                 if filling else
                 '<div class="nodata">no prices yet</div>')
        deltas = ""
    target = ""
    if bought_at:
        target = f'<div class="boughtnote">✓ bought {esc(bought_at)}</div>'
    elif entry.get("target_price") is not None:
        usd_hint = " (USD)" if cur != "$" else ""
        if hit:
            target = (f'<div class="target hit">🎯 at target '
                      f'${entry["target_price"]:.2f}{usd_hint} — buy window</div>')
        else:
            gap = (f' · ${s["current"] - entry["target_price"]:.2f} above'
                   if s and shop == "tcgplayer" else "")
            target = f'<div class="target">target ${entry["target_price"]:.2f}{usd_hint}{gap}</div>'
    spark_color = ("var(--overlay)" if bought_at
                   else "var(--green)" if hit else "var(--blue)")
    spark = _spark_svg(points, entry["card_name"], spark_color) if points else ""
    sub = (f'{entry["set_code"]} #{entry["collector_number"]}'
           if entry.get("set_code") else "cheapest printing") + f" · {shop}"
    tgt_attr = (f'{entry["target_price"]:.2f}'
                if entry.get("target_price") is not None else "")
    data = (f' data-name="{name}" data-sub="{esc(sub)}"'
            f' data-entry="{entry["entry_id"]}"'
            f' data-target="{tgt_attr}"'
            f' data-bought="{esc(bought_at) if bought_at else ""}"'
            f' data-sites="{esc(_site_links(entry))}"'
            f' data-pts="{esc(json.dumps(points))}"'
            f' data-chart="{esc(_big_svg(points, entry["card_name"], cur, entry.get("target_price") if shop == "tcgplayer" and not bought_at else None, bought_at))}"'
            f' data-tail="{esc(_tail_table(points, cur))}"')
    cls = "card bought" if bought_at else ("card hit" if hit else "card")
    return (f'<article class="{cls}" '
            f'aria-label="{name} details" style="animation-delay:{idx * 45}ms"{data}>'
            f'<h3>{name}{badge}</h3>{note}{price}{deltas}{target}{spark}</article>')


def _verdict(pairs) -> str:
    """One server-rendered sentence: BUY / WAIT / nothing close.

    tcgplayer basis. Rule: at/below target → BUY when well under (≥10%) or no
    longer falling; WAIT while it just crossed and is still dropping."""
    buys, waits = [], []
    misses = []
    for entry, s, hit in pairs:
        if hit:
            well_under = s["current"] <= entry["target_price"] * 0.9
            falling = s["d7"] is not None and s["d7"] < 0
            pct = (1 - s["current"] / entry["target_price"]) * 100
            if well_under or not falling:
                buys.append(f'<span class="buy">BUY</span> {esc(entry["card_name"])} '
                            f'${s["current"]:.2f} ({pct:.0f}% under target)')
            else:
                waits.append(f'<span class="wait">WAIT</span> {esc(entry["card_name"])} '
                             f'${s["current"]:.2f} (under target, still falling)')
        elif s and entry.get("target_price") is not None:
            misses.append((s["current"] - entry["target_price"], entry, s))
    parts = buys + waits
    if not parts:
        if misses:
            gap, entry, s = min(misses, key=lambda t: t[0])
            return (f'<div class="verdict verdict--quiet"><span class="quiet">'
                    f'No buy windows open — closest: {esc(entry["card_name"])} '
                    f'${gap:.2f} above target.</span></div>')
        return ""
    mood = "verdict--buy" if buys else "verdict--wait"
    return (f'<div class="verdict {mood}">'
            + ' <span class="quiet">·</span> '.join(parts) + "</div>")


def _pager(base, param, page, total, per, keep=""):
    pages = max(1, -(-total // per))
    if pages == 1:
        return ""
    prev = (f'<a href="{base}?{param}={page - 1}{keep}">‹ newer</a>'
            if page > 1 else "<span>‹</span>")
    nxt = (f'<a href="{base}?{param}={page + 1}{keep}">older ›</a>'
           if page < pages else "<span>›</span>")
    return f'<nav class="pager">{prev}<span>{page}/{pages}</span>{nxt}</nav>'


def _shell(row, editable, body, dialogs, cur="$", subtitle="", rightnav=""):
    key = row["_key"]
    label = row["label"] or "Watchlist"
    title = esc(label)
    long_cls = ' class="long"' if len(label) > 16 else ""
    js = _JS % {"key": json.dumps(key), "editable": json.dumps(editable),
                "cpad": CPAD, "cw": CW, "ch": CH, "cur": json.dumps(cur),
                "prefix": json.dumps(PREFIX)}
    rename = (f'<button class="iconbtn" id="rename" title="Rename list" '
              f'aria-label="Rename list" data-label="{esc(row["label"] or "")}">✎</button>'
              if editable else "")
    # keep the pencil glued to the title's last word so it never orphans
    if rename and " " in title:
        head, tail = title.rsplit(" ", 1)
        title_html = f'{head} <span style="white-space:nowrap">{tail}{rename}</span>'
    else:
        title_html = f"{title}{rename}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#eff1f5">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ctext x='8' y='13' font-size='14' text-anchor='middle' fill='%238839ef'%3E✦%3C/text%3E%3C/svg%3E">
<title>{title} · Mystic Forge</title>
<script>(()=>{{const t=localStorage.getItem('mf-theme')||
  (matchMedia('(prefers-color-scheme: dark)').matches?'macchiato':'latte');
document.documentElement.dataset.theme=t;
document.querySelector('meta[name=theme-color]').content=
  t==='macchiato'?'#24273a':'#eff1f5';}})();</script>
<style>{_CSS}</style></head><body>
<div class="wrap">
<header class="masthead"><span class="mright">{rightnav}<button class="iconbtn" id="theme" aria-label="Toggle theme"></button></span>
<h1{long_cls}><span class="rune">✦</span> {title_html}</h1></header>
{f'<p class="subtitle">{subtitle}</p>' if subtitle else ''}
{body}
<footer>forged in the Mystic Forge · card price watchlist ·
<a href="{PREFIX}/health" title="server status">status</a></footer>
</div>
{dialogs}
<script>{js}</script>
</body></html>"""


SORTS = (("target", "near target"), ("age", "newest"), ("price", "cheapest"),
         ("d7", "7d drop"), ("d30", "30d drop"))
_INF = float("inf")


def _sort_key(sort, e, base_s, disp_s):
    """Ordering value, smaller first. No-signal entries sink (never above
    priced/targeted ones); bought partitioning happens outside."""
    if sort == "age":
        return -e["entry_id"]                       # newest added first
    if sort == "price":
        return disp_s["current"] if disp_s else _INF
    if sort == "d7":
        return disp_s["d7"] if disp_s and disp_s["d7"] is not None else _INF
    if sort == "d30":
        return disp_s["d30"] if disp_s and disp_s["d30"] is not None else _INF
    # default "target": USD distance to target — buy windows go negative,
    # so "target met first" falls out of the same ordering.
    if base_s and e.get("target_price") is not None:
        return base_s["current"] - e["target_price"]
    return _INF


def render_main(db, row, editable: bool, cp: int = 1, shop: str = "tcgplayer",
                filling: bool = False, sort: str = "target",
                show_bought: bool = True) -> str:
    """The board: verdict + stat tiles + card grid, buy windows first."""
    key = row["_key"]
    shop = shop if shop in SHOPS else "tcgplayer"
    cur = SHOPS[shop]
    base = (f"{PREFIX}/w/{esc(key)}" if editable
            else f"{PREFIX}/s/{esc(key)}")
    sort = sort if sort in dict(SORTS) else "target"
    # view-state query string, minus defaults; cp handled by the pager
    state = [p for p in (
        f"sort={sort}" if sort != "target" else "",
        f"shop={shop}" if shop != "tcgplayer" else "",
        "" if show_bought else "bought=hide") if p]

    def _url(**over):
        parts = [p for p in (
            f"sort={over.get('sort', sort)}" if over.get('sort', sort) != "target" else "",
            f"shop={over.get('shop', shop)}" if over.get('shop', shop) != "tcgplayer" else "",
            "" if over.get('show_bought', show_bought) else "bought=hide") if p]
        return base + ("?" + "&".join(parts) if parts else "")

    keep = "".join(f"&{p}" for p in state)
    qshop = f"?shop={shop}" if shop != "tcgplayer" else ""
    entries = watchlist_db.current_entries(db, row["id"])

    # tcgplayer basis for hits/verdict (targets are USD); display shop for
    # prices. Bought cards keep their spot in history but leave the math.
    pairs = []
    for e in entries:
        base_s = watchlist_db.entry_price_summary(db, e, provider="tcgplayer")
        disp_s = (base_s if shop == "tcgplayer"
                  else watchlist_db.entry_price_summary(db, e, provider=shop))
        bought = bool(e.get("bought_at"))
        pairs.append((e, base_s, disp_s, _hit(e, base_s) and not bought, bought))
    # chosen ordering; bought always last regardless of sort
    pairs.sort(key=lambda t: (t[4], _sort_key(sort, t[0], t[1], t[2])))
    bought_n = sum(1 for t in pairs if t[4])
    if not show_bought:
        grid_pairs = [t for t in pairs if not t[4]]
    else:
        grid_pairs = pairs

    active = [(e, bs, ds, h) for e, bs, ds, h, b in pairs if not b]
    total_val = sum(s["current"] for _, _, s, _ in active if s)
    net7 = sum(s["d7"] for _, _, s, _ in active if s and s["d7"] is not None)
    hits = sum(1 for _, _, _, h in active if h)
    through = db.execute("SELECT MAX(date) FROM prices WHERE provider=?",
                         (shop,)).fetchone()[0]
    last_ingest = db.execute(
        "SELECT value FROM meta WHERE key='last_ingest'").fetchone()
    last_ingest = last_ingest["value"] if last_ingest else "never"

    page = grid_pairs[(cp - 1) * CARDS_PER_PAGE: cp * CARDS_PER_PAGE]
    cards = "".join(_card_html(db, e, disp_s, h, i, shop, cur, filling)
                    for i, (e, _, disp_s, h, _b) in enumerate(page)) or \
        '<p class="nodata">Nothing watched yet — use “Add card” or ask Claude.</p>'

    shop_names = {"tcgplayer": "TCGplayer", "cardkingdom": "Card Kingdom",
                  "cardmarket": "Cardmarket"}
    shop_links = "".join(
        f'<a href="{_url(shop=s)}" class="{"on" if s == shop else ""}">{shop_names[s]}</a>'
        for s in SHOPS)
    sort_links = "".join(
        f'<a href="{_url(sort=k)}" class="{"on" if k == sort else ""}">{label}</a>'
        for k, label in SORTS)
    bought_toggle = ""
    if bought_n:
        bought_toggle = (f'<a class="textlink mla" href="{_url(show_bought=not show_bought)}">'
                         f'{"hide" if show_bought else "show"} bought ({bought_n})</a>')
    sortbar = (f'<div class="sortbar"><span class="shoplbl">sort:</span>'
               f'<span class="shops">{sort_links}</span>{bought_toggle}</div>')
    share_path = f"{PREFIX}/s/{esc(row['share_code'])}"
    share = (f'<button class="textlink" data-copy="{share_path}" '
             f'title="Copy the read-only link (code {esc(row["share_code"])})">'
             f'Share ⧉</button>' if editable else "")
    claim = ('' if editable else
             '<button class="act primary" id="claim">Make my own copy</button>')
    superseded = ('<p class="note">⚠ superseded by a recovery clone — this copy '
                  'is historical.</p>' if row["superseded_by"] else "")

    tgt_edit = ""
    modal_actions = ""
    if editable:
        tgt_edit = ('<div class="tgtedit" id="tgtEdit"><label for="tgtInput">'
                    'target price ($)</label><input id="tgtInput" type="number" '
                    'step="0.01" min="0" placeholder="none">'
                    '<button class="act" id="tgtSave">Save</button>'
                    '<span class="err" id="tgtErr"></span></div>')
        modal_actions = (
            '<div class="modalend">'
            '<button class="act" id="boughtBtn">Bought ✓</button>'
            '<button class="act danger" id="removeBtn">Remove</button>'
            '<span id="rmConfirm" style="display:none">Really remove? '
            '<button class="act danger" id="rmYes">Yes, remove</button> '
            '<button class="act" id="rmNo">Keep</button></span></div>')
    xbtn = '<button class="xclose" aria-label="Close">×</button>'
    dialogs = (f'<dialog id="cardDlg">{xbtn}<h3 id="cardTitle"></h3><p class="sub" id="cardSub"></p>'
               f'<div class="chart-wrap"><div id="chartHost"></div><div class="tip" id="tip"></div></div>'
               f'{tgt_edit}{modal_actions}<div id="siteHost"></div><div id="snapHost"></div>'
               f'</dialog>')
    topic = watchlist_ingest.ntfy_topic(row["share_code"])
    dialogs += (f'<dialog id="alertsDlg">{xbtn}<h3>Buy-window alerts</h3>'
                f'<p class="sub">When a card first drops to its target, this list '
                f'pings a push topic after the nightly price update.</p>'
                f'<p>Install the free <a href="https://ntfy.sh" target="_blank" '
                f'rel="noopener">ntfy</a> app and subscribe to '
                f'<button class="chip" data-copy="{esc(topic)}">{esc(topic)} ⧉</button>'
                f'<br>or watch it in a browser: '
                f'<a href="{esc(watchlist_ingest.NTFY_BASE)}/{esc(topic)}" '
                f'target="_blank" rel="noopener">{esc(watchlist_ingest.NTFY_BASE)}/{esc(topic)}</a></p>'
                f'</dialog>')
    if editable:
        dialogs += (f'<dialog id="renameDlg">{xbtn}<h3>Rename list</h3>'
                    '<input id="renameInput" maxlength="80">'
                    '<span class="err" id="renameErr"></span>'
                    '<div class="btnrow"><button class="act primary" id="renameSave">Save</button>'
                    '<button class="act close">Cancel</button></div></dialog>')
        dialogs += (f'<dialog id="addDlg">{xbtn}<h3>Add a card</h3>'
                    '<p class="sub">Paste a Scryfall link to pin that exact printing, '
                    'or type a card name to track its cheapest printing.</p>'
                    '<input id="addInput" placeholder="Card name or Scryfall link">'
                    '<span class="err" id="addErr"></span>'
                    '<div id="addPreview"></div>'
                    '<div class="btnrow"><button class="act" id="addLookup">Preview</button>'
                    '<button class="act primary" id="addGo" style="display:none">Add to watchlist</button></div>'
                    '</dialog>')
    else:
        dialogs += (f'<dialog id="claimDlg">{xbtn}<h3>Your own watchlist</h3>'
                    '<div id="claimOut"></div></dialog>')

    add_btn = ('<button class="act primary" id="addCard">＋ Add card</button>'
               if editable else "")
    hist_title = ("Every change ever made to this list — inspect or restore any point"
                  if editable else "See every change made to this list")
    if through:
        freshness = f"Buy prices through {esc(through)}"
    elif filling:
        freshness = ("⏳ Fetching 90 days of price history now — first run on a "
                     "new server takes a few minutes; this page refreshes itself")
    else:
        freshness = "No price data yet"
    usd_note = (" · targets always compare against TCGplayer USD"
                if shop != "tcgplayer" else "")
    ro_note = "" if editable else "Read-only view · "
    subtitle = (f"{ro_note}{freshness} · ▼ green = cheaper · ▲ red = pricier "
                f"(these are buy prices){usd_note}")
    rightnav = (f'<button class="textlink" id="alerts">Alerts</button>'
                f'<a class="textlink" href="{base}/history{qshop}" '
                f'title="{hist_title}">History</a>')
    net_cls = "dn" if net7 < 0 else "up" if net7 > 0 else "fl"
    body = f"""
<div class="actions">{add_btn}{claim}{share}
<span class="shopgrp mla"><span class="shoplbl">prices:</span><span class="shops">{shop_links}</span></span></div>
{superseded}
{_verdict([(e, bs, h) for e, bs, _, h, b in pairs if not b])}
<div class="stats">
<div class="stat"><b>{hits}</b><span>buy windows</span></div>
<div class="stat" title="sum of 7-day changes — down is good">
<b><span class="{net_cls}">{"▼" if net7 < 0 else "▲" if net7 > 0 else "·"}</span>{cur}{abs(net7):.2f}</b><span>7-day net</span></div>
<div class="stat"><b>{cur}{total_val:.2f}</b><span>list total</span></div>
<div class="stat"><b>{len(entries)}</b><span>cards</span></div>
</div>
{sortbar}
<div class="grid">{cards}</div>
{_pager(base, "cp", cp, len(grid_pairs), CARDS_PER_PAGE, keep=keep)}"""
    return _shell(row, editable, body, dialogs, cur,
                  subtitle=subtitle, rightnav=rightnav)


_ACTION_LABELS = {"create": ("forged", "var(--mauve)"),
                  "add": ("added", "var(--green)"),
                  "remove": ("removed", "var(--red)"),
                  "set_target": ("target set", "var(--yellow)"),
                  "set_note": ("note set", "var(--yellow)"),
                  "set_label": ("renamed", "var(--yellow)"),
                  "bought": ("bought", "var(--lavender)"),
                  "unbought": ("unbought", "var(--yellow)"),
                  "clone_init": ("cloned from", "var(--mauve)")}


def render_history(db, row, editable: bool, hp: int = 1,
                   shop: str = "tcgplayer") -> str:
    """The stashed-away revision view: full chain, revision modal, fork/restore."""
    key = row["_key"]
    base = (f"{PREFIX}/w/{esc(key)}" if editable
            else f"{PREFIX}/s/{esc(key)}")
    qshop = f"?shop={shop}" if shop in SHOPS and shop != "tcgplayer" else ""
    total_ev = db.execute("SELECT COUNT(*) FROM events WHERE list_id=?",
                          (row["id"],)).fetchone()[0]
    events = db.execute(
        "SELECT * FROM events WHERE list_id=? ORDER BY seq DESC LIMIT ? OFFSET ?",
        (row["id"], EVENTS_PER_PAGE, (hp - 1) * EVENTS_PER_PAGE)).fetchall()
    # older set_target/set_note/remove payloads carry only entry_id — resolve
    # the card name through the add event that minted it (entry_id == seq).
    adds = {ev["seq"]: json.loads(ev["payload_json"]).get("card_name", "")
            for ev in db.execute(
                "SELECT seq, payload_json FROM events WHERE list_id=?"
                " AND action='add'", (row["id"],))}

    def _detail(ev, payload):
        d = payload.get("card_name") or payload.get("label") or ""
        if not d and payload.get("entry_id") in adds:
            d = adds[payload["entry_id"]]
        if ev["action"] == "set_target" and d:
            tp = payload.get("target_price")
            d += " → no target" if tp is None else f" → ${tp:.2f}"
        return d

    revs = ""
    for ev in events:
        label, color = _ACTION_LABELS.get(ev["action"],
                                          (ev["action"].replace("_", " "), "var(--sub)"))
        revs += (
            f'<div class="rev" data-seq="{ev["seq"]}" aria-label="Revision {ev["seq"]}">'
            f'<span class="n">#{ev["seq"]}</span>'
            f'<span class="a"><span class="dot" style="background:{color}"></span>'
            f'{esc(label)}</span>'
            f'<span class="d">{esc(_detail(ev, json.loads(ev["payload_json"])))}</span>'
            f'<span class="t">{_ago(ev["ts"])}</span></div>')
    recover_btn = ('<button class="act" id="recoverBtn">Restore here (supersede)</button>'
                   if editable else "")
    dialogs = (f'<dialog id="revDlg"><h3 id="revTitle"></h3>'
               f'<p class="sub">The list as it stood at this revision.</p>'
               f'<div id="revBody"></div><div id="forkOut"></div>'
               f'<div class="btnrow"><button class="act primary" id="forkBtn">⑂ Fork this revision</button>'
               f'{recover_btn}<button class="act close">Close</button></dialog>')
    intro = ("Every change, newest first — nothing is ever deleted. Tap a "
             "revision to view, copy it, or restore it."
             if editable else
             "Every change made to this list, newest first — tap one to view "
             "or copy it.")
    rightnav = f'<a class="textlink" href="{base}{qshop}">← Back to board</a>'
    body = f"""
<h2 class="pagehead">History</h2>
<div class="rail">{revs}
{_pager(base + "/history", "hp", hp, total_ev, EVENTS_PER_PAGE,
        keep=qshop.replace("?", "&") if qshop else "")}</div>"""
    return _shell(row, editable, body, dialogs,
                  subtitle=intro, rightnav=rightnav)
