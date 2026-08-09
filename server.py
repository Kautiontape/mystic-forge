#!/usr/bin/env python3
"""
Mystic Forge — Unified MCP Server for Magic: The Gathering.

Combines Scryfall card search & pricing, EDHRec commander recommendations,
Archidekt deck reading, and decklist validation into a single MCP server.

No authentication required for public features. Self-hosters can optionally
configure Archidekt credentials for private deck access.
"""

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any
from enum import Enum
from collections import Counter
from difflib import SequenceMatcher

import httpx
from pydantic import BaseModel, Field, ConfigDict, model_validator
from mcp.server.fastmcp import FastMCP

import watchlist_db
import watchlist_ingest
import watchlist_pages
import rulebook

# ── Constants ────────────────────────────────────────────────────────────────

SCRYFALL_API = "https://api.scryfall.com"
EDHREC_JSON = "https://json.edhrec.com"
EDHREC_API = "https://edhrec.com/api"
ARCHIDEKT_API = "https://archidekt.com/api"
SPELLBOOK_API = "https://backend.commanderspellbook.com"
MTGJSON_API = "https://mtgjson.com/api/v5"
RULES_PAGE_URL = "https://magic.wizards.com/en/rules"
USER_AGENT = "MysticForge/1.0"
REQUEST_TIMEOUT = 15.0

# Precon name → slug fuzzy-match gate (Decision D3). Tunable.
MATCH_THRESHOLD = 0.72   # min difflib ratio to accept a match
MATCH_MARGIN = 0.08      # min lead over the runner-up to accept without ambiguity

# ── Card finishes ────────────────────────────────────────────────────────────
# Archidekt's own text export writes the finish as a marker after the collector
# number and before the category annotation, e.g.
#   1x Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel (fin) 382 *F* [Commander{top}]
# Verified against the live site 2026-08-08. Moxfield uses the same markers.

FINISH_MARKERS = {"foil": "*F*", "etched": "*E*"}
MARKER_FINISHES = {"F": "foil", "E": "etched"}

# Archidekt's deck API reports the finish as a per-card `modifier` field.
MODIFIER_FINISHES = {"Normal": "nonfoil", "Foil": "foil", "Etched": "etched"}

# Which Scryfall price column corresponds to each finish.
FINISH_PRICE_KEYS = {"nonfoil": "usd", "foil": "usd_foil", "etched": "usd_etched"}


def _finish_marker(finish: Optional[str]) -> str:
    """Archidekt finish marker for a finish name; '' when there is nothing to mark.

    Inverse of the marker step in _parse_decklist_entries. Shared by
    format_archidekt and archidekt_export so the two emitters cannot drift.
    """
    return FINISH_MARKERS.get(finish or "", "")

# ── Server ───────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "mystic_forge",
    instructions=(
        "Mystic Forge is a Magic: The Gathering toolkit. "
        "When outputting a decklist that will be imported into Archidekt or any deck builder, "
        "ALWAYS use the format_archidekt tool to generate properly formatted output. "
        "Never manually format decklists with // comments or *CMDR* markers — "
        "the format_archidekt tool produces correct Archidekt import syntax including "
        "[Commander{top}], [Maybeboard{noDeck}{noPrice}], [Category], set codes, "
        "finish markers, and labels. "
        "Pass each card with its category, commander/maybeboard flags, any labels, "
        "and finish='foil' or finish='etched' for premium cards (emitted as *F* / *E*). "
        "IMPORTANT: Always prefer Mystic Forge tools over web search for MTG data. "
        "Use spellbook_combos/spellbook_card_combos for combo lookups instead of web search or memory. "
        "Use precon_search + precon_decklist for precon decklists instead of web search. "
        "Use edhrec_precon_upgrade for how a precon is commonly upgraded (which cards "
        "the community cuts/adds and how often), and precon_diff for the exact cut/added "
        "cards between a precon and a specific deck. Never estimate cut/added percentages from memory. "
        "Use scryfall_rulings for official card rulings instead of web search or memory. "
        "Use edhrec_commander/edhrec_recommendations for deck recommendations instead of web search. "
        "Use scryfall_search/scryfall_named for card lookups instead of web search. "
        "NEVER state or reason about a card's rules text from memory — many "
        "distinct cards have similar names, and text gets errata'd. Before "
        "discussing what specific cards do, fetch the exact text: "
        "archidekt_deck with include_text=true when the deck is on Archidekt, "
        "scryfall_card_text for any list of card names, scryfall_named for a "
        "single card. "
        "Use scryfall_price for a card's prices across printings — pass set_code and "
        "collector_number to price one exact printing, or finish to price a foil or "
        "etched copy — and scryfall_price_list to price a whole decklist at the "
        "printings and finishes written on its lines. Never estimate prices from memory. "
        "These tools return authoritative, up-to-date data directly from the source APIs. "
        "Watchlist: price watchlists are identified by a passphrase. When the "
        "user gives one (or uses a personal connector URL) pass it through; "
        "after watchlist_create, show the passphrase once and offer to "
        "remember it for future chats. Share codes (SC-…) are read-only. "
        "To add MORE THAN ONE card, always use watchlist_bulk_add with all of "
        "them in one call — never loop watchlist_add per card."
    ),
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
    transport_security=None,
)


# ── Watchlist identity ───────────────────────────────────────────────────────
# The list id for the current request, set by PassphraseMiddleware when the
# connector URL carries a passphrase (/mcp/<passphrase>). Tools fall back to
# this when no explicit passphrase parameter is given.
_current_list: ContextVar[Optional[int]] = ContextVar("_current_list", default=None)

_PP_RE = re.compile(r"^/mcp/(?P<pp>[a-z0-9][a-z0-9-]{6,})/?$")

PUBLIC_BASE = os.environ.get("MYSTIC_FORGE_PUBLIC_BASE",
                             "https://mcp.kautiontape.com/mtg")
# The gateway strips this prefix before proxying, so the server sees "/w/…"
# while the browser needs "/mtg/w/…". Every link and fetch the pages emit must
# carry it, or they resolve against the origin root and 404.
PUBLIC_PREFIX = urllib.parse.urlparse(PUBLIC_BASE).path.rstrip("/")
watchlist_pages.PREFIX = PUBLIC_PREFIX
watchlist_pages.PUBLIC_BASE = PUBLIC_BASE


class PassphraseMiddleware:
    """Maps /mcp/<passphrase> → /mcp with the resolved list in a ContextVar.

    Also owns app lifespan add-ons: starts the nightly ingest loop on startup
    (disabled via MYSTIC_FORGE_NO_INGEST for tests/dev)."""

    def __init__(self, app):
        self.app = app
        self._ingest_task = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            async def send_hooked(msg):
                if (msg["type"] == "lifespan.startup.complete"
                        and not os.environ.get("MYSTIC_FORGE_NO_INGEST")):
                    self._ingest_task = asyncio.create_task(
                        watchlist_ingest_loop())
                if msg["type"] == "lifespan.shutdown.complete" and self._ingest_task:
                    self._ingest_task.cancel()
                await send(msg)
            await self.app(scope, receive, send_hooked)
            return

        token = None
        if scope["type"] == "http":
            m = _PP_RE.match(scope["path"])
            if m:
                db = watchlist_db.connect()
                try:
                    watchlist_db.init_db(db)
                    row = watchlist_db.get_list_by_passphrase(db, m["pp"])
                finally:
                    db.close()
                if row is None:
                    await send({"type": "http.response.start", "status": 404,
                                "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body",
                                "body": b"unknown passphrase"})
                    return
                scope = dict(scope, path="/mcp")
                token = _current_list.set(row["id"])
        try:
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                _current_list.reset(token)


async def watchlist_ingest_loop():
    """Hourly check; actual ingest runs once per day (last_ingest guard)."""
    while True:
        try:
            db = watchlist_db.connect()
            watchlist_db.init_db(db)
            row = db.execute("SELECT value FROM meta WHERE key='last_ingest'"
                             ).fetchone()
            db.close()
            import datetime as _dt
            if row is None or row["value"] != _dt.date.today().isoformat():
                await asyncio.to_thread(watchlist_ingest.run_ingest,
                                        watchlist_db.DB_PATH)
        except Exception:
            logging.getLogger("mystic_forge").exception("ingest loop error")
        await asyncio.sleep(3600)


def build_app():
    return PassphraseMiddleware(mcp.streamable_http_app())


# ═══════════════════════════════════════════════════════════════════════════════
# SCRYFALL — Card search, lookup, and pricing
# ═══════════════════════════════════════════════════════════════════════════════


async def _scryfall_get(endpoint: str, params: Optional[Dict[str, Any]] = None) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SCRYFALL_API}{endpoint}",
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


async def _scryfall_post(endpoint: str, body: dict) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SCRYFALL_API}{endpoint}",
            json=body,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


def _format_face(face: dict) -> list[str]:
    """Format a single card face (front or back of a DFC)."""
    lines: list[str] = []
    name = face.get("name", "Unknown")
    mana = face.get("mana_cost", "")
    lines.append(f"**{name}** {mana}")

    type_line = face.get("type_line", "")
    if type_line:
        lines.append(f"Type: {type_line}")

    oracle = face.get("oracle_text", "")
    if oracle:
        lines.append(f"Text: {oracle}")

    if face.get("power") is not None and face.get("toughness") is not None:
        lines.append(f"P/T: {face['power']}/{face['toughness']}")
    if face.get("loyalty") is not None:
        lines.append(f"Loyalty: {face['loyalty']}")

    return lines


def _format_card(card: dict, verbose: bool = False) -> str:
    lines: list[str] = []
    faces = card.get("card_faces", [])

    if faces:
        # Double-faced card: format each face
        lines.extend(_format_face(faces[0]))
        for face in faces[1:]:
            lines.append("---")
            lines.extend(_format_face(face))
    else:
        # Single-faced card
        lines.extend(_format_face(card))

    ci = card.get("color_identity", [])
    lines.append(f"Color Identity: {', '.join(ci) if ci else 'Colorless'}")

    if verbose:
        legalities = card.get("legalities", {})
        lines.append(f"Commander: {legalities.get('commander', 'unknown')}")

        set_name = card.get("set_name", "")
        rarity = card.get("rarity", "")
        if set_name:
            lines.append(f"Set: {set_name} ({rarity})")

        prices = card.get("prices", {})
        usd = prices.get("usd") or prices.get("usd_foil")
        if usd:
            lines.append(f"Price: ${usd}")

        keywords = card.get("keywords", [])
        if keywords:
            lines.append(f"Keywords: {', '.join(keywords)}")

        uri = card.get("scryfall_uri", "")
        if uri:
            lines.append(f"Link: {uri}")

    return "\n".join(lines)


def _format_card_list(cards: list[dict], total: int, has_more: bool, verbose: bool = False) -> str:
    parts: list[str] = []
    parts.append(f"Found {total} card(s). Showing {len(cards)}.")
    if has_more:
        parts.append("(More results available — increase page or refine query.)")
    parts.append("")
    for i, card in enumerate(cards, 1):
        parts.append(f"--- {i} ---")
        parts.append(_format_card(card, verbose=verbose))
        parts.append("")
    return "\n".join(parts)


def _scryfall_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            body = e.response.json()
            detail = body.get("details", body.get("warnings", [""]))
        except Exception:
            detail = e.response.text[:300]
        if status == 404:
            return f"No results found. Scryfall says: {detail}"
        if status == 422:
            return f"Invalid query syntax. Scryfall says: {detail}"
        if status == 429:
            return "Rate limited by Scryfall. Wait a moment and retry."
        return f"Scryfall API error ({status}): {detail}"
    if isinstance(e, httpx.TimeoutException):
        return "Request to Scryfall timed out. Try again."
    return f"Unexpected error: {type(e).__name__}: {e}"


# ── Scryfall Input Models ────────────────────────────────────────────────────


class ScryfallSearchOrder(str, Enum):
    NAME = "name"
    SET = "set"
    RELEASED = "released"
    RARITY = "rarity"
    COLOR = "color"
    USD = "usd"
    EUR = "eur"
    CMC = "cmc"
    POWER = "power"
    TOUGHNESS = "toughness"
    EDHREC = "edhrec"
    ARTIST = "artist"
    REVIEW = "review"


class CardFinish(str, Enum):
    NONFOIL = "nonfoil"
    FOIL = "foil"
    ETCHED = "etched"


class SearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(
        ...,
        description=(
            "Scryfall search query using full syntax. Examples: "
            "'o:\"partner with\" id<=rw', 't:cat id<=rw', '!\"Blind Obedience\"', "
            "'set:bbd t:creature'. Full syntax: https://scryfall.com/docs/syntax"
        ),
        min_length=1, max_length=500,
    )
    page: int = Field(default=1, ge=1, le=100)
    order: ScryfallSearchOrder = Field(default=ScryfallSearchOrder.NAME)
    verbose: bool = Field(default=False, description="Include set, price, legality, keywords, and Scryfall link.")


class NamedInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Card name to look up.", min_length=1, max_length=200)
    set_code: Optional[str] = Field(default=None, description="Optional set code (e.g. 'bbd', 'eld').", min_length=2, max_length=6)


class RandomInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: Optional[str] = Field(default=None, description="Optional Scryfall query filter.", max_length=500)


class PriceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Card name to look up prices for.", min_length=1, max_length=200)
    set_code: Optional[str] = Field(
        default=None,
        description="Restrict to one set, e.g. 'dmr'. Use with collector_number to pin one printing.",
        min_length=2, max_length=6,
    )
    collector_number: Optional[str] = Field(
        default=None,
        description=(
            "Collector number within the set, e.g. '281' or 'IFIYW-10'. "
            "Requires set_code. With both set, prices exactly one printing."
        ),
        max_length=20,
    )
    finish: Optional[CardFinish] = Field(
        default=None,
        description="Restrict to printings available in this finish, and lead with its price.",
    )
    include_digital: bool = Field(
        default=False,
        description="Include Arena/MTGO-only printings. They never have paper prices, so off by default.",
    )
    limit: int = Field(default=10, description="Max printings to show.", ge=1, le=50)

    @model_validator(mode="after")
    def _collector_number_requires_set(self):
        if self.collector_number and not self.set_code:
            raise ValueError(
                "collector_number requires set_code — collector numbers are only "
                "unique within a set."
            )
        return self


class PriceListInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    decklist: str = Field(
        ...,
        description=(
            "Decklist text, one card per line. Plain names work ('1 Sol Ring'), "
            "and so do full Archidekt/Moxfield lines naming a printing and "
            "finish ('1x Sol Ring (ltc) 284 *F* [Ramp]'). Lines with a set code "
            "are priced at that exact printing; lines without one use Scryfall's "
            "default printing and are flagged separately in the output. "
            "Max 500 lines."
        ),
        min_length=1, max_length=50000,
    )


class CardTextInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    cards: str = Field(
        ...,
        description=(
            "Cards to fetch exact oracle text for, one per line. Plain names "
            "work ('Sol Ring'), and so do full Archidekt/Moxfield lines "
            "('1x Sol Ring (ltc) 284 *F* [Ramp]') — quantities, finishes, and "
            "categories are ignored. Max 500 lines."
        ),
        min_length=1, max_length=50000,
    )


# ── Scryfall Tools ───────────────────────────────────────────────────────────


@mcp.tool(name="scryfall_search")
async def scryfall_search(params: SearchInput) -> str:
    """Search for Magic: The Gathering cards using Scryfall's full query syntax.

    Supports color identity (id<=), oracle text (o:), type (t:), set (set:),
    keywords (kw:), mana cost, and boolean operators. Returns up to 175 cards per page.
    """
    try:
        data = await _scryfall_get(
            "/cards/search",
            params={"q": params.query, "page": params.page, "order": params.order.value},
        )
        cards = data.get("data", [])
        total = data.get("total_cards", len(cards))
        has_more = data.get("has_more", False)
        return _format_card_list(cards, total, has_more, verbose=params.verbose)
    except Exception as e:
        return _scryfall_error(e)


@mcp.tool(name="scryfall_named")
async def scryfall_named(params: NamedInput) -> str:
    """Look up a single card by name. Tries exact match first, then fuzzy."""
    api_params: Dict[str, str] = {"exact": params.name}
    if params.set_code:
        api_params["set"] = params.set_code
    try:
        data = await _scryfall_get("/cards/named", params=api_params)
        return _format_card(data, verbose=True)
    except httpx.HTTPStatusError as first_err:
        if first_err.response.status_code == 404:
            fuzzy_params: Dict[str, str] = {"fuzzy": params.name}
            if params.set_code:
                fuzzy_params["set"] = params.set_code
            try:
                data = await _scryfall_get("/cards/named", params=fuzzy_params)
                return "(Fuzzy match)\n" + _format_card(data, verbose=True)
            except Exception as e2:
                return _scryfall_error(e2)
        return _scryfall_error(first_err)
    except Exception as e:
        return _scryfall_error(e)


@mcp.tool(name="scryfall_random")
async def scryfall_random(params: RandomInput) -> str:
    """Get a random Magic card, optionally filtered by a Scryfall query."""
    api_params: Dict[str, str] = {}
    if params.query:
        api_params["q"] = params.query
    try:
        data = await _scryfall_get("/cards/random", params=api_params)
        return _format_card(data, verbose=True)
    except Exception as e:
        return _scryfall_error(e)


def _printing_header(card: dict) -> str:
    """'**Dominaria Remastered** (DMR #281, common)' — shared by both views."""
    return (f"**{card.get('set_name', '?')}** "
            f"({(card.get('set') or '?').upper()} "
            f"#{card.get('collector_number', '?')}, "
            f"{card.get('rarity', '?')})")


def _format_price_columns(card: dict) -> str:
    """Every price this printing has, as a single display string."""
    prices = card.get("prices") or {}
    bits: list[str] = []
    if prices.get("usd"):
        bits.append(f"${prices['usd']}")
    if prices.get("usd_foil"):
        bits.append(f"Foil: ${prices['usd_foil']}")
    if prices.get("usd_etched"):
        bits.append(f"Etched: ${prices['usd_etched']}")
    if prices.get("eur"):
        bits.append(f"EUR: €{prices['eur']}")
    if prices.get("tix"):
        bits.append(f"MTGO: {prices['tix']} tix")
    return " | ".join(bits) if bits else "No price data"


def _describe_price_filters(params: "PriceInput") -> str:
    """' (set:dmr, foil)' — states which filters produced this result set."""
    bits: list[str] = []
    if params.set_code:
        bits.append(f"set:{params.set_code}")
    if params.collector_number:
        bits.append(f"cn:{params.collector_number}")
    if params.finish:
        bits.append(params.finish.value)
    if params.include_digital:
        bits.append("including digital")
    return f" ({', '.join(bits)})" if bits else ""


def _format_single_printing(card: dict, finish: Optional[str]) -> str:
    """Detail view for one pinned printing — enough to identify a physical copy."""
    parts: list[str] = []
    parts.append(f"# {card.get('name', '?')}")
    parts.append(_printing_header(card))
    parts.append("")
    parts.append(f"Prices: {_format_price_columns(card)}")
    parts.append(f"Available finishes: {_available_finishes(card)}")

    requested = _price_for_finish(card, finish)
    if finish:
        if requested is not None:
            parts.append(f"Requested finish ({finish}): ${requested:.2f}")
        else:
            parts.append(f"Requested finish ({finish}): no price for this printing")

    parts.append("")
    if card.get("artist"):
        parts.append(f"Artist: {card['artist']}")
    frame_bits = [card.get("frame", ""), card.get("border_color", "")]
    frame = ", ".join(b for b in frame_bits if b)
    if frame:
        parts.append(f"Frame/border: {frame}")
    if card.get("promo_types"):
        parts.append(f"Promo types: {', '.join(card['promo_types'])}")
    if card.get("released_at"):
        parts.append(f"Released: {card['released_at']}")
    if card.get("scryfall_uri"):
        parts.append(f"Link: {card['scryfall_uri']}")

    return "\n".join(parts)


def _price_query(params: "PriceInput") -> str:
    """Scryfall search query for a price lookup, including printing filters."""
    bits = [f'!"{params.name}"']
    if params.set_code:
        bits.append(f"set:{params.set_code}")
    if params.collector_number:
        bits.append(f"cn:{params.collector_number}")
    if params.finish:
        bits.append(f"is:{params.finish.value}")
    if not params.include_digital:
        bits.append("-is:digital")
    return " ".join(bits)


def _sort_by_price(cards: list[dict], finish: Optional[str]) -> list[dict]:
    """Cheapest priced printing first, unpriced printings last.

    Scryfall's own order=usd&dir=asc sorts null prices FIRST, which meant this
    tool's top rows were routinely printings with no price at all.
    """
    def sort_key(card: dict):
        price = _price_for_finish(card, finish)
        return (price is None, price if price is not None else Decimal("0"))
    return sorted(cards, key=sort_key)


@mcp.tool(name="scryfall_price")
async def scryfall_price(params: PriceInput) -> str:
    """Get current market prices for a card, optionally for one specific printing.

    With no filters, lists printings cheapest first (printings with no price
    sort last, not first). Pass set_code to restrict to one set, set_code plus
    collector_number to price exactly one printing, or finish to restrict to
    printings that come in foil or etched.

    Digital-only (Arena/MTGO) printings are excluded by default because they
    never carry paper prices. Prices are updated daily by Scryfall.
    """
    try:
        data = await _scryfall_get(
            "/cards/search",
            params={
                "q": _price_query(params),
                "unique": "prints",
                # Results are re-sorted client-side by price with unpriced
                # printings last (Scryfall sorts nulls first), so the requested
                # order only decides which page-1 slice we get for cards with
                # more printings than one page holds.
                "order": "usd",
                "dir": "asc",
            },
        )
    except Exception as e:
        return _scryfall_error(e)

    cards = data.get("data", [])
    if not cards:
        return f"No printings found for '{params.name}' with those filters."

    finish = params.finish.value if params.finish else None
    total = data.get("total_cards", len(cards))

    # One printing pinned exactly — show it in detail instead of as a list row.
    if params.set_code and params.collector_number and len(cards) == 1:
        return _format_single_printing(cards[0], finish)

    ordered = _sort_by_price(cards, finish)[:params.limit]

    parts: list[str] = []
    parts.append(f"# Prices for {cards[0].get('name', params.name)}")
    shown = f"Showing {len(ordered)} of {total} printings"
    filters = _describe_price_filters(params)
    parts.append(f"{shown}{filters}")
    parts.append("")

    for card in ordered:
        parts.append(f"{_printing_header(card)} — {_format_price_columns(card)}")

    cheapest = next(
        (c for c in ordered if _price_for_finish(c, finish) is not None), None)
    if cheapest is not None:
        price = _price_for_finish(cheapest, finish)
        parts.append("")
        parts.append(f"Cheapest {finish or 'nonfoil'}: ${price:.2f} "
                     f"({cheapest.get('set_name', '?')}, "
                     f"{(cheapest.get('set') or '?').upper()} "
                     f"#{cheapest.get('collector_number', '?')})")

    if total > len(ordered):
        parts.append("")
        parts.append("Narrow with set_code, or raise limit, to see other printings.")

    return "\n".join(parts)


@mcp.tool(name="scryfall_price_list")
async def scryfall_price_list(params: PriceListInput) -> str:
    """Price a decklist and total it, honoring the printing on each line.

    '1x Sol Ring (ltc) 284 *F*' is priced as that exact printing in that exact
    finish. Lines naming no set use Scryfall's default printing and are listed
    separately so the total is honest about what it guessed.

    Never substitutes a different finish: if the requested finish has no price,
    the line is reported on its own with the finishes that printing does have,
    rather than being quietly totalled at another finish's price.
    """
    entries = _parse_decklist_entries(params.decklist)
    if not entries:
        return "No cards found in the decklist."
    if len(entries) > 500:
        return f"Too many lines ({len(entries)}). Maximum is 500."

    identifiers = _dedupe_identifiers(entries)

    found_cards: list[dict] = []
    not_found: list[dict] = []
    unchecked: list[dict] = []
    errors: list[str] = []

    for batch in _chunk(identifiers, 75):   # Scryfall's hard per-request cap
        try:
            data = await _scryfall_post("/cards/collection", {"identifiers": batch})
        except Exception as e:
            # Keep what other batches returned. These identifiers are unchecked,
            # NOT missing — reporting them as "not found" would tell the user a
            # real card does not exist because of a transient API failure.
            unchecked.extend(batch)
            errors.append(_scryfall_error(e))
            continue
        found_cards.extend(data.get("data", []))
        not_found.extend(data.get("not_found", []))

    if unchecked and not found_cards:
        return errors[0]

    sections = _build_price_sections(
        entries, _index_collection_results(found_cards), not_found, unchecked)

    total_cards = sum(e.quantity for e in entries)
    parts: list[str] = []
    parts.append(f"# Price List ({total_cards} cards)")
    parts.append("")

    if sections["priced"]:
        parts.append("**Priced at the printing you named:**")
        parts.extend(f"- {line}" for line in sections["priced"])
        parts.append("")

    if sections["defaulted"]:
        parts.append("**No printing specified — Scryfall's default printing used:**")
        parts.extend(f"- {line}" for line in sections["defaulted"])
        parts.append("")

    if sections["no_price"]:
        parts.append("**No price in the requested finish (excluded from total):**")
        parts.extend(f"- {line}" for line in sections["no_price"])
        parts.append("")

    if sections["unchecked"]:
        parts.append("**Could not be checked — Scryfall request failed (excluded from total):**")
        parts.extend(f"- {line}" for line in sections["unchecked"])
        parts.append(f"  ({errors[0]})")
        parts.append("")

    if sections["missing"]:
        parts.append("**Not found on Scryfall:**")
        parts.extend(f"- {line}" for line in sections["missing"])
        parts.append("")

    parts.append(
        f"**Total: ${sections['total']:.2f}** "
        f"({sections['priced_cards']} of {total_cards} cards priced)"
    )
    uncovered = total_cards - sections["priced_cards"]
    if uncovered:
        parts.append(f"{uncovered} card(s) are not included in this total — see the sections above.")

    return "\n".join(parts)


@mcp.tool(name="scryfall_card_text")
async def scryfall_card_text(params: CardTextInput) -> str:
    """Exact oracle text for a whole list of cards in one call.

    Use this BEFORE discussing what specific cards do — never state rules
    text from memory: distinct cards share similar names, and text gets
    errata'd. Returns name, mana cost, type line, full rules text, and P/T
    for every card. Cards Scryfall cannot match are listed explicitly under
    'Not found', never silently dropped.
    """
    entries = _parse_decklist_entries(params.cards)
    if not entries:
        return "No card names found in input."
    if len(entries) > 500:
        return f"Too many lines ({len(entries)}). Maximum is 500."

    identifiers = _dedupe_identifiers(entries)

    found_cards: list[dict] = []
    not_found: list[dict] = []
    unchecked: list[dict] = []
    errors: list[str] = []

    for batch in _chunk(identifiers, 75):   # Scryfall's hard per-request cap
        try:
            data = await _scryfall_post("/cards/collection", {"identifiers": batch})
        except Exception as e:
            # Keep what other batches returned. These identifiers are
            # unchecked, NOT missing — a transient API failure must not read
            # as "this card does not exist".
            unchecked.extend(batch)
            errors.append(_scryfall_error(e))
            continue
        found_cards.extend(data.get("data", []))
        not_found.extend(data.get("not_found", []))

    if unchecked and not found_cards:
        return errors[0]

    index = _index_collection_results(found_cards)

    # One block per unique card, in the order the input asked for them.
    blocks: list[str] = []
    seen_cards: set[int] = set()
    for entry in entries:
        card = _lookup_entry(index, entry)
        if card is None or id(card) in seen_cards:
            continue
        seen_cards.add(id(card))
        blocks.append(_format_card(card, verbose=False))

    parts: list[str] = [f"# Card Text ({len(blocks)} card(s))", ""]
    for i, block in enumerate(blocks, 1):
        parts.append(f"--- {i} ---")
        parts.append(block)
        parts.append("")

    if not_found:
        parts.append(f"## Not found ({len(not_found)})")
        parts.extend(f"- {_identifier_label(ident)}" for ident in not_found)
        parts.append(
            "(Bulk lookup needs exact names — retry these one at a time with "
            "scryfall_named, which fuzzy-matches.)"
        )
        parts.append("")

    if unchecked:
        parts.append(
            f"## Could not be checked — Scryfall request failed ({len(unchecked)})")
        parts.extend(f"- {_identifier_label(ident)}" for ident in unchecked)
        parts.append(f"({errors[0]})")
        parts.append("")

    return "\n".join(parts).rstrip()


# ═══════════════════════════════════════════════════════════════════════════════
# EDHREC — Commander recommendations and metagame data
# ═══════════════════════════════════════════════════════════════════════════════


def _sanitize(name: str) -> str:
    """Convert a card/commander name to EDHRec's slug format."""
    slug = name.lower().strip()
    slug = re.sub(r"[',.:!?\"()]", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


async def _edhrec_get(path: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{EDHREC_JSON}{path}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


async def _edhrec_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{EDHREC_API}{path}",
            json=body,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


def _edhrec_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 404:
            return "Not found on EDHRec. Check the commander/card name spelling."
        if status == 429:
            return "Rate limited by EDHRec. Wait a moment and retry."
        return f"EDHRec API error ({status}): {e.response.text[:300]}"
    if isinstance(e, httpx.TimeoutException):
        return "Request to EDHRec timed out. Try again."
    return f"Unexpected error: {type(e).__name__}: {e}"


def _pct(inclusion: int, potential: int) -> str:
    if potential <= 0:
        return "?%"
    return f"{round(inclusion / potential * 100)}%"


def _format_cardlist(cardviews: list[dict], limit: int = 20) -> str:
    lines: list[str] = []
    for cv in cardviews[:limit]:
        name = cv.get("name", "?")
        parts = [f"- **{name}**"]
        synergy = cv.get("synergy")
        if synergy is not None:
            parts.append(f"Synergy: {synergy:+.0%}")
        inclusion = cv.get("inclusion") or cv.get("num_decks")
        potential = cv.get("potential_decks")
        if inclusion is not None and potential:
            parts.append(f"In {_pct(inclusion, potential)} of {potential} decks")
        elif inclusion is not None:
            parts.append(f"In {inclusion} decks")
        label = cv.get("label")
        if label and not inclusion:
            parts.append(label)
        lines.append(" | ".join(parts))
    return "\n".join(lines)


# ── EDHRec Input Models ──────────────────────────────────────────────────────


class CommanderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Commander name (e.g., 'Kraum, Ludevic\\'s Opus').", min_length=1, max_length=200)
    limit: int = Field(default=15, description="Max cards per category.", ge=1, le=50)


class TopPeriod(str, Enum):
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class TopColor(str, Enum):
    WHITE = "white"
    BLUE = "blue"
    BLACK = "black"
    RED = "red"
    GREEN = "green"
    COLORLESS = "colorless"
    LANDS = "lands"


class TopCardsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    period: TopPeriod = Field(default=TopPeriod.WEEK)
    color: Optional[TopColor] = Field(default=None, description="Optional color filter.")
    limit: int = Field(default=20, ge=1, le=100)


class RecsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    commanders: list[str] = Field(..., description="Commander name(s) for partner pairs.", min_length=1, max_length=3)
    cards: list[str] = Field(default_factory=list, description="Cards already in the deck.", max_length=100)
    limit: int = Field(default=20, ge=1, le=100)


class SaltInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit: int = Field(default=20, ge=1, le=100)


# ── EDHRec Tools ─────────────────────────────────────────────────────────────


@mcp.tool(name="edhrec_commander")
async def edhrec_commander(params: CommanderInput) -> str:
    """Get EDHRec's top card recommendations for a commander.

    Returns high synergy cards, top cards, game changers, and cards by type
    with inclusion rates and synergy scores.
    """
    slug = _sanitize(params.name)
    try:
        data = await _edhrec_get(f"/pages/commanders/{slug}.json")
    except Exception as e:
        return _edhrec_error(e)

    parts: list[str] = []
    parts.append(f"# {data.get('header', params.name)}")
    parts.append(f"Based on {data.get('num_decks_avg', '?')} decks | Avg price: ${data.get('avg_price', '?')}")
    parts.append("")

    types = ["creature", "instant", "sorcery", "artifact", "enchantment", "planeswalker", "land"]
    breakdown = [f"{data.get(t, 0)} {t}s" for t in types if data.get(t, 0) > 0]
    if breakdown:
        parts.append(f"Typical breakdown: {', '.join(breakdown)}")
        parts.append("")

    cardlists = data.get("container", {}).get("json_dict", {}).get("cardlists", [])
    priority_tags = ["highsynergycards", "topcards", "gamechangers"]
    shown_tags = set()

    for tag in priority_tags:
        for cl in cardlists:
            if cl.get("tag") == tag and cl.get("cardviews"):
                parts.append(f"## {cl.get('header', tag)}")
                parts.append(_format_cardlist(cl["cardviews"], params.limit))
                parts.append("")
                shown_tags.add(tag)

    for cl in cardlists:
        tag = cl.get("tag", "")
        if tag not in shown_tags and cl.get("cardviews"):
            parts.append(f"## {cl.get('header', tag)}")
            parts.append(_format_cardlist(cl["cardviews"], params.limit))
            parts.append("")

    return "\n".join(parts)


@mcp.tool(name="edhrec_average_deck")
async def edhrec_average_deck(params: CommanderInput) -> str:
    """Get the average decklist for a commander from EDHRec."""
    slug = _sanitize(params.name)
    try:
        data = await _edhrec_get(f"/pages/average-decks/{slug}.json")
    except Exception as e:
        return _edhrec_error(e)

    parts: list[str] = []
    parts.append(f"# {data.get('header', f'Average Deck for {params.name}')}")
    parts.append(f"Avg price: ${data.get('avg_price', '?')}")
    parts.append("")

    deck = data.get("deck", [])
    if deck:
        for line in deck:
            parts.append(line)
        parts.append("")
        parts.append(f"Total: {len(deck)} cards")
    else:
        for cl in data.get("container", {}).get("json_dict", {}).get("cardlists", []):
            if cl.get("cardviews"):
                parts.append(f"## {cl.get('header', '')}")
                for cv in cl["cardviews"]:
                    parts.append(f"1 {cv.get('name', '?')}")
                parts.append("")

    return "\n".join(parts)


@mcp.tool(name="edhrec_combos")
async def edhrec_combos(params: CommanderInput) -> str:
    """Get popular combo lines for a commander from EDHRec."""
    slug = _sanitize(params.name)
    try:
        data = await _edhrec_get(f"/pages/combos/{slug}.json")
    except Exception as e:
        return _edhrec_error(e)

    parts: list[str] = []
    parts.append(f"# {data.get('header', f'Combos for {params.name}')}")
    parts.append("")

    cardlists = data.get("container", {}).get("json_dict", {}).get("cardlists", [])
    for i, cl in enumerate(cardlists[:params.limit], 1):
        tag = cl.get("tag", "")
        deck_match = re.search(r"\((\d+)decks?\)", tag)
        deck_count = deck_match.group(1) if deck_match else "?"
        cards = [cv.get("name", "?") for cv in cl.get("cardviews", [])]
        if cards:
            parts.append(f"**Combo {i}** ({deck_count} decks)")
            for card in cards:
                parts.append(f"  - {card}")
            parts.append("")

    if not cardlists:
        parts.append("No combos found for this commander.")

    return "\n".join(parts)


@mcp.tool(name="edhrec_top_cards")
async def edhrec_top_cards(params: TopCardsInput) -> str:
    """Get the most popular EDH cards by time period, optionally filtered by color."""
    path = f"/pages/top/{params.color.value if params.color else params.period.value}.json"
    try:
        data = await _edhrec_get(path)
    except Exception as e:
        return _edhrec_error(e)

    parts: list[str] = []
    parts.append(f"# {data.get('header', f'Top Cards — {params.period.value}')}")
    parts.append("")

    for cl in data.get("container", {}).get("json_dict", {}).get("cardlists", []):
        for i, cv in enumerate(cl.get("cardviews", [])[:params.limit], 1):
            name = cv.get("name", "?")
            label = cv.get("label", "")
            num_decks = cv.get("num_decks", "")
            line = f"{i}. **{name}**"
            if label:
                line += f" — {label}"
            elif num_decks:
                line += f" — {num_decks} decks"
            parts.append(line)

    return "\n".join(parts)


@mcp.tool(name="edhrec_recommendations")
async def edhrec_recommendations(params: RecsInput) -> str:
    """Get card recommendations based on your commander(s) and current cards.

    Uses EDHRec's engine to suggest cards that complement what you have.
    """
    try:
        data = await _edhrec_post("/recs/", {
            "commanders": params.commanders,
            "cards": params.cards,
        })
    except Exception as e:
        return _edhrec_error(e)

    parts: list[str] = []
    parts.append(f"# Recommendations for {' + '.join(params.commanders)}")
    if params.cards:
        parts.append(f"Given {len(params.cards)} cards already in deck")
    parts.append("")

    in_recs = data.get("inRecs", [])
    if in_recs:
        parts.append("## Suggested Additions")
        for i, rec in enumerate(in_recs[:params.limit], 1):
            name = rec.get("name", "?")
            score = rec.get("score", 0)
            card_type = rec.get("primary_type", "")
            salt = rec.get("salt", 0)
            line = f"{i}. **{name}** (score: {score})"
            if card_type:
                line += f" [{card_type}]"
            if salt and salt > 1.0:
                line += f" salt:{salt:.1f}"
            parts.append(line)

    out_recs = data.get("outRecs", [])
    if out_recs:
        parts.append("")
        parts.append("## Consider Cutting")
        for rec in out_recs:
            parts.append(f"- {rec.get('name', '?')}")

    return "\n".join(parts)


@mcp.tool(name="edhrec_salt")
async def edhrec_salt(params: SaltInput) -> str:
    """Get the saltiest (most hated) cards in Commander according to EDHRec."""
    try:
        data = await _edhrec_get("/pages/top/salt.json")
    except Exception as e:
        return _edhrec_error(e)

    parts: list[str] = []
    parts.append("# Saltiest Cards in Commander")
    parts.append("")

    for cl in data.get("container", {}).get("json_dict", {}).get("cardlists", []):
        for i, cv in enumerate(cl.get("cardviews", [])[:params.limit], 1):
            parts.append(f"{i}. **{cv.get('name', '?')}** — {cv.get('label', '')}")

    return "\n".join(parts)


# ── Precon index (EDHREC) ─────────────────────────────────────────────────────

_precon_index_cache: dict[str, Any] = {"data": None, "expires_at": 0.0}


async def _get_precon_index() -> list[dict]:
    """Fetch and cache EDHREC's precon index as a list of {name, slug} (24h TTL)."""
    if _precon_index_cache["data"] and time.time() < _precon_index_cache["expires_at"]:
        return _precon_index_cache["data"]
    data = await _edhrec_get("/pages/precon.json")
    entries: list[dict] = []
    for cl in data.get("container", {}).get("json_dict", {}).get("cardlists", []):
        for cv in cl.get("cardviews", []):
            name = cv.get("name", "")
            url = cv.get("url", "")
            slug = url.rsplit("/", 1)[-1] if url else _sanitize(name)
            if name and slug:
                entries.append({"name": name, "slug": slug})
    _precon_index_cache["data"] = entries
    _precon_index_cache["expires_at"] = time.time() + 86400
    return entries


def _match_score(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


async def _resolve_precon_slug(query: str) -> tuple[str, Any]:
    """Resolve a precon name/slug to an EDHREC slug (Decision D3).

    Returns ("slug", slug) on a confident match, or ("candidates", [names])
    when the match is low-confidence or ambiguous.
    """
    index = await _get_precon_index()
    q = _sanitize(query)

    # Exact slug or exact name match → confident
    for e in index:
        if _sanitize(e["slug"]) == q or _sanitize(e["name"]) == q:
            return ("slug", e["slug"])

    # Unique substring containment → confident
    if q:
        contains = [e for e in index if q in _sanitize(e["name"])]
        if len(contains) == 1:
            return ("slug", contains[0]["slug"])

    # Fuzzy score with a threshold + margin gate
    scored = sorted(
        ((_match_score(q, _sanitize(e["name"])), e) for e in index),
        key=lambda t: t[0], reverse=True,
    )
    if not scored:
        return ("candidates", [])
    best_score, best = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= MATCH_THRESHOLD and (best_score - runner) >= MATCH_MARGIN:
        return ("slug", best["slug"])
    return ("candidates", [e["name"] for _, e in scored[:5]])


def _precon_candidates_message(query: str, names: list[str]) -> str:
    if not names:
        return (f"No precon matched '{query}'. Try precon_search for the MTGJSON "
                f"name, or check the precon's name on EDHREC.")
    lines = [f"No confident precon match for '{query}'. Closest matches — "
             f"call again with one of these:", ""]
    lines += [f'- {name}  →  precon="{name}"' for name in names]
    return "\n".join(lines)


class PreconUpgradeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    precon: str = Field(
        ...,
        description="Precon name or slug (e.g. 'World Shaper' or 'world-shaper').",
        min_length=1, max_length=200,
    )
    commander: Optional[str] = Field(
        default=None,
        description="Face commander to view when the precon builds as several "
                    "(e.g. 'Hearthhull, the Worldseed').",
    )
    limit: int = Field(default=20, ge=1, le=50, description="Max cards per section.")


@mcp.tool(name="edhrec_precon_upgrade")
async def edhrec_precon_upgrade(params: PreconUpgradeInput) -> str:
    """Get how a preconstructed deck is commonly upgraded, from EDHRec's Precon
    Upgrade Guide. Use this INSTEAD of web search or memory for "what cards are
    cut/added" questions.

    Returns the most-added cards/lands with real inclusion percentages, and the
    most-cut cards/lands as a ranked list (EDHRec's unpopularity score, not a
    percentage). Precons that build as multiple commanders are handled explicitly;
    pass `commander` to switch faces. For an exact cut percentage against a
    specific deck, use precon_diff.
    """
    query = params.precon.strip()

    # 1. Resolve to a base slug (fast path for literal slugs, else gated resolve).
    slug: Optional[str] = None
    if re.fullmatch(r"[a-z0-9-]+", query):
        slug = query  # optimistic; validated by the fetch below
    else:
        try:
            kind, val = await _resolve_precon_slug(query)
        except Exception as e:
            return _edhrec_error(e)
        if kind == "candidates":
            return _precon_candidates_message(params.precon, val)
        slug = val

    # 2. Fetch the base page; if an optimistic slug 403/404s, fall back to resolve.
    try:
        page = await _edhrec_get(f"/pages/precon/{slug}.json")
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (403, 404):
            try:
                kind, val = await _resolve_precon_slug(query)
            except Exception as e2:
                return _edhrec_error(e2)
            if kind == "candidates":
                return _precon_candidates_message(params.precon, val)
            slug = val
            try:
                page = await _edhrec_get(f"/pages/precon/{slug}.json")
            except Exception as e2:
                return _edhrec_error(e2)
        else:
            return _edhrec_error(e)
    except Exception as e:
        return _edhrec_error(e)

    commander_counts = page.get("precon_commander_counts", []) or []
    base_href = f"/precon/{slug}"
    active_href = base_href
    selected = page

    # 3. Optional face-commander switch → fetch the sub-page.
    if params.commander and commander_counts:
        cq = _sanitize(params.commander)
        best_c, best_s = None, 0.0
        for c in commander_counts:
            s = _match_score(cq, _sanitize(c.get("value", "")))
            if s > best_s:
                best_s, best_c = s, c
        if best_c and best_s >= MATCH_THRESHOLD:
            href = best_c.get("href", "")
            if href and href != base_href and href.startswith("/precon/"):
                sub = href[len("/precon/"):]
                try:
                    selected = await _edhrec_get(f"/pages/precon/{sub}.json")
                    active_href = href
                except Exception as e:
                    return _edhrec_error(e)

    # Which commander do the shown tables describe?
    active_commander = next(
        (c for c in commander_counts if c.get("href") == active_href), None)

    # 4. Render.
    parts: list[str] = []
    parts.append(f"# {selected.get('header', 'Precon Upgrade')}")
    if commander_counts:
        parts.append("")
        if len(commander_counts) > 1:
            parts.append("**Commanders in this precon:**")
            for c in commander_counts:
                mark = "  ← shown below" if c.get("href") == active_href else ""
                parts.append(f"- {c.get('value', '?')} "
                             f"({c.get('count', '?')} decks){mark}")
            if not params.commander:
                parts.append("")
                parts.append('_Pass `commander="<name>"` to see another '
                             'commander\'s upgrades._')
    parts.append("")

    cardlists = {cl.get("tag"): cl for cl in
                 selected.get("container", {}).get("json_dict", {}).get("cardlists", [])}

    for tag, title in (("cardstoadd", "Cards to Add"), ("landstoadd", "Lands to Add")):
        cl = cardlists.get(tag)
        if cl and cl.get("cardviews"):
            parts.append(f"## {title}")
            for cv in cl["cardviews"][:params.limit]:
                inc, pot = cv.get("inclusion"), cv.get("potential_decks")
                pct = _pct(inc, pot) if inc is not None and pot else "?%"
                line = f"- **{cv.get('name', '?')}** — {pct}"
                if inc is not None and pot:
                    line += f" ({inc} of {pot} decks)"
                syn = cv.get("synergy")
                if syn is not None:
                    line += f" | synergy {syn:+.0%}"
                parts.append(line)
            parts.append("")

    any_cuts = False
    for tag, title in (("cardstocut", "Cards to Cut"), ("landstocut", "Lands to Cut")):
        cl = cardlists.get(tag)
        if cl and cl.get("cardviews"):
            any_cuts = True
            parts.append(f"## {title} (ranked by how often they're cut)")
            for cv in cl["cardviews"][:params.limit]:
                score = cv.get("unpopularity")
                suffix = (f" — cut-rank score {score:.2f}"
                          if isinstance(score, (int, float)) else "")
                parts.append(f"- **{cv.get('name', '?')}**{suffix}")
            parts.append("")

    if any_cuts:
        parts.append("_Cut ranking is EDHRec's unpopularity score (higher = cut "
                     "more often), not a percentage. For an exact cut percentage "
                     "against a specific deck, use precon_diff._")
        parts.append("")

    parts.append("---")
    footer = f"Source: https://edhrec.com{active_href}"
    if active_commander and active_commander.get("count") is not None:
        footer += f" | Based on {active_commander['count']} decks"
    parts.append(footer)

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# ARCHIDEKT — Deck reading and export
# ═══════════════════════════════════════════════════════════════════════════════


async def _archidekt_get(path: str, params: Optional[Dict[str, Any]] = None) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ARCHIDEKT_API}{path}",
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


def _archidekt_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 404:
            return "Deck not found on Archidekt. It may be private or the ID is wrong."
        if status == 429:
            return "Rate limited by Archidekt. Wait a moment and retry."
        return f"Archidekt API error ({status}): {e.response.text[:300]}"
    if isinstance(e, httpx.TimeoutException):
        return "Request to Archidekt timed out. Try again."
    return f"Unexpected error: {type(e).__name__}: {e}"


def _archidekt_in_deck_cards(data: dict) -> dict[str, int]:
    """Return {card_name: quantity} for cards in the deck (commander included,
    maybeboard excluded). Mirrors the inclusion logic used by archidekt_deck."""
    categories = {c["name"]: c for c in data.get("categories", [])}
    counts: dict[str, int] = {}
    for entry in data.get("cards", []):
        qty = entry.get("quantity", 1)
        name = entry.get("card", {}).get("oracleCard", {}).get("name", "?")
        entry_cats = entry.get("categories", [])
        if entry_cats:
            in_deck = any(
                categories.get(cn, {}).get("isPremier", False)
                or categories.get(cn, {}).get("includedInDeck", True)
                for cn in entry_cats
            )
        else:
            in_deck = True  # uncategorized cards default into the deck
        if in_deck:
            counts[name] = counts.get(name, 0) + qty
    return counts


def _archidekt_type_line(oracle: dict) -> str:
    """'Legendary Creature — Human Warlock' from Archidekt's split type fields."""
    left = " ".join([*(oracle.get("superTypes") or []), *(oracle.get("types") or [])])
    subs = " ".join(oracle.get("subTypes") or [])
    return f"{left} — {subs}" if subs else left


def _archidekt_face_lines(face: dict) -> list[str]:
    """Type/PT/loyalty line, then rules text, for one oracleCard or face dict.

    Archidekt reports absent power/toughness as '' (Vehicles do carry real
    values despite not being creatures), and absent loyalty as None.
    """
    lines: list[str] = []
    header = _archidekt_type_line(face)
    power, toughness = face.get("power"), face.get("toughness")
    if power not in (None, "") and toughness not in (None, ""):
        header = f"{header} {power}/{toughness}".strip()
    if face.get("loyalty"):
        header = f"{header} [Loyalty {face['loyalty']}]".strip()
    if header:
        lines.append(header)
    text = (face.get("text") or "").strip()
    if text:
        lines.extend(text.split("\n"))
    return lines


def _archidekt_card_detail(oracle: dict) -> list[str]:
    """Detail lines for include_text; face-by-face for multi-faced cards.

    Multi-faced cards carry empty top-level text and a combined manaCost —
    the real data lives in `faces`.
    """
    faces = oracle.get("faces") or []
    if not faces:
        return _archidekt_face_lines(oracle)
    lines: list[str] = []
    for i, face in enumerate(faces):
        if i:
            lines.append("//")
        lines.append(f"{face.get('name', '?')} {face.get('manaCost') or ''}".strip())
        lines.extend(_archidekt_face_lines(face))
    return lines


def _parse_deck_id(deck_ref: str) -> str:
    """Extract deck ID from an Archidekt URL or raw ID."""
    match = re.search(r"archidekt\.com/decks/(\d+)", deck_ref)
    if match:
        return match.group(1)
    match = re.match(r"^\d+$", deck_ref.strip())
    if match:
        return match.group(0)
    return deck_ref.strip()


FORMAT_NAMES = {1: "Standard", 2: "Modern", 3: "Commander", 4: "Legacy",
                5: "Vintage", 6: "Pauper", 7: "Custom", 8: "Frontier",
                9: "Future Standard", 10: "Penny Dreadful", 11: "1v1 Commander",
                12: "Duel Commander", 13: "Brawl", 14: "Oathbreaker",
                15: "Pioneer", 16: "Historic", 17: "Pauper Commander",
                18: "Alchemy", 19: "Explorer", 20: "Historic Brawl",
                21: "Gladiator", 22: "Premodern", 23: "Predh", 24: "Standard Brawl"}


# ── Archidekt Input Models ───────────────────────────────────────────────────


class ArchidektDeckInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    deck: str = Field(
        ...,
        description="Archidekt deck ID or full URL (e.g., '365563' or 'https://archidekt.com/decks/365563').",
        min_length=1,
    )
    include_text: bool = Field(
        default=False,
        description=(
            "Include mana cost, type line, and full oracle text for every "
            "card. Set true whenever you will discuss what cards do — never "
            "rely on memory for card text."
        ),
    )


class ArchidektUserInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    username: str = Field(..., description="Archidekt username.", min_length=1, max_length=100)
    limit: int = Field(default=20, ge=1, le=60)


# ── Archidekt Tools ──────────────────────────────────────────────────────────


@mcp.tool(name="archidekt_deck")
async def archidekt_deck(params: ArchidektDeckInput) -> str:
    """Fetch a public Archidekt deck by ID or URL.

    Returns the deck's cards organized by category, with commander info,
    format, and deck statistics.
    """
    deck_id = _parse_deck_id(params.deck)
    try:
        data = await _archidekt_get(f"/decks/{deck_id}/")
    except Exception as e:
        return _archidekt_error(e)

    parts: list[str] = []
    name = data.get("name", "Unknown Deck")
    fmt = FORMAT_NAMES.get(data.get("deckFormat", 0), "Unknown")
    owner = data.get("owner", {}).get("username", "?")
    bracket = data.get("edhBracket")

    parts.append(f"# {name}")
    parts.append(f"Owner: {owner} | Format: {fmt}" + (f" | Bracket: {bracket}" if bracket else ""))
    parts.append("")

    # Parse categories
    categories = {c["name"]: c for c in data.get("categories", [])}
    cards_by_cat: dict[str, list[str]] = {}
    total_in_deck = 0

    for entry in data.get("cards", []):
        qty = entry.get("quantity", 1)
        oracle = entry.get("card", {}).get("oracleCard", {})
        card_name = oracle.get("name", "?")
        entry_cats = entry.get("categories", [])

        for cat_name in entry_cats:
            cat_def = categories.get(cat_name, {})
            included = cat_def.get("includedInDeck", True)
            is_premier = cat_def.get("isPremier", False)

            cards_by_cat.setdefault(cat_name, [])
            prefix = ""
            if is_premier:
                prefix = "[CMDR] "
            elif not included:
                prefix = "[MB] "
            line = f"{qty} {prefix}{card_name}"
            if params.include_text:
                if oracle.get("manaCost"):
                    line += f" {oracle['manaCost']}"
                detail = _archidekt_card_detail(oracle)
                if detail:
                    line += "\n" + "\n".join(f"   {d}" for d in detail)
            cards_by_cat[cat_name].append(line)

            if included:
                total_in_deck += qty

    # Show commander(s) first
    for cat_name, card_list in sorted(cards_by_cat.items()):
        cat_def = categories.get(cat_name, {})
        if cat_def.get("isPremier"):
            parts.append(f"## {cat_name}")
            for line in card_list:
                parts.append(line)
            parts.append("")

    # Then other categories
    for cat_name, card_list in sorted(cards_by_cat.items()):
        cat_def = categories.get(cat_name, {})
        if not cat_def.get("isPremier"):
            included = cat_def.get("includedInDeck", True)
            suffix = "" if included else " (not in deck)"
            parts.append(f"## {cat_name}{suffix} ({len(card_list)})")
            for line in card_list:
                parts.append(line)
            parts.append("")

    parts.append(f"**Total in deck: {total_in_deck}**")

    return "\n".join(parts)


@mcp.tool(name="archidekt_user_decks")
async def archidekt_user_decks(params: ArchidektUserInput) -> str:
    """List a user's public decks on Archidekt."""
    try:
        data = await _archidekt_get("/decks/v3/", params={
            "owner": params.username,
            "ownerexact": "true",
            "orderBy": "-updatedAt",
            "pageSize": params.limit,
        })
    except Exception as e:
        return _archidekt_error(e)

    results = data.get("results", [])
    total = data.get("count", len(results))

    parts: list[str] = []
    parts.append(f"# Decks by {params.username} ({total} total)")
    parts.append("")

    for deck in results:
        deck_id = deck.get("id", "?")
        name = deck.get("name", "?")
        fmt = FORMAT_NAMES.get(deck.get("deckFormat", 0), "?")
        updated = deck.get("updatedAt", "")[:10]
        parts.append(f"- **{name}** (ID: {deck_id}) — {fmt}, updated {updated}")

    if not results:
        parts.append("No public decks found.")

    return "\n".join(parts)


@mcp.tool(name="archidekt_export")
async def archidekt_export(params: ArchidektDeckInput) -> str:
    """Export an Archidekt deck in Archidekt-compatible import format.

    Uses the full Archidekt import syntax with set codes, finishes, categories,
    and labels:
      1x Card Name (set) 123 *F* [Category{flags}] ^Label,#hex^

    Foil and etched cards keep their finish (*F* / *E*), so the exported list
    re-imports as the same cards and prices correctly via scryfall_price_list.

    Category flags: {top} for commander, {noDeck}{noPrice} for maybeboard.
    Output can be pasted directly into Archidekt's import dialog.
    """
    deck_id = _parse_deck_id(params.deck)
    try:
        data = await _archidekt_get(f"/decks/{deck_id}/")
    except Exception as e:
        return _archidekt_error(e)

    categories = {c["name"]: c for c in data.get("categories", [])}

    parts: list[str] = []
    total_in_deck = 0

    for entry in data.get("cards", []):
        qty = entry.get("quantity", 1)
        card_data = entry.get("card", {})
        oracle = card_data.get("oracleCard", {})
        card_name = oracle.get("name", "?")
        edition = card_data.get("edition", {})
        set_code = edition.get("editioncode", "")
        collector = card_data.get("collectorNumber", "")
        entry_cats = entry.get("categories", [])
        labels = entry.get("labels") or []

        modifier = entry.get("modifier", "")

        # Determine category annotation and deck inclusion
        cat_annotation = ""
        is_in_deck = True
        for cat_name in entry_cats:
            cat_def = categories.get(cat_name, {})
            if cat_def.get("isPremier"):
                cat_annotation = f" [{cat_name}{{top}}]"
            elif not cat_def.get("includedInDeck", True):
                cat_annotation = f" [{cat_name}{{noDeck}}{{noPrice}}]"
                is_in_deck = False
            else:
                cat_annotation = f" [{cat_name}]"

        if is_in_deck:
            total_in_deck += qty

        line = _archidekt_line(
            quantity=qty,
            name=card_name,
            set_code=set_code,
            collector=collector,
            finish=MODIFIER_FINISHES.get(modifier),
            category=cat_annotation,
            labels="",
        )

        # Labels
        for label in labels:
            label_name = label.get("name", "")
            label_color = label.get("color", "")
            if label_name:
                if label_color:
                    line += f" ^{label_name},{label_color}^"
                else:
                    line += f" ^{label_name}^"

        parts.append(line)

    parts.sort()
    parts.append("")
    parts.append(f"# Total in deck: {total_in_deck}")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# FORMATTING — Generate Archidekt-importable decklists
# ═══════════════════════════════════════════════════════════════════════════════


def _archidekt_line(
    quantity: int,
    name: str,
    set_code: str,
    collector: str,
    finish: Optional[str],
    category: str,
    labels: str,
) -> str:
    """Build one Archidekt import line.

    Grammar, verified against Archidekt's own text export 2026-08-08:
      {qty}x {name} ({set}) {collector} *F* [{Category}{flags}] ^{Label},{#hex}^

    `category` and `labels` arrive already formatted with their leading space.
    """
    line = f"{quantity}x {name}"
    if set_code:
        line += f" ({set_code})"
    if collector:
        line += f" {collector}"
    marker = _finish_marker(finish)
    if marker:
        line += f" {marker}"
    return line + category + labels


class DeckCardEntry(BaseModel):
    """A single card entry for deck formatting."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Card name.")
    quantity: int = Field(default=1, ge=1, le=99)
    finish: Optional[CardFinish] = Field(
        default=None,
        description=(
            "Card finish. 'foil' emits *F* and 'etched' emits *E* on the line, "
            "which Archidekt imports as that finish. Omit for a normal card."
        ),
    )
    category: Optional[str] = Field(default=None, description="Category name (e.g., 'Ramp', 'Draw', 'Removal', 'Lands').")
    commander: bool = Field(default=False, description="True if this is the commander (or partner).")
    maybeboard: bool = Field(default=False, description="True if this should go in the maybeboard.")
    label: Optional[str] = Field(default=None, description="Optional label text (e.g., 'To Buy').")
    label_color: Optional[str] = Field(default=None, description="Optional label hex color (e.g., '#2ccce4').")


class FormatDeckInput(BaseModel):
    """Input for format_archidekt."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    cards: list[DeckCardEntry] = Field(
        ...,
        description="List of cards with optional category, commander/maybeboard flags, and labels.",
        min_length=1,
    )
    include_set_codes: bool = Field(
        default=False,
        description="If true, look up and include set codes from Scryfall. Slower but useful for specific printings.",
    )


@mcp.tool(name="format_archidekt")
async def format_archidekt(params: FormatDeckInput) -> str:
    """REQUIRED when outputting any decklist for Archidekt import. Do NOT manually
    format decklists — always call this tool instead.

    Takes card names with categories, commander/maybeboard flags, and labels,
    validates all names against Scryfall, and outputs text that pastes directly
    into Archidekt's import dialog.

    Output format (Archidekt native):
      1x Card Name [Category]
      1x Commander Name [Commander{top}]
      1x Maybe Card [Maybeboard{noDeck}{noPrice}]
      1x Labeled Card [Draw] ^To Buy,#2ccce4^
      1x Foil Card (dmr) 281 *F* [Ramp]

    Set finish='foil' or finish='etched' on a card to mark it as such (*F* / *E*).
    Pair it with include_set_codes so the marked line names a printing.

    Category examples: Ramp, Draw, Removal, Counters, Evasion, Finisher,
    Sacrifice, Recursion, Lands, Protection, Combo, Tokens, Tribal, etc.
    Assign categories based on each card's role in the deck.
    """
    # Batch validate on Scryfall
    unique_names = list({c.name for c in params.cards})
    found: dict[str, dict] = {}
    not_found_names: list[str] = []

    for i in range(0, len(unique_names), 75):
        batch = unique_names[i:i + 75]
        identifiers = [{"name": name} for name in batch]
        try:
            data = await _scryfall_post("/cards/collection", {"identifiers": identifiers})
            for card in data.get("data", []):
                found[card["name"].lower()] = card
            for item in data.get("not_found", []):
                not_found_names.append(item.get("name", str(item)))
        except Exception as e:
            return f"Scryfall lookup failed: {_scryfall_error(e)}"

    lines: list[str] = []
    warnings: list[str] = []

    for entry in params.cards:
        scryfall_card = found.get(entry.name.lower())

        # Use Scryfall's canonical name if found
        if scryfall_card:
            card_name = scryfall_card["name"]
        else:
            card_name = entry.name
            warnings.append(f"# WARNING: '{entry.name}' not found on Scryfall")

        set_code = ""
        collector = ""
        if params.include_set_codes and scryfall_card:
            set_code = scryfall_card.get("set", "")
            collector = scryfall_card.get("collector_number", "")

        # Category annotation
        cat_annotation = ""
        if entry.commander:
            cat_annotation = " [Commander{top}]"
        elif entry.maybeboard:
            cat_annotation = " [Maybeboard{noDeck}{noPrice}]"
        elif entry.category:
            cat_annotation = f" [{entry.category}]"

        # Labels
        label_text = ""
        if entry.label:
            if entry.label_color:
                label_text = f" ^{entry.label},{entry.label_color}^"
            else:
                label_text = f" ^{entry.label}^"

        line = _archidekt_line(
            quantity=entry.quantity,
            name=card_name,
            set_code=set_code,
            collector=collector,
            finish=entry.finish.value if entry.finish else None,
            category=cat_annotation,
            labels=label_text,
        )

        lines.append(line)

    lines.sort()

    # Append warnings at end
    if warnings:
        lines.append("")
        lines.extend(warnings)

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# VALIDATION — Decklist and deck verification
# ═══════════════════════════════════════════════════════════════════════════════


class ValidateDecklistInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    decklist: str = Field(
        ...,
        description="Decklist in '1 Card Name' format (one per line). Lines starting with // are treated as category headers.",
        min_length=1,
    )
    commander: Optional[str] = Field(
        default=None,
        description="Commander name for color identity validation. If omitted, validation skips color checks.",
    )


class ValidateArchidektInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    deck: str = Field(
        ...,
        description="Archidekt deck ID or URL.",
        min_length=1,
    )


@dataclass(frozen=True)
class DecklistEntry:
    """One parsed decklist line, including the printing it names."""
    quantity: int
    name: str
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    finish: Optional[str] = None


_QTY_RE = re.compile(r"^(\d+)x?\s+(.+)$")
_LABEL_RE = re.compile(r"\s*\^[^^]*\^")
_CATEGORY_RE = re.compile(r"\s*\[[^\]]*\]")
_MARKER_RE = re.compile(r"\s*\*([FE])\*", re.IGNORECASE)
_WORD_FINISH_RE = re.compile(r"\s*[(\[](foil|etched)[)\]]", re.IGNORECASE)
# Anchored to the end so it can only match a trailing printing suffix, never a
# card name that happens to contain parentheses (e.g. "B.F.M. (Big Furry
# Monster)" — the inner text has spaces and cannot match a set token).
_SET_CN_RE = re.compile(r"\s*\((?P<set>[a-zA-Z0-9]{2,6})\)(?:\s+(?P<cn>[^\s\[\]^*]+))?\s*$")


def _parse_decklist_entries(text: str) -> list[DecklistEntry]:
    """Parse a decklist into entries that keep set, collector number, and finish.

    Handles the Archidekt/Moxfield grammar:
      [qty][x] Name [(set)] [collector] [*F*|*E*] [[Category{flags}]] [^Label,#hex^]

    Bare names and quantity-only lines still parse; the printing fields are just
    None. Lines starting with '#' or '//' are comments.
    """
    entries: list[DecklistEntry] = []
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue

        match = _QTY_RE.match(line)
        if match:
            qty = int(match.group(1))
            rest = match.group(2).strip()
        else:
            qty = 1
            rest = line

        # Labels and categories carry no pricing information.
        rest = _LABEL_RE.sub("", rest)
        rest = _CATEGORY_RE.sub("", rest)

        # Finish marker, before the set suffix so that "(foil)" is consumed here
        # rather than being mistaken for a 4-character set code below.
        finish: Optional[str] = None
        marker = _MARKER_RE.search(rest)
        if marker:
            finish = MARKER_FINISHES[marker.group(1).upper()]
            rest = _MARKER_RE.sub("", rest, count=1)
        else:
            worded = _WORD_FINISH_RE.search(rest)
            if worded:
                finish = worded.group(1).lower()
                rest = _WORD_FINISH_RE.sub("", rest, count=1)

        # Set and collector number, anchored at the end of what remains.
        set_code: Optional[str] = None
        collector: Optional[str] = None
        suffix = _SET_CN_RE.search(rest)
        if suffix:
            set_code = suffix.group("set").lower()
            collector = suffix.group("cn")
            rest = rest[:suffix.start()]

        # Legacy residual strips, kept verbatim so validate_decklist and
        # precon_diff see the names they have always seen.
        name = rest
        name = re.sub(r"\s*\^[^^]*\^", "", name)
        name = re.sub(r"\s*\[[^\]]*\]", "", name)
        name = re.sub(r"\s+\d+$", "", name)
        name = re.sub(r"\s*\([a-z0-9]+\)$", "", name)
        name = name.strip()

        if name:
            entries.append(DecklistEntry(qty, name, set_code, collector, finish))
    return entries


def _parse_decklist(text: str) -> list[tuple[int, str]]:
    """Parse a decklist into (quantity, card_name) tuples.

    Thin wrapper over _parse_decklist_entries, kept for validate_decklist and
    precon_diff, which do not care about printings.
    """
    return [(e.quantity, e.name) for e in _parse_decklist_entries(text)]


def _entry_identifier(entry: DecklistEntry) -> dict:
    """Scryfall /cards/collection identifier naming this entry's printing.

    Set plus collector number pins one printing exactly. Set alone lets Scryfall
    choose within that set. Neither means Scryfall picks the default printing,
    which the caller is responsible for flagging in its output.
    """
    if entry.set_code and entry.collector_number:
        return {"set": entry.set_code, "collector_number": entry.collector_number}
    if entry.set_code:
        return {"name": entry.name, "set": entry.set_code}
    return {"name": entry.name}


def _identifier_key(identifier: dict) -> str:
    """Stable comparable key for a Scryfall identifier dict.

    Identifier shapes differ ({set,collector_number} / {name,set} / {name}), so
    sort before repr to get a key that does not depend on construction order.
    """
    return repr(sorted(identifier.items()))


def _dedupe_identifiers(entries: list[DecklistEntry]) -> list[dict]:
    """Unique identifiers for these entries, in first-seen order.

    Several lines may name the same printing; each printing only needs asking
    about once.
    """
    identifiers: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        identifier = _entry_identifier(entry)
        key = _identifier_key(identifier)
        if key not in seen:
            seen.add(key)
            identifiers.append(identifier)
    return identifiers


def _chunk(items: list, size: int) -> list[list]:
    """Split items into chunks of at most `size`."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _price_for_finish(card: dict, finish: Optional[str]) -> Optional[Decimal]:
    """USD price of this card in exactly the requested finish, or None.

    Deliberately does NOT fall back to another finish. A missing price is
    reported as missing; quoting a foil price for a nonfoil request produces a
    confidently wrong number, which is worse than no number.
    """
    key = FINISH_PRICE_KEYS.get(finish or "nonfoil", "usd")
    raw = (card.get("prices") or {}).get(key)
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _card_name_aliases(card: dict) -> list[str]:
    """Lowercased names a decklist line might use for this card.

    Includes each face of a double-faced card, since a list may write only the
    front face while Scryfall returns 'Front // Back'.
    """
    full = (card.get("name") or "").lower().strip()
    if not full:
        return []
    aliases = [full]
    if "//" in full:
        aliases.extend(part.strip() for part in full.split("//") if part.strip())
    return aliases


def _index_collection_results(cards: list[dict]) -> dict:
    """Index collection results for lookup by printing, name+set, or name.

    Scryfall does not guarantee that response order matches request order, and
    omits not-found entries, so results must be matched explicitly rather than
    by position. First card wins on any given key.
    """
    index: dict = {}
    for card in cards:
        set_code = (card.get("set") or "").lower()
        collector = card.get("collector_number") or ""
        if set_code and collector:
            index.setdefault(("printing", set_code, collector), card)
        for alias in _card_name_aliases(card):
            if set_code:
                index.setdefault(("name_set", alias, set_code), card)
            index.setdefault(("name", alias), card)
    return index


def _lookup_entry(index: dict, entry: DecklistEntry) -> Optional[dict]:
    """Find the card matching this entry, most specific key first.

    An entry that names a collector number is asking for one exact printing, so
    a miss there is a miss — degrading to another printing of the same card
    would report someone else's price as the user's. Entries that named only a
    set, or only a name, do degrade.
    """
    name = entry.name.lower().strip()
    set_code = (entry.set_code or "").lower()

    if set_code and entry.collector_number:
        return index.get(("printing", set_code, entry.collector_number))
    if set_code:
        hit = index.get(("name_set", name, set_code))
        if hit is not None:
            return hit
    return index.get(("name", name))


def _identifier_label(identifier: dict) -> str:
    """Human-readable rendering of an identifier we sent to Scryfall.

    /cards/collection echoes unmatched identifiers back verbatim, so this is
    what the 'not found' section prints.
    """
    name = identifier.get("name")
    set_code = identifier.get("set")
    collector = identifier.get("collector_number")
    if set_code and collector:
        return f"{set_code.upper()} #{collector}"
    if name and set_code:
        return f"{name} ({set_code.upper()})"
    return name or str(identifier)


def _printing_label(card: dict, finish: Optional[str]) -> str:
    """'Counterspell (DMR #281, foil)' — the printing a price refers to."""
    name = card.get("name", "?")
    set_code = (card.get("set") or "?").upper()
    collector = card.get("collector_number", "?")
    return f"{name} ({set_code} #{collector}, {finish or 'nonfoil'})"


def _available_finishes(card: dict) -> str:
    """What this printing does exist in, and what those cost.

    Shown when the requested finish has no price, so the user learns why rather
    than just that the number is missing.
    """
    bits: list[str] = []
    for finish in ("nonfoil", "foil", "etched"):
        if finish not in (card.get("finishes") or []):
            continue
        price = _price_for_finish(card, finish)
        bits.append(f"{finish} ${price:.2f}" if price is not None else f"{finish} (no price)")
    return ", ".join(bits) if bits else "none listed"


def _entry_suffix(entry: DecklistEntry) -> str:
    """What the decklist line asked for, for lines with no matching card."""
    bits: list[str] = []
    if entry.set_code:
        printing = entry.set_code.upper()
        if entry.collector_number:
            printing += f" #{entry.collector_number}"
        bits.append(f"({printing})")
    if entry.finish:
        bits.append(entry.finish)
    return (" " + " ".join(bits)) if bits else ""


def _build_price_sections(
    entries: list[DecklistEntry],
    index: dict,
    not_found: list[dict],
    unchecked: Optional[list[dict]] = None,
) -> dict:
    """Sort priced lines into sections and total only what was actually priced.

    Returns lists of preformatted strings plus the total, so the tool body is
    assembly only and this logic stays testable without network access.
    """
    priced: list[tuple[Decimal, str]] = []
    defaulted: list[tuple[Decimal, str]] = []
    no_price: list[str] = []
    missing: list[str] = []
    missing_identifiers: set[str] = set()
    unchecked_lines: list[str] = []
    unchecked_keys = {_identifier_key(i) for i in (unchecked or [])}

    total = Decimal("0")
    priced_cards = 0

    for entry in entries:
        card = _lookup_entry(index, entry)
        if card is None:
            entry_key = _identifier_key(_entry_identifier(entry))
            if entry_key in unchecked_keys:
                unchecked_lines.append(
                    f"{entry.quantity}x {entry.name}{_entry_suffix(entry)}")
            else:
                missing.append(f"{entry.quantity}x {entry.name}{_entry_suffix(entry)}")
                missing_identifiers.add(entry_key)
            continue

        unit = _price_for_finish(card, entry.finish)
        if unit is None:
            no_price.append(
                f"{entry.quantity}x {_printing_label(card, entry.finish)} — "
                f"no {entry.finish or 'nonfoil'} price. "
                f"This printing has: {_available_finishes(card)}"
            )
            continue

        line_total = unit * entry.quantity
        total += line_total
        priced_cards += entry.quantity
        text = (f"{entry.quantity}x {_printing_label(card, entry.finish)} — "
                f"${unit:.2f} ea → ${line_total:.2f}")
        # A line that named no set got whichever printing Scryfall chose.
        (priced if entry.set_code else defaulted).append((line_total, text))

    priced.sort(key=lambda item: item[0], reverse=True)
    defaulted.sort(key=lambda item: item[0], reverse=True)

    # Every not_found identifier corresponds to some entry above whose lookup
    # already failed (that's the identifier we sent for it), so it is already
    # represented in `missing` with quantity and printing detail. Only surface
    # a not_found identifier here if it somehow has no matching line above —
    # defensive, but avoids reporting the same missing card twice.
    for identifier in not_found:
        key = _identifier_key(identifier)
        if key not in missing_identifiers:
            missing.append(_identifier_label(identifier))

    return {
        "priced": [text for _, text in priced],
        "defaulted": [text for _, text in defaulted],
        "no_price": no_price,
        "missing": missing,
        "unchecked": unchecked_lines,
        "total": total,
        "priced_cards": priced_cards,
    }


@mcp.tool(name="validate_decklist")
async def validate_decklist(params: ValidateDecklistInput) -> str:
    """Validate a Commander decklist for card name accuracy, deck size, and color identity.

    Checks all card names against Scryfall, verifies deck size is 100
    (including commander), and optionally checks color identity.
    Returns a structured pass/fail report.
    """
    cards = _parse_decklist(params.decklist)
    if not cards:
        return "No cards found in decklist. Use '1 Card Name' format, one per line."

    issues: list[str] = []
    total_qty = sum(qty for qty, _ in cards)

    # Batch-verify card names via Scryfall collection API (75 at a time)
    unique_names = list({name for _, name in cards})
    all_found: dict[str, dict] = {}
    not_found: list[str] = []

    for i in range(0, len(unique_names), 75):
        batch = unique_names[i:i + 75]
        identifiers = [{"name": name} for name in batch]
        try:
            data = await _scryfall_post("/cards/collection", {"identifiers": identifiers})
            for card in data.get("data", []):
                all_found[card["name"].lower()] = card
            for item in data.get("not_found", []):
                not_found.append(item.get("name", str(item)))
        except Exception as e:
            issues.append(f"Scryfall lookup failed: {_scryfall_error(e)}")

    # Report invalid card names
    if not_found:
        issues.append(f"**{len(not_found)} card(s) not found on Scryfall:**")
        for name in not_found:
            issues.append(f"  - {name}")

    # Deck size check
    if total_qty != 100:
        issues.append(f"**Deck size: {total_qty}** (expected 100 for Commander)")

    # Commander color identity check
    if params.commander:
        commander_data = all_found.get(params.commander.lower())
        if not commander_data:
            # Try fetching commander directly
            try:
                commander_data = await _scryfall_get("/cards/named", params={"exact": params.commander})
            except Exception:
                issues.append(f"Could not look up commander '{params.commander}' on Scryfall.")

        if commander_data:
            # Check if it's a legal commander
            type_line = commander_data.get("type_line", "")
            oracle_text = commander_data.get("oracle_text", "")
            is_legendary_creature = "Legendary" in type_line and "Creature" in type_line
            can_be_commander = "can be your commander" in oracle_text.lower()
            if not is_legendary_creature and not can_be_commander:
                issues.append(f"**{params.commander}** is not a legal commander (not a legendary creature and doesn't have 'can be your commander').")

            # Color identity check
            commander_ci = set(commander_data.get("color_identity", []))
            violations: list[str] = []
            for _, name in cards:
                card_data = all_found.get(name.lower())
                if card_data:
                    card_ci = set(card_data.get("color_identity", []))
                    outside = card_ci - commander_ci
                    if outside:
                        violations.append(f"  - {name} (has {', '.join(sorted(outside))})")

            if violations:
                ci_str = ', '.join(sorted(commander_ci)) if commander_ci else 'Colorless'
                issues.append(f"**{len(violations)} card(s) outside commander's color identity ({ci_str}):**")
                issues.extend(violations)

    # Build report
    parts: list[str] = []
    if issues:
        parts.append(f"# Validation: ISSUES FOUND")
        parts.append("")
        parts.extend(issues)
    else:
        parts.append(f"# Validation: PASSED")
        parts.append("")
        parts.append(f"All {len(unique_names)} unique cards verified on Scryfall.")
        parts.append(f"Deck size: {total_qty} cards.")
        if params.commander:
            parts.append(f"All cards within {params.commander}'s color identity.")

    return "\n".join(parts)


@mcp.tool(name="validate_archidekt_deck")
async def validate_archidekt_deck(params: ValidateArchidektInput) -> str:
    """Validate an Archidekt deck for card accuracy, deck size, color identity, and category structure.

    Fetches the deck, verifies all cards on Scryfall, checks Commander format
    rules, and validates Archidekt-specific category structure (commander zone,
    maybeboard, card counts).
    """
    deck_id = _parse_deck_id(params.deck)
    try:
        data = await _archidekt_get(f"/decks/{deck_id}/")
    except Exception as e:
        return _archidekt_error(e)

    issues: list[str] = []
    deck_name = data.get("name", "Unknown")
    fmt = FORMAT_NAMES.get(data.get("deckFormat", 0), "Unknown")

    parts: list[str] = []
    parts.append(f"# Validating: {deck_name} ({fmt})")
    parts.append("")

    categories = {c["name"]: c for c in data.get("categories", [])}

    # Parse all cards
    in_deck_cards: list[tuple[int, str]] = []
    commander_cards: list[str] = []
    maybeboard_cards: list[str] = []
    uncategorized: list[str] = []
    cat_counts: Counter[str] = Counter()

    for entry in data.get("cards", []):
        qty = entry.get("quantity", 1)
        oracle = entry.get("card", {}).get("oracleCard", {})
        card_name = oracle.get("name", "?")
        entry_cats = entry.get("categories", [])

        if not entry_cats:
            uncategorized.append(card_name)

        in_deck = False
        for cat_name in entry_cats:
            cat_def = categories.get(cat_name, {})
            cat_counts[cat_name] += qty

            if cat_def.get("isPremier"):
                commander_cards.append(card_name)
                in_deck = True
            elif cat_def.get("includedInDeck", True):
                in_deck = True
            else:
                maybeboard_cards.append(card_name)

        if in_deck:
            in_deck_cards.append((qty, card_name))

    total_in_deck = sum(qty for qty, _ in in_deck_cards)

    # Category structure checks
    if not commander_cards:
        issues.append("**No commander found.** No card is in a premier (Commander) category.")
    elif len(commander_cards) > 2:
        issues.append(f"**{len(commander_cards)} cards in Commander zone** (expected 1-2): {', '.join(commander_cards)}")

    if uncategorized:
        issues.append(f"**{len(uncategorized)} uncategorized card(s):** {', '.join(uncategorized[:10])}")

    # Deck size (Commander = 100)
    is_commander = data.get("deckFormat") == 3
    if is_commander and total_in_deck != 100:
        issues.append(f"**Deck size: {total_in_deck}** (expected 100 for Commander)")

    commander_name = commander_cards[0] if commander_cards else None

    # Batch verify on Scryfall
    unique_names = list({name for _, name in in_deck_cards})
    all_found: dict[str, dict] = {}
    not_found_names: list[str] = []

    for i in range(0, len(unique_names), 75):
        batch = unique_names[i:i + 75]
        identifiers = [{"name": name} for name in batch]
        try:
            resp = await _scryfall_post("/cards/collection", {"identifiers": identifiers})
            for card in resp.get("data", []):
                all_found[card["name"].lower()] = card
            for item in resp.get("not_found", []):
                not_found_names.append(item.get("name", str(item)))
        except Exception as e:
            issues.append(f"Scryfall lookup error: {_scryfall_error(e)}")

    if not_found_names:
        issues.append(f"**{len(not_found_names)} card(s) not found on Scryfall:**")
        for name in not_found_names:
            issues.append(f"  - {name}")

    # Commander legality + color identity
    if commander_name and is_commander:
        cmd_data = all_found.get(commander_name.lower())
        if cmd_data:
            type_line = cmd_data.get("type_line", "")
            oracle_text = cmd_data.get("oracle_text", "")
            is_legendary = "Legendary" in type_line and "Creature" in type_line
            can_be_cmdr = "can be your commander" in oracle_text.lower()
            if not is_legendary and not can_be_cmdr:
                issues.append(f"**{commander_name}** is not a legal commander.")

            commander_ci = set(cmd_data.get("color_identity", []))
            violations: list[str] = []
            for _, name in in_deck_cards:
                card_data = all_found.get(name.lower())
                if card_data:
                    card_ci = set(card_data.get("color_identity", []))
                    outside = card_ci - commander_ci
                    if outside:
                        violations.append(f"  - {name} (has {', '.join(sorted(outside))})")

            if violations:
                ci_str = ', '.join(sorted(commander_ci)) if commander_ci else 'Colorless'
                issues.append(f"**{len(violations)} color identity violation(s) ({ci_str}):**")
                issues.extend(violations)

    # Category summary
    parts.append("## Category Breakdown")
    for cat_name, count in cat_counts.most_common():
        cat_def = categories.get(cat_name, {})
        flags = []
        if cat_def.get("isPremier"):
            flags.append("commander zone")
        if not cat_def.get("includedInDeck", True):
            flags.append("not in deck")
        suffix = f" ({', '.join(flags)})" if flags else ""
        parts.append(f"- {cat_name}: {count}{suffix}")
    parts.append("")

    # Issues or pass
    if issues:
        parts.append("## Issues Found")
        parts.extend(issues)
    else:
        parts.append("## Result: PASSED")
        parts.append(f"All {len(unique_names)} cards verified. Deck size: {total_in_deck}. Categories valid.")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMANDER SPELLBOOK — Combo search
# ═══════════════════════════════════════════════════════════════════════════════


async def _spellbook_get(path: str, params: Optional[Dict[str, Any]] = None) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SPELLBOOK_API}{path}",
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


def _spellbook_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"Commander Spellbook API error ({e.response.status_code}): {e.response.text[:300]}"
    if isinstance(e, httpx.TimeoutException):
        return "Request to Commander Spellbook timed out. Try again."
    return f"Unexpected error: {type(e).__name__}: {e}"


def _format_combo(variant: dict) -> str:
    lines: list[str] = []

    # Cards used
    uses = variant.get("uses", [])
    card_names = [u.get("card", {}).get("name", "?") for u in uses]
    lines.append(f"**Cards:** {' + '.join(card_names)}")

    # Color identity
    identity = variant.get("identity", "")
    if identity:
        lines.append(f"Colors: {identity}")

    # Mana needed
    mana = variant.get("manaNeeded", "")
    if mana:
        lines.append(f"Mana needed: {mana}")

    # Prerequisites
    prereqs = variant.get("easyPrerequisites", "")
    if prereqs:
        lines.append(f"Prerequisites: {prereqs}")

    # Steps
    desc = variant.get("description", "")
    if desc:
        lines.append(f"Steps: {desc}")

    # Results
    produces = variant.get("produces", [])
    results = [p.get("feature", {}).get("name", "?") for p in produces if p.get("feature")]
    if results:
        lines.append(f"Produces: {', '.join(results)}")

    # Popularity
    pop = variant.get("popularity")
    if pop:
        lines.append(f"Popularity: {pop} decks")

    return "\n".join(lines)


# ── Spellbook Input Models ───────────────────────────────────────────────────


class SpellbookCombosInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    cards: list[str] = Field(
        ...,
        description="Card names to find combos for (e.g., ['Dualcaster Mage', 'Twinflame']).",
        min_length=1, max_length=10,
    )
    color_identity: Optional[str] = Field(
        default=None,
        description="Optional color identity filter (e.g., 'wubrg', 'bg', 'r').",
    )
    limit: int = Field(default=10, ge=1, le=25)


class SpellbookCardInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    card: str = Field(..., description="Card name to find combos for.", min_length=1, max_length=200)
    color_identity: Optional[str] = Field(default=None, description="Optional color identity filter.")
    limit: int = Field(default=10, ge=1, le=25)


# ── Spellbook Tools ──────────────────────────────────────────────────────────


@mcp.tool(name="spellbook_combos")
async def spellbook_combos(params: SpellbookCombosInput) -> str:
    """Search for combos involving specific cards together. Use this INSTEAD of
    web search when looking up MTG combos or infinite combos.

    Finds combos that use ALL the specified cards. Returns step-by-step
    instructions, prerequisites, and what the combo produces.
    """
    q_parts = [f'card:"{name}"' for name in params.cards]
    if params.color_identity:
        q_parts.append(f"ci:{params.color_identity}")
    query = " ".join(q_parts)

    try:
        data = await _spellbook_get("/variants/", params={"q": query, "limit": params.limit})
    except Exception as e:
        return _spellbook_error(e)

    results = data.get("results", [])
    if not results:
        return f"No combos found involving {' + '.join(params.cards)}."

    parts: list[str] = []
    parts.append(f"# Combos with {' + '.join(params.cards)}")
    parts.append("")

    for i, variant in enumerate(results, 1):
        parts.append(f"## Combo {i}")
        parts.append(_format_combo(variant))
        parts.append("")

    return "\n".join(parts)


@mcp.tool(name="spellbook_card_combos")
async def spellbook_card_combos(params: SpellbookCardInput) -> str:
    """Find all combos that use a specific card. Use this INSTEAD of web search
    when asked about combos or synergies for a card.

    Useful for discovering what combo potential a card has.
    """
    q_parts = [f'card:"{params.card}"']
    if params.color_identity:
        q_parts.append(f"ci:{params.color_identity}")
    query = " ".join(q_parts)

    try:
        data = await _spellbook_get("/variants/", params={"q": query, "limit": params.limit})
    except Exception as e:
        return _spellbook_error(e)

    results = data.get("results", [])
    if not results:
        return f"No combos found for {params.card}."

    parts: list[str] = []
    count = data.get("count", len(results))
    parts.append(f"# Combos with {params.card} ({count} total, showing {len(results)})")
    parts.append("")

    for i, variant in enumerate(results, 1):
        parts.append(f"## Combo {i}")
        parts.append(_format_combo(variant))
        parts.append("")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# RULINGS — Official card rulings from Scryfall
# ═══════════════════════════════════════════════════════════════════════════════


class RulingsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Card name to get rulings for.", min_length=1, max_length=200)


@mcp.tool(name="scryfall_rulings")
async def scryfall_rulings(params: RulingsInput) -> str:
    """Get official Wizards of the Coast rulings for a card.

    Returns dated rulings that clarify how a card works, including
    interactions, edge cases, and errata.
    """
    # Look up the card to get its rulings URI
    try:
        card = await _scryfall_get("/cards/named", params={"exact": params.name})
    except httpx.HTTPStatusError:
        try:
            card = await _scryfall_get("/cards/named", params={"fuzzy": params.name})
        except Exception as e:
            return _scryfall_error(e)
    except Exception as e:
        return _scryfall_error(e)

    rulings_uri = card.get("rulings_uri", "")
    if not rulings_uri:
        return f"No rulings URI found for {card.get('name', params.name)}."

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                rulings_uri,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return _scryfall_error(e)

    rulings = data.get("data", [])
    if not rulings:
        return f"No rulings found for {card.get('name', params.name)}."

    parts: list[str] = []
    parts.append(f"# Rulings for {card.get('name', params.name)}")
    parts.append("")

    for r in rulings:
        date = r.get("published_at", "?")
        source = r.get("source", "?")
        comment = r.get("comment", "")
        parts.append(f"**{date}** ({source}): {comment}")
        parts.append("")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# RULEBOOK — Comprehensive Rules lookup and search
# ═══════════════════════════════════════════════════════════════════════════════

CR_VENDORED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MagicCompRules.txt")
CR_CACHE_PATH = os.environ.get("MYSTIC_FORGE_CR", "cr_cache.txt")
CR_REFRESH_INTERVAL = 86400.0
CR_MIN_RULES = 1000  # a real CR has ~3,300 numbered rules; reject partial downloads

_rules_state: dict[str, Any] = {"index": None, "checked_at": 0.0, "source_date": ""}
_rules_lock = asyncio.Lock()

_CR_TXT_RE = re.compile(r"https://media\.wizards\.com/[^\"']*?\.txt")
_CR_DATE_RE = re.compile(r"(\d{8})\.txt$")


def _rules_plausible(idx: rulebook.RulesIndex) -> bool:
    return len(idx.rules) >= CR_MIN_RULES and bool(idx.glossary)


def _load_rules_from_disk() -> Optional[rulebook.RulesIndex]:
    """Best available local copy: the newer of the refresh cache and the
    vendored snapshot (a git pull can leapfrog a stale cache)."""
    log = logging.getLogger("mystic_forge")
    best = None
    for path in (CR_CACHE_PATH, CR_VENDORED_PATH):
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
        except OSError:
            continue
        try:
            idx = rulebook.parse(text)
        except ValueError:
            log.warning("CR copy at %s is malformed; skipping", path)
            continue
        if not _rules_plausible(idx):
            log.warning("CR copy at %s failed the sanity check; skipping", path)
            continue
        if best is None or idx.effective_yyyymmdd > best.effective_yyyymmdd:
            best = idx
    return best


async def _fetch_page_text(url: str, timeout: float = REQUEST_TIMEOUT) -> str:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        return resp.text


def _discover_cr_url(page_html: str) -> Optional[tuple[str, str]]:
    """Extract (fetchable txt URL, YYYYMMDD stamp) from the rules page.

    The href contains a literal space ('MagicCompRules 20260807.txt') that
    must be percent-encoded before fetching. If several .txt links appear,
    the one with the newest filename date wins.
    """
    best = None  # (date, raw url)
    for raw in _CR_TXT_RE.findall(page_html):
        raw = urllib.parse.unquote(raw)
        dm = _CR_DATE_RE.search(raw)
        date = dm.group(1) if dm else ""
        if best is None or date > best[0]:
            best = (date, raw)
    if best is None:
        return None
    return urllib.parse.quote(best[1], safe=":/"), best[0]


async def _get_rules_index() -> Optional[rulebook.RulesIndex]:
    """Serve the current index; load from disk on first call and kick a
    background refresh at most once per CR_REFRESH_INTERVAL. Never blocks
    on the network."""
    async with _rules_lock:
        if _rules_state["index"] is None:
            loaded = await asyncio.to_thread(_load_rules_from_disk)
            if _rules_state["index"] is None:  # a refresh may have landed meanwhile
                _rules_state["index"] = loaded
        if time.time() - _rules_state["checked_at"] >= CR_REFRESH_INTERVAL:
            _rules_state["checked_at"] = time.time()
            _rules_state["refresh_task"] = asyncio.create_task(_refresh_rules())
    return _rules_state["index"]


async def _refresh_rules() -> None:
    log = logging.getLogger("mystic_forge")
    try:
        found = _discover_cr_url(await _fetch_page_text(RULES_PAGE_URL))
        if not found:
            log.warning("CR refresh: no .txt link found on %s", RULES_PAGE_URL)
            return
        url, new_date = found
        cur = _rules_state["index"]
        cur_date = _rules_state["source_date"] or (cur.effective_yyyymmdd if cur else "")
        if cur is not None and new_date and cur_date and new_date <= cur_date:
            return
        text = await _fetch_page_text(url, timeout=60.0)
        idx = await asyncio.to_thread(rulebook.parse, text)
        if not _rules_plausible(idx):
            log.warning("CR refresh: download failed the sanity check; keeping current rules")
            return
        _rules_state["index"] = idx
        _rules_state["source_date"] = new_date
        try:
            tmp_path = CR_CACHE_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_path, CR_CACHE_PATH)
        except OSError:
            log.warning("CR refresh: could not write cache to %s", CR_CACHE_PATH, exc_info=True)
        log.info("CR refreshed; now effective %s", idx.effective_date)
    except Exception:
        log.warning("CR refresh failed; keeping current rules", exc_info=True)
        if _rules_state["index"] is None:
            _rules_state["checked_at"] = 0.0  # nothing to serve; allow a prompt retry


# ═══════════════════════════════════════════════════════════════════════════════
# PRECON DECKS — Preconstructed deck lookup via MTGJSON
# ═══════════════════════════════════════════════════════════════════════════════

_deck_list_cache: dict[str, Any] = {"data": None, "expires_at": 0.0}


async def _mtgjson_get(path: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{MTGJSON_API}{path}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,  # MTGJSON DeckList.json is large
        )
        resp.raise_for_status()
        return resp.json()


def _mtgjson_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"MTGJSON API error ({e.response.status_code}): {e.response.text[:300]}"
    if isinstance(e, httpx.TimeoutException):
        return "Request to MTGJSON timed out. Try again."
    return f"Unexpected error: {type(e).__name__}: {e}"


async def _get_deck_list() -> list[dict]:
    if _deck_list_cache["data"] and time.time() < _deck_list_cache["expires_at"]:
        return _deck_list_cache["data"]
    data = await _mtgjson_get("/DeckList.json")
    _deck_list_cache["data"] = data.get("data", [])
    _deck_list_cache["expires_at"] = time.time() + 86400
    return _deck_list_cache["data"]


BASIC_LANDS = {
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest", "Snow-Covered Wastes",
}


def _diff_key(name: str) -> str:
    """Normalized comparison key for card names (case/whitespace-insensitive)."""
    return re.sub(r"\s+", " ", name).strip().lower()


async def _canonical_names(names: list[str]) -> Optional[tuple[dict[str, str], set[str]]]:
    """Look card names up on Scryfall.

    Returns (mapping, not_found) where mapping is _diff_key(name) → canonical name
    for recognized cards, and not_found is the set of _diff_key(name) values that
    Scryfall did not recognize. Returns None if the lookup fails entirely (caller
    falls back to raw names). Recognition is judged by Scryfall's own not_found list
    — a name it resolves under a fuller canonical spelling is NOT unrecognized.
    """
    mapping: dict[str, str] = {}
    not_found: set[str] = set()
    try:
        for i in range(0, len(names), 75):
            batch = names[i:i + 75]
            data = await _scryfall_post(
                "/cards/collection",
                {"identifiers": [{"name": n} for n in batch]},
            )
            for card in data.get("data", []):
                cname = card.get("name", "")
                if cname:
                    mapping[_diff_key(cname)] = cname
            for item in data.get("not_found", []):
                nm = item.get("name", "") if isinstance(item, dict) else str(item)
                if nm:
                    not_found.add(_diff_key(nm))
    except Exception:
        return None
    return mapping, not_found


# ── Precon Input Models ──────────────────────────────────────────────────────


class PreconSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., description="Search term for deck name or set code (e.g., 'Forces of the Imperium' or 'MKC').", min_length=1)
    commander_only: bool = Field(default=True, description="If true, only show Commander precons.")
    limit: int = Field(default=15, ge=1, le=50)


class PreconDeckInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    file_name: str = Field(..., description="Deck fileName from precon_search results (e.g., 'ForcesOfTheImperium_40K').", min_length=1)


# ── Precon Tools ─────────────────────────────────────────────────────────────


@mcp.tool(name="precon_search")
async def precon_search(params: PreconSearchInput) -> str:
    """Search for preconstructed decks by name or set code. Use this INSTEAD of
    web search when looking up precon decklists — it has the complete database.

    Searches MTGJSON's database of 2700+ official precon decks. Returns deck names,
    set codes, and fileNames needed to fetch full decklists with precon_decklist.
    """
    try:
        decks = await _get_deck_list()
    except Exception as e:
        return _mtgjson_error(e)

    query_lower = params.query.lower()
    matches: list[dict] = []

    for deck in decks:
        name = deck.get("name", "")
        code = deck.get("code", "")
        deck_type = deck.get("type", "")

        if params.commander_only and "Commander" not in deck_type:
            continue

        if query_lower in name.lower() or query_lower in code.lower():
            matches.append(deck)

    if not matches:
        return f"No precon decks found matching '{params.query}'."

    parts: list[str] = []
    parts.append(f"# Precon Search: '{params.query}' ({len(matches)} results)")
    parts.append("")

    for deck in matches[:params.limit]:
        name = deck.get("name", "?")
        code = deck.get("code", "?")
        deck_type = deck.get("type", "?")
        file_name = deck.get("fileName", "?")
        parts.append(f"- **{name}** ({code}) — {deck_type}")
        parts.append(f"  fileName: `{file_name}`")

    return "\n".join(parts)


@mcp.tool(name="precon_decklist")
async def precon_decklist(params: PreconDeckInput) -> str:
    """Get the full official decklist for a preconstructed deck. Use this INSTEAD
    of web search when a user asks about a precon's contents.

    Returns commander, main deck, and sideboard. Use precon_search first to find
    the fileName.
    """
    try:
        data = await _mtgjson_get(f"/decks/{params.file_name}.json")
    except Exception as e:
        return _mtgjson_error(e)

    deck = data.get("data", {})
    parts: list[str] = []
    parts.append(f"# {deck.get('name', 'Unknown Deck')}")
    parts.append(f"Set: {deck.get('code', '?')} | Type: {deck.get('type', '?')}")
    parts.append("")

    commander = deck.get("commander", [])
    if commander:
        parts.append("## Commander")
        for card in commander:
            parts.append(f"{card.get('count', 1)} {card.get('name', '?')}")
        parts.append("")

    main = deck.get("mainBoard", [])
    if main:
        parts.append(f"## Main Deck ({sum(c.get('count', 1) for c in main)} cards)")
        for card in sorted(main, key=lambda c: c.get("name", "")):
            parts.append(f"{card.get('count', 1)} {card.get('name', '?')}")
        parts.append("")

    side = deck.get("sideBoard", [])
    if side:
        parts.append(f"## Sideboard ({sum(c.get('count', 1) for c in side)} cards)")
        for card in sorted(side, key=lambda c: c.get("name", "")):
            parts.append(f"{card.get('count', 1)} {card.get('name', '?')}")
        parts.append("")

    total = sum(c.get("count", 1) for c in commander) + sum(c.get("count", 1) for c in main)
    parts.append(f"**Total: {total} cards**")

    return "\n".join(parts)


@mcp.tool(name="precon_export")
async def precon_export(params: PreconDeckInput) -> str:
    """Export a preconstructed deck in Archidekt import format.

    Returns the deck in Archidekt-compatible syntax with [Commander{top}]
    annotations. Paste directly into Archidekt's import dialog.
    """
    try:
        data = await _mtgjson_get(f"/decks/{params.file_name}.json")
    except Exception as e:
        return _mtgjson_error(e)

    deck = data.get("data", {})
    lines: list[str] = []

    commander = deck.get("commander", [])
    for card in commander:
        lines.append(f"{card.get('count', 1)}x {card.get('name', '?')} [Commander{{top}}]")

    main = deck.get("mainBoard", [])
    for card in sorted(main, key=lambda c: c.get("name", "")):
        lines.append(f"{card.get('count', 1)}x {card.get('name', '?')}")

    side = deck.get("sideBoard", [])
    for card in side:
        lines.append(f"{card.get('count', 1)}x {card.get('name', '?')} [Sideboard]")

    return "\n".join(lines)


class PreconDiffInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    file_name: str = Field(
        ...,
        description="MTGJSON precon fileName from precon_search (e.g. 'WorldShaper_TDC').",
        min_length=1,
    )
    deck: Optional[str] = Field(
        default=None,
        description="Upgraded deck as an Archidekt deck ID or URL.",
    )
    decklist: Optional[str] = Field(
        default=None,
        description="OR the upgraded deck as a pasted list ('1 Card Name' per line).",
    )
    canonicalize: bool = Field(
        default=True,
        description="Normalize display names via Scryfall and flag unrecognized "
                    "cards. Matching is always case-insensitive.",
    )


@mcp.tool(name="precon_diff")
async def precon_diff(params: PreconDiffInput) -> str:
    """Compute the exact cards cut and added between a preconstructed deck and a
    specific upgraded deck. Use this INSTEAD of estimating from memory when a user
    wants a real cut/added percentage for one deck.

    The precon baseline comes from MTGJSON (use precon_search for the fileName).
    Provide the upgraded deck as either an Archidekt deck (`deck`) or a pasted
    `decklist`. Basic lands are reported separately so routine mana-base churn
    doesn't distort the numbers.
    """
    if bool(params.deck) == bool(params.decklist):
        return ("Provide exactly one of `deck` (Archidekt ID/URL) or `decklist` "
                "(pasted list).")

    # Precon baseline (MTGJSON).
    try:
        pdata = await _mtgjson_get(f"/decks/{params.file_name}.json")
    except Exception as e:
        return _mtgjson_error(e)
    deck_obj = pdata.get("data", {})
    precon: dict[str, int] = {}
    for card in deck_obj.get("commander", []) + deck_obj.get("mainBoard", []):
        name = card.get("name", "?")
        precon[name] = precon.get(name, 0) + card.get("count", 1)

    # Upgraded side.
    if params.deck:
        deck_id = _parse_deck_id(params.deck)
        try:
            adata = await _archidekt_get(f"/decks/{deck_id}/")
        except Exception as e:
            return _archidekt_error(e)
        upgraded = _archidekt_in_deck_cards(adata)
        upgraded_src = f"Archidekt deck {deck_id}"
    else:
        upgraded = {}
        for qty, name in _parse_decklist(params.decklist or ""):
            upgraded[name] = upgraded.get(name, 0) + qty
        upgraded_src = "pasted decklist"

    # Optional Scryfall canonicalization (display + unknown-card flagging).
    canon: Optional[dict[str, str]] = None
    unknown: list[str] = []
    canon_note = ""
    if params.canonicalize:
        result = await _canonical_names(list({*precon, *upgraded}))
        if result is None:
            canon_note = ("_Name canonicalization unavailable — matched on raw "
                          "names._")
        else:
            canon, not_found_keys = result
            for n in {*precon, *upgraded}:
                if _diff_key(n) in not_found_keys:
                    unknown.append(n)

    def display(name: str) -> str:
        if canon:
            return canon.get(_diff_key(name), name)
        return name

    # Split basics out, then diff non-basics by normalized key.
    def split(counts: dict[str, int]):
        main, basics = {}, {}
        for n, c in counts.items():
            (basics if n in BASIC_LANDS else main)[n] = c
        return main, basics

    precon_main, precon_basics = split(precon)
    upgraded_main, upgraded_basics = split(upgraded)

    precon_keys = {_diff_key(n): n for n in precon_main}
    upgraded_keys = {_diff_key(n): n for n in upgraded_main}
    added = sorted(display(upgraded_keys[k])
                   for k in set(upgraded_keys) - set(precon_keys))
    cut = sorted(display(precon_keys[k])
                 for k in set(precon_keys) - set(upgraded_keys))
    kept = set(precon_keys) & set(upgraded_keys)

    precon_size = sum(precon.values())
    pct = lambda n: (round(n / precon_size * 100) if precon_size else 0)

    parts: list[str] = []
    parts.append(f"# Precon Diff: {deck_obj.get('name', params.file_name)}")
    parts.append(f"Baseline: {deck_obj.get('name', '?')} "
                 f"({deck_obj.get('code', '?')})  vs  {upgraded_src}")
    parts.append("")

    parts.append(f"## Added ({len(added)})")
    parts.extend(f"- {n}" for n in added) if added else parts.append("- (none)")
    parts.append("")

    parts.append(f"## Cut ({len(cut)})")
    parts.extend(f"- {n}" for n in cut) if cut else parts.append("- (none)")
    parts.append("")

    parts.append("## Summary")
    parts.append(f"- Precon size: {precon_size} cards")
    parts.append(f"- Cut: {len(cut)} ({pct(len(cut))}% of precon)")
    parts.append(f"- Added: {len(added)} ({pct(len(added))}% of precon)")
    parts.append(f"- Kept: {len(kept)}")
    parts.append("- (Basic lands excluded above and listed separately.)")

    basic_names = sorted(set(precon_basics) | set(upgraded_basics))
    basic_changes = [(n, precon_basics.get(n, 0), upgraded_basics.get(n, 0))
                     for n in basic_names
                     if precon_basics.get(n, 0) != upgraded_basics.get(n, 0)]
    if basic_changes:
        parts.append("")
        parts.append("## Basic land changes")
        for n, b, a in basic_changes:
            parts.append(f"- {n}: {b} → {a}")

    if unknown:
        parts.append("")
        parts.append("## Not recognized by Scryfall")
        parts.append("These names were diffed as-is and may be typos:")
        parts.extend(f"- {n}" for n in sorted(unknown))

    parts.append("")
    parts.append("---")
    parts.append(f"Precon source: MTGJSON /decks/{params.file_name}.json | "
                 f"Upgraded: {upgraded_src}")
    if canon_note:
        parts.append(canon_note)

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# WATCHLIST — Passphrase-named price watchlists (spec 2026-08-08)
# ═══════════════════════════════════════════════════════════════════════════════


class WatchlistCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Optional[str] = Field(None, description="Optional list label, e.g. 'Cloud deck upgrades'")


class WatchlistAddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., description="Card name (fuzzy-matched via Scryfall)")
    set_code: Optional[str] = Field(None, description="Pin a specific printing: set code")
    collector_number: Optional[str] = Field(None, description="Pin a specific printing: collector number")
    target_price: Optional[float] = Field(None, description="Alert threshold in USD")
    note: Optional[str] = Field(None, description="Free-form note, e.g. deck/batch")
    passphrase: Optional[str] = Field(None, description="List passphrase (omit when using a personal connector URL)")


class WatchlistBulkAddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decklist: str = Field(..., description=(
        "Cards to add, one per line. Accepts plain names, decklist quantities "
        "('1 Sol Ring', '1x Sol Ring'), and Archidekt-style suffixes "
        "((set) 123 [Category] ^label^) which are stripped. Quantities are "
        "ignored — a watchlist tracks cards, not copies. An optional target "
        "price may follow the name after ' @ ', e.g. 'Rhystic Study @ 60'."))
    note: Optional[str] = Field(None, description="Note applied to every card added, e.g. the deck name")
    target_price: Optional[float] = Field(None, description="Default target for cards without a per-line ' @ ' target")
    passphrase: Optional[str] = Field(None, description="List passphrase (omit when using a personal connector URL)")


class WatchlistRemoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, description="Card name to remove")
    entry_id: Optional[int] = Field(None, description="Entry id (from watchlist_list/history)")
    set_code: Optional[str] = Field(None, description="Disambiguate a specific pinned printing")
    collector_number: Optional[str] = Field(None, description="Disambiguate a specific pinned printing")
    passphrase: Optional[str] = Field(None, description="List passphrase (omit when using a personal connector URL)")


class WatchlistListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: Optional[str] = Field(None, description="List passphrase (omit when using a personal connector URL)")


class _NoIdentity(Exception):
    pass


NO_IDENTITY_MSG = (
    "No watchlist identity. Pass your `passphrase`, use your personal "
    "connector URL, or create a list with `watchlist_create`."
)


def _wl_db():
    db = watchlist_db.connect()
    watchlist_db.init_db(db)
    return db


# Mint throttle (spec: the mitigation for "anyone who finds the public URL can
# create lists"). In-memory and per-process — enough for a friend-group server;
# a restart forgives, which is fine for abuse this mild.
MINT_LIMIT = int(os.environ.get("MYSTIC_FORGE_MINT_LIMIT", "8"))
MINT_WINDOW = 3600.0
_mint_log: dict[str, list[float]] = {}


def _mint_allowed(who: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _mint_log.get(who, []) if now - t < MINT_WINDOW]
    if len(hits) >= MINT_LIMIT:
        _mint_log[who] = hits
        return False
    hits.append(now)
    _mint_log[who] = hits
    if len(_mint_log) > 4096:                      # bound the dict
        for k in [k for k, v in _mint_log.items()
                  if not any(now - t < MINT_WINDOW for t in v)]:
            _mint_log.pop(k, None)
    return True


def _client_key(request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


_history_task: Optional[asyncio.Task] = None


def _history_filling() -> bool:
    return _history_task is not None and not _history_task.done()


def _schedule_backfill(card_names=None) -> bool:
    """Kick the on-demand history fill after an add, off the request path.

    History is a static backfill, so it never waits for the nightly cycle: if
    the MTGJSON files aren't cached yet this downloads them first. Single-
    flight — a run in progress already covers every watched card, so a bulk
    add of 38 cards triggers one pass, not 38. Returns True if history is
    being fetched now."""
    global _history_task
    if os.environ.get("MYSTIC_FORGE_NO_INGEST"):
        return False
    if _history_filling():
        return True
    data_dir = watchlist_ingest._data_dir()

    async def run():
        try:
            await asyncio.to_thread(watchlist_ingest.ensure_history,
                                    watchlist_db.DB_PATH, data_dir)
        except Exception:
            logging.getLogger("mystic_forge").exception("history fill failed")

    try:
        _history_task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:                     # no loop (stdio/tests)
        return False
    return True


def _resolve_list_row(db, passphrase: Optional[str]):
    """Explicit passphrase param wins over URL context (spec)."""
    if passphrase:
        row = watchlist_db.get_list_by_passphrase(db, passphrase)
        if row is None:
            raise _NoIdentity("That passphrase is not recognized. Check for "
                              "typos, or create a list with `watchlist_create`.")
        return row
    list_id = _current_list.get()
    if list_id is not None:
        return watchlist_db.get_list(db, list_id)
    raise _NoIdentity(NO_IDENTITY_MSG)


def _supersession_warning(db, row) -> str:
    if row["superseded_by"] is None:
        return ""
    succ = watchlist_db.get_list(db, row["superseded_by"])
    return (f"⚠️ This list was **superseded** by a recovery clone "
            f"(share code `{succ['share_code']}`, created {succ['created_at']}). "
            f"You are editing the old copy — switch to the new passphrase/URL "
            f"if that was unintended.\n\n")


def _fmt_price(v) -> str:
    return f"${v:.2f}" if v is not None else "—"


def _fmt_delta(v) -> str:
    if v is None:
        return "—"
    return f"{'▼' if v < 0 else '▲' if v > 0 else '·'}{abs(v):.2f}"


def _fmt_summary_price(s) -> str:
    """Price cell for an entry_price_summary result; marks foil fallback."""
    if not s:
        return "—"
    p = _fmt_price(s["current"])
    return f"{p} (foil)" if s.get("finish") == "foil" else p


@mcp.tool(name="watchlist_create")
async def watchlist_create(params: WatchlistCreateInput) -> str:
    """Create a new price watchlist. Returns its passphrase (SHOWN ONLY ONCE —
    offer to remember it for the user), personal connector URL, and read-only
    share code."""
    if not _mint_allowed("mcp"):
        return ("Too many new watchlists just now — try again later. "
                "(If you already have a list, give me its passphrase instead.)")
    db = _wl_db()
    try:
        _, pp, sc = watchlist_db.create_list(db, label=params.label)
    finally:
        db.close()
    return (
        f"# Watchlist created{': ' + params.label if params.label else ''}\n\n"
        f"**Passphrase (save this — shown only once):** `{pp}`\n\n"
        f"- Personal connector URL: `{PUBLIC_BASE}/mcp/{pp}`\n"
        f"- History page: {PUBLIC_BASE}/w/{pp}\n"
        f"- Read-only share code: `{sc}` (viewable at {PUBLIC_BASE}/s/{sc})\n\n"
        f"Add this server with the personal URL for automatic identity, or "
        f"give the passphrase in chat. Share the share code (not the "
        f"passphrase) with friends."
    )


@mcp.tool(name="watchlist_add")
async def watchlist_add(params: WatchlistAddInput) -> str:
    """Add a card to a watchlist (or update its target/note if already
    watched). Tracks the cheapest printing unless set_code+collector_number
    pin one."""
    db = _wl_db()
    try:
        try:
            row = _resolve_list_row(db, params.passphrase)
        except _NoIdentity as e:
            return str(e)
        warning = _supersession_warning(db, row)

        name = params.name
        current_usd = None
        if params.set_code and params.collector_number:
            # Pinned printing: validate it actually exists before storing,
            # otherwise a typo would sit at "history pending" forever.
            try:
                card = await _scryfall_get(
                    f"/cards/{params.set_code.lower()}/{params.collector_number}")
                name = card.get("name", params.name)
                current_usd = (card.get("prices") or {}).get("usd")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return (f"No printing {params.set_code.upper()} "
                            f"#{params.collector_number} found on Scryfall — "
                            f"check the set code and collector number.")
            except Exception:
                pass  # Scryfall unreachable: accept the pin unvalidated
        else:
            try:
                card = await _scryfall_get("/cards/named", {"fuzzy": params.name})
                name = card.get("name", params.name)
                current_usd = (card.get("prices") or {}).get("usd")
            except Exception:
                pass  # offline/unknown: keep the user's spelling

        _, entry = watchlist_db.add_card(
            db, row["id"], name, set_code=params.set_code,
            collector_number=params.collector_number,
            target_price=params.target_price, note=params.note)
        uuids = watchlist_db.uuids_for_entry(db, entry)
        summary = watchlist_db.entry_price_summary(db, entry)
        if summary:
            backfill = "history ready"
        elif _schedule_backfill():
            backfill = "fetching 90 days of price history now"
        else:
            backfill = "history pending next ingest"
        printing = f" [{entry['set_code']} {entry['collector_number']}]" \
            if entry.get("set_code") else ""
        lines = [warning + f"Added **{name}**{printing} "
                 f"(entry #{entry['entry_id']}) — {backfill}."]
        if current_usd:
            lines.append(f"Scryfall market price now: ${current_usd}")
        if entry.get("target_price") is not None:
            lines.append(f"Target: {_fmt_price(entry['target_price'])}")
        if uuids:
            lines.append(f"Tracking {len(uuids)} printing(s).")
        return "\n".join(lines)
    finally:
        db.close()


@mcp.tool(name="watchlist_bulk_add")
async def watchlist_bulk_add(params: WatchlistBulkAddInput) -> str:
    """Add many cards to a watchlist at once — USE THIS instead of calling
    watchlist_add repeatedly. Paste a decklist, a shopping list, or any list
    of card names (one per line); optional per-line target after ' @ '.
    Validates every name against Scryfall in batches and backfills all their
    price history in a single pass."""
    db = _wl_db()
    try:
        try:
            row = _resolve_list_row(db, params.passphrase)
        except _NoIdentity as e:
            return str(e)
        warning = _supersession_warning(db, row)

        # Peel any ' @ <price>' off each line FIRST — _parse_decklist strips
        # trailing digits as collector numbers and would eat the target.
        wanted: list[tuple[str, Optional[float]]] = []
        seen_keys: set[str] = set()
        for line in params.decklist.splitlines():
            target = params.target_price
            m = re.search(r"\s+@\s*[$€]?\s*(\d+(?:\.\d+)?)\s*$", line)
            if m:
                target = float(m.group(1))
                line = line[:m.start()]
            parsed = _parse_decklist(line)
            if not parsed:
                continue
            name = parsed[0][1]
            if not name:
                continue
            key = name.lower()
            if key in seen_keys:      # same card twice in one paste
                continue
            seen_keys.add(key)
            wanted.append((name, target))
        if not wanted:
            return "No card names found in that list."
        if len(wanted) > 300:
            return (f"That's {len(wanted)} cards — more than this tool adds at "
                    f"once. Split it into batches of 300 or fewer.")

        # Validate every name against Scryfall, 75 identifiers per request
        found: dict[str, dict] = {}
        unresolved: list[str] = []
        names = [n for n, _ in wanted]
        for i in range(0, len(names), 75):
            identifiers = [{"name": n} for n in names[i:i + 75]]
            try:
                data = await _scryfall_post("/cards/collection",
                                            {"identifiers": identifiers})
            except Exception as e:
                return warning + f"Scryfall lookup failed: {_scryfall_error(e)}"
            for card in data.get("data", []):
                found[card["name"].lower()] = card
                # Scryfall matches on exact/oracle name; map the query back too
            for item in data.get("not_found", []):
                unresolved.append(item.get("name", str(item)))

        added, updated, skipped = [], [], []
        for name, target in wanted:
            card = found.get(name.lower())
            if card is None:
                # fall back to a fuzzy single lookup for near-misses
                try:
                    card = await _scryfall_get("/cards/named", {"fuzzy": name})
                except Exception:
                    skipped.append(name)
                    continue
            canonical = card.get("name", name)
            existed = watchlist_db._find_entry(db, row["id"], name=canonical)
            _, entry = watchlist_db.add_card(
                db, row["id"], canonical, target_price=target, note=params.note)
            (updated if existed else added).append(entry["card_name"])

        backfilling = _schedule_backfill() if added else False

        lines = [warning + f"**Added {len(added)} card(s)** to "
                 f"{row['label'] or 'your watchlist'}."]
        if added:
            lines.append(", ".join(sorted(added)[:40])
                         + (" …" if len(added) > 40 else ""))
        if updated:
            lines.append(f"\n{len(updated)} already watched (target/note "
                         f"updated): " + ", ".join(sorted(updated)[:20])
                         + (" …" if len(updated) > 20 else ""))
        if skipped:
            lines.append(f"\n⚠️ {len(skipped)} not recognized by Scryfall — "
                         f"check spelling: " + ", ".join(skipped[:20])
                         + (" …" if len(skipped) > 20 else ""))
        lines.append("\n" + ("Fetching 90 days of price history now — it "
                             "appears on the board shortly (first run on a "
                             "new server takes a few minutes)."
                             if backfilling else
                             "Price history arrives with the next ingest."))
        return "\n".join(lines)
    finally:
        db.close()


@mcp.tool(name="watchlist_remove")
async def watchlist_remove(params: WatchlistRemoveInput) -> str:
    """Remove a card from a watchlist by name or entry id."""
    if params.name is None and params.entry_id is None:
        return "Give a card `name` or an `entry_id` to remove."
    db = _wl_db()
    try:
        try:
            row = _resolve_list_row(db, params.passphrase)
        except _NoIdentity as e:
            return str(e)
        warning = _supersession_warning(db, row)
        try:
            removed = watchlist_db.remove_entry(
                db, row["id"], entry_id=params.entry_id, name=params.name,
                set_code=params.set_code,
                collector_number=params.collector_number)
        except watchlist_db.NotFound as e:
            return warning + str(e)
        return warning + f"Removed **{removed['card_name']}** (entry #{removed['entry_id']})."
    finally:
        db.close()


def _render_entries(db, list_id: int) -> list[str]:
    lines = ["| # | Card | Price | Δ7d | Δ30d | Target | Note |",
             "|---|------|-------|-----|------|--------|------|"]
    entries = watchlist_db.current_entries(db, list_id)
    rows = []
    for e in entries:
        rows.append((e, watchlist_db.entry_price_summary(db, e)))
    rows.sort(key=lambda t: (t[1] is None,
                             t[1]["d30"] if t[1] and t[1]["d30"] is not None else 0))
    for e, s in rows:
        printing = f" [{e['set_code']} {e['collector_number']}]" \
            if e.get("set_code") else ""
        lines.append(
            f"| {e['entry_id']} | {e['card_name']}{printing} "
            f"| {_fmt_summary_price(s)} "
            f"| {_fmt_delta(s['d7']) if s else '—'} "
            f"| {_fmt_delta(s['d30']) if s else '—'} "
            f"| {_fmt_price(e['target_price'])} | {e['note'] or ''} |")
    if not entries:
        lines = ["*(empty list)*"]
    return lines


@mcp.tool(name="watchlist_list")
async def watchlist_list(params: WatchlistListInput) -> str:
    """Show a watchlist: current price (cheapest normal-finish tcgplayer),
    7/30-day movement, targets, notes. Sorted by 30-day movement."""
    db = _wl_db()
    try:
        try:
            row = _resolve_list_row(db, params.passphrase)
        except _NoIdentity as e:
            return str(e)
        header = f"# Watchlist{': ' + row['label'] if row['label'] else ''}\n"
        return _supersession_warning(db, row) + header + \
            "\n".join(_render_entries(db, row["id"]))
    finally:
        db.close()


class WatchlistViewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    share_code: str = Field(..., description="Read-only share code, e.g. SC-ABC123")


class WatchlistHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: Optional[str] = Field(None, description="List passphrase")
    share_code: Optional[str] = Field(None, description="Read-only share code")
    limit: int = Field(50, description="Most recent events to show")


class WatchlistCloneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: Optional[str] = Field(None, description="Clone your own list (recovery: marks it superseded)")
    share_code: Optional[str] = Field(None, description="Clone someone's shared list (fork)")
    at_seq: Optional[int] = Field(None, description="Revision to clone at (from watchlist_history); default latest")


class PriceHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., description="Card name")
    days: int = Field(90, description="Days of history")
    provider: str = Field("tcgplayer", description="tcgplayer | cardkingdom | cardmarket")
    set_code: Optional[str] = Field(None, description="Pin a specific printing: set code")
    collector_number: Optional[str] = Field(None, description="Pin a specific printing: collector number")


@mcp.tool(name="watchlist_report")
async def watchlist_report(params: WatchlistListInput) -> str:
    """Movers report: biggest 7-day drops/rises and anything at/below target."""
    db = _wl_db()
    try:
        try:
            row = _resolve_list_row(db, params.passphrase)
        except _NoIdentity as e:
            return str(e)
        entries = watchlist_db.current_entries(db, row["id"])
        priced = []
        for e in entries:
            s = watchlist_db.entry_price_summary(db, e)
            if s:
                priced.append((e, s))
        if not priced:
            return "No price data yet — history arrives with the nightly ingest."
        hits = [(e, s) for e, s in priced
                if e["target_price"] is not None and s["current"] <= e["target_price"]]
        movers = sorted((t for t in priced if t[1]["d7"] is not None),
                        key=lambda t: t[1]["d7"])
        lines = [f"# Watchlist report{': ' + row['label'] if row['label'] else ''}"]
        if hits:
            lines.append("\n## 🎯 At or below target")
            for e, s in hits:
                lines.append(f"- **{e['card_name']}** {_fmt_price(s['current'])}"
                             f" (target {_fmt_price(e['target_price'])})")
        if movers:
            lines.append("\n## 📉 Biggest 7-day drops")
            for e, s in movers[:5]:
                if s["d7"] < 0:
                    lines.append(f"- {e['card_name']}: {_fmt_price(s['current'])}"
                                 f" ({_fmt_delta(s['d7'])})")
            lines.append("\n## 📈 Biggest 7-day rises")
            for e, s in movers[::-1][:5]:
                if s["d7"] > 0:
                    lines.append(f"- {e['card_name']}: {_fmt_price(s['current'])}"
                                 f" ({_fmt_delta(s['d7'])})")
        return "\n".join(lines)
    finally:
        db.close()


@mcp.tool(name="watchlist_view")
async def watchlist_view(params: WatchlistViewInput) -> str:
    """View a friend's watchlist read-only via its share code."""
    db = _wl_db()
    try:
        row = watchlist_db.get_list_by_share(db, params.share_code)
        if row is None:
            return "That share code is not recognized."
        header = (f"# Shared watchlist"
                  f"{': ' + row['label'] if row['label'] else ''} "
                  f"(read-only via `{row['share_code']}`)\n")
        return header + "\n".join(_render_entries(db, row["id"]))
    finally:
        db.close()


@mcp.tool(name="watchlist_history")
async def watchlist_history(params: WatchlistHistoryInput) -> str:
    """Show a list's append-only event chain (what changed, when). Accepts a
    passphrase (own list) or share code (read-only)."""
    db = _wl_db()
    try:
        if params.share_code:
            row = watchlist_db.get_list_by_share(db, params.share_code)
            if row is None:
                return "That share code is not recognized."
        else:
            try:
                row = _resolve_list_row(db, params.passphrase)
            except _NoIdentity as e:
                return str(e)
        events = db.execute(
            "SELECT * FROM events WHERE list_id=? ORDER BY seq DESC LIMIT ?",
            (row["id"], params.limit)).fetchall()
        lines = [f"# History{': ' + row['label'] if row['label'] else ''} "
                 f"(newest first)"]
        for ev in events:
            payload = json.loads(ev["payload_json"])
            detail = payload.get("card_name") or payload.get("label") or ""
            extras = {k: v for k, v in payload.items()
                      if k not in ("card_name", "label", "added_at") and v is not None}
            lines.append(f"- **#{ev['seq']}** {ev['ts']} `{ev['action']}` "
                         f"{detail} {extras if extras else ''}".rstrip())
        lines.append(f"\nRecover any revision with `watchlist_clone(at_seq=N)`.")
        return "\n".join(lines)
    finally:
        db.close()


@mcp.tool(name="watchlist_clone")
async def watchlist_clone(params: WatchlistCloneInput) -> str:
    """Clone a list at a revision into a NEW list with a new passphrase.
    Cloning your own list (passphrase) is recovery and supersedes it; cloning
    a share code is a fork of a friend's list."""
    db = _wl_db()
    try:
        recovery = False
        if params.share_code:
            row = watchlist_db.get_list_by_share(db, params.share_code)
            if row is None:
                return "That share code is not recognized."
        else:
            try:
                row = _resolve_list_row(db, params.passphrase)
            except _NoIdentity as e:
                return str(e)
            recovery = True
        new_id, pp, sc = watchlist_db.clone_list(db, row["id"],
                                                 at_seq=params.at_seq,
                                                 recovery=recovery)
        kind = "Recovery clone — the old list is now marked superseded" \
            if recovery else "Fork"
        return (
            f"# {kind}\n\n"
            f"**Passphrase (save this — shown only once):** `{pp}`\n\n"
            f"- Personal connector URL: `{PUBLIC_BASE}/mcp/{pp}`\n"
            f"- History page: {PUBLIC_BASE}/w/{pp}\n"
            f"- Share code: `{sc}`\n\n"
            f"Update your claude.ai connector URL and anywhere the old "
            f"passphrase is remembered."
        )
    finally:
        db.close()


from html import escape as _esc

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse


@mcp.custom_route("/og.png", methods=["GET"])
async def og_image(request: Request):
    """Static Open Graph banner for link previews (no perishable data)."""
    from starlette.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "og.png"), media_type="image/png")


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    """Health surface for compose healthcheck and /mtg/health monitoring."""
    import datetime as _dt
    try:
        db = _wl_db()
        lists = db.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
        cards = db.execute("SELECT COUNT(*) FROM watchlist_current").fetchone()[0]
        row = db.execute("SELECT value FROM meta WHERE key='last_ingest'").fetchone()
        db.close()
    except Exception as e:
        return JSONResponse({"status": "error", "db": False, "error": str(e)},
                            status_code=500)
    last = row["value"] if row else None
    stale = False
    if cards and last:
        age = _dt.date.today() - _dt.date.fromisoformat(last)
        stale = age.days > 1                  # > 36h in whole-day terms
    elif cards:
        stale = True
    return JSONResponse({
        "status": "degraded" if stale else "ok",
        "db": True, "lists": lists, "watched_cards": cards,
        "last_ingest": last, "ingest_stale": stale,
    })


def _page_int(request, name) -> int:
    try:
        return max(1, int(request.query_params.get(name, "1")))
    except ValueError:
        return 1


def _resolve_page_key(db, key: str):
    """A page/API key is a passphrase (editable) or a share code (read-only)."""
    row = watchlist_db.get_list_by_passphrase(db, key)
    if row is not None:
        return row, True
    row = watchlist_db.get_list_by_share(db, key)
    if row is not None:
        return row, False
    return None, False


def _page_row(db, request, param, by_share: bool):
    key = request.path_params[param]
    row = (watchlist_db.get_list_by_share(db, key) if by_share
           else watchlist_db.get_list_by_passphrase(db, key))
    if row is None:
        return None
    row = dict(row)
    row["_key"] = row["share_code"] if by_share else key
    return row


def _ensure_history_for(db, row) -> bool:
    """Opening a board is itself a request for history: if anything watched
    still lacks prices, start the fill now rather than waiting for a cycle."""
    unpriced = db.execute(
        """SELECT 1 FROM watchlist_current wc
           WHERE NOT EXISTS (
             SELECT 1 FROM card_uuids cu JOIN prices p ON p.uuid=cu.uuid
             WHERE LOWER(cu.card_name)=LOWER(wc.card_name))
           AND wc.list_id=? LIMIT 1""", (row["id"],)).fetchone()
    return _schedule_backfill() if unpriced else False


@mcp.custom_route("/w/{passphrase}", methods=["GET"])
async def watch_page(request: Request):
    db = _wl_db()
    try:
        row = _page_row(db, request, "passphrase", by_share=False)
        if row is None:
            return HTMLResponse("unknown passphrase", status_code=404)
        filling = _ensure_history_for(db, row)
        return HTMLResponse(watchlist_pages.render_main(
            db, row, editable=True, cp=_page_int(request, "cp"),
            shop=request.query_params.get("shop", "tcgplayer"),
            filling=filling,
            sort=request.query_params.get("sort", "target"),
            show_bought=request.query_params.get("bought") != "hide",
            q=request.query_params.get("q", "")))
    finally:
        db.close()


@mcp.custom_route("/w/{passphrase}/history", methods=["GET"])
async def watch_history_page(request: Request):
    db = _wl_db()
    try:
        row = _page_row(db, request, "passphrase", by_share=False)
        if row is None:
            return HTMLResponse("unknown passphrase", status_code=404)
        return HTMLResponse(watchlist_pages.render_history(
            db, row, editable=True, hp=_page_int(request, "hp"),
            shop=request.query_params.get("shop", "tcgplayer")))
    finally:
        db.close()


@mcp.custom_route("/w/{passphrase}/export.csv", methods=["GET"])
async def export_csv(request: Request):
    """Dense spreadsheet feed: every number the board computes, one row per
    card, stable columns — built for Sheets IMPORTDATA."""
    import csv
    import io
    db = _wl_db()
    try:
        row = _page_row(db, request, "passphrase", by_share=False)
        if row is None:
            return PlainTextResponse("unknown passphrase", status_code=404)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["card", "set_code", "collector_number", "tcgplayer_usd",
                    "cardkingdom_usd", "cardmarket_eur", "d7", "d7_pct",
                    "d30", "d30_pct", "target_usd", "pct_to_target",
                    "price_date"])
        for e in watchlist_db.current_entries(db, row["id"]):
            per_shop = {shop: watchlist_db.entry_price_summary(db, e, provider=shop)
                        for shop in ("tcgplayer", "cardkingdom", "cardmarket")}
            s = per_shop["tcgplayer"]

            def pct(delta):
                if not s or delta is None or s["current"] == delta:
                    return ""
                then = s["current"] - delta
                return round(delta / then * 100, 1) if then else ""

            tgt = e["target_price"]
            w.writerow([
                e["card_name"], e["set_code"] or "", e["collector_number"] or "",
                *(per_shop[shop]["current"] if per_shop[shop] else ""
                  for shop in ("tcgplayer", "cardkingdom", "cardmarket")),
                s["d7"] if s else "", pct(s["d7"]) if s else "",
                s["d30"] if s else "", pct(s["d30"]) if s else "",
                tgt if tgt is not None else "",
                (round((s["current"] - tgt) / tgt * 100, 1)
                 if s and tgt else ""),
                s["date"] if s else "",
            ])
        return PlainTextResponse(out.getvalue(), media_type="text/csv")
    finally:
        db.close()


@mcp.custom_route("/s/{share_code}", methods=["GET"])
async def share_page(request: Request):
    db = _wl_db()
    try:
        row = _page_row(db, request, "share_code", by_share=True)
        if row is None:
            return HTMLResponse("unknown share code", status_code=404)
        filling = _ensure_history_for(db, row)
        return HTMLResponse(watchlist_pages.render_main(
            db, row, editable=False, cp=_page_int(request, "cp"),
            shop=request.query_params.get("shop", "tcgplayer"),
            filling=filling,
            sort=request.query_params.get("sort", "target"),
            show_bought=request.query_params.get("bought") != "hide",
            q=request.query_params.get("q", "")))
    finally:
        db.close()


@mcp.custom_route("/s/{share_code}/history", methods=["GET"])
async def share_history_page(request: Request):
    db = _wl_db()
    try:
        row = _page_row(db, request, "share_code", by_share=True)
        if row is None:
            return HTMLResponse("unknown share code", status_code=404)
        return HTMLResponse(watchlist_pages.render_history(
            db, row, editable=False, hp=_page_int(request, "hp")))
    finally:
        db.close()


@mcp.custom_route("/api/target", methods=["POST"])
async def api_target(request: Request):
    """Set or clear an entry's target from the page. Passphrase key only."""
    db = _wl_db()
    try:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        row, editable = _resolve_page_key(db, str(body.get("key", "")))
        if row is None:
            return JSONResponse({"error": "unknown key"}, status_code=404)
        if not editable:
            return JSONResponse({"error": "share codes are read-only"},
                                status_code=403)
        tp = body.get("target_price")
        if tp is not None and (not isinstance(tp, (int, float)) or tp < 0):
            return JSONResponse({"error": "bad target_price"}, status_code=400)
        try:
            entry = watchlist_db.set_entry_target(
                db, row["id"], int(body.get("entry_id", -1)), tp)
        except watchlist_db.NotFound as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"entry_id": entry["entry_id"],
                             "target_price": entry["target_price"]})
    finally:
        db.close()


_SCRYFALL_URL_RE = re.compile(
    r"scryfall\.com/card/(?P<set>[a-z0-9]+)/(?P<cn>[^/?#]+)", re.I)


@mcp.custom_route("/api/resolve", methods=["POST"])
async def api_resolve(request: Request):
    """Preview a card for the page's add flow: Scryfall URL → that printing;
    plain text → fuzzy name. Returns display data + local history if any."""
    db = _wl_db()
    try:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        row, _ = _resolve_page_key(db, str(body.get("key", "")))
        if row is None:
            return JSONResponse({"error": "unknown key"}, status_code=404)
        query = str(body.get("query", "")).strip()
        if not query:
            return JSONResponse({"error": "empty query"}, status_code=400)
        m = _SCRYFALL_URL_RE.search(query)
        try:
            if m:
                card = await _scryfall_get(
                    f"/cards/{m['set'].lower()}/{urllib.parse.quote(m['cn'])}")
            else:
                card = await _scryfall_get("/cards/named", {"fuzzy": query})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return JSONResponse({"error": "Scryfall doesn't know that one — "
                                     "check the link or spelling"},
                                    status_code=404)
            return JSONResponse({"error": "Scryfall is unreachable"},
                                status_code=502)
        except Exception:
            return JSONResponse({"error": "Scryfall is unreachable"},
                                status_code=502)
        name = card.get("name", query)
        set_code = card.get("set", "").upper() if m else None
        cn = card.get("collector_number") if m else None
        entry = {"card_name": name, "set_code": set_code,
                 "collector_number": cn, "uuid": None}
        series = watchlist_db.price_series(
            db, watchlist_db.uuids_for_entry(db, entry), days=90)
        points = series["points"] if series else []
        return JSONResponse({
            "name": name, "set_code": set_code, "collector_number": cn,
            "usd": (card.get("prices") or {}).get("usd"),
            "chart": watchlist_pages._big_svg(points, name, "$") if points else "",
            "sites": watchlist_pages._site_links(entry),
        })
    finally:
        db.close()


@mcp.custom_route("/api/add", methods=["POST"])
async def api_add(request: Request):
    """Add a card from the page. Passphrase key only; the /api/resolve step
    already validated the printing against Scryfall."""
    db = _wl_db()
    try:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        row, editable = _resolve_page_key(db, str(body.get("key", "")))
        if row is None:
            return JSONResponse({"error": "unknown key"}, status_code=404)
        if not editable:
            return JSONResponse({"error": "share codes are read-only"},
                                status_code=403)
        name = str(body.get("name", "")).strip()
        if not name:
            return JSONResponse({"error": "missing name"}, status_code=400)
        tp = body.get("target_price")
        if tp is not None and (not isinstance(tp, (int, float)) or tp < 0):
            return JSONResponse({"error": "bad target_price"}, status_code=400)
        _, entry = watchlist_db.add_card(
            db, row["id"], name,
            set_code=body.get("set_code") or None,
            collector_number=body.get("collector_number") or None,
            target_price=tp,
            note=str(body.get("note") or "").strip()[:200] or None)
        backfilling = (not watchlist_db.entry_price_summary(db, entry)
                       and _schedule_backfill(entry["card_name"]))
        return JSONResponse({"entry_id": entry["entry_id"],
                             "backfilling": backfilling})
    finally:
        db.close()


@mcp.custom_route("/api/remove", methods=["POST"])
async def api_remove(request: Request):
    """Remove an entry from the page (the UI confirms first). Passphrase only."""
    db = _wl_db()
    try:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        row, editable = _resolve_page_key(db, str(body.get("key", "")))
        if row is None:
            return JSONResponse({"error": "unknown key"}, status_code=404)
        if not editable:
            return JSONResponse({"error": "share codes are read-only"},
                                status_code=403)
        try:
            removed = watchlist_db.remove_entry(
                db, row["id"], entry_id=int(body.get("entry_id", -1)))
        except watchlist_db.NotFound as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"removed": removed["card_name"]})
    finally:
        db.close()


@mcp.custom_route("/api/bought", methods=["POST"])
async def api_bought(request: Request):
    """Mark an entry bought (kept, muted, annotated) or un-mark it."""
    db = _wl_db()
    try:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        row, editable = _resolve_page_key(db, str(body.get("key", "")))
        if row is None:
            return JSONResponse({"error": "unknown key"}, status_code=404)
        if not editable:
            return JSONResponse({"error": "share codes are read-only"},
                                status_code=403)
        try:
            entry = watchlist_db.set_bought(
                db, row["id"], int(body.get("entry_id", -1)),
                bought=bool(body.get("bought", True)))
        except watchlist_db.NotFound as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"entry_id": entry["entry_id"],
                             "bought_at": entry["bought_at"]})
    finally:
        db.close()


@mcp.custom_route("/api/note", methods=["POST"])
async def api_note(request: Request):
    """Set or clear an entry's note from the page. Passphrase key only."""
    db = _wl_db()
    try:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        row, editable = _resolve_page_key(db, str(body.get("key", "")))
        if row is None:
            return JSONResponse({"error": "unknown key"}, status_code=404)
        if not editable:
            return JSONResponse({"error": "share codes are read-only"},
                                status_code=403)
        note = str(body.get("note") or "").strip()[:200] or None
        try:
            entry = watchlist_db.set_entry_note(
                db, row["id"], int(body.get("entry_id", -1)), note)
        except watchlist_db.NotFound as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"entry_id": entry["entry_id"],
                             "note": entry["note"]})
    finally:
        db.close()


@mcp.custom_route("/api/rename", methods=["POST"])
async def api_rename(request: Request):
    """Rename a list from the page. Passphrase key only."""
    db = _wl_db()
    try:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        row, editable = _resolve_page_key(db, str(body.get("key", "")))
        if row is None:
            return JSONResponse({"error": "unknown key"}, status_code=404)
        if not editable:
            return JSONResponse({"error": "share codes are read-only"},
                                status_code=403)
        label = str(body.get("label", "")).strip()[:80] or None
        watchlist_db.set_label(db, row["id"], label)
        return JSONResponse({"label": label})
    finally:
        db.close()


@mcp.custom_route("/api/revision/{key}/{seq}", methods=["GET"])
async def api_revision(request: Request):
    """Snapshot of a list at a revision, for the page's revision modal."""
    db = _wl_db()
    try:
        row, _ = _resolve_page_key(db, request.path_params["key"])
        if row is None:
            return JSONResponse({"error": "unknown key"}, status_code=404)
        try:
            seq = int(request.path_params["seq"])
        except ValueError:
            return JSONResponse({"error": "bad seq"}, status_code=400)
        state = watchlist_db.state_at(db, row["id"], seq)
        entries = sorted(state.values(), key=lambda e: e["entry_id"])
        return JSONResponse({"seq": seq, "label": row["label"],
                             "entries": entries})
    finally:
        db.close()


@mcp.custom_route("/api/fork", methods=["POST"])
async def api_fork(request: Request):
    """Fork (anyone with a key) or recover (passphrase only) at a revision.

    Non-destructive either way: recovery only marks the source superseded."""
    db = _wl_db()
    try:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        row, editable = _resolve_page_key(db, str(body.get("key", "")))
        if row is None:
            return JSONResponse({"error": "unknown key"}, status_code=404)
        mode = body.get("mode", "fork")
        if mode == "recover" and not editable:
            return JSONResponse(
                {"error": "recovery needs the passphrase, not a share code"},
                status_code=403)
        if not _mint_allowed(_client_key(request)):
            return JSONResponse({"error": "too many new lists from this address;"
                                 " try again later"}, status_code=429)
        at_seq = body.get("at_seq")
        new_id, pp, sc = watchlist_db.clone_list(
            db, row["id"], at_seq=at_seq, recovery=(mode == "recover"))
        return JSONResponse({
            "passphrase": pp, "share_code": sc,
            "url": f"{PUBLIC_BASE}/mcp/{pp}", "page": f"{PUBLIC_BASE}/w/{pp}"})
    finally:
        db.close()


@mcp.tool(name="price_history")
async def price_history(params: PriceHistoryInput) -> str:
    """Daily price series for a card from the local price DB — cheapest
    printing by default, or a specific one via set_code+collector_number.
    Data exists for cards someone watches; global, needs no passphrase."""
    db = _wl_db()
    try:
        uuids = watchlist_db.uuids_for_entry(db, {
            "card_name": params.name, "set_code": params.set_code,
            "collector_number": params.collector_number, "uuid": None})
        series = watchlist_db.price_series(db, uuids, days=params.days,
                                           provider=params.provider)
        if not series or not series["points"]:
            return (f"No local history for '{params.name}'. It appears after a "
                    f"watchlist add + nightly ingest; for a spot price use "
                    f"scryfall_price.")
        pts = "\n".join(f"{d}: {p}" for d, p in series["points"])
        which = (f"{params.set_code.upper()} #{params.collector_number}"
                 if params.set_code else "cheapest printing")
        return (f"# {params.name} — {params.provider}, {which} "
                f"({series['uuid']})\n```\n{pts}\n```")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(build_app(), host="0.0.0.0",
                    port=int(os.environ.get("MYSTIC_FORGE_PORT", "8000")))
