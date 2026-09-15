"""Server-rendered watchlist pages — the "arcane ledger".

Catppuccin Latte (day) / Macchiato (night), serif display over mono data.
Three views: the main board (cards + sparklines), a separate history view
(revision chain + fork/restore, Google-Docs style), and the forge page that
mints a new list from pasted cards.

Semantics that keep the numbers honest (persona-review driven):
- Unpinned cards chart the min-across-printings envelope (watchlist_db).
- Every card has a price *basis*: its pinned shop, else the cheapest of the
  USD markets. Hit state is judged on the basis whatever the board's display
  shop is, so "at target" never changes meaning when you flip the dropdown.
- A target is a fixed number or a rule that follows the historic low
  (watchlist/targets.py); the board shows the number the rule resolves to.
- The freshness line shows the newest PRICE date, not the ingest date.
Page assets inline; the browser reaches out only for the card face
(Scryfall) and the MTGStocks id lookup. Modal content that round-trips
through innerHTML is escaped client-side (X()).
"""

import json
import urllib.parse
from datetime import datetime, timezone
from html import escape as esc

from . import db as watchlist_db
from . import ingest as watchlist_ingest
from . import mtgstocks
from . import targets as watchlist_targets

EVENTS_PER_PAGE = 15
CARDS_PER_PAGE = 24

SHOPS = dict(watchlist_db.SHOP_CURRENCY)     # display shops → currency
SHOP_NAMES = dict(watchlist_db.SHOP_NAMES)
ALL = "all"                                   # the cheapest-across-USD view
USD_LABEL = "cheapest of TCGplayer, Card Kingdom and Mana Pool"

# Public path prefix stripped by the gateway (set from PUBLIC_BASE by server.py).
# Every emitted link and fetch must include it; empty when served at the root.
PREFIX = ""
# Absolute public base for OG tags (scrapers need absolute image URLs).
PUBLIC_BASE = ""

# Chart geometry shared with the inline JS crosshair (keep in sync there).
# The bottom margin is deeper than the top: it holds two rows of labels
# (the low price, then the dates), which at phone-width axis sizes need it.
CW, CH, CPAD, CPADB = 640, 236, 34, 50
SW, SH = 240, 56

# Where a card can be bought / looked up. Mana Pool numbers printings its own
# way, so its badge links the card page (every printing) rather than guessing.
TCG_MASSENTRY = "https://www.tcgplayer.com/massentry?productline=Magic&c="
CK_BUILDER = "https://www.cardkingdom.com/builder?c="
MP_ADDDECK = "https://manapool.com/add-deck"

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
.actions{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-bottom:1.3rem}
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
select.shopsel,select.sel{font-family:var(--font-ui);font-size:.85rem;color:var(--text);
  background:var(--mantle);border:1px solid var(--surface1);border-radius:.5rem;
  padding:.35rem 1.7rem .35rem .6rem;cursor:pointer;appearance:none;-webkit-appearance:none;
  background-image:linear-gradient(45deg,transparent 50%,var(--sub) 50%),
    linear-gradient(135deg,var(--sub) 50%,transparent 50%);
  background-position:calc(100% - .95rem) 55%,calc(100% - .65rem) 55%;
  background-size:.3rem .3rem;background-repeat:no-repeat}
select.shopsel:hover,select.sel:hover{border-color:var(--lavender)}
select.shopsel:focus-visible,select.sel:focus-visible{outline:2px solid var(--lavender);outline-offset:1px}
.pagehead{font-size:1.1rem;letter-spacing:.05em;text-transform:uppercase;
  color:var(--sub);text-align:center;margin-bottom:.6rem}
.sortbar{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;
  margin:-.5rem 0 .9rem}
.filterbox{font-family:var(--font-ui);font-size:.85rem;background:var(--mantle);
  color:var(--text);border:1px solid var(--surface0);border-radius:.45rem;
  padding:.3rem .55rem;width:11rem}
.filterbox:focus{outline:2px solid var(--lavender);outline-offset:1px}
.shops{display:inline-flex;gap:.1rem}           /* view tabs, not buttons */
.shops a{font-family:var(--font-ui);font-size:.82rem;padding:.3rem .55rem;
  color:var(--sub);text-decoration:none;border-bottom:2px solid transparent}
.shops a.on{color:var(--text);border-bottom-color:var(--mauve);font-weight:600}
.shops a:not(.on):hover{color:var(--text)}
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
.wrap.nofx .card{animation:none}   /* same-view morphs land instantly */
.card h3{font-family:var(--font-display);font-size:1.05rem;font-weight:600;line-height:1.25}
dialog h3{font-family:var(--font-display)}
.badge{font-family:var(--font-data);font-size:.7rem;color:var(--sub);
  border:1px solid var(--surface1);border-radius:.35rem;padding:.05rem .35rem;
  vertical-align:2px;margin-left:.35rem;white-space:nowrap}
.note{font-family:var(--font-display);font-style:italic;color:var(--sub);
  font-size:.78rem;margin:.15rem 0 .4rem;min-height:1em}
.price{font-family:var(--font-data);font-size:1.5rem;font-weight:600;letter-spacing:-.01em}
.price small{font-size:.7rem;color:var(--peach-text);font-weight:400}
.price .via{font-family:var(--font-ui);font-size:.7rem;color:var(--sub);font-weight:400;
  margin-left:.35rem;letter-spacing:0}
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
.pager{display:flex;justify-content:center;align-items:center;gap:.35rem;
  margin-top:1rem;font-family:var(--font-ui);font-size:.85rem;flex-wrap:wrap}
.pnum{padding:.4rem .75rem;border-radius:.45rem;color:var(--text);
  text-decoration:none;border:1px solid var(--surface0);background:var(--mantle)}
a.pnum:hover{border-color:var(--lavender)}
.pnum.cur{background:var(--mauve);border-color:var(--mauve);color:var(--base);
  font-weight:600}
.pnum.dis{opacity:.4;border-color:transparent;background:none}
.gap{padding:.4rem .15rem;color:var(--sub)}
dialog{border:1px solid var(--surface1);border-radius:1rem;background:var(--base);
  color:var(--text);max-width:44rem;width:92vw;padding:1.3rem;margin:auto;
  max-height:90vh;overflow:auto;box-shadow:0 20px 60px var(--shadow);
  overscroll-behavior:contain}
dialog[open]{animation:dlgin .28s cubic-bezier(.2,.9,.3,1.05)}
@keyframes dlgin{from{opacity:0;transform:translateY(14px) scale(.985)}}
dialog::backdrop{background:#0006;backdrop-filter:blur(3px);
  animation:backdropin .28s ease-out}
@keyframes backdropin{from{opacity:0}}
body:has(dialog[open]){overflow:hidden}   /* page can't scroll behind a sheet */
@media(prefers-reduced-motion:reduce){
  dialog[open],dialog::backdrop,.card,.spark polyline{animation:none}}
dialog h3{font-size:1.2rem;margin-bottom:.2rem;padding-right:2rem}
dialog .sub{color:var(--sub);font-size:.8rem;margin-bottom:.8rem}
dialog p.sub a{color:var(--sub)}
/* ── card modal ── */
.cardhead{display:flex;gap:1rem;align-items:stretch;margin-bottom:.9rem}
.cardimg{width:8.6rem;flex:none;border-radius:.55rem;aspect-ratio:488/680;
  object-fit:cover;background:var(--mantle);box-shadow:0 4px 14px var(--shadow)}
.cardimg[hidden]{display:none}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:.55rem;flex:1;align-content:start}
.kpi{background:var(--card);border:1px solid var(--surface0);border-radius:.7rem;
  padding:.55rem .75rem;min-width:0}
.kpi b{display:block;font-family:var(--font-data);font-size:1.25rem;font-weight:600;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi span{font-size:.68rem;color:var(--sub);text-transform:uppercase;letter-spacing:.08em;
  display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi.hit b{color:var(--green-text)}
.shoprow{display:flex;gap:.4rem;flex-wrap:wrap;margin:0 0 .6rem;align-items:center}
.shoprow .lbl{font-size:.75rem;color:var(--sub);margin-right:.1rem}
.shopchip{font-family:var(--font-data);font-size:.74rem;border:1px solid var(--surface1);
  background:var(--mantle);color:var(--text);border-radius:.5rem;padding:.25rem .6rem;
  cursor:pointer;display:inline-flex;gap:.35rem;align-items:center}
.shopchip .n{font-family:var(--font-ui);color:var(--sub)}
.shopchip.on{border-color:var(--mauve);box-shadow:0 0 0 1px var(--mauve)}
.shopchip.best{border-color:var(--green)}
.shopchip.best .n::after{content:" ✓";color:var(--green-text)}
.shopchip.pinned .n::before{content:"📌 "}
.shopchip:disabled{opacity:.55;cursor:default}
.chart-wrap{position:relative}
.tip{position:absolute;pointer-events:none;background:var(--crust);border:1px solid var(--surface1);
  border-radius:.5rem;padding:.25rem .55rem;font-family:var(--font-data);font-size:.72rem;
  transform:translate(-50%,-115%);white-space:nowrap;display:none}
.sites{display:flex;gap:.45rem;flex-wrap:wrap;margin:.7rem 0 .2rem}
.sites a{font-family:var(--font-data);font-size:.72rem;border:1px solid var(--surface1);
  border-radius:.45rem;padding:.2rem .55rem;color:var(--sub);text-decoration:none}
.sites a:hover{border-color:var(--lavender);color:var(--text)}
.sites a::after{content:" ↗";color:var(--sub)}
.tgtbox{background:var(--card);border:1px solid var(--surface0);border-radius:.8rem;
  padding:.8rem .9rem;margin-top:.8rem}
.tgtrow{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin:.25rem 0}
.tgtrow label,.sclbl{font-size:.8rem;color:var(--sub)}
.tgtrow label.head{font-weight:600;color:var(--text)}
.money{display:inline-flex;align-items:center;font-family:var(--font-data);
  background:var(--mantle);border:1px solid var(--surface1);border-radius:.45rem;
  padding:0 0 0 .5rem;color:var(--sub)}
.money input{border:none;background:none;width:5.6rem;font-family:var(--font-data);
  font-size:.9rem;color:var(--text);padding:.32rem .4rem}
.money input:focus{outline:none}
.money:focus-within{outline:2px solid var(--lavender);outline-offset:1px}
.money input:disabled{color:var(--sub);font-style:italic}
.money:has(input:disabled){background:var(--crust);border-style:dashed}
.money:has(input:disabled)::after{content:"⟳";padding:0 .45rem 0 0;font-size:.8rem;color:var(--sub)}
.tgtedit input,#renameInput,.tgtrow input.txt{font-family:var(--font-data);font-size:.85rem;
  background:var(--mantle);color:var(--text);border:1px solid var(--surface1);
  border-radius:.45rem;padding:.3rem .5rem}
.tgtedit{display:flex;gap:.5rem;align-items:center;margin-top:.7rem;flex-wrap:wrap}
.tgtedit label{font-size:.8rem;color:var(--sub)}
.tgtedit input{width:6.5rem}
#noteInput{width:14rem;flex:1 1 12rem;min-width:0}
#renameInput{width:100%;margin:.5rem 0}
.sc{font-family:var(--font-ui);font-size:.76rem;border:1px solid var(--surface1);
  background:var(--mantle);color:var(--text);border-radius:.45rem;padding:.28rem .55rem;
  cursor:pointer;white-space:nowrap}
.sc:hover{border-color:var(--lavender)}
.pctin{font-family:var(--font-data);font-size:.76rem;width:5.2rem;background:var(--mantle);
  color:var(--text);border:1px solid var(--surface1);border-radius:.45rem;padding:.26rem .4rem}
.shortcuts{margin:.35rem 0 .1rem;border-top:1px dashed var(--surface0);padding-top:.4rem}
.shortcuts[hidden]{display:none}
.scrow{display:flex;gap:.35rem;align-items:center;flex-wrap:wrap;margin:.3rem 0}
.scrow .sclbl{min-width:11rem}
.scrow .sclbl b{font-family:var(--font-data);color:var(--text);font-weight:600}
.switch{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;font-size:.82rem;
  margin:.55rem 0 .2rem;cursor:pointer}
.switch input{accent-color:var(--mauve);width:1rem;height:1rem}
.switch select{font-size:.78rem;padding:.2rem 1.5rem .2rem .45rem}
.hint{font-size:.74rem;color:var(--sub);margin:.15rem 0 0 1.5rem;font-style:italic}
details.histbox{margin:.8rem 0 .6rem}
details.histbox summary{cursor:pointer;color:var(--sub);font-size:.82rem;padding:.3rem 0;
  list-style:none;display:flex;align-items:center;gap:.4rem}
details.histbox summary::-webkit-details-marker{display:none}
details.histbox summary::before{content:"▸";font-size:.8rem;transition:transform .15s}
details.histbox[open] summary::before{transform:rotate(90deg)}
.err{color:var(--red);font-family:var(--font-data);font-size:.75rem}
.ok{color:var(--green-text);font-family:var(--font-data);font-size:.78rem}
table.snap{width:100%;border-collapse:collapse;font-size:.84rem;margin:.5rem 0}
table.snap th{text-align:left;color:var(--sub);font-size:.72rem;text-transform:uppercase;
  letter-spacing:.06em;padding:.3rem .5rem;border-bottom:1px solid var(--surface1)}
table.snap td{padding:.32rem .5rem;border-bottom:1px solid var(--surface0)}
table.snap td.num{font-family:var(--font-data)}
.btnrow{display:flex;gap:.6rem;margin-top:.9rem;flex-wrap:wrap;align-items:center}
.act{font-family:inherit;font-size:.88rem;border-radius:.6rem;cursor:pointer;
  padding:.45rem 1rem;border:1px solid var(--surface1);background:var(--mantle);color:var(--text);
  display:inline-flex;align-items:center;gap:.35rem;text-decoration:none;line-height:1.2}
.act.primary{background:var(--mauve);border-color:var(--mauve);color:var(--base)}
.act.secondary{background:var(--card);border-color:var(--lavender);color:var(--text)}
.act.danger{color:var(--red);border-color:var(--red)}
.act.ghost{background:none;border-color:transparent;color:var(--red)}
.act.ghost:hover{background:var(--mantle)}
.act:hover{filter:brightness(1.08)}
.act:disabled{opacity:.5;cursor:default;filter:none}
.xclose{position:absolute;top:.55rem;right:.75rem;background:none;border:none;
  color:var(--sub);font-size:1.15rem;cursor:pointer;line-height:1;padding:.2rem .4rem;
  border-radius:.4rem}
.xclose:hover{background:var(--mantle);color:var(--text)}
/* no position override on dialog: the UA's dialog:modal{position:fixed} must win,
   else the sheet anchors to the document and rides up with page scroll */
.modalend{display:flex;gap:.6rem;margin-top:1rem;justify-content:flex-end;
  align-items:center;flex-wrap:wrap;position:sticky;bottom:-1.3rem;
  background:var(--base);padding:.7rem 0 .2rem;z-index:2;border-top:1px solid var(--surface0)}
.modalend .left{margin-right:auto;display:flex;gap:.4rem;align-items:center}
#addInput,textarea.box{font-family:var(--font-data);font-size:.85rem;width:100%;margin:.5rem 0;
  background:var(--mantle);color:var(--text);border:1px solid var(--surface1);
  border-radius:.45rem;padding:.35rem .5rem}
textarea.box{min-height:7rem;resize:vertical;line-height:1.45}
textarea.box:focus,#addInput:focus{outline:2px solid var(--lavender);outline-offset:1px}
.secret{font-family:var(--font-data);background:var(--mantle);border:1px dashed var(--peach);
  border-radius:.5rem;padding:.5rem .7rem;margin:.5rem 0;word-break:break-all}
/* ── export / import ── */
.segs{display:flex;gap:.3rem;flex-wrap:wrap;align-items:center;margin:.2rem 0 .6rem}
.segs label{font-size:.82rem;border:1px solid var(--surface1);border-radius:2rem;
  padding:.28rem .8rem;cursor:pointer;display:inline-flex;gap:.35rem;align-items:center;
  background:var(--mantle)}
.segs label:has(input:checked){background:var(--mauve);border-color:var(--mauve);color:var(--base)}
.segs input{position:absolute;opacity:0;width:0;height:0}
.segs .cnt{font-family:var(--font-data);font-size:.72rem;opacity:.8}
.picklist{max-height:13rem;overflow:auto;border:1px solid var(--surface0);border-radius:.6rem;
  padding:.3rem .5rem;background:var(--card);margin-bottom:.6rem}
.picklist label{display:flex;gap:.5rem;align-items:center;font-size:.82rem;padding:.22rem .1rem;
  cursor:pointer;border-bottom:1px dashed var(--surface0)}
.picklist label:last-child{border-bottom:none}
.picklist input{accent-color:var(--mauve)}
.picklist .pr{margin-left:auto;font-family:var(--font-data);font-size:.76rem;color:var(--sub);
  white-space:nowrap}
.picklist .hitmk{color:var(--green-text);font-size:.7rem}
.picklist .bmk{color:var(--lavender);font-size:.7rem}
.xopts{display:flex;gap:1rem;flex-wrap:wrap;font-size:.78rem;color:var(--sub);margin:.2rem 0}
.xopts label{display:inline-flex;gap:.3rem;align-items:center;cursor:pointer}
.xopts input{accent-color:var(--mauve)}
.stores{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
.stores .lbl{font-size:.78rem;color:var(--sub);width:100%}
/* ── forge (new list) page ── */
.forge{max-width:40rem;margin:0 auto}
.forge label.fl{display:block;font-size:.8rem;color:var(--sub);margin-top:.9rem}
.forge input.txt{width:100%;font-family:var(--font-data);font-size:.9rem;background:var(--mantle);
  color:var(--text);border:1px solid var(--surface1);border-radius:.45rem;padding:.45rem .6rem;
  margin-top:.3rem}
.forge .row{display:flex;gap:1rem;flex-wrap:wrap}
.forge .row>div{flex:1 1 12rem}
.forge .foot{margin-top:1rem}
.forge .switch{flex-wrap:nowrap;align-items:flex-start}
.forgelinks{line-height:2;font-size:.9rem;word-break:break-all}
.forgelinks a,dialog .secret a,.claimlink{color:var(--blue)}
.forgelinks code{font-family:var(--font-data);font-size:.82rem}
.secret .chip{vertical-align:middle;margin-left:.3rem}
.forge .switch input{flex:none;margin-top:.15rem}
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
  .actions{gap:.5rem}
  .shopgrp{width:100%;margin-left:0}
  .shopgrp select{flex:1}
  .shops{display:flex;flex:1;overflow-x:auto;scrollbar-width:none}
  .shops a{flex:none;white-space:nowrap;text-align:center;justify-content:center}
  /* 44px-rule tap targets: height, not font inflation */
  .chip,.shops a,.pnum,.textlink,.sc,.shopchip{min-height:2.75rem;display:inline-flex;align-items:center}
  .shops a{display:flex}
  .act{min-height:2.75rem;padding:.65rem 1.1rem}
  .xclose{padding:.6rem .8rem}
  .rev{padding:.75rem .3rem;flex-wrap:wrap}
  .rev .d{flex:1 1 100%;white-space:normal;order:9;padding-left:1.15rem}
  .filterbox{width:100%;font-size:1rem;padding:.5rem .6rem}
  .stats{grid-template-columns:repeat(2,1fr);gap:.6rem}
  .stat{padding:.55rem .7rem}
  .stat b{font-size:1.1rem}
  #addInput,#renameInput,.tgtedit input,.money input,textarea.box,.forge input.txt{font-size:1rem}  /* no iOS zoom-on-focus */
  .tgtedit input{width:8rem;padding:.5rem .6rem}
  .cardimg{width:6.2rem}
  .kpis{grid-template-columns:1fr 1fr;gap:.4rem}
  .kpi{padding:.45rem .55rem}
  .kpi b{font-size:1rem}
  .kpi span{white-space:normal;font-size:.62rem;line-height:1.25}
  .scrow .sclbl{min-width:100%}
  .axis{font-size:20px}                        /* ≈10px rendered at phone width */
  .badge,.deltas,.rev .a,.sites a,.tip,.boughtnote{font-size:.8rem}
  dialog{width:100vw;max-width:100vw;margin:auto 0 0;border-radius:1rem 1rem 0 0;
    max-height:92dvh;padding:1rem}
  dialog[open]{animation:sheetin .32s cubic-bezier(.2,.9,.3,1.02)}
  .modalend{bottom:-1rem}
  .card::after{content:"›";position:absolute;top:.8rem;right:.9rem;
    color:var(--sub);font-size:1.1rem}
}
@keyframes sheetin{from{opacity:.4;transform:translateY(100%)}}
"""

# Filled by _shell via a single JSON blob (no %-formatting: the JS is full of
# literal percent signs).
_JS = r"""
const CFG=__CFG__;
const KEY=CFG.key, EDITABLE=CFG.editable, CPAD=CFG.cpad, CPADB=CFG.cpadb, CW=CFG.cw, CH=CFG.ch, CUR=CFG.cur;
const P=CFG.prefix;                       // gateway path prefix, e.g. '/mtg'
const SHOP=CFG.shop;                      // the board's display shop ('all' = cheapest USD)
const SHOPN=CFG.shopNames, SHOPC=CFG.shopCur;
const U=path=>P+path;                     // build a browser-correct URL
const X=s=>String(s??'').replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=(v,c)=>v==null||v===''||isNaN(+v)?'—':(c||'$')+(+v).toFixed(2);
const J=(el,body)=>fetch(U(el),{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(Object.assign({key:KEY},body))});
// ── flashless navigation: every internal link and mutation morphs in place ──
async function morphNavigate(url,push=true){
  try{
    const sameView=new URL(url,location.origin).pathname===location.pathname;
    const r=await fetch(url);
    if(!r.ok)throw 0;
    const doc=new DOMParser().parseFromString(await r.text(),'text/html');
    const nw=doc.querySelector('.wrap');
    if(!nw)throw 0;
    if(sameView)nw.classList.add('nofx');   // filter/sort/page: no re-entrance
    const apply=()=>{
      document.querySelector('.wrap').replaceWith(nw);
      document.querySelectorAll('body > dialog').forEach(d=>d.remove());
      doc.querySelectorAll('body > dialog').forEach(d=>document.body.append(d));
      document.querySelectorAll('body > script[type="application/json"]').forEach(d=>d.remove());
      doc.querySelectorAll('body > script[type="application/json"]').forEach(d=>document.body.append(d));
      document.title=doc.title;
      wire();wireDialogs();
    };
    // crossfade only when actually changing views — instant otherwise
    if(!sameView&&document.startViewTransition)document.startViewTransition(apply);
    else apply();
    if(push&&url!==location.href)history.pushState({},'',url);
  }catch(e){location.href=url}
}
const normName=s=>s.toLowerCase().replace(/[^a-z0-9]/g,'');
const refresh=()=>morphNavigate(location.href,false);
window.onpopstate=()=>morphNavigate(location.href,false);
const flash=(c,text)=>{
  if(!c.style.minWidth){c.style.minWidth=c.getBoundingClientRect().width+'px';
    c.style.textAlign='center'}
  if(!c.dataset.orig)c.dataset.orig=c.textContent;
  c.textContent=text;clearTimeout(c._t);
  c._t=setTimeout(()=>{c.textContent=c.dataset.orig},1100);
};
const copyable=c=>c.onclick=e=>{
  e.stopPropagation();
  navigator.clipboard.writeText(c.dataset.copy.startsWith('/')
    ?location.origin+c.dataset.copy:c.dataset.copy);
  flash(c,'copied ✓');
};
const enterClicks=(inputId,btnId)=>{const i=document.getElementById(inputId);
  if(i)i.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();
    document.getElementById(btnId).click()}}};
const keyable=el=>{el.setAttribute('tabindex','0');el.setAttribute('role','button');
  el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();el.click()}}};
const themeGlyph=()=>{const b=document.getElementById('theme');if(!b)return;
  b.textContent=document.documentElement.dataset.theme==='latte'?'\u{1F319}':'☀️';
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
  const shopSel=document.getElementById('shopSel');
  if(shopSel)shopSel.onchange=()=>morphNavigate(shopSel.selectedOptions[0].dataset.href);
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
  const exportBtn=document.getElementById('exportBtn');
  if(exportBtn)exportBtn.onclick=openExport;
  const importBtn=document.getElementById('importBtn');
  if(importBtn)importBtn.onclick=()=>{
    document.getElementById('imOut').innerHTML='';
    document.getElementById('imErr').textContent='';
    document.getElementById('importDlg').showModal();
    document.getElementById('imText').focus();};
  const alertsBtn=document.getElementById('alerts');
  if(alertsBtn)alertsBtn.onclick=()=>document.getElementById('alertsDlg').showModal();
  const forgeBtn=document.getElementById('forgeGo');
  if(forgeBtn)forgeBtn.onclick=forgeFlow;
  if(document.getElementById('cardDlg'))
    document.querySelectorAll('.card[data-name]').forEach(wireCard);
  if(document.getElementById('revDlg'))
    document.querySelectorAll('.rev').forEach(wireRev);
  const fl=document.getElementById('filter');
  if(fl){
    fl.oninput=()=>{
      // instant: hide non-matching cards on THIS page right away…
      const nq=normName(fl.value);
      document.querySelectorAll('.card[data-name]').forEach(c=>
        c.style.display=normName(c.dataset.name).includes(nq)?'':'none');
      // …then reconcile with the server (matches on other pages, pager, URL)
      clearTimeout(fl._t);fl._t=setTimeout(()=>{
      const u=new URL(location.href);
      if(fl.value.trim())u.searchParams.set('q',fl.value.trim());
      else u.searchParams.delete('q');
      u.searchParams.delete('cp');
      window._refocusFilter=true;
      morphNavigate(u.pathname+u.search);
    },250);};
    fl.onkeydown=e=>{if(e.key==='Escape'){fl.value='';fl.oninput()}};
    if(window._refocusFilter){
      fl.focus();fl.setSelectionRange(fl.value.length,fl.value.length);
      window._refocusFilter=false;
    }
  }
}
// ── wireDialogs(): bindings inside <dialog>s — rerun when dialogs are swapped ──
function wireDialogs(){
  document.querySelectorAll('dialog').forEach(d=>
    d.onclick=e=>{if(e.target===d)d.close()});
  document.querySelectorAll('dialog .xclose,dialog .close').forEach(b=>
    b.onclick=()=>b.closest('dialog').close());
  const renameSave=document.getElementById('renameSave');
  if(renameSave){renameSave.onclick=async()=>{
    const r=await J('/api/rename',{label:document.getElementById('renameInput').value});
    if(r.ok){document.getElementById('renameDlg').close();refresh()}
    else document.getElementById('renameErr').textContent='could not rename';
  };enterClicks('renameInput','renameSave');}
  const addLookup=document.getElementById('addLookup');
  if(addLookup){
    addLookup.onclick=async()=>{
      const q=document.getElementById('addInput').value.trim();if(!q)return;
      document.getElementById('addErr').textContent='';
      document.getElementById('addPreview').innerHTML='<p class=sub>consulting Scryfall…</p>';
      const r=await J('/api/resolve',{query:q});
      if(!r.ok){document.getElementById('addPreview').innerHTML='';
        document.getElementById('addErr').textContent=(await r.json()).error||'not found';return}
      pending=await r.json();
      const printing=pending.set_code?` <span class=badge>${X(pending.set_code.toUpperCase())} #${X(pending.collector_number)}</span>`:'';
      document.getElementById('addPreview').innerHTML=
        `<div class=cardhead>`+(pending.image?`<img class=cardimg src="${X(pending.image)}" alt="">`:'')+
        `<div><h3>${X(pending.name)}${printing}</h3>`+
        (pending.usd?`<div class=price>$${(+pending.usd).toFixed(2)} <span class=via>Scryfall market</span></div>`:'')+
        `</div></div>`+
        (pending.chart||'<p class=nodata>Local history arrives after tonight’s ingest.</p>')+
        (pending.sites||'')+
        `<div class=tgtedit><label for=addTarget>target</label>`+
        `<input id=addTarget type=text placeholder="e.g. 5, low, -20%" style="width:9rem">`+
        `<label for=addNote>note</label><input id=addNote type=text maxlength=200 `+
        `placeholder="e.g. deck name"></div>`+
        `<p class=hint style="margin-left:0">A number is a fixed target; “low” follows the historic low `+
        `(“low-10%” stays 10% under it); “-20%” means 20% under today’s price.</p>`;
      document.getElementById('addGo').style.display='';
    };
    enterClicks('addInput','addLookup');
    document.getElementById('addGo').onclick=async()=>{
      const t=document.getElementById('addTarget');
      const n=document.getElementById('addNote');
      const r=await J('/api/add',{name:pending.name,set_code:pending.set_code,
          collector_number:pending.collector_number,
          target:t&&t.value.trim()!==''?t.value.trim():null,
          note:n&&n.value.trim()!==''?n.value.trim():null});
      if(!r.ok){document.getElementById('addErr').textContent=(await r.json()).error||'could not add';return}
      const d=await r.json();
      if(d.backfilling)document.getElementById('addPreview').innerHTML+=
        '<p class=sub>added ✓ — pulling 90 days of history from the '+
        'cached price data; it appears within a minute or two.</p>';
      setTimeout(()=>{document.getElementById('addDlg').close();refresh()},
                 d.backfilling?1500:0);
    };
  }
  const boughtBtn=document.getElementById('boughtBtn');
  if(boughtBtn)boughtBtn.onclick=async()=>{
    const r=await J('/api/bought',{entry_id:+boughtBtn.dataset.entry,
                                   bought:!boughtBtn.dataset.bought});
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
      const r=await J('/api/remove',{entry_id:+removeBtn.dataset.entry});
      if(r.ok){document.getElementById('cardDlg').close();refresh()}
    };
  }
  wireTargetEditor();
  wireExport();
  wireImport();
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
  out.innerHTML='<p class=sub>minting your copy…</p>';
  const r=await J('/api/fork',{mode:'fork'});
  if(!r.ok){out.innerHTML='<p class=err>could not create a copy</p>';return}
  const d=await r.json();
  out.innerHTML=`<div class=secret>Your key (screenshot this — it is shown once):<br>`+
    `<b>${X(d.passphrase)}</b></div>`+
    `<p class=sub>That key is how you edit your list — open `+
    `<a href="${X(d.page)}">your page</a> or tell it to Claude in chat. `+
    `Your copy starts with everything on this board and is yours alone.</p>`;
}
/* ── the forge page: mint a list, optionally seeded from pasted cards ── */
async function forgeFlow(){
  const btn=document.getElementById('forgeGo'),out=document.getElementById('forgeOut');
  btn.disabled=true;out.hidden=false;out.innerHTML='<p class=sub>forging…</p>';
  const r=await fetch(U('/api/create'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({label:document.getElementById('newLabel').value.trim(),
      decklist:document.getElementById('newCards').value,
      note:document.getElementById('newNote').value.trim(),
      target:document.getElementById('newTarget').value.trim(),
      pin_printings:document.getElementById('newPin').checked})});
  btn.disabled=false;
  if(!r.ok){out.innerHTML='<p class=err>'+X((await r.json().catch(()=>({}))).error||'could not create the list')+'</p>';return}
  const d=await r.json();
  const copy=(v,label)=>`<button class=chip data-copy="${X(v)}" title="Copy ${label}">${label} ⧉</button>`;
  let h=`<div class=secret>⚠ shown once — your passphrase:<br><b>${X(d.passphrase)}</b> `+copy(d.passphrase,'copy')+`</div>`+
    `<p class=sub>The passphrase <i>is</i> the list: anyone holding it can edit. `+
    `Keep it somewhere safe or tell it to Claude in chat.</p>`+
    `<p class=forgelinks><b>Board</b> <a href="${X(d.page)}">${X(d.page)}</a> ${copy(d.page,'copy link')}<br>`+
    `<b>Connector URL</b> for Claude <code>${X(d.url)}</code> ${copy(d.url,'copy')}<br>`+
    `<b>Read-only share</b> <code>${X(d.share_code)}</code> · <a href="${X(d.share)}">${X(d.share)}</a> ${copy(d.share,'copy link')}</p>`;
  if(d.import){const i=d.import;
    h+=`<p class=sub><span class=ok>Added ${i.added.length} card(s)</span>`+(i.updated.length?`, ${i.updated.length} merged`:'')+
      (i.skipped.length?` · <span class=err>${i.skipped.length} not recognised: ${X(i.skipped.slice(0,12).join(', '))}</span>`:'')+'.</p>';
    if(i.error)h+=`<p class=err>${X(i.error)}</p>`;}
  h+=`<div class=btnrow><a class="act primary" style="text-decoration:none" href="${X(d.page)}">Open the board →</a></div>`;
  out.innerHTML=h;out.hidden=false;
  out.querySelectorAll('[data-copy]').forEach(copyable);
  document.getElementById('forgeForm').hidden=true;
}
/* MTGStocks refuses this server's hosting provider outright, so the badge
   cannot be rendered server-side. Your browser can reach them, and it is
   already looking at the card, so it resolves the print id once, shows the
   link, remembers it locally, and reports it back for everyone else. */
const SAPI='https://api.mtgstocks.com', SKEY='mfStocks:';
function stocksUrl(id,slug){
  const head=id+'-';
  const tail=slug?('-'+(slug.indexOf(head)===0?slug.slice(head.length):slug)):'';
  return 'https://www.mtgstocks.com/prints/'+id+tail;
}
async function stocksLookup(name,set){
  const front=name.split(' // ')[0].trim();
  try{
    const r=await fetch(SAPI+'/search/autocomplete/'+encodeURIComponent(front));
    if(!r.ok)return null;
    const list=await r.json();
    const want=[name.toLowerCase(),front.toLowerCase()];
    const hit=(Array.isArray(list)?list:[]).find(
      x=>x&&typeof x.id==='number'&&want.indexOf((x.name||'').toLowerCase())>=0);
    if(!hit)return null;
    let best={id:hit.id,slug:hit.slug||''};
    if(set){                       /* pinned printing: siblings carry the set code */
      const p=await fetch(SAPI+'/prints/'+hit.id);
      if(p.ok){
        const sets=(await p.json()).sets;
        const m=(Array.isArray(sets)?sets:[])
          .filter(s=>s&&(s.abbreviation||'').toUpperCase()===set)
          .sort((a,b)=>(a.foil?1:0)-(b.foil?1:0))[0];   /* a set, not a finish */
        if(m&&typeof m.id==='number')best={id:m.id,slug:m.slug||''};
      }
    }
    return {id:best.id,slug:best.slug,url:stocksUrl(best.id,best.slug)};
  }catch(e){return null;}          /* blocked or offline: no badge, no noise */
}
async function maybeStocks(card){
  const host=document.getElementById('siteHost');
  if(!host||(card.dataset.sites||'').indexOf('mtgstocks.com')>=0)return;
  const name=card.dataset.name,set=(card.dataset.set||'').toUpperCase();
  const ck=SKEY+name.toLowerCase()+'|'+set;
  let url=null;
  try{url=localStorage.getItem(ck);}catch(e){}
  if(!url){
    const hit=await stocksLookup(name,set);
    if(!hit)return;
    url=hit.url;
    try{localStorage.setItem(ck,url);}catch(e){}
    J('/api/mtgstocks',{card_name:name,set_code:set,print_id:hit.id,slug:hit.slug}).catch(()=>{});
  }
  /* The dialog is shared, so a slow lookup must not land on another card. */
  if(document.getElementById('cardTitle').textContent!==name)return;
  const box=host.querySelector('.sites');
  if(!box||box.querySelector('a[href*="mtgstocks.com"]'))return;
  const a=document.createElement('a');
  a.href=url;a.target='_blank';a.rel='noopener';a.textContent='MTGStocks';
  box.insertBefore(a,box.lastElementChild);
  card.dataset.sites=host.innerHTML;
}
/* ── card modal ── */
let M=null;                                   // the card the modal is showing
function kpi(id,val,lbl,cls){const el=document.getElementById(id);if(!el)return;
  el.querySelector('b').textContent=val;el.querySelector('span').textContent=lbl;
  el.className='kpi'+(cls?' '+cls:'');}
function renderShops(){
  const row=document.getElementById('shopRow');if(!row)return;
  const shops=M.shops||{};let h='<span class=lbl>markets</span>';
  const best=M.basisProv;
  for(const s of ['tcgplayer','cardkingdom','manapool','cardmarket']){
    const p=shops[s];const on=M.view===s?' on':'';
    const cls=(M.shop===s?' pinned':'')+(best===s&&!M.shop?' best':'')+(p?'':' none');
    h+=`<button class="shopchip${on}${cls}" data-shop="${s}"${p?'':' disabled'} `+
       `title="${p?X(SHOPN[s])+' on '+X(p.date):'no price at '+X(SHOPN[s])}">`+
       `<span class=n>${X(SHOPN[s])}</span>${p?money(p.price,SHOPC[s]):'—'}</button>`;
  }
  row.innerHTML=h;
  row.querySelectorAll('.shopchip').forEach(b=>b.onclick=()=>viewShop(b.dataset.shop));
}
async function viewShop(shop){
  if(!M||M.view===shop)return;
  const eid=M.entry;
  const r=await fetch(U('/api/card/'+encodeURIComponent(KEY)+'/'+eid+'?shop='+encodeURIComponent(shop)));
  if(!r.ok||!M||M.entry!==eid)return;
  const d=await r.json();
  M.view=shop;
  document.getElementById('chartHost').innerHTML=d.chart||'<p class=nodata>Not enough history yet.</p>';
  document.getElementById('snapHost').innerHTML=d.tail||'';
  document.getElementById('cardSub').textContent=M.sub0+' · viewing '+SHOPN[shop];
  kpi('kNow',money(d.current,d.cur),'now · '+SHOPN[shop]);
  kpi('kLow',money(d.low,d.cur),'lowest · '+(d.low_date||'—'));
  if(d.pts&&d.pts.length)armCrosshair(d.pts,d.cur);
  renderShops();
}
function wireCard(card){
  keyable(card);
  card.onclick=()=>{
    const cardDlg=document.getElementById('cardDlg'),d=card.dataset;
    const shops=d.shops?JSON.parse(d.shops):{};
    M={entry:+d.entry,name:d.name,set:d.set||'',shop:d.shop||'',mode:d.mode||'fixed',
       pct:+(d.pct||0),target:d.target||'',note:d.note||'',cur:d.cur||'$',
       current:d.current!==''?+d.current:null,low:d.low!==''?+d.low:null,lowDate:d.lowdate||'',
       ref:d.ref!==''?+d.ref:null,eff:d.eff!==''?+d.eff:null,hit:!!d.hit,
       shops:shops,basisProv:d.basis||'',sub0:d.sub,view:d.shop||'basis'};
    document.getElementById('cardTitle').textContent=d.name;
    document.getElementById('cardSub').textContent=d.sub;
    const im=document.getElementById('cardImg');
    if(im){if(d.img){im.hidden=false;im.src=d.img;im.alt=d.name+' card face';}
      else{im.hidden=true;im.removeAttribute('src');}}
    document.getElementById('chartHost').innerHTML=d.chart||'<p class=nodata>Not enough history yet.</p>';
    document.getElementById('snapHost').innerHTML=d.tail||'';
    document.getElementById('siteHost').innerHTML=d.sites||'';
    const where=M.shop?SHOPN[M.shop]+' (pinned)':(M.basisProv?SHOPN[M.basisProv]:'—');
    kpi('kNow',money(M.current,M.cur),'now · '+where);
    kpi('kLow',money(M.low,M.cur),'lowest · '+(M.lowDate||'—'));
    const tl=M.eff!=null?money(M.eff,M.cur):(M.mode==='low'?'—':'none');
    kpi('kTarget',tl,M.mode==='low'?'target · follows the low':'target',M.hit?'hit':'');
    const d30=d.d30!==''?+d.d30:null;
    kpi('kD30',d30==null?'—':(d30<0?'▼':d30>0?'▲':'·')+money(Math.abs(d30),M.cur).slice(0),'30-day change');
    renderShops();
    const te=document.getElementById('tgtEdit');
    if(te){te.dataset.entry=d.entry;
      document.getElementById('noteInput').value=M.note;
      document.getElementById('tgtErr').textContent='';
      document.getElementById('tgtCur').textContent=M.cur;
      const bs=document.getElementById('basisSel');if(bs)bs.value=M.shop;
      document.getElementById('scCur').textContent=money(M.current,M.cur);
      document.getElementById('scLow').textContent=money(M.low,M.cur);
      document.getElementById('shortcuts').hidden=true;
      document.getElementById('scMore').textContent='More shortcuts ▾';
      const fl=document.getElementById('followLow');fl.checked=M.mode==='low';
      const fp=document.getElementById('followPct');
      fp.querySelectorAll('option[data-custom]').forEach(o=>o.remove());
      if(![...fp.options].some(o=>+o.value===M.pct)){   /* a rule typed as low-15% keeps its 15 */
        const o=document.createElement('option');o.value=String(M.pct);o.dataset.custom='1';
        o.textContent=M.pct?`stay ${M.pct}% below`:'match it';fp.append(o);}
      fp.value=String(M.pct);
      document.getElementById('tgtInput').value=M.mode==='fixed'?M.target:'';
      syncFollow();}
    const bb=document.getElementById('boughtBtn'),rb=document.getElementById('removeBtn');
    if(bb){bb.dataset.entry=d.entry;
      bb.textContent=d.bought?'Not bought after all':'Bought ✓';
      bb.dataset.bought=d.bought||'';}
    if(rb){rb.dataset.entry=d.entry;
      document.getElementById('rmConfirm').style.display='none';rb.style.display='';}
    const pts=d.pts?JSON.parse(d.pts):[];
    if(pts.length)armCrosshair(pts,M.cur);
    cardDlg.showModal();
    maybeStocks(card);
    /* the board is showing one market: open the chart on that market too */
    if(SHOP!=='all'&&SHOP!==M.view&&shops[SHOP])viewShop(SHOP);
  };
}
/* ── target editor: fixed number or follow-the-low rule ── */
function syncFollow(){
  const fl=document.getElementById('followLow'),inp=document.getElementById('tgtInput');
  const pct=+document.getElementById('followPct').value;
  const hint=document.getElementById('followHint');
  if(fl.checked){
    inp.disabled=true;
    const ref=M&&M.ref!=null?M.ref:null;
    inp.value=ref!=null?(ref*(1-pct/100)).toFixed(2):'';
    inp.placeholder=ref!=null?'':'needs 2 days of history';
    hint.textContent=ref!=null
      ?`Lowest before today: ${money(ref,M.cur)}${pct?` → ${pct}% under it is ${money(ref*(1-pct/100),M.cur)}`:''}. Each new low moves the target.`
      :'Two days of price history are needed before the low can be followed; the rule is saved now and starts working then.';
  }else{inp.disabled=false;inp.placeholder='none';hint.textContent='';}
}
function setFixed(v){
  const fl=document.getElementById('followLow');fl.checked=false;syncFollow();
  document.getElementById('tgtInput').value=v==null?'':(Math.max(0,v)).toFixed(2);
}
function wireTargetEditor(){
  const te=document.getElementById('tgtEdit');if(!te)return;
  const fl=document.getElementById('followLow');
  fl.onchange=syncFollow;
  document.getElementById('followPct').onchange=()=>{if(!fl.checked){fl.checked=true}syncFollow()};
  document.getElementById('scMore').onclick=()=>{const s=document.getElementById('shortcuts');
    s.hidden=!s.hidden;document.getElementById('scMore').textContent=s.hidden?'More shortcuts ▾':'Fewer shortcuts ▴'};
  const cur=()=>M&&M.current!=null?M.current:null;
  const low=()=>M&&M.low!=null?M.low:null;      /* the KPI's number: lowest ever, today included */
  document.getElementById('scBeat').onclick=()=>{const c=cur();if(c!=null)setFixed(Math.floor((c-0.01)*100)/100)};
  document.getElementById('scMatchLow').onclick=()=>{const l=low();if(l!=null)setFixed(l)};
  te.querySelectorAll('.sc[data-from]').forEach(b=>b.onclick=()=>{
    const base=b.dataset.from==='cur'?cur():low();if(base==null)return;
    setFixed(base*(1-(+b.dataset.pct)/100));});
  te.querySelectorAll('.pctin').forEach(i=>i.onchange=()=>{
    const base=i.dataset.from==='cur'?cur():low();const p=+i.value;
    if(base==null||!(p>=0&&p<100))return;setFixed(base*(1-p/100));i.value='';});
  const save=document.getElementById('tgtSave');
  save.onclick=async()=>{
    const eid=+te.dataset.entry;
    const mode=fl.checked?'low':'fixed';
    const pct=+document.getElementById('followPct').value;
    const tv=document.getElementById('tgtInput').value.trim();
    const nv=document.getElementById('noteInput').value.trim();
    const bs=document.getElementById('basisSel');
    const calls=[];                       // only send what actually changed
    const tgtChanged=mode!==M.mode||(mode==='low'&&pct!==M.pct)||(mode==='fixed'&&tv!==M.target);
    if(tgtChanged)calls.push(['/api/target',{entry_id:eid,
      target_price:mode==='fixed'?(tv===''?null:+tv):null,target_mode:mode,target_pct:pct}]);
    if(nv!==M.note)calls.push(['/api/note',{entry_id:eid,note:nv}]);
    if(bs&&bs.value!==M.shop)calls.push(['/api/shop',{entry_id:eid,shop:bs.value||null}]);
    if(!calls.length){document.getElementById('cardDlg').close();return}
    save.disabled=true;
    for(const [ep,payload] of calls){
      const r=await J(ep,payload);
      if(!r.ok){save.disabled=false;
        document.getElementById('tgtErr').textContent=(await r.json().catch(()=>({}))).error||'could not save';return}
    }
    save.disabled=false;
    document.getElementById('cardDlg').close();refresh();
  };
  enterClicks('tgtInput','tgtSave');enterClicks('noteInput','tgtSave');
}
/* ── export: plain "1 Card Name" lines for TCGplayer, Mana Pool, Card Kingdom ── */
let XD=[];
function exportRows(){
  const mode=(document.querySelector('input[name=xsel]:checked')||{}).value||'all';
  const withBought=document.getElementById('xBought').checked;
  return XD.filter(e=>{
    if(e.bought&&!withBought)return false;
    if(mode==='hit')return e.hit;
    if(mode==='pick')return e.picked;
    return true;});
}
function exportLines(rows,store){
  const printing=document.getElementById('xPrinting').checked;
  const targets=!store&&document.getElementById('xTargets').checked;
  return rows.map(e=>{
    let l='1 '+e.name;
    if(printing&&e.set){l+=store==='tcg'||store==='mp'?` [${e.set}] ${e.cn}`:` (${e.set}) ${e.cn}`}
    if(targets&&e.spec)l+=' @ '+e.spec;
    return l;});
}
function renderExport(){
  const rows=exportRows();
  const mode=(document.querySelector('input[name=xsel]:checked')||{}).value||'all';
  const list=document.getElementById('xList');
  const withBought=document.getElementById('xBought').checked;
  list.innerHTML=XD.filter(e=>withBought||!e.bought).map(e=>
    `<label><input type=checkbox data-id="${e.id}" ${rows.includes(e)?'checked':''}>`+
    `<span>${X(e.name)}${e.set?` <span class=badge>${X(e.set)} #${X(e.cn)}</span>`:''}</span>`+
    (e.hit?'<span class=hitmk>\u{1F3AF} at target</span>':'')+(e.bought?'<span class=bmk>✓ bought</span>':'')+
    `<span class=pr>${money(e.price,e.cur)}</span></label>`).join('')||'<p class=nodata>nothing to export</p>';
  list.querySelectorAll('input').forEach(i=>i.onchange=()=>{
    const e=XD.find(x=>x.id===+i.dataset.id);if(e)e.picked=i.checked;
    if(mode!=='pick'){XD.forEach(x=>{x.picked=rows.includes(x)});if(e)e.picked=i.checked;
      document.querySelector('input[name=xsel][value=pick]').checked=true;}
    renderExport();});
  document.getElementById('xText').value=exportLines(rows).join('\n');
  document.getElementById('xCount').textContent=rows.length+' card'+(rows.length===1?'':'s');
  document.querySelectorAll('#exportDlg .btnrow button').forEach(b=>b.disabled=!rows.length);
  document.getElementById('xHint').textContent='';
}
function openExport(){
  const raw=document.getElementById('exportData');
  XD=raw?JSON.parse(raw.textContent):[];
  XD.forEach(e=>{e.picked=false});
  const hits=XD.filter(e=>e.hit&&!e.bought).length;
  document.getElementById('xHitCnt').textContent=hits;
  document.getElementById('xAllCnt').textContent=XD.filter(e=>!e.bought).length;
  document.querySelector('input[name=xsel][value='+(hits?'hit':'all')+']').checked=true;
  renderExport();
  document.getElementById('exportDlg').showModal();
}
function wireExport(){
  const dlg=document.getElementById('exportDlg');if(!dlg)return;
  dlg.querySelectorAll('input[name=xsel],#xBought,#xPrinting,#xTargets').forEach(i=>i.onchange=renderExport);
  const hint=t=>document.getElementById('xHint').textContent=t;
  document.getElementById('xCopy').onclick=e=>{
    navigator.clipboard.writeText(document.getElementById('xText').value);flash(e.currentTarget,'copied ✓')};
  document.getElementById('xTcg').onclick=()=>{
    const lines=exportLines(exportRows(),'tcg');
    window.open(CFG.tcg+encodeURIComponent(lines.join('||')),'_blank','noopener');
    hint('Opened TCGplayer Mass Entry with '+lines.length+' card(s).');};
  document.getElementById('xCk').onclick=()=>{
    const lines=exportLines(exportRows(),'ck');
    window.open(CFG.ck+encodeURIComponent(lines.join('\n')),'_blank','noopener');
    hint('Opened the Card Kingdom deck builder with '+lines.length+' card(s).');};
  document.getElementById('xMp').onclick=e=>{
    const lines=exportLines(exportRows(),'mp');
    navigator.clipboard.writeText(lines.join('\n'));
    window.open(CFG.mp,'_blank','noopener');
    hint('Mana Pool has no link-in, so the list is on your clipboard — paste it into the box on the page that just opened.');
    flash(e.currentTarget,'copied ✓');};
}
/* ── import: paste lines, optional "@ target", into this list ── */
function wireImport(){
  const go=document.getElementById('imGo');if(!go)return;
  go.onclick=async()=>{
    const text=document.getElementById('imText').value;
    const err=document.getElementById('imErr'),out=document.getElementById('imOut');
    err.textContent='';if(!text.trim()){err.textContent='paste at least one card';return}
    go.disabled=true;out.innerHTML='<p class=sub>checking names with Scryfall…</p>';
    const r=await J('/api/import',{decklist:text,
      note:document.getElementById('imNote').value.trim()||null,
      target:document.getElementById('imTarget').value.trim()||null,
      pin_printings:document.getElementById('imPin').checked});
    go.disabled=false;
    const d=await r.json().catch(()=>({}));
    if(!r.ok){out.innerHTML='';err.textContent=d.error||'import failed';return}
    let h=`<p class=ok>Added ${d.added.length} card(s)`+(d.updated.length?`, ${d.updated.length} already watched (target/note updated)`:'')+'.</p>';
    if(d.skipped.length)h+=`<p class=err>Not recognised by Scryfall: ${X(d.skipped.join(', '))}</p>`;
    if(d.unpriced&&d.unpriced.length)h+=`<p class=err>No price to take a percent off for: ${X(d.unpriced.join(', '))} (added without a target)</p>`;
    if(d.backfilling)h+='<p class=sub>Fetching price history now — it appears within a minute or two.</p>';
    out.innerHTML=h;
    if(!d.skipped.length&&!(d.unpriced||[]).length)setTimeout(()=>{document.getElementById('importDlg').close();refresh()},d.backfilling?1800:600);
    else refresh();
  };
}
function wireRev(r){
  keyable(r);r.onclick=async()=>{
    const seq=r.dataset.seq,revDlg=document.getElementById('revDlg');
    document.getElementById('revTitle').textContent='Revision #'+seq;
    document.getElementById('revBody').innerHTML='<p class=sub>consulting the ledger…</p>';
    document.getElementById('forkOut').innerHTML='';
    revDlg.showModal();
    const res=await fetch(U('/api/revision/'+encodeURIComponent(KEY)+'/'+seq));
    if(!res.ok){document.getElementById('revBody').textContent='Could not read revision.';return}
    const d=await res.json();
    let h='<table class=snap><tr><th>Card</th><th>Printing</th><th>Target</th><th>Note</th></tr>';
    if(!d.entries.length)h+='<tr><td colspan=4><i>empty at this revision</i></td></tr>';
    for(const e of d.entries){
      const t=e.target_mode==='low'?('historic low'+(e.target_pct?' −'+e.target_pct+'%':''))
             :(e.target_price!=null?'$'+(+e.target_price).toFixed(2):'—');
      h+=`<tr><td>${X(e.card_name)}</td><td class=num>${e.set_code?X(e.set_code)+' #'+X(e.collector_number):'cheapest'}</td>`+
         `<td class=num>${X(t)}</td><td>${X(e.note||'')}</td></tr>`;
    }
    document.getElementById('revBody').innerHTML=h+'</table>';
    document.getElementById('forkBtn').dataset.seq=seq;
    const rec=document.getElementById('recoverBtn');
    if(rec)rec.dataset.seq=seq;
  };
}
async function doFork(mode,seq){
  const res=await J('/api/fork',{at_seq:+seq,mode});
  const out=document.getElementById('forkOut');
  if(!res.ok){out.textContent='Fork failed: '+await res.text();return}
  const d=await res.json();
  out.innerHTML=`<div class=secret>⚠ shown once — passphrase: <b>${X(d.passphrase)}</b><br>`+
    `page: <a href="${X(d.page)}">${X(d.page)}</a> · share: ${X(d.share_code)}</div>`+
    (mode==='recover'?'<p class=sub>The current list is now marked superseded.</p>':'');
}
function armCrosshair(pts,cur){
  const svg=document.querySelector('#chartHost svg');if(!svg)return;
  cur=cur||CUR;
  const tip=document.getElementById('tip');
  const ns='http://www.w3.org/2000/svg';
  const vline=document.createElementNS(ns,'line');
  vline.setAttribute('stroke','var(--overlay)');vline.setAttribute('stroke-dasharray','3 3');
  const dot=document.createElementNS(ns,'circle');
  dot.setAttribute('r','4');dot.setAttribute('fill','var(--blue)');
  dot.setAttribute('stroke','var(--base)');dot.setAttribute('stroke-width','2');
  dot.setAttribute('cx','-9');dot.setAttribute('cy','-9');   /* parked until the pointer arrives */
  vline.setAttribute('x1','-9');vline.setAttribute('x2','-9');
  svg.append(vline,dot);
  const lo=Math.min(...pts.map(p=>p[1])),hi=Math.max(...pts.map(p=>p[1]));
  const pad=(hi-lo)||1;
  const show=e=>{
    const r=svg.getBoundingClientRect();
    const fx=Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));
    const i=Math.round(fx*(pts.length-1));
    const x=CPAD+(CW-2*CPAD)*(pts.length>1?i/(pts.length-1):.5);
    const y=CH-CPADB-(CH-CPAD-CPADB)*((pts[i][1]-lo)/pad);
    vline.setAttribute('x1',x);vline.setAttribute('x2',x);
    vline.setAttribute('y1',CPAD);vline.setAttribute('y2',CH-CPADB);
    dot.setAttribute('cx',x);dot.setAttribute('cy',y);
    tip.style.display='block';
    tip.style.left=(x/CW*r.width)+'px';tip.style.top=(y/CH*r.height)+'px';
    tip.textContent=pts[i][0]+' · '+cur+pts[i][1].toFixed(2);
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


def _coords(points, w, h, pad, padb=None):
    padb = pad if padb is None else padb
    lo = min(p[1] for p in points)
    hi = max(p[1] for p in points)
    rng = (hi - lo) or 1.0
    n = len(points)
    out = []
    for i, (_, v) in enumerate(points):
        x = pad + (w - 2 * pad) * (i / (n - 1) if n > 1 else 0.5)
        y = h - padb - (h - pad - padb) * ((v - lo) / rng)
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
    xy, lo, hi = _coords(points, CW, CH, CPAD, CPADB)
    bottom = CH - CPADB
    pl = " ".join(f"{x},{y}" for x, y in xy)
    grid = "".join(f'<line class="gridline" x1="{CPAD}" y1="{y}" x2="{CW - CPAD}" y2="{y}"/>'
                   for y in (CPAD, (CPAD + bottom) / 2, bottom))
    bline = ""
    if bought_at and points[0][0] <= bought_at <= points[-1][0]:
        # nearest point index for the purchase date → vertical marker
        bi = max(i for i, (d, _) in enumerate(points) if d <= bought_at)
        bx = xy[bi][0]
        bline = (f'<line x1="{bx}" y1="{CPAD}" x2="{bx}" y2="{bottom}"'
                 f' stroke="var(--lavender)" stroke-width="1.5" stroke-dasharray="2 4"/>'
                 f'<text class="axis" x="{bx + 4}" y="{CPAD + 12}"'
                 f' fill="var(--lavender)">bought {esc(bought_at)}</text>')
    tline = ""
    if target is not None and lo <= target <= hi:
        rng = (hi - lo) or 1.0
        ty = round(bottom - (bottom - CPAD) * ((target - lo) / rng), 1)
        tline = (f'<line class="targetline" x1="{CPAD}" y1="{ty}"'
                 f' x2="{CW - CPAD}" y2="{ty}"/>'
                 f'<text class="axis" x="{CW - CPAD}" y="{ty - 5}"'
                 f' text-anchor="end" fill="var(--peach-text)">target {cur}{target:.2f}</text>')
    return (
        f'<svg viewBox="0 0 {CW} {CH}" style="width:100%;height:auto;touch-action:pan-y" role="img">'
        f'<title>{esc(name)} — 90 day price history</title>{grid}{tline}{bline}'
        f'<text class="axis" x="{CPAD}" y="{CPAD - 6}">{cur}{hi:.2f}</text>'
        f'<text class="axis" x="{CPAD}" y="{bottom + 14}">{cur}{lo:.2f}</text>'
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


def _slug(name: str) -> str:
    return "".join(ch for ch in name.lower().replace(" ", "-")
                   if ch.isalnum() or ch == "-")


def _site_links(entry, db=None) -> str:
    """External hop-out badges. Pinned printings deep-link where possible.

    MTGStocks is the odd one out: it has no name-addressable URL, only
    `/prints/<id>`, so its badge appears only once that id is cached (see
    `watchlist/mtgstocks.py`). Omitted rather than linked to a search page
    that 404s. Mana Pool numbers printings its own way, so its badge links
    the card page, which lists every printing."""
    name = entry["card_name"]
    front = name.split(" // ")[0].strip()
    q = urllib.parse.quote(name)
    slug = _slug(front)
    if entry.get("set_code") and entry.get("collector_number"):
        scry = (f"https://scryfall.com/card/{entry['set_code'].lower()}/"
                f"{urllib.parse.quote(entry['collector_number'])}")
    else:
        scry = f"https://scryfall.com/search?q={urllib.parse.quote(f'!\"{name}\"')}"
    stocks = mtgstocks.cached_url(db, name, entry.get("set_code"))
    ck = ("https://www.cardkingdom.com/catalog/search?search=header&filter[name]="
          + urllib.parse.quote_plus(front))
    links = [
        ("Scryfall", scry),
        ("EDHREC", f"https://edhrec.com/cards/{slug}"),
        *([("MTGStocks", stocks)] if stocks else []),
        ("TCGplayer", f"https://www.tcgplayer.com/search/magic/product?q={q}"),
        ("Card Kingdom", ck),
        ("Mana Pool", f"https://manapool.com/card/{slug}"),
    ]
    return '<div class="sites">' + "".join(
        f'<a href="{esc(u)}" target="_blank" rel="noopener">{n}</a>'
        for n, u in links) + "</div>"


def _image_url(db, entry, uuid=None) -> str:
    """The card face, from Scryfall: the cheapest printing's when known,
    else the name's default printing. Fetched by the browser on modal open."""
    sid = watchlist_db.scryfall_id_for(db, entry, uuid)
    if sid:
        return f"https://api.scryfall.com/cards/{sid}?format=image&version=normal"
    front = entry["card_name"].split(" // ")[0].strip()
    return ("https://api.scryfall.com/cards/named?exact="
            + urllib.parse.quote(front) + "&format=image&version=normal")


def _hit(entry, s, target) -> bool:
    return watchlist_db.is_hit(entry, s, target)


class _Card:
    """Everything the board and the modal know about one entry, computed
    once: basis summary (pinned shop, else cheapest USD), display summary
    for the board's shop, effective target, hit state."""

    def __init__(self, db, entry, shop):
        self.e = entry
        self.base = watchlist_db.basis_summary(db, entry)
        self.disp = (self.base if shop == ALL
                     else watchlist_db.entry_price_summary(db, entry, provider=shop))
        self.target = watchlist_db.effective_target(db, entry, self.base)
        self.bought = bool(entry.get("bought_at"))
        self.hit = _hit(entry, self.base, self.target)
        self.cur = watchlist_db.entry_currency(entry)          # basis currency
        self.dcur = self.cur if shop == ALL else SHOPS[shop]   # display currency


def _card_html(db, c: _Card, idx, shop, filling=False):
    """One board card. Price shown is the display shop's (or the basis when
    the board shows "all markets"); hit state is the basis' and never flips
    with the dropdown."""
    entry, s, base = c.e, c.disp, c.base
    cur = c.dcur
    uuids = watchlist_db.uuids_for_entry(db, entry)
    finish = (s or base or {}).get("finish", "normal")
    providers = watchlist_db.entry_shops(entry) if shop == ALL else shop
    points = []
    if s:
        series = watchlist_db.price_series(db, uuids, days=90, provider=providers,
                                           finish=finish)
        points = series["points"] if series else []
    name = esc(entry["card_name"])
    bought_at = entry.get("bought_at")
    badge = (f'<span class="badge">{esc(entry["set_code"])} '
             f'#{esc(entry["collector_number"] or "")}</span>'
             if entry.get("set_code") else "")
    note = f'<p class="note">{esc(entry["note"] or "")}</p>'
    if s:
        foil = ' <small>(foil)</small>' if s.get("finish") == "foil" else ""
        via = ""
        if shop == ALL and s.get("provider"):
            pin = "📌 " if entry.get("shop") else "via "
            via = f' <span class="via">{pin}{esc(SHOP_NAMES[s["provider"]])}</span>'
        price = f'<div class="price">{cur}{s["current"]:.2f}{foil}{via}</div>'
        deltas = (f'<div class="deltas">{_delta_pct(s["d7"], s["current"], "7d", cur)}'
                  f'{_delta_pct(s["d30"], s["current"], "30d", cur)}</div>')
    else:
        price = ('<div class="nodata skel">fetching price history…</div>'
                 if filling else
                 '<div class="nodata">no prices yet</div>')
        deltas = ""
    rule = watchlist_targets.describe(entry, c.cur, effective=c.target)
    target = ""
    if bought_at:
        target = f'<div class="boughtnote">✓ bought {esc(bought_at)}</div>'
    elif rule:
        basis_hint = (f" ({'USD' if c.cur == '$' else 'EUR'})"
                      if cur != c.cur else "")
        if c.hit:
            target = (f'<div class="target hit">🎯 at target '
                      f'{esc(rule)}{basis_hint} — buy window</div>')
        else:
            gap = ""
            if base and c.target is not None and cur == c.cur:
                gap = f' · {c.cur}{base["current"] - c.target:.2f} above'
            elif c.target is None:
                gap = " · needs 2 days of history"
            target = f'<div class="target">target {esc(rule)}{basis_hint}{gap}</div>'
    spark_color = ("var(--overlay)" if bought_at
                   else "var(--green)" if c.hit else "var(--blue)")
    spark = _spark_svg(points, entry["card_name"], spark_color) if points else ""
    where = (f"{SHOP_NAMES[entry['shop']]} only" if entry.get("shop")
             else USD_LABEL if shop == ALL else SHOP_NAMES[shop])
    sub = (f'{entry["set_code"]} #{entry["collector_number"]}'
           if entry.get("set_code") else "cheapest printing") + f" · {where}"
    tgt_attr = (f'{entry["target_price"]:.2f}'
                if entry.get("target_price") is not None else "")
    ref = None
    if uuids:
        ref = watchlist_db.reference_low(db, uuids, watchlist_db.entry_shops(entry),
                                         finish)
    shops = {p: {"price": v[0], "date": v[1]}
             for p, v in watchlist_db.latest_by_shop(db, uuids, finish).items()}
    chart_target = c.target if cur == c.cur and not bought_at else None
    data = (f' data-name="{name}" data-sub="{esc(sub)}"'
            f' data-set="{esc((entry.get("set_code") or "").upper())}"'
            f' data-entry="{entry["entry_id"]}"'
            f' data-target="{tgt_attr}"'
            f' data-mode="{esc(entry.get("target_mode") or "fixed")}"'
            f' data-pct="{float(entry.get("target_pct") or 0):g}"'
            f' data-shop="{esc(entry.get("shop") or "")}"'
            f' data-basis="{esc((base or {}).get("provider") or "")}"'
            f' data-cur="{c.cur}"'
            f' data-current="{base["current"] if base else ""}"'
            f' data-d30="{base["d30"] if base and base["d30"] is not None else ""}"'
            f' data-low="{base["low"] if base and base.get("low") is not None else ""}"'
            f' data-lowdate="{esc((base or {}).get("low_date") or "")}"'
            f' data-ref="{ref[0] if ref else ""}"'
            f' data-eff="{c.target if c.target is not None else ""}"'
            f' data-hit="{"1" if c.hit else ""}"'
            f' data-note="{esc(entry["note"] or "")}"'
            f' data-bought="{esc(bought_at) if bought_at else ""}"'
            f' data-sites="{esc(_site_links(entry, db))}"'
            f' data-img="{esc(_image_url(db, entry, (base or {}).get("uuid")))}"'
            f' data-shops="{esc(json.dumps(shops))}"'
            f' data-pts="{esc(json.dumps(points))}"'
            f' data-chart="{esc(_big_svg(points, entry["card_name"], cur, chart_target, bought_at))}"'
            f' data-tail="{esc(_tail_table(points, cur))}"')
    cls = "card bought" if bought_at else ("card hit" if c.hit else "card")
    return (f'<article class="{cls}" '
            f'aria-label="{name} details" style="animation-delay:{idx * 45}ms"{data}>'
            f'<h3>{name}{badge}</h3>{note}{price}{deltas}{target}{spark}</article>')


def card_payload(db, entry, shop) -> dict:
    """What the modal needs to show one card at one shop (or its basis when
    `shop` is 'basis'): chart, tail table, points, current and lowest."""
    if shop == "basis":
        providers = watchlist_db.entry_shops(entry)
        cur = watchlist_db.entry_currency(entry)
    else:
        providers, cur = shop, SHOPS[shop]
    s = watchlist_db.entry_price_summary(db, entry, provider=providers)
    if not s:
        return {"chart": "", "tail": "", "pts": [], "current": None,
                "low": None, "low_date": None, "cur": cur, "provider": None}
    series = watchlist_db.price_series(
        db, watchlist_db.uuids_for_entry(db, entry), days=90,
        provider=providers, finish=s["finish"])
    points = series["points"] if series else []
    target = watchlist_db.effective_target(db, entry, s)
    same_cur = cur == watchlist_db.entry_currency(entry)
    return {"chart": _big_svg(points, entry["card_name"], cur,
                              target if same_cur and not entry.get("bought_at") else None,
                              entry.get("bought_at")),
            "tail": _tail_table(points, cur), "pts": points,
            "current": s["current"], "low": s.get("low"),
            "low_date": s.get("low_date"), "cur": cur,
            "provider": s.get("provider")}


def _pager(base, param, page, total, per, keep=""):
    """Numbered pager with windowing: ‹ Prev 1 … 4 [5] 6 … 12 Next ›.

    Sort-agnostic labels — under "cheapest" or "7d drop", page 2 isn't
    "older", it's just the next page."""
    pages = max(1, -(-total // per))
    if pages == 1:
        return ""
    page = min(max(1, page), pages)

    def link(p, label=None):
        return (f'<a class="pnum" href="{base}?{param}={p}{keep}">'
                f'{label or p}</a>')

    shown = sorted({1, pages, page - 1, page, page + 1}
                   & set(range(1, pages + 1)))
    nums, last = [], 0
    for p in shown:
        if p - last > 1:
            nums.append('<span class="gap">…</span>')
        nums.append(f'<span class="pnum cur" aria-current="page">{p}</span>'
                    if p == page else link(p))
        last = p
    prev = (link(page - 1, "‹ Prev") if page > 1
            else '<span class="pnum dis">‹ Prev</span>')
    nxt = (link(page + 1, "Next ›") if page < pages
           else '<span class="pnum dis">Next ›</span>')
    return (f'<nav class="pager" aria-label="Pages">{prev}{"".join(nums)}'
            f'{nxt}</nav>')


def _shell(row, editable, body, dialogs, cur="$", subtitle="", rightnav="",
           shop=ALL, extra=""):
    return (_shell_open(row, editable, rightnav) + _subtitle(subtitle)
            + _shell_close(row, editable, body, dialogs, cur, shop, extra))


def _subtitle(subtitle):
    return f'<p class="subtitle">{subtitle}</p>\n' if subtitle else ""


def _shell_open(row, editable, rightnav=""):
    """Head and masthead: everything a page knows from its row alone, not
    one price read. Streamed first, so the board paints its title and
    theme -- and link previews get their tags -- while the cards render."""
    label = row["label"] or "Watchlist"
    title = esc(label)
    long_cls = ' class="long"' if len(label) > 16 else ""
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
<meta property="og:site_name" content="Mystic Forge">
<meta property="og:type" content="website">
<meta property="og:title" content="{title} · a Magic price watchlist">
<meta property="og:description" content="A Magic: The Gathering price
watchlist — buy-window targets, price trends, and shareable boards, forged
in Mystic Forge.">
<meta property="og:image" content="{PUBLIC_BASE}/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
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
"""


def _shell_close(row, editable, body, dialogs, cur="$", shop=ALL, extra=""):
    """The rest of the page: body, footer, dialogs and the script."""
    cfg = {"key": row["_key"], "editable": editable, "cpad": CPAD,
           "cpadb": CPADB, "cw": CW, "ch": CH,
           "cur": cur, "prefix": PREFIX, "shop": shop,
           "shopNames": SHOP_NAMES, "shopCur": SHOPS,
           "tcg": TCG_MASSENTRY, "ck": CK_BUILDER, "mp": MP_ADDDECK}
    js = _JS.replace("__CFG__", json.dumps(cfg).replace("</", "<\\/"))
    return f"""{body}
<footer>forged in the Mystic Forge · card price watchlist ·
<a href="{PREFIX}/w/new" title="Forge a new list from pasted cards">new list</a> ·
<a href="{PREFIX}/health" title="server status">status</a></footer>
</div>
{dialogs}
{extra}
<script>{js}</script>
</body></html>"""


SORTS = (("target", "near target"), ("age", "newest"), ("price", "cheapest"),
         ("d7", "7d drop"), ("d30", "30d drop"))
_INF = float("inf")


def _norm(s: str) -> str:
    """Match-normalization: case- and punctuation-blind ("sram senior"
    matches "Sram, Senior Edificer")."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _sort_key(sort, c: _Card):
    """Ordering value, smaller first. No-signal entries sink (never above
    priced/targeted ones); bought partitioning happens outside."""
    e, base_s, disp_s = c.e, c.base, c.disp
    if sort == "age":
        return -e["entry_id"]                       # newest added first
    if sort == "price":
        return disp_s["current"] if disp_s else _INF
    if sort == "d7":
        return disp_s["d7"] if disp_s and disp_s["d7"] is not None else _INF
    if sort == "d30":
        return disp_s["d30"] if disp_s and disp_s["d30"] is not None else _INF
    # default "target": distance to target on the basis — buy windows go
    # negative, so "target met first" falls out of the same ordering.
    if base_s and c.target is not None:
        return base_s["current"] - c.target
    return _INF


def _export_data(cards) -> str:
    """The whole list (not just this page) for the export modal, as JSON."""
    rows = []
    for c in cards:
        e = c.e
        rows.append({"id": e["entry_id"], "name": e["card_name"],
                     "set": (e.get("set_code") or "").upper(),
                     "cn": e.get("collector_number") or "",
                     "hit": c.hit, "bought": c.bought,
                     "price": c.base["current"] if c.base else None,
                     "cur": c.cur, "spec": watchlist_targets.to_spec(e)})
    return ('<script type="application/json" id="exportData">'
            + json.dumps(rows).replace("</", "<\\/") + "</script>")


def _target_editor() -> str:
    """The modal's target box: a number, gg.deals-style shortcuts off the
    current price and the historic low, and the follow-the-low switch."""
    basis_opts = '<option value="">cheapest across USD markets</option>' + "".join(
        f'<option value="{s}">{SHOP_NAMES[s]}{" (€)" if SHOPS[s] == "€" else ""} only</option>'
        for s in SHOPS)
    pcts = lambda frm: "".join(  # noqa: E731
        f'<button class="sc" data-from="{frm}" data-pct="{p}">−{p}%</button>'
        for p in (10, 20, 30))
    return (
        '<div class="tgtbox" id="tgtEdit">'
        '<div class="tgtrow"><label class="head" for="tgtInput">Your target</label>'
        '<span class="money"><span id="tgtCur">$</span>'
        '<input id="tgtInput" type="number" step="0.01" min="0" placeholder="none"></span>'
        '<button class="sc" id="scBeat" title="one cent under today\'s price">Beat current</button>'
        '<button class="sc" id="scMatchLow" title="the lowest price seen so far">Match historic low</button>'
        '<button class="textlink" id="scMore">More shortcuts ▾</button></div>'
        '<div class="shortcuts" id="shortcuts" hidden>'
        f'<div class="scrow"><span class="sclbl">Current <b id="scCur"></b></span>{pcts("cur")}'
        '<input class="pctin" data-from="cur" type="number" min="0" max="99" '
        'placeholder="custom %" aria-label="custom percent under the current price"></div>'
        '<div class="scrow"><span class="sclbl">Historic low <b id="scLow"></b></span>'
        '<button class="sc" data-from="low" data-pct="0">Match</button>'
        f'{pcts("low")}'
        '<input class="pctin" data-from="low" type="number" min="0" max="99" '
        'placeholder="custom %" aria-label="custom percent under the historic low"></div>'
        '</div>'
        '<label class="switch"><input type="checkbox" id="followLow">'
        '<span>Follow the historic low — when a new low is set, the target moves to it</span>'
        '<select class="sel" id="followPct"><option value="0">match it</option>'
        '<option value="5">stay 5% below</option><option value="10">stay 10% below</option>'
        '<option value="20">stay 20% below</option><option value="30">stay 30% below</option></select></label>'
        '<p class="hint" id="followHint"></p>'
        '<div class="tgtrow"><label for="noteInput">note</label>'
        '<input id="noteInput" class="txt" type="text" maxlength="200" '
        'placeholder="e.g. Cloud deck, batch 2">'
        '<label for="basisSel">price basis</label>'
        f'<select class="sel" id="basisSel">{basis_opts}</select></div>'
        '<span class="err" id="tgtErr"></span></div>')


def _norm_shop(shop: str) -> str:
    return shop if shop in SHOPS or shop == ALL else ALL


def board_through(db, list_id: int, shop: str = ALL) -> str | None:
    """The newest price date among the board's own cards at its shops --
    the "prices through" line. Constrained to the cards on view so it is a
    seek per (shop, finish, printing) on the covering index; asked of the
    whole table it was a scan of every row at those shops."""
    shops = (watchlist_db.USD_SHOPS if _norm_shop(shop) == ALL
             else (_norm_shop(shop),))
    uuids = sorted({u for e in watchlist_db.current_entries(db, list_id)
                    for u in watchlist_db.uuids_for_entry(db, e)})
    if not uuids:
        return None
    return db.execute(
        f"SELECT MAX(date) FROM prices WHERE provider IN "
        f"({','.join('?' * len(shops))}) AND finish IN ('normal', 'foil')"
        f" AND uuid IN ({','.join('?' * len(uuids))})",
        [*shops, *uuids]).fetchone()[0]


_WAIT = '<p class="nodata" id="wait">Reading prices…</p>\n'


def render_main_head(row, editable: bool, shop: str = ALL) -> str:
    """The instant half of a board: head, masthead and a placeholder the
    body replaces. Built from the list row alone -- no price is read."""
    key = row["_key"]
    shop = _norm_shop(shop)
    base = (f"{PREFIX}/w/{esc(key)}" if editable
            else f"{PREFIX}/s/{esc(key)}")
    qshop = f"?shop={shop}" if shop != ALL else ""
    hist_title = ("Every change ever made to this list — inspect or restore any point"
                  if editable else "See every change made to this list")
    rightnav = (f'<button class="textlink" id="alerts">Alerts</button>'
                f'<a class="textlink" href="{base}/history{qshop}" '
                f'title="{hist_title}">History</a>')
    return _shell_open(row, editable, rightnav) + _WAIT


def _board_subtitle(shop, editable, filling, through) -> str:
    if through:
        freshness = f"Buy prices through {esc(through)}"
    elif filling:
        freshness = ("⏳ Fetching 90 days of price history now — first run on a "
                     "new server takes a few minutes; this page refreshes itself")
    else:
        freshness = "No price data yet"
    where = (USD_LABEL if shop == ALL else SHOP_NAMES[shop])
    basis_note = ("" if shop == ALL else
                  " · targets still judge the cheapest USD market (or a card's pinned shop)")
    ro_note = "" if editable else "Read-only view · "
    return _subtitle(f"{ro_note}{freshness} · {where} · ▼ green = cheaper · "
                     f"▲ red = pricier{basis_note}")


def render_main_error() -> str:
    """What a streamed board says when its body fails after the head has
    already gone out: the status can no longer change, so say so in-page."""
    return ('<script>document.getElementById("wait")?.remove()</script>'
            '<p class="nodata">Something went wrong reading this list\'s '
            'prices. <a href="">Try again</a>.</p></div></body></html>')


def render_main(db, row, editable: bool, cp: int = 1, shop: str = ALL,
                filling: bool = False, sort: str = "target",
                show_bought: bool = True, q: str = "") -> str:
    """The board: stat tiles + card grid, buy windows first."""
    return (render_main_head(row, editable, shop)
            + render_main_rest(db, row, editable, cp, shop, filling, sort,
                               show_bought, q))


def render_main_rest(db, row, editable: bool, cp: int = 1, shop: str = ALL,
                     filling: bool = False, sort: str = "target",
                     show_bought: bool = True, q: str = "") -> str:
    """The slow half: subtitle, stat tiles, cards, dialogs, script. Reads
    every watched card's history; render_main_head has already gone out."""
    key = row["_key"]
    shop = _norm_shop(shop)
    cur = "$" if shop == ALL else SHOPS[shop]
    base = (f"{PREFIX}/w/{esc(key)}" if editable
            else f"{PREFIX}/s/{esc(key)}")
    sort = sort if sort in dict(SORTS) else "target"
    q = (q or "").strip()[:80]
    qq = urllib.parse.quote(q)
    # view-state query string, minus defaults; cp handled by the pager
    state = [p for p in (
        f"sort={sort}" if sort != "target" else "",
        f"shop={shop}" if shop != ALL else "",
        "" if show_bought else "bought=hide",
        f"q={qq}" if q else "") if p]

    def _url(**over):
        parts = [p for p in (
            f"sort={over.get('sort', sort)}" if over.get('sort', sort) != "target" else "",
            f"shop={over.get('shop', shop)}" if over.get('shop', shop) != ALL else "",
            "" if over.get('show_bought', show_bought) else "bought=hide",
            f"q={qq}" if q else "") if p]
        return base + ("?" + "&".join(parts) if parts else "")

    keep = "".join(f"&{p}" for p in state)
    qshop = f"?shop={shop}" if shop != ALL else ""
    with watchlist_db.envelope_memo():
        subtitle = _board_subtitle(shop, editable, filling,
                                   board_through(db, row["id"], shop))
        return subtitle + _render_board(db, row, editable, cp, shop, filling,
                                        sort, show_bought, q, cur, base,
                                        _url, keep)


def _render_board(db, row, editable, cp, shop, filling, sort, show_bought,
                  q, cur, base, _url, keep):
    entries = watchlist_db.current_entries(db, row["id"])

    # basis for hits (pinned shop, else cheapest USD); display shop for the
    # prices. Bought cards keep their spot in history but leave the math.
    cards = [_Card(db, e, shop) for e in entries]
    # chosen ordering; bought always last regardless of sort
    cards.sort(key=lambda c: (c.bought, _sort_key(sort, c)))
    bought_n = sum(1 for c in cards if c.bought)
    grid = cards if show_bought else [c for c in cards if not c.bought]
    if q:
        nq = _norm(q)
        grid = [c for c in grid if nq in _norm(c.e["card_name"])]

    active = [c for c in cards if not c.bought]
    if shop == ALL:
        priced = [c.base for c in active if c.base and c.cur == "$"]
    else:
        priced = [c.disp for c in active if c.disp]
    total_val = sum(s["current"] for s in priced)
    net7 = sum(s["d7"] for s in priced if s["d7"] is not None)
    hits = sum(1 for c in active if c.hit)
    page = grid[(cp - 1) * CARDS_PER_PAGE: cp * CARDS_PER_PAGE]
    empty_msg = (f'No cards match “{esc(q)}”.' if q else
                 'Nothing watched yet — use “Add card”, “Import”, or ask Claude.')
    cards_html = "".join(_card_html(db, c, i, shop, filling)
                         for i, c in enumerate(page)) or \
        f'<p class="nodata">{empty_msg}</p>'

    shop_opts = [(ALL, "All markets · cheapest")] + [
        (s, f"{SHOP_NAMES[s]}{' (€)' if SHOPS[s] == '€' else ''}") for s in SHOPS]
    shop_select = (
        '<select class="shopsel" id="shopSel" aria-label="Which market\'s prices to show">'
        + "".join(f'<option value="{s}" data-href="{_url(shop=s)}"'
                  f'{" selected" if s == shop else ""}>{label}</option>'
                  for s, label in shop_opts) + "</select>")
    sort_links = "".join(
        f'<a href="{_url(sort=k)}" class="{"on" if k == sort else ""}">{label}</a>'
        for k, label in SORTS)
    bought_toggle = ""
    if bought_n:
        bought_toggle = (f'<a class="textlink" href="{_url(show_bought=not show_bought)}">'
                         f'{"hide" if show_bought else "show"} bought ({bought_n})</a>')
    filterbox = (f'<input id="filter" class="filterbox mla" type="search" '
                 f'placeholder="filter cards…" value="{esc(q)}" '
                 f'aria-label="Filter cards by name">')
    sortbar = (f'<div class="sortbar"><span class="shoplbl">sort:</span>'
               f'<span class="shops">{sort_links}</span>'
               f'{filterbox}{bought_toggle}</div>')
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
        tgt_edit = _target_editor()
        modal_actions = (
            '<div class="modalend">'
            '<span class="left"><button class="act ghost" id="removeBtn">Remove</button>'
            '<span id="rmConfirm" style="display:none">Really remove? '
            '<button class="act danger" id="rmYes">Yes, remove</button> '
            '<button class="act" id="rmNo">Keep</button></span></span>'
            '<button class="act primary" id="tgtSave">Save</button>'
            '<button class="act" id="boughtBtn">Bought ✓</button></div>')
    else:
        modal_actions = ('<div class="modalend"><button class="act close">Close</button></div>')
    xbtn = '<button class="xclose" aria-label="Close">×</button>'
    kpis = ("".join(f'<div class="kpi" id="{i}"><b></b><span></span></div>'
                    for i in ("kNow", "kLow", "kTarget", "kD30")))
    dialogs = (f'<dialog id="cardDlg">{xbtn}<h3 id="cardTitle"></h3><p class="sub" id="cardSub"></p>'
               f'<div class="cardhead"><img class="cardimg" id="cardImg" alt="" loading="lazy" hidden>'
               f'<div class="kpis">{kpis}</div></div>'
               f'<div class="shoprow" id="shopRow"></div>'
               f'<div class="chart-wrap"><div id="chartHost"></div><div class="tip" id="tip"></div></div>'
               f'{tgt_edit}<div id="siteHost"></div>'
               f'<details class="histbox"><summary>Recent prices</summary><div id="snapHost"></div></details>'
               f'{modal_actions}</dialog>')
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
    dialogs += (
        f'<dialog id="exportDlg">{xbtn}<h3>Export</h3>'
        '<p class="sub">Plain <code>1 Card Name</code> lines — paste into TCGplayer '
        'Mass Entry, Mana Pool, Card Kingdom, Moxfield, Archidekt, anywhere.</p>'
        '<div class="segs" role="radiogroup" aria-label="Which cards">'
        '<label><input type="radio" name="xsel" value="all" checked>Everything '
        '<span class="cnt" id="xAllCnt"></span></label>'
        '<label><input type="radio" name="xsel" value="hit">At target '
        '<span class="cnt" id="xHitCnt"></span></label>'
        '<label><input type="radio" name="xsel" value="pick">Picked by hand</label>'
        '<label class="mla" style="border:none;background:none"><input type="checkbox" id="xBought" '
        'style="position:static;opacity:1;width:1rem;height:1rem;accent-color:var(--mauve)">'
        'include bought</label></div>'
        '<div class="picklist" id="xList"></div>'
        '<div class="xopts"><label><input type="checkbox" id="xPrinting" checked> printing, where pinned</label>'
        '<label title="Appends each card\'s target after @ so the list can be imported back here"><input type="checkbox" id="xTargets"> include targets (for re-import)</label>'
        '<span class="mla" id="xCount"></span></div>'
        '<textarea class="box" id="xText" readonly rows="6" aria-label="Export text"></textarea>'
        '<div class="btnrow"><button class="act primary" id="xCopy">Copy ⧉</button>'
        '<span class="shoplbl">open in</span>'
        '<button class="act" id="xTcg">TCGplayer ↗</button>'
        '<button class="act" id="xCk">Card Kingdom ↗</button>'
        '<button class="act" id="xMp" title="Copies the list; Mana Pool has no link-in">Mana Pool ↗</button></div>'
        '<p class="sub" id="xHint" style="margin-top:.5rem"></p></dialog>')
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
        dialogs += (
            f'<dialog id="importDlg">{xbtn}<h3>Import cards</h3>'
            '<p class="sub">One card per line — quantities and Archidekt/Moxfield '
            'suffixes are fine. Put a target after <code>@</code>: a price '
            '(<code>Rhystic Study @ 25</code>), <code>@ low</code> to follow the '
            'historic low, <code>@ low-10%</code> to stay 10% under it, or '
            '<code>@ -20%</code> for 20% under today\'s price.</p>'
            '<textarea class="box" id="imText" rows="8" placeholder="1 Sol Ring&#10;'
            '1 Rhystic Study @ 25&#10;Smothering Tithe @ low-10%&#10;'
            'Dockside Extortionist (2X2) 96 @ -20%"></textarea>'
            '<div class="tgtedit"><label for="imTarget">default target</label>'
            '<input id="imTarget" type="text" placeholder="e.g. 5, low, -20%" style="width:11rem">'
            '<label for="imNote">note</label><input id="imNote" type="text" maxlength="200" '
            'placeholder="e.g. deck name" style="width:11rem">'
            '<label class="switch" style="margin:0" title="Deck exports name the printing you own; '
            'a watchlist usually wants the cheapest one"><input type="checkbox" id="imPin"> '
            'pin printings</label></div>'
            '<span class="err" id="imErr"></span><div id="imOut"></div>'
            '<div class="btnrow"><button class="act primary" id="imGo">Import</button>'
            '<button class="act close">Cancel</button></div></dialog>')
    else:
        dialogs += (f'<dialog id="claimDlg">{xbtn}<h3>Your own watchlist</h3>'
                    '<div id="claimOut"></div></dialog>')

    add_btn = ('<button class="act primary" id="addCard">＋ Add card</button>'
               if editable else "")
    export_btn = ('<button class="act secondary" id="exportBtn" '
                  'title="Copy as a decklist or open it in a store">⇪ Export</button>'
                  if entries else "")
    import_btn = ('<button class="act" id="importBtn" '
                  'title="Paste a decklist or shopping list">⇩ Import</button>'
                  if editable else "")
    net_cls = "dn" if net7 < 0 else "up" if net7 > 0 else "fl"
    body = f"""<script>document.getElementById("wait")?.remove()</script>
<div class="actions">{add_btn}{export_btn}{import_btn}{claim}{share}
<span class="shopgrp mla"><label class="shoplbl" for="shopSel">prices:</label>{shop_select}</span></div>
{superseded}
<div class="stats">
<div class="stat"><b>{hits}</b><span>buy windows</span></div>
<div class="stat" title="sum of 7-day changes — down is good">
<b><span class="{net_cls}">{"▼" if net7 < 0 else "▲" if net7 > 0 else "·"}</span>{cur}{abs(net7):.2f}</b><span>7-day net</span></div>
<div class="stat"><b>{cur}{total_val:.2f}</b><span>list total</span></div>
<div class="stat"><b>{len(entries)}</b><span>cards</span></div>
</div>
{sortbar}
<div class="grid">{cards_html}</div>
{_pager(base, "cp", cp, len(grid), CARDS_PER_PAGE, keep=keep)}"""
    return _shell_close(row, editable, body, dialogs, cur, shop,
                        extra=_export_data(cards))


_ACTION_LABELS = {"create": ("forged", "var(--mauve)"),
                  "add": ("added", "var(--green)"),
                  "remove": ("removed", "var(--red)"),
                  "set_target": ("target set", "var(--yellow)"),
                  "set_note": ("note set", "var(--yellow)"),
                  "set_shop": ("basis set", "var(--yellow)"),
                  "set_label": ("renamed", "var(--yellow)"),
                  "bought": ("bought", "var(--lavender)"),
                  "unbought": ("unbought", "var(--yellow)"),
                  "clone_init": ("cloned from", "var(--mauve)")}


def render_history(db, row, editable: bool, hp: int = 1,
                   shop: str = ALL) -> str:
    """The stashed-away revision view: full chain, revision modal, fork/restore."""
    key = row["_key"]
    base = (f"{PREFIX}/w/{esc(key)}" if editable
            else f"{PREFIX}/s/{esc(key)}")
    qshop = f"?shop={shop}" if shop in SHOPS else ""
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
            rule = watchlist_targets.describe(payload)
            d += f" → {rule}" if rule else " → no target"
        if ev["action"] == "set_shop" and d:
            s = payload.get("shop")
            d += f" → {SHOP_NAMES[s]} only" if s in SHOP_NAMES else " → cheapest USD"
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


def render_new() -> str:
    """The forge: mint a list by hand — name it, paste cards, get the key.

    The same thing `watchlist_create` + `watchlist_bulk_add` do from chat,
    for people who would rather not go through an assistant."""
    row = {"_key": "", "label": "New watchlist", "share_code": "",
           "superseded_by": None}
    body = """
<div class="forge rail" id="forgeForm">
<label class="fl" for="newLabel">Name</label>
<input class="txt" id="newLabel" maxlength="80" placeholder="e.g. Eriette upgrades">
<label class="fl" for="newCards">Cards <span style="font-weight:400">(optional — one per line; a target may follow <code>@</code>)</span></label>
<textarea class="box" id="newCards" rows="9" placeholder="1 Sol Ring&#10;1 Rhystic Study @ 25&#10;Smothering Tithe @ low-10%&#10;Dockside Extortionist @ -20%"></textarea>
<div class="row">
<div><label class="fl" for="newTarget">Default target</label>
<input class="txt" id="newTarget" placeholder="e.g. 5, low, -20%"></div>
<div><label class="fl" for="newNote">Note for every card</label>
<input class="txt" id="newNote" maxlength="200" placeholder="e.g. deck name"></div>
</div>
<label class="switch" style="margin-top:.9rem"><input type="checkbox" id="newPin"> pin the printings named on the lines</label>
<p class="hint" style="margin-left:0">Deck exports name the copy you own; a watchlist usually wants the cheapest printing, so this is off by default.</p>
<p class="hint" style="margin-left:0">A number is a fixed target. <code>low</code> follows the historic low as it moves; <code>low-10%</code> stays 10% under it; <code>-20%</code> means 20% under today's price.</p>
<div class="btnrow foot"><button class="act primary" id="forgeGo">✦ Forge the list</button>
<span class="shoplbl">You get a passphrase — it is the key to the list, shown once.</span></div>
</div>
<div class="forge rail" id="forgeOut" hidden></div>"""
    subtitle = ("Prices from TCGplayer, Card Kingdom, Mana Pool and Cardmarket, "
                "updated nightly · targets, history, alerts, and a read-only "
                "share link — no account, just a passphrase")
    return _shell(row, False, body, "", subtitle=subtitle)
