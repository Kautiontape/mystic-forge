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
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any
from enum import Enum
from collections import Counter, OrderedDict
from difflib import SequenceMatcher

import httpx
from pydantic import BaseModel, Field, ConfigDict, model_validator
from mcp.server.fastmcp import FastMCP

from mystic_forge.watchlist import db as watchlist_db
from mystic_forge.watchlist import ingest as watchlist_ingest
from mystic_forge.watchlist import pages as watchlist_pages
from mystic_forge import rulebook

from mystic_forge.goldfish import ENGINE_VERSION, autoderive, metrics, report
from mystic_forge.goldfish.cards import (CONDITIONS, COST_REDUCTION_FILTERS, DAMAGE_TARGETS,
                            GLOBAL_EVENTS, SELF_EVENTS, SPELL_CAST_FILTERS,
                            STATIC_KINDS, SYMBOLIC_COUNTS, TUTOR_FILTERS, VERBS,
                            SimCard, merge_card, validate_annotations)
from mystic_forge.goldfish.engine import (Game, IllegalAction, MulliganRules,
                             auto_mulligan, effective_power,
                             ensure_synthetic_cards, legal_actions, new_game,
                             step)
from mystic_forge.goldfish.odds import odds_at_least, odds_groups
from mystic_forge.goldfish.policy import choose_action
from mystic_forge.goldfish.runner import is_binary_ab_metric, run_ab, run_batch

# ── Constants ────────────────────────────────────────────────────────────────

# Release version, single source of truth for the image tag, /health, and the
# outbound User-Agent. Read from the file rather than duplicated in code so the
# release checks in mcp-servers can resolve it at any pinned commit.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "VERSION"), encoding="utf-8") as _version_file:
    VERSION = _version_file.read().strip()

SCRYFALL_API = "https://api.scryfall.com"
EDHREC_JSON = "https://json.edhrec.com"
EDHREC_API = "https://edhrec.com/api"
ARCHIDEKT_API = "https://archidekt.com/api"
SPELLBOOK_API = "https://backend.commanderspellbook.com"
MTGJSON_API = "https://mtgjson.com/api/v5"
RULES_PAGE_URL = "https://magic.wizards.com/en/rules"
USER_AGENT = f"MysticForge/{VERSION}"
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
        "Use rules_get for exact Comprehensive Rules text by rule number or keyword, and "
        "rules_search to find rules by content. For rules-interaction questions (stack, "
        "layers, replacement effects, state-based actions) use these instead of memory "
        "or web search, and cite rule numbers. "
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
        "For deck simulation questions (how fast, how consistent, what turn, "
        "did this swap help), use the goldfish tools: goldfish_annotate to "
        "prepare a deck's effect annotations, goldfish_run for seeded Monte "
        "Carlo stats, goldfish_ab for paired A/B of two lists, goldfish_odds "
        "for exact draw probabilities, goldfish_start/step/state to play an "
        "interactive seeded game. Goldfish sims measure speed and consistency "
        "only — they cannot value interaction (removal, counterspells); never "
        "present goldfish deltas as judgments of interaction cards. Publish "
        "run reports verbatim via goldfish_report; never hand-author the "
        "numbers. "
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

    # Second pass for names split on their own comma. Costs one request, and
    # only when something already missed.
    recovered: dict[str, dict] = {}
    candidates = _rejoin_candidates(not_found)
    if candidates:
        try:
            rejoined = await _scryfall_post(
                "/cards/collection",
                {"identifiers": [{"name": name} for _, name in candidates]})
        except Exception:
            rejoined = None      # best-effort: a failure here changes nothing
        if rejoined:
            by_alias: dict[str, dict] = {}
            for card in rejoined.get("data", []):
                for alias in _card_name_aliases(card):
                    by_alias.setdefault(alias, card)
            consumed: set[int] = set()
            for i, name in candidates:
                card = by_alias.get(name.lower())
                if card is None or i in consumed or i + 1 in consumed:
                    continue
                found_cards.append(card)
                # Both halves point at the recovered card, so the entry loop
                # below emits a single block for the pair.
                recovered[not_found[i]["name"].lower()] = card
                recovered[not_found[i + 1]["name"].lower()] = card
                consumed |= {i, i + 1}
            not_found = [ident for j, ident in enumerate(not_found)
                         if j not in consumed]

    index = _index_collection_results(found_cards)
    for alias, card in recovered.items():
        index.setdefault(("name", alias), card)

    # One block per unique card, in the order the input asked for them.
    blocks: list[str] = []
    seen_cards: set[int] = set()
    reported = {_identifier_key(ident) for ident in not_found + unchecked}
    for entry in entries:
        card = _lookup_entry(index, entry)
        if card is None:
            # Scryfall answered but never echoed this identifier as missing, so
            # no other section will mention it. A pinned printing that does not
            # come back is the usual way here.
            identifier = _entry_identifier(entry)
            key = _identifier_key(identifier)
            if key not in reported:
                reported.add(key)
                not_found.append(identifier)
            continue
        if id(card) in seen_cards:
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


def _rejoin_candidates(not_found: list[dict]) -> list[tuple[int, str]]:
    """(position, rejoined name) for every adjacent pair of name-only misses.

    Most legendary creature names carry a comma, and a caller reading
    'Delney, Streetwise Lookout' as a list produces two adjacent misses that
    rejoin into one real card. Identifiers that pinned a set or collector
    number are skipped — those did not come from splitting a name.
    """
    candidates: list[tuple[int, str]] = []
    for i in range(len(not_found) - 1):
        first, second = not_found[i], not_found[i + 1]
        if set(first) == {"name"} and set(second) == {"name"}:
            candidates.append((i, f"{first['name']}, {second['name']}"))
    return candidates


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

CR_VENDORED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "mystic_forge", "data", "MagicCompRules.txt")
CR_CACHE_PATH = os.environ.get("MYSTIC_FORGE_CR", "cr_cache.txt")
CR_REFRESH_INTERVAL = 86400.0
CR_RETRY_INTERVAL = 300.0  # retry window when we have nothing to serve
CR_MIN_RULES = 1000  # a real CR has ~3,300 numbered rules; reject partial downloads

_rules_state: dict[str, Any] = {"index": None, "checked_at": 0.0, "source_date": "", "refresh_task": None}
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
            _rules_state["refresh_task"] = asyncio.create_task(_refresh_rules())  # strong ref: keeps the task from being GC'd mid-flight
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
        # Deliberately not persisted to disk: on a process restart the effective
        # date is recomputed from the loaded text, so at worst one extra
        # download happens if filename and effective dates diverge.
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
            # Nothing to serve; allow a retry after CR_RETRY_INTERVAL, not immediately.
            _rules_state["checked_at"] = time.time() - CR_REFRESH_INTERVAL + CR_RETRY_INTERVAL


RULES_GET_MAX_CHARS = 10000
RULES_SEARCH_HIT_CHARS = 400

_RULES_NUM_RE = re.compile(r"^\d{1,3}(?:\.\d+)?[a-z]{0,2}$")


class RulesGetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    ref: str = Field(
        ...,
        description=(
            "A Comprehensive Rules number at any depth ('7', '702', '702.2', "
            "'702.2b'; a trailing period is fine) or a keyword/glossary term "
            "('deathtouch', 'exploit')."
        ),
        min_length=1, max_length=100,
    )


def _rule_line(rule: rulebook.Rule) -> str:
    sep = " " if rule.number[-1].isalpha() else ". "
    return f"{rule.number}{sep}{rule.text}"


def _first_sentence(text: str) -> str:
    """First sentence of a rule's first line, for compact listings."""
    first_line = text.split("\n", 1)[0]
    dot = first_line.find(". ")
    return first_line[: dot + 1] if dot != -1 else first_line


def _child_listing(idx: rulebook.RulesIndex, rule: rulebook.Rule, brief: bool) -> list[str]:
    """One line per child; brief mode trims each to its first sentence."""
    lines = []
    for c in rule.children:
        child = idx.rules[c]
        sep = " " if child.number[-1].isalpha() else ". "
        text = _first_sentence(child.text) if brief else child.text.split("\n", 1)[0]
        lines.append(f"- {child.number}{sep}{text}")
    return lines


def _rule_with_subrules(idx: rulebook.RulesIndex, rule: rulebook.Rule) -> list[str]:
    parts = [_rule_line(rule)]
    for child in rule.children:
        parts.append("")
        parts.append(_rule_line(idx.rules[child]))
    return parts


def _rules_header(idx: rulebook.RulesIndex) -> str:
    return f"Comprehensive Rules, effective {idx.effective_date}"


def _rules_unavailable() -> str:
    return ("Comprehensive Rules are unavailable: no local copy could be "
            "loaded and the download from Wizards has not succeeded yet. "
            "Try again in a moment.")


def _rules_not_found(idx: rulebook.RulesIndex, ref: str) -> str:
    suggestions = idx.suggest(ref)
    if not suggestions:
        return f"No rule or glossary entry matches '{ref}'. Try rules_search instead."
    lines = [f"No rule or glossary entry matches '{ref}'. Closest matches:", ""]
    lines += [f"- {s}" for s in suggestions]
    lines += ["", "Or use rules_search for full-text search."]
    return "\n".join(lines)


def _rules_glossary_get(idx: rulebook.RulesIndex, ref: str) -> str:
    definition = idx.glossary.get(ref.lower())
    if definition is None:
        return _rules_not_found(idx, ref)
    display = idx.glossary_display[ref.lower()]
    parts = [_rules_header(idx), "", f"Glossary: {display}", definition]
    omitted: list[str] = []
    for num in idx.glossary_refs(ref):
        rule = idx.rules.get(num)
        if rule is None:
            continue
        if len(num) <= 3 and rule.children:
            # Cited section/subsection: list its rules like rules_get(num) would.
            block = [_rule_line(rule)]
            block += _child_listing(idx, rule, brief=False)
            block += [f"Call rules_get with a specific number "
                      f"(e.g. '{rule.children[0]}') to expand it."]
        else:
            block = _rule_with_subrules(idx, rule)
        budget = RULES_GET_MAX_CHARS - len("\n".join(parts))
        if len("\n".join(block)) > budget:
            if rule.children:
                block = [_rule_line(rule)]
                block += _child_listing(idx, rule, brief=True)
                block += [f"(Trimmed — call rules_get('{num}') for full text.)"]
            if not rule.children or len("\n".join(block)) > budget:
                omitted.append(num)
                continue
        parts.append("")
        parts.extend(block)
    for num in omitted:
        parts += ["", f"(Rule {num} omitted for length — call rules_get('{num}').)"]
    return "\n".join(parts)


@mcp.tool(name="rules_get")
async def rules_get(params: RulesGetInput) -> str:
    """Look up Magic's Comprehensive Rules by rule number or keyword.

    Use this INSTEAD of memory or web search for what the rulebook says.
    A rule number ('702.2b', '601.2') returns exact rule text — '702.2'
    includes its subrules, '702' lists that subsection's rules. A keyword
    or glossary term ('deathtouch') returns the glossary entry plus the
    rules it cites. Cite rule numbers in answers.
    """
    idx = await _get_rules_index()
    if idx is None:
        return _rules_unavailable()
    ref = params.ref.rstrip(".")
    nref = ref.lower()

    if _RULES_NUM_RE.fullmatch(nref):
        rule = idx.rules.get(nref)
        if rule is None:
            return _rules_not_found(idx, ref)
        parts = [_rules_header(idx), ""]
        if len(ref) <= 3:  # section or subsection: children as one-liners
            parts.append(_rule_line(rule))
            if not rule.children:
                parts += ["", f"(No numbered rules under {ref} — try a more "
                              "specific number or rules_search.)"]
            else:
                listing = _child_listing(idx, rule, brief=False)
                if len("\n".join(parts + listing)) > RULES_GET_MAX_CHARS:
                    listing = _child_listing(idx, rule, brief=True)
                parts.extend(listing)
                parts += ["", f"Call rules_get with a specific number "
                              f"(e.g. '{rule.children[0]}') to expand it."]
        elif ref[-1].isalpha():  # subrule: parent heading for context
            parent = idx.rules.get(rule.parent or "")
            if parent is not None:
                parts.append(_rule_line(parent))
            parts.append(_rule_line(rule))
        else:  # rule: full text with subrules; trim to snippets when too large
            body = _rule_with_subrules(idx, rule)
            if len("\n".join(body)) > RULES_GET_MAX_CHARS:
                body = [_rule_line(rule)]
                body += _child_listing(idx, rule, brief=True)
                body += ["", "(Subrules trimmed to first sentences — call "
                             "rules_get on a subrule number for full text.)"]
            parts.extend(body)
        return "\n".join(parts)

    return _rules_glossary_get(idx, ref)


class RulesSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(
        ...,
        description=(
            "Words or a phrase to find in Comprehensive Rules and glossary "
            "text. Prefer the CR's own wording — singular forms and rules "
            "vocabulary ('replacement effect order', not 'ordering of "
            "replacement effects')."
        ),
        min_length=1, max_length=300,
    )
    limit: int = Field(default=10, ge=1, le=25, description="Max hits to return.")


@mcp.tool(name="rules_search")
async def rules_search(params: RulesSearchInput) -> str:
    """Full-text search of Magic's Comprehensive Rules and glossary.

    Use this INSTEAD of memory or web search when a rules-interaction
    question (stack, layers, replacement effects, state-based actions)
    doesn't start from a known rule number; then cite the rule numbers
    it returns, or drill in with rules_get.
    """
    idx = await _get_rules_index()
    if idx is None:
        return _rules_unavailable()
    hits, total = idx.search(params.query, limit=params.limit)
    if not hits:
        return (f"No Comprehensive Rules matches for '{params.query}'. Try "
                "different terms, or rules_get with a rule number or keyword.")
    lines: list[str] = []
    used = 0
    trimmed = 0
    for h in hits:
        if h.kind == "glossary":
            line = f"Glossary: {h.ref} — {idx.glossary[h.ref.lower()]}"
        else:
            # First line only: Example blocks belong in rules_get output.
            line = _rule_line(idx.rules[h.ref]).split("\n", 1)[0]
        if len(line) > RULES_SEARCH_HIT_CHARS:
            suffix = f" … (rules_get('{h.ref}') for the rest)"
            line = line[:RULES_SEARCH_HIT_CHARS - len(suffix)].rsplit(" ", 1)[0] + suffix
        if used + len(line) > RULES_GET_MAX_CHARS:
            trimmed = len(hits) - len(lines)
            break
        lines.append(line)
        used += len(line) + 2
    noun = "CR entry mentions" if total == 1 else "CR entries mention"
    parts = [_rules_header(idx),
             f"{total} {noun} these terms; showing the {len(lines)} most relevant:", ""]
    for line in lines:
        parts.append(line)
        parts.append("")
    if trimmed:
        hit_word = "hit" if trimmed == 1 else "hits"
        parts.append(f"({trimmed} more {hit_word} trimmed for length — "
                     "narrow the query or lower the limit.)")
    return "\n".join(parts)


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
# GOLDFISH — Deck simulation support: annotation pre-flight and draw odds
# ═══════════════════════════════════════════════════════════════════════════════


#: In-process cache of raw Scryfall card JSON, keyed by exact requested name.
_GOLDFISH_CARD_CACHE: dict[str, dict] = {}

_GOLDFISH_CMDR_RE = re.compile(r"\s*\*CMDR\*\s*$", re.IGNORECASE)
_GOLDFISH_COUNT_RE = re.compile(r"^\d+x?\s+\S")
_GOLDFISH_ARCHIDEKT_RE = re.compile(r"archidekt\.com/decks/\d+|^\d+$")

#: One-line explanations for the D9 out-of-scope classes.
_GOLDFISH_D9_NOTES = {
    "interaction_removal": "interaction — counted, not simulated",
    "interaction_wipe": "interaction — counted, not simulated",
    "interaction_counter": "interaction — counted, not simulated",
    "protection": "opponent-dependent protection — counted, not simulated",
    "political": "table politics — counted, not simulated",
    "unmodeled_other": "out of scope in v1 (Sagas, X-costs, coin flips, "
                       "sac/life-payment costs, ...) — inert, counted",
}

_GOLDFISH_EXAMPLE_CLOUD = '''{"name": "Cloud, Ex-SOLDIER",
 "triggers": [
   {"on": "etb", "do": "attach_from_board"},
   {"on": "attack", "do": "draw", "count": "per_equipped_attacker"},
   {"on": "attack", "if": "power_gte:7", "do": "treasure", "count": 2}],
 "statics": [],
 "activated": [{"cost": "{3}{R}{W}", "do": "extra_combat"}]}'''

_GOLDFISH_EXAMPLE_SIGNET = ('{"name": "Boros Signet",\n'
                            ' "activated": [{"cost": "{1}{T}", "do": "add_mana",'
                            ' "pips": "{R}{W}"}]}')


async def _goldfish_fetch_cards(names: list[str]) -> tuple[list[dict], list[str]]:
    """Fetch raw Scryfall card JSON for the given names (deduplicated, order
    preserved) via batched POST /cards/collection, 75 identifiers per request.

    Returns (cards, not_found_names). Cards are the untrimmed Scryfall dicts —
    autoderive.derive consumes them as-is. Found cards are cached in
    _GOLDFISH_CARD_CACHE under the exact requested name; unrecognized names
    are not cached (a fixed typo re-queries)."""
    unique = list(dict.fromkeys(names))
    to_fetch = [n for n in unique if n not in _GOLDFISH_CARD_CACHE]
    for i in range(0, len(to_fetch), 75):
        batch = to_fetch[i:i + 75]
        data = await _scryfall_post(
            "/cards/collection",
            {"identifiers": [{"name": n} for n in batch]},
        )
        missing = {item.get("name", "") if isinstance(item, dict) else str(item)
                   for item in data.get("not_found", [])}
        found = [n for n in batch if n not in missing]
        # /cards/collection returns data in identifier order, skipping misses.
        for req_name, card in zip(found, data.get("data", [])):
            _GOLDFISH_CARD_CACHE[req_name] = card
    cards = [_GOLDFISH_CARD_CACHE[n] for n in unique if n in _GOLDFISH_CARD_CACHE]
    not_found = [n for n in unique if n not in _GOLDFISH_CARD_CACHE]
    return cards, not_found


def _goldfish_archidekt_commanders(data: dict) -> list[str]:
    """Names of the cards in the deck's premier (Commander) category, in
    deck order."""
    premier = {c["name"] for c in data.get("categories", [])
               if c.get("isPremier") or c.get("name") == "Commander"}
    out: list[str] = []
    for entry in data.get("cards", []):
        if any(cn in premier for cn in entry.get("categories", [])):
            name = entry.get("card", {}).get("oracleCard", {}).get("name")
            if name:
                out.append(name)
    return out


def _goldfish_n_cards(n: int) -> str:
    return f"{n} card" + ("" if n == 1 else "s")


async def _goldfish_load_deck(deck: str) -> tuple[list[str], str, str | None]:
    """Resolve a deck reference into (flat card-name list, commander name,
    commander_note).

    Decklist text — anything containing a newline or starting with a count
    like '2 Plains' — is parsed directly: the FIRST line's card is the
    commander, unless a line ends with ' *CMDR*' (marker stripped, that card
    is commander). Otherwise an Archidekt deck ID/URL is fetched and the
    commander is the first card in the premier (Commander) category;
    commander_note carries a display caveat when that category holds more
    than one card (partner precons), else None. Raises ValueError when the
    deck is empty, a non-Archidekt URL is given, or the commander cannot be
    identified; Archidekt HTTP errors propagate for _archidekt_error."""
    text = deck.strip()
    is_text = "\n" in text or _GOLDFISH_COUNT_RE.match(text) is not None
    if not is_text and _GOLDFISH_ARCHIDEKT_RE.search(text):
        data = await _archidekt_get(f"/decks/{_parse_deck_id(text)}/")
        premier = _goldfish_archidekt_commanders(data)
        if not premier:
            raise ValueError(
                "Archidekt deck has no card in a premier (Commander) "
                "category; cannot identify the commander.")
        note = (f"(first of {len(premier)} premier cards — v1 simulates a "
                f"single commander)" if len(premier) > 1 else None)
        counts = _archidekt_in_deck_cards(data)
        names = [n for n, qty in counts.items() for _ in range(qty)]
        return names, premier[0], note
    if not is_text and "://" in text:
        raise ValueError(
            "Only Archidekt URLs are supported; paste the decklist text "
            "otherwise.")

    pairs = _parse_decklist(text)
    if not pairs:
        raise ValueError(
            "No cards found. Provide an Archidekt deck ID/URL or decklist "
            "text ('1 Card Name' per line, commander first).")
    commander: str | None = None
    names: list[str] = []
    for qty, name in pairs:
        m = _GOLDFISH_CMDR_RE.search(name)
        if m:
            name = name[:m.start()].strip()
            commander = name
        names.extend([name] * qty)
    if not names:
        raise ValueError("Deck has no cards (every quantity is zero).")
    if commander is None:
        commander = names[0]        # first line's card (marker-free deck)
    return names, commander, None


def _goldfish_summary(d: autoderive.Derived) -> str:
    """One-line human summary of an auto-derived card's model."""
    card = d.card
    data = card.data
    ann = card.ann
    if ann is not None and any(a.sac_self and a.do == "ramp_land"
                               for a in ann.activated):
        return "fetch land — sacrifice to put a land onto the battlefield"
    if data.produces:
        colors = [c for c in ("W", "U", "B", "R", "G", "C") if c in data.produces]
        qty = max(data.produces[c] for c in colors)
        if len(colors) == 1:
            pips = f"{{{colors[0]}}}" * qty
        elif qty == 1:
            pips = " or ".join(f"{{{c}}}" for c in colors)
        else:
            pips = f"{qty} mana of " + "/".join(f"{{{c}}}" for c in colors)
        kind = ("land" if card.is_land
                else "mana creature" if card.is_creature else "mana rock")
        return (f"{kind} — taps for {pips}"
                + (", enters tapped" if data.enters_tapped else ""))
    if card.is_land:
        return "land" + (", enters tapped" if data.enters_tapped else "")
    if ann is not None:
        for t in ann.triggers:
            if t.do == "draw":
                return f"draw {t.count} on {t.on}"
            if t.do == "ramp_land":
                return f"ramp — {t.count} land(s) onto the battlefield on {t.on}"
        if ann.grants is not None:
            bits: list[str] = []
            if "power" in ann.grants or "toughness" in ann.grants:
                bits.append(f"+{ann.grants.get('power', 0)}"
                            f"/+{ann.grants.get('toughness', 0)}")
            bits.extend(str(k) for k in ann.grants.get("keywords", ()))
            return "equipment — grants " + ", ".join(bits)
    if card.is_creature:
        kw = ", ".join(sorted(data.keywords))
        base = f"vanilla {data.power}/{data.toughness}"
        return f"{base} ({kw})" if kw else base
    return "modeled from card data"


# ── Goldfish Input Models ────────────────────────────────────────────────────


class GoldfishOddsGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    copies: int = Field(..., ge=0, description="Copies of this group's cards in the deck.")
    min_successes: int = Field(default=1, ge=0, description="Minimum copies that must appear in the sample.")


class GoldfishOddsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deck_size: int = Field(..., ge=2, le=500, description="Cards in the deck (library) being drawn from.")
    draws: int = Field(..., ge=0, le=100, description="Cards seen (e.g. 7 for an opening hand).")
    copies: int | None = Field(
        default=None, ge=0,
        description="Flat mode: copies of the card of interest in the deck.",
    )
    min_successes: int = Field(
        default=1, ge=0,
        description="Flat mode: minimum copies that must appear among the draws.",
    )
    groups: list[GoldfishOddsGroup] | None = Field(
        default=None, max_length=8,
        description="Joint mode: up to 8 groups of {copies, min_successes}; "
                    "returns P(every group meets its minimum simultaneously), "
                    "e.g. '>=1 piece A AND >=1 piece B in the top 12'.",
    )


class GoldfishAnnotateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    deck: str = Field(
        ...,
        description="Archidekt deck ID/URL, or decklist text ('1 Card Name' "
                    "per line). For decklist text, list your commander first "
                    "(or mark its line with a trailing ' *CMDR*').",
        min_length=1,
    )


# ── Goldfish Tools ───────────────────────────────────────────────────────────


@mcp.tool(name="goldfish_odds")
async def goldfish_odds(params: GoldfishOddsInput) -> str:
    """Exact hypergeometric draw odds — no simulation, closed form.

    Flat mode (`copies`, optional `min_successes`): P(at least min_successes
    of the copies appear in the top `draws` of a `deck_size`-card deck).
    Joint mode (`groups`): P(EVERY group meets its minimum simultaneously) by
    exact multivariate enumeration — answers "≥1 piece A AND ≥1 piece B in
    the top m". Provide exactly one of `copies` or `groups`.
    """
    groups_in = params.groups or []
    if (params.copies is None) == (not groups_in):
        return ("Provide exactly one of `copies` (flat mode) or `groups` "
                "(joint mode: [{copies, min_successes}, ...]).")
    if params.draws > params.deck_size:
        return "draws cannot exceed deck_size."
    try:
        if params.copies is not None:
            p = odds_at_least(params.deck_size, params.draws, params.copies,
                              params.min_successes)
            return (f"P(≥{params.min_successes} of {params.copies} copies in "
                    f"top {params.draws} of {params.deck_size}) = {p * 100:.2f}%"
                    f"  [exact hypergeometric]")
        groups = [{"copies": g.copies, "min_successes": g.min_successes}
                  for g in groups_in]
        joint = odds_groups(params.deck_size, params.draws, groups)
        lines: list[str] = []
        for i, g in enumerate(groups, 1):
            p = odds_at_least(params.deck_size, params.draws, g["copies"],
                              g["min_successes"])
            lines.append(f"Group {i}: P(≥{g['min_successes']} of "
                         f"{g['copies']} copies in top {params.draws} of "
                         f"{params.deck_size}) = {p * 100:.2f}%")
        lines.append(f"Joint: P(all {len(groups)} groups meet their minimums "
                     f"in top {params.draws} of {params.deck_size}) = "
                     f"{joint * 100:.2f}%  [exact multivariate hypergeometric]")
        return "\n".join(lines)
    except ValueError as e:
        return str(e)


@mcp.tool(name="goldfish_annotate")
async def goldfish_annotate(params: GoldfishAnnotateInput) -> str:
    """Pre-flight a deck for goldfish simulation: auto-derive what the engine
    models on its own, classify out-of-scope cards, and show oracle text ONLY
    for the plausibly simulable gaps — cutting a full-deck annotation pass to
    the handful of cards that need one.

    Deck input is an Archidekt deck ID/URL or decklist text. For decklist
    text, list your commander first; a line ending in ' *CMDR*' overrides.
    """
    try:
        names, commander, cmdr_note = await _goldfish_load_deck(params.deck)
    except ValueError as e:
        return str(e)
    except httpx.HTTPError as e:
        return _archidekt_error(e)
    try:
        cards, not_found = await _goldfish_fetch_cards(names)
    except httpx.HTTPError as e:
        return _scryfall_error(e)

    derived = autoderive.derive(cards)

    auto_lines: list[str] = []
    out_by_class: dict[str, list[str]] = {}
    needs: list[tuple[dict, autoderive.Derived]] = []
    seen: set[str] = set()
    for raw in cards:
        d = derived.get(raw.get("name", ""))
        if d is None or d.card.name in seen:
            continue
        seen.add(d.card.name)
        kinds = sorted({n.split(":", 1)[0] for n in d.approx_notes})
        suffix = f"  [{', '.join(kinds)}]" if kinds else ""
        if d.auto_annotated:
            auto_lines.append(f"- {d.card.name} — {_goldfish_summary(d)}{suffix}")
        elif d.needs_annotation:
            needs.append((raw, d))
        else:
            cls = d.card.scope_class or "unmodeled_other"
            out_by_class.setdefault(cls, []).append(f"{d.card.name}{suffix}")

    total = len(names)
    parts: list[str] = []
    parts.append("# Goldfish annotation pre-flight")
    cmdr_display = f"{commander} {cmdr_note}" if cmdr_note else commander
    parts.append(f"Commander: {cmdr_display} | "
                 f"Deck: {_goldfish_n_cards(total)} including commander")
    if total != 100:
        parts.append(f"Warning: {_goldfish_n_cards(total)} including commander "
                     f"— EDH decks are 100. Simulating anyway if that's intended.")
    if not_found:
        parts.append("")
        parts.append("Not recognized by Scryfall: " + ", ".join(not_found))
        parts.append("These names were skipped — fix any typos and rerun.")

    parts.append("")
    parts.append(f"## Auto-derived ({_goldfish_n_cards(len(auto_lines))})")
    if auto_lines:
        parts.extend(auto_lines)
    else:
        parts.append("- (none)")

    out_total = sum(len(v) for v in out_by_class.values())
    parts.append("")
    parts.append(f"## Out of scope ({_goldfish_n_cards(out_total)})")
    if out_total:
        parts.append("Inert in the sim; counted by class in the honesty report.")
        for cls in sorted(out_by_class):
            parts.append(f"**{cls}** — {_GOLDFISH_D9_NOTES.get(cls, cls)}")
            parts.extend(f"- {n}" for n in out_by_class[cls])
    else:
        parts.append("- (none)")

    parts.append("")
    parts.append(f"## Needs annotation ({_goldfish_n_cards(len(needs))})")
    if not needs:
        parts.append("- (none) — every card is auto-derived or classified.")
        return "\n".join(parts)

    parts.append("Write a JSON annotation for each card below (pass them to "
                 "goldfish_run as `annotations`), or leave a card out to "
                 "treat it as inert.")
    for raw, d in needs:
        mana = raw.get("mana_cost") or "—"
        parts.append("")
        parts.append(f"### {d.card.name} — {mana} — {raw.get('type_line', '?')}")
        parts.append(d.oracle or "(no oracle text)")

    parts.append("")
    parts.append("### Registry cheat-sheet (unknown names are rejected)")
    parts.append(f"- self events (on): {', '.join(sorted(SELF_EVENTS))}")
    parts.append(f"- global events (on): {', '.join(sorted(GLOBAL_EVENTS))}")
    parts.append(f"- verbs (do): {', '.join(sorted(VERBS))}")
    parts.append(f"- conditions (if): {', '.join(sorted(CONDITIONS))} "
                 f"(power_gte takes ':N', e.g. 'power_gte:7')")
    parts.append(f"- counts: an integer or {', '.join(sorted(SYMBOLIC_COUNTS))}")
    parts.append(f"- statics: {', '.join(sorted(STATIC_KINDS))}")
    parts.append(f"- spell_cast filters (filter): "
                 f"{', '.join(sorted(SPELL_CAST_FILTERS))}")
    parts.append(f"- tutor filters (filter): "
                 f"{', '.join(sorted(TUTOR_FILTERS))}, or name:<CardName>")
    parts.append(f"- cost_reduction filters: "
                 f"{', '.join(sorted(COST_REDUCTION_FILTERS))}, "
                 f"or color:<W|U|B|R|G>")
    parts.append(f"- damage targets (target): "
                 f"{', '.join(sorted(DAMAGE_TARGETS))}")
    parts.append("")
    parts.append("Worked example — trigger/static/activated card:")
    parts.append("```json")
    parts.append(_GOLDFISH_EXAMPLE_CLOUD)
    parts.append("```")
    parts.append("Worked example — Signet-style filter rock (these always "
                 "land in this section by design):")
    parts.append("```json")
    parts.append(_GOLDFISH_EXAMPLE_SIGNET)
    parts.append("```")

    return "\n".join(parts)


# ── Goldfish batch simulation (spec §Concurrency, §Statistics, D4) ───────────


GOLDFISH_MAX_N = 20_000
GOLDFISH_MAX_RUNS = 50

# Hosted report delivery (spec §Run reports, delivery path 2). When
# GOLDFISH_REPORT_DIR is set, every completed run's HTML is persisted there
# (content-addressed <run_code>.html, size-capped); when the public base URL
# is ALSO set, run/AB outputs append a public Report: link served by the
# /goldfish/r/{code} route. When unset, tools simply omit the link and
# goldfish_report serves from the in-memory LRU.
GOLDFISH_REPORT_DIR = os.environ.get("GOLDFISH_REPORT_DIR")
GOLDFISH_PUBLIC_BASE_URL = os.environ.get(
    "GOLDFISH_PUBLIC_BASE_URL", "").rstrip("/")
GOLDFISH_REPORT_FILE_CAP = 500     # newest files kept by the dir cleanup

#: At most two CPU-bound sims run concurrently server-wide; further requests
#: queue here while every other tool call stays responsive (spec §Concurrency).
_GOLDFISH_SEMAPHORE = asyncio.Semaphore(2)

#: run_id -> {"inputs", "result", "kind": "run"|"ab"}; LRU capped, consumed by
#: goldfish_report (Task 21).
_GOLDFISH_RUNS: "OrderedDict[str, dict]" = OrderedDict()

#: Spec §Statistics scope-banner sentence, verbatim.
_GOLDFISH_AB_BANNER_SENTENCE = (
    "Deltas measure speed and consistency only; the sim cannot value "
    "interaction, and converting out-of-scope cards to simulable ones "
    "inflates deltas — do not read results as 'cut your removal'.")


async def _run_sim_offloop(fn, *args, **kw):
    """Run a CPU-bound simulation off the event loop (spec §Concurrency): a
    sim in flight must never block other users' tool calls."""
    async with _GOLDFISH_SEMAPHORE:
        return await asyncio.to_thread(fn, *args, **kw)


def _goldfish_store_run(run_id: str, inputs: dict, result: dict,
                        kind: str) -> None:
    """LRU-store a completed run, keeping only what the report renders:
    inputs (resolved library + commander header info), metrics/deltas/
    correlation and the extras the tools fold in. Per-game records are
    dropped — they dwarf everything else and nothing re-reads them."""
    slim = {k: v for k, v in result.items()
            if k not in ("records", "records_a", "records_b")}
    _GOLDFISH_RUNS[run_id] = {"inputs": inputs, "result": slim,
                              "kind": kind, "html": None}
    _GOLDFISH_RUNS.move_to_end(run_id)
    while len(_GOLDFISH_RUNS) > GOLDFISH_MAX_RUNS:
        _GOLDFISH_RUNS.popitem(last=False)


def _goldfish_get_run(run_id: str) -> dict | None:
    """Fetch a stored run and refresh its LRU recency (reads keep a
    frequently-consulted report alive, mirroring the games store)."""
    entry = _GOLDFISH_RUNS.get(run_id)
    if entry is not None:
        _GOLDFISH_RUNS.move_to_end(run_id)
    return entry


def _goldfish_entry_html(entry: dict) -> str:
    """Render an entry's report lazily and cache it. The generated-at stamp
    is applied HERE (server side) — report.render_report itself never reads
    the clock — and is excluded from the run-code hash by the renderer."""
    if entry["html"] is None:
        stamped = dict(entry["inputs"])
        stamped["generated_at"] = time.strftime(
            "%Y-%m-%d %H:%M UTC", time.gmtime())
        entry["html"] = report.render_report(
            entry["result"], stamped, kind=entry["kind"])
    return entry["html"]


def _goldfish_persist_report(run_id: str, entry: dict) -> str | None:
    """Hosted mode (spec §Run reports): when GOLDFISH_REPORT_DIR is set,
    write the rendered report as <run_code>.html and prune the directory to
    the newest GOLDFISH_REPORT_FILE_CAP files by mtime. Returns the public
    report URL when GOLDFISH_PUBLIC_BASE_URL is also set, else None."""
    if not GOLDFISH_REPORT_DIR:
        return None
    os.makedirs(GOLDFISH_REPORT_DIR, exist_ok=True)
    path = os.path.join(GOLDFISH_REPORT_DIR, f"{run_id}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_goldfish_entry_html(entry))
    paths = [os.path.join(GOLDFISH_REPORT_DIR, name)
             for name in os.listdir(GOLDFISH_REPORT_DIR)
             if name.endswith(".html")]

    def _mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0        # concurrently removed: sorts oldest, prune skips
    paths.sort(key=_mtime, reverse=True)                # newest first
    for stale in paths[GOLDFISH_REPORT_FILE_CAP:]:
        try:
            os.remove(stale)
        except OSError:
            pass                     # a racing cleanup already removed it
    if GOLDFISH_PUBLIC_BASE_URL:
        return f"{GOLDFISH_PUBLIC_BASE_URL}/goldfish/r/{run_id}"
    return None


def _goldfish_read_report_file(code: str) -> str | None:
    """Blocking read of a persisted report; None when unconfigured/absent."""
    if not GOLDFISH_REPORT_DIR:
        return None
    path = os.path.join(GOLDFISH_REPORT_DIR, f"{code}.html")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


async def _goldfish_report_page(request):
    """GET /goldfish/r/{code} — serve a persisted report (public-with-code,
    the same capability-token model as game ids). Unknown/bad codes 404."""
    from starlette.responses import HTMLResponse, PlainTextResponse
    code = request.path_params["code"]
    if not re.fullmatch(r"[a-z0-9]{13}", code):
        return PlainTextResponse("bad code", status_code=404)
    html = await asyncio.to_thread(_goldfish_read_report_file, code)
    if html is None:
        return PlainTextResponse("unknown run code", status_code=404)
    return HTMLResponse(html)


if GOLDFISH_REPORT_DIR:
    mcp.custom_route("/goldfish/r/{code}",
                     methods=["GET"])(_goldfish_report_page)


async def _goldfish_prepare(
    deck_str: str, annotations: list[dict],
) -> tuple[dict[str, SimCard], list[str], str, str | None,
           dict[str, str | None], dict[str, list[str]], list[str], list[str]]:
    """Shared goldfish_run/goldfish_ab prep: validate client annotations
    (pure, fails fast before any network) -> load deck -> fetch -> derive ->
    merge.

    Returns (cards_pool, deck_names, commander, commander_note, card_scopes,
    approx_by_card, not_found, unmatched_anns). ``deck_names`` is the
    LIBRARY: one commander copy removed (it starts in the command zone) and
    Scryfall-unrecognized names dropped (reported via ``not_found``).
    ``card_scopes`` feeds metrics.aggregate's honesty split: name -> D9
    scope class, "unrecognized" (needs annotation, none supplied), or None
    (modeled). ``unmatched_anns`` names client annotations that matched no
    deck card (likely typos — the tools warn instead of silently no-oping
    the self-correction loop). Raises ValueError for every user-fixable
    problem — AnnotationError messages propagate verbatim (the
    self-correction contract) and HTTP failures are pre-formatted through
    the existing error helpers."""
    client_anns = validate_annotations(annotations)   # AnnotationError -> up
    try:
        names, commander, cmdr_note = await _goldfish_load_deck(deck_str)
    except httpx.HTTPError as e:
        raise ValueError(_archidekt_error(e)) from e
    fetch_names = list(names)
    if commander not in fetch_names:
        fetch_names.append(commander)
    try:
        cards_raw, not_found = await _goldfish_fetch_cards(fetch_names)
    except httpx.HTTPError as e:
        raise ValueError(_scryfall_error(e)) from e

    derived = autoderive.derive(cards_raw)

    # Pool cards under the REQUESTED (deck) name — _goldfish_fetch_cards
    # returns found cards in requested order, so zip recovers the mapping
    # even when Scryfall canonicalizes the name.
    missing = set(not_found)
    found_names = [n for n in dict.fromkeys(fetch_names) if n not in missing]
    cards_pool: dict[str, SimCard] = {}
    card_scopes: dict[str, str | None] = {}
    approx_by_card: dict[str, list[str]] = {}
    matched_anns: set[str] = set()
    for req_name, raw in zip(found_names, cards_raw):
        d = derived.get(raw.get("name") or req_name)
        if d is None:
            continue
        ann_key = req_name if req_name in client_anns else d.card.name
        ann = client_anns.get(ann_key)
        if ann is not None:
            matched_anns.add(ann_key)
            # A client annotation replaces/extends the derived model; an
            # annotated card is MODELED regardless of its prior class (D1).
            cards_pool[req_name] = merge_card(d.card.data, ann,
                                              scope_class=None)
            card_scopes[req_name] = None
        else:
            cards_pool[req_name] = d.card
            card_scopes[req_name] = ("unrecognized" if d.needs_annotation
                                     else d.card.scope_class)
        if d.approx_notes:
            approx_by_card[req_name] = list(d.approx_notes)
    unmatched_anns = sorted(set(client_anns) - matched_anns)

    if commander not in cards_pool:
        raise ValueError(
            f"Commander {commander!r} was not recognized by Scryfall; fix "
            "the name and rerun.")
    library = [n for n in names if n in cards_pool]
    if commander in library:
        library.remove(commander)     # one copy starts in the command zone
    if not library:
        raise ValueError(
            "No recognized cards besides the commander; nothing to simulate.")
    return (cards_pool, library, commander, cmdr_note, card_scopes,
            approx_by_card, not_found, unmatched_anns)


def _goldfish_check_combos(combos: list, valid_names: set[str]) -> str | None:
    """Tools-layer combo validation: the engine silently never-assembles a
    typo'd piece, so this layer owns the rejection. Returns an error message,
    or None when every piece is a deck card or the commander."""
    bad: list[str] = []
    for combo in combos:
        pieces = combo.get("cards") if isinstance(combo, dict) else combo
        if not isinstance(pieces, list) or not pieces:
            return ("Each combo must be a list of card names, or "
                    '{"cards": [names, ...], "wins": true|false}.')
        bad.extend(str(p) for p in pieces if p not in valid_names)
    if bad:
        return ("Combo piece(s) not in the deck: " + ", ".join(sorted(set(bad)))
                + ". Fix the names — a piece the deck can't draw never "
                "assembles.")
    return None


def _goldfish_fmt_prop(p: dict) -> str:
    lo, hi = p["ci"]
    return f"{p['value'] * 100:.1f}% [{lo * 100:.1f}–{hi * 100:.1f}]"


def _goldfish_fmt_turn(v) -> str:
    return "n/a" if v is None else f"T{v}"


def _goldfish_summary_lines(m: dict, until_turn: int) -> list[str]:
    """The 8-line headline summary (D4): commander cast, kill, damage,
    mulligans, on-curve, top-3 trigger table lines."""
    cc = m["commander_cast"]
    by = cc["pct_by_turn"]
    kill = m["kill"]
    dmg = m["damage"]
    mull = m["mulligans"]
    lines = [
        (f"- Commander cast: median {_goldfish_fmt_turn(cc['median_among_reached'])} "
         f"(reached {_goldfish_fmt_prop(cc['reached_pct'])}); "
         f"by T4 {_goldfish_fmt_prop(by['4'])}, T5 {_goldfish_fmt_prop(by['5'])}, "
         f"T6 {_goldfish_fmt_prop(by['6'])}"),
        (f"- Kill (40 damage): reached {_goldfish_fmt_prop(kill['reached_pct'])} "
         f"by T{until_turn}; median {_goldfish_fmt_turn(kill['median_among_reached'])} "
         f"among reached"),
        (f"- Damage by T{until_turn}: avg {dmg['avg_total']:.1f} total "
         f"(combat {dmg['combat']['avg_total']:.1f}, "
         f"noncombat {dmg['noncombat']['avg_total']:.1f})"),
        (f"- Mulligans: {_goldfish_fmt_prop(mull['pct_mulliganed'])} of games; "
         f"avg kept hand {mull['avg_kept_hand_size']:.1f} with "
         f"{mull['avg_kept_lands']:.1f} lands"),
        (f"- On-curve: {_goldfish_fmt_prop(m['on_curve']['pct_all_drops_through_t5'])} "
         f"made every land drop through T5"),
    ]
    top = sorted(m["trigger_fires"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    for key, avg_fires in top:
        card, event, verb = key.rsplit("|", 2)
        lines.append(f"- Trigger: {card} ({event} → {verb}) — "
                     f"{avg_fires:.2f} fires/game")
    if not top:
        lines.append("- Triggers: (none fired — no annotated triggers)")
    return lines


def _goldfish_honesty_lines(honesty: dict, approx_by_card: dict[str, list[str]],
                            heading: str = "## Honesty report") -> list[str]:
    """Three-way honesty split (spec §v1 metrics) plus the standing
    approximation notes grouped by kind prefix."""

    def drawn(entries: list[dict]) -> str:
        return ", ".join(f"{e['name']} (drawn "
                         f"{e['drawn_pct']['value'] * 100:.0f}%)"
                         for e in entries)

    lines = [heading]
    oos = honesty["out_of_scope"]
    n_oos = sum(len(v) for v in oos.values())
    lines.append(f"Out of scope — {_goldfish_n_cards(n_oos)}, inert, counted "
                 f"by class (the sim cannot value these):")
    if n_oos:
        lines.extend(f"- {cls}: {drawn(oos[cls])}" for cls in sorted(oos))
    else:
        lines.append("- (none)")
    unrec = honesty["unrecognized"]
    lines.append(f"Unrecognized — {_goldfish_n_cards(len(unrec))} "
                 f"(candidates for annotation; see goldfish_annotate):")
    lines.append(f"- {drawn(unrec)}" if unrec else "- (none)")
    low = honesty["modeled_low_impact"]
    lines.append("Modeled, low-impact — drawn but never fired (only this "
                 "group supports 'the sim agrees this card underperforms'):")
    lines.append("- " + ", ".join(low) if low else "- (none)")
    by_kind: dict[str, set[str]] = {}
    for name, notes in approx_by_card.items():
        for note in notes:
            by_kind.setdefault(note.split(":", 1)[0], set()).add(name)
    if by_kind:
        lines.append("Standing approximations (v1 model shortcuts):")
        lines.extend(f"- {kind}: {', '.join(sorted(by_kind[kind]))}"
                     for kind in sorted(by_kind))
    return lines


def _goldfish_scope_banner(
    lib_a: list[str], lib_b: list[str],
    scopes_a: dict[str, str | None], scopes_b: dict[str, str | None],
) -> str:
    """The goldfish_ab opening paragraph (spec §Statistics, goal 7): honesty
    accounting for the changed cards plus the verbatim delta-scope sentence."""
    ca, cb = Counter(lib_a), Counter(lib_b)
    changed_sides = ((ca - cb, scopes_a), (cb - ca, scopes_b))
    changed = sim = 0
    out_by_class: Counter = Counter()
    for only, scopes in changed_sides:
        for name, qty in only.items():
            changed += qty
            cls = scopes.get(name)
            if cls is None:
                sim += qty
            else:
                out_by_class[cls] += qty
    n_out = sum(out_by_class.values())
    out_part = f"{n_out} out of scope"
    if out_by_class:
        out_part += (" (" + ", ".join(f"{q} {cls}" for cls, q
                                      in sorted(out_by_class.items())) + ")")

    def oos_count(lib: list[str], scopes: dict[str, str | None]) -> int:
        return sum(1 for n in lib if scopes.get(n) is not None)

    return (f"Changed: {_goldfish_n_cards(changed)} — {sim} simulated, "
            f"{out_part}. "
            f"Deck A: {oos_count(lib_a, scopes_a)}/{len(lib_a)} out of scope; "
            f"Deck B: {oos_count(lib_b, scopes_b)}/{len(lib_b)}. "
            + _GOLDFISH_AB_BANNER_SENTENCE)


class GoldfishMulliganInput(BaseModel):
    """MulliganRules overrides; unset (None) fields keep the engine
    defaults. Field names mirror goldfish.engine.MulliganRules exactly — a
    test pins the correspondence — and pydantic types/bounds reject bad
    values here instead of letting them escape mid-sim as TypeErrors."""
    model_config = ConfigDict(extra="forbid")
    min_sources: int | None = Field(
        default=None, ge=0, le=7,
        description="Keep a 7-card hand only with at least this many mana "
                    "sources (default 3).")
    max_sources: int | None = Field(
        default=None, ge=0, le=7,
        description="...and at most this many (default 5).")
    lands_only: bool | None = Field(
        default=None,
        description="Count only true lands as sources, not mana rocks.")
    free_first: bool | None = Field(
        default=None, description="EDH free first mulligan (default true).")
    min_real_lands: int | None = Field(
        default=None, ge=0, le=7,
        description="Minimum actual lands in a kept 7-card hand (default 2).")


class GoldfishRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deck: str = Field(
        ..., min_length=1,
        description="Archidekt deck ID/URL, or decklist text ('1 Card Name' "
                    "per line, commander first or marked ' *CMDR*').")
    annotations: list[dict] = Field(
        default_factory=list,
        description="Card annotations (goldfish_annotate's DSL) for cards "
                    "the engine can't auto-derive.")
    combos: list = Field(
        default_factory=list,
        description="Declared combos: each a list of card names or "
                    '{"cards": [...], "wins": bool}.')
    n: int = Field(default=1000, ge=1, le=GOLDFISH_MAX_N,
                   description="Number of games to simulate.")
    seed: int = Field(default=42, description="Master RNG seed.")
    until_turn: int = Field(default=10, ge=1, le=30,
                            description="Simulate through this turn.")
    opponents: int = Field(default=1, ge=1, le=5,
                           description="Goldfish opponents (life totals).")
    mulligan: GoldfishMulliganInput = Field(
        default_factory=GoldfishMulliganInput,
        description="MulliganRules overrides: min_sources, max_sources, "
                    "lands_only, free_first, min_real_lands.")


class GoldfishAbInput(BaseModel):
    # Deliberate v1 pin: no mulligan/opponents knobs here — A/B compares
    # decks under the engine-default rules, and per-arm knobs would confound
    # the paired deltas. Revisit only with a spec change.
    model_config = ConfigDict(extra="forbid")
    deck_a: str = Field(..., min_length=1,
                        description="Deck A: Archidekt ID/URL or decklist text.")
    deck_b: str = Field(..., min_length=1,
                        description="Deck B: Archidekt ID/URL or decklist text.")
    annotations: list[dict] = Field(
        default_factory=list,
        description="Shared annotations applied to both decks.")
    annotations_a: list[dict] = Field(
        default_factory=list,
        description="Deck-A-only annotations (override shared ones by name).")
    annotations_b: list[dict] = Field(
        default_factory=list,
        description="Deck-B-only annotations (override shared ones by name).")
    combos: list = Field(
        default_factory=list,
        description="Declared combos, applied to both decks.")
    n: int = Field(default=1000, ge=1, le=GOLDFISH_MAX_N,
                   description="Number of paired games per arm.")
    seed: int = Field(default=42, description="Master RNG seed (shared).")
    until_turn: int = Field(default=10, ge=1, le=30,
                            description="Simulate through this turn.")
    allow_different_commanders: bool = Field(
        default=False,
        description="Compare decks with different commanders anyway "
                    "(cross-commander deltas confound every metric).")


@mcp.tool(name="goldfish_run")
async def goldfish_run(params: GoldfishRunInput) -> str:
    """Batch goldfish simulation: n policy-driven games of the deck, with
    per-proportion 95% CIs, an honesty report of what was and wasn't
    simulated, and a run_id content-addressing the inputs.

    Run goldfish_annotate first — pass its requested annotations here as
    `annotations`. Declared `combos` are tracked (assembled / castable by
    turn); every piece must be a deck card or the commander.
    """
    try:
        (cards_pool, library, commander, cmdr_note, card_scopes,
         approx_by_card, not_found, unmatched_anns) = await _goldfish_prepare(
            params.deck, params.annotations)
    except ValueError as e:
        return str(e)      # AnnotationError message verbatim: self-correction
    combo_err = _goldfish_check_combos(
        params.combos, set(library) | {commander})
    if combo_err:
        return combo_err
    mull_overrides = params.mulligan.model_dump(exclude_none=True)
    rules = MulliganRules(**mull_overrides)
    until_turn = params.until_turn

    def _run() -> dict:
        result = run_batch(
            cards_pool, library, commander, params.n, params.seed, until_turn,
            opponents=params.opponents, combos=params.combos, rules=rules)
        # run_batch aggregates without card scopes; re-aggregate WITH them so
        # the honesty split lands in the metrics. One extra aggregate pass
        # (~100ms at n=10k) — accepted, and it stays off the event loop here.
        result["metrics"] = metrics.aggregate(
            result["records"], until_turn, card_scopes)
        return result

    try:
        result = await _run_sim_offloop(_run)
    # Deliberate catch-all: engine backstops (e.g. the runner's livelock
    # guard RuntimeError) must not surface as raw tracebacks to the client.
    except Exception:  # noqa: BLE001
        return ("internal simulator error — please report with these "
                f"inputs: seed={params.seed}, n={params.n}, "
                f"until_turn={until_turn}")
    # Content-address the RESOLVED library in its GIVEN order + commander,
    # not the raw deck string: this list is exactly what the engine
    # shuffles, so identical code ⇒ byte-identical results — the spec's
    # reproducibility claim (report.run_code docstring).
    inputs = {"library": list(library), "commander": commander,
              "annotations": params.annotations,
              "combos": params.combos, "n": params.n, "seed": params.seed,
              "until_turn": until_turn, "opponents": params.opponents,
              "mulligan": mull_overrides, "engine_version": ENGINE_VERSION}
    run_id = report.run_code(inputs)
    # Fold in what the report renders beyond runner output (D11 honesty
    # standing notes + header note); stored alongside metrics, records-free.
    result["approx_by_card"] = approx_by_card
    result["commander_note"] = cmdr_note
    _goldfish_store_run(run_id, inputs, result, "run")
    # Persist (render + disk IO) off the event loop, like the route reads.
    report_url = await asyncio.to_thread(
        _goldfish_persist_report, run_id, _GOLDFISH_RUNS[run_id])

    m = result["metrics"]
    cmdr_display = f"{commander} {cmdr_note}" if cmdr_note else commander
    parts = [
        "# Goldfish run",
        (f"Deck: {_goldfish_n_cards(len(library) + 1)} including commander | "
         f"Commander: {cmdr_display} | {params.n} games, seed {params.seed}, "
         f"through turn {until_turn} | run_id: {run_id}"),
    ]
    if unmatched_anns:
        parts += ["", "Annotations for cards not in deck (check spelling): "
                  + ", ".join(unmatched_anns)]
    if not_found:
        parts += ["", "Warning — not recognized by Scryfall, skipped: "
                  + ", ".join(not_found)]
    parts += ["", "## Summary"]
    parts += _goldfish_summary_lines(m, until_turn)
    payload = {"n": result["n"], "seed": result["seed"],
               "until_turn": result["until_turn"], "run_id": run_id,
               "metrics": m}
    parts += ["", "## Metrics", "```json", json.dumps(payload), "```"]
    parts += [""] + _goldfish_honesty_lines(m["honesty"], approx_by_card)
    parts += ["", f'Full report: goldfish_report("{run_id}")']
    if report_url:
        parts += [f"Report: {report_url}"]
    return "\n".join(parts)


@mcp.tool(name="goldfish_ab")
async def goldfish_ab(params: GoldfishAbInput) -> str:
    """Paired A/B goldfish comparison: both decks run game-for-game under
    identical seeds with position-stable deck alignment; reports per-metric
    paired deltas ± CI with significance flags (exact McNemar for binary
    metrics), the achieved pairing correlation, and both honesty reports.

    Refuses decks with different commanders unless
    allow_different_commanders=true.
    """
    try:
        (cards_a, lib_a, cmdr_a, _note_a, scopes_a, approx_a,
         nf_a, unmatched_a) = await _goldfish_prepare(
            params.deck_a, params.annotations + params.annotations_a)
    except ValueError as e:
        return f"Deck A: {e}"
    try:
        (cards_b, lib_b, cmdr_b, _note_b, scopes_b, approx_b,
         nf_b, unmatched_b) = await _goldfish_prepare(
            params.deck_b, params.annotations + params.annotations_b)
    except ValueError as e:
        return f"Deck B: {e}"
    if len(lib_a) != len(lib_b):
        return (f"A/B decks must be the same size for position-stable "
                f"pairing: deck A has {_goldfish_n_cards(len(lib_a))}, "
                f"deck B has {len(lib_b)} (excluding commanders and any "
                f"Scryfall-unrecognized names).")
    combo_err = _goldfish_check_combos(
        params.combos, set(lib_a) | set(lib_b) | {cmdr_a, cmdr_b})
    if combo_err:
        return combo_err
    until_turn = params.until_turn

    def _run() -> dict:
        result = run_ab(
            cards_a, lib_a, cmdr_a, cards_b, lib_b, cmdr_b,
            params.n, params.seed, until_turn, combos=params.combos,
            allow_different_commanders=params.allow_different_commanders)
        # Same re-aggregation as goldfish_run: run_ab's per-arm metrics lack
        # card scopes; redo them so both honesty reports exist. Off-loop.
        result["metrics_a"] = metrics.aggregate(
            result["records_a"], until_turn, scopes_a)
        result["metrics_b"] = metrics.aggregate(
            result["records_b"], until_turn, scopes_b)
        return result

    try:
        result = await _run_sim_offloop(_run)
    except ValueError as e:
        return str(e)                      # different-commander guard
    # Deliberate catch-all — same rationale as goldfish_run's.
    except Exception:  # noqa: BLE001
        return ("internal simulator error — please report with these "
                f"inputs: seed={params.seed}, n={params.n}, "
                f"until_turn={until_turn}")
    # Resolved libraries hashed in GIVEN order, as in goldfish_run
    # (report.run_code docstring: order is a run input).
    inputs = {"library_a": list(lib_a), "library_b": list(lib_b),
              "commander_a": cmdr_a, "commander_b": cmdr_b,
              "annotations": params.annotations,
              "annotations_a": params.annotations_a,
              "annotations_b": params.annotations_b,
              "combos": params.combos, "n": params.n, "seed": params.seed,
              "until_turn": until_turn,
              "allow_different_commanders": params.allow_different_commanders,
              "engine_version": ENGINE_VERSION}
    run_id = report.run_code(inputs)
    banner = _goldfish_scope_banner(lib_a, lib_b, scopes_a, scopes_b)
    # The report re-emits the banner verbatim and the standing notes (D11).
    result["scope_banner"] = banner
    result["approx_by_card_a"] = approx_a
    result["approx_by_card_b"] = approx_b
    _goldfish_store_run(run_id, inputs, result, "ab")
    # Persist (render + disk IO) off the event loop, like the route reads.
    report_url = await asyncio.to_thread(
        _goldfish_persist_report, run_id, _GOLDFISH_RUNS[run_id])

    # SCOPE BANNER FIRST (spec §Statistics) — nothing may precede it.
    parts = [banner, ""]
    cmdrs = (f"Commander: {cmdr_a}" if cmdr_a == cmdr_b
             else f"Commanders: A {cmdr_a} vs B {cmdr_b}")
    parts.append(f"# Goldfish A/B\n{cmdrs} | {params.n} paired games, "
                 f"seed {params.seed}, through turn {until_turn} | "
                 f"run_id: {run_id}")
    for label, nf, unmatched in (("A", nf_a, unmatched_a),
                                 ("B", nf_b, unmatched_b)):
        if unmatched:
            parts += ["", f"Deck {label} annotations for cards not in deck "
                      f"(check spelling): " + ", ".join(unmatched)]
        if nf:
            parts += ["", f"Warning — deck {label} names not recognized by "
                      f"Scryfall, skipped: " + ", ".join(nf)]
    parts += ["", "## Deltas (A − B)"]
    for name, d in result["deltas"].items():
        ci = f"[{d['ci_low']:+.4f}, {d['ci_high']:+.4f}]"
        sig = "YES" if d["significant"] else "no"
        if is_binary_ab_metric(name) and d.get("mcnemar_p") is not None:
            sig += f" (McNemar p={d['mcnemar_p']:.3g})"
        parts.append(f"- {name}: Δ {d['mean']:+.4f}, 95% CI {ci} — "
                     f"significant: {sig}")
    parts += ["", (f"Pair correlation: {result['pair_correlation']:.3f} "
                   f"(per-game total damage between arms; ~1.0 = pairing "
                   f"held, near 0 = degraded pairing)")]
    parts += ["", f"Caveat: {result['caveat']}"]
    parts += [""] + _goldfish_honesty_lines(
        result["metrics_a"]["honesty"], approx_a,
        heading="## Honesty report — Deck A")
    parts += [""] + _goldfish_honesty_lines(
        result["metrics_b"]["honesty"], approx_b,
        heading="## Honesty report — Deck B")
    payload = {"deltas": result["deltas"],
               "pair_correlation": result["pair_correlation"],
               "until_turn": result["until_turn"], "n": result["n"],
               "run_id": run_id}
    parts += ["", "## Deltas JSON", "```json", json.dumps(payload), "```"]
    parts += ["", f'Full report: goldfish_report("{run_id}")']
    if report_url:
        parts += [f"Report: {report_url}"]
    return "\n".join(parts)


class GoldfishReportInput(BaseModel):
    run_id: str = Field(...,
                        description="run_id from goldfish_run/goldfish_ab "
                                    "(the 13-char content-addressed run "
                                    "code).")


@mcp.tool(name="goldfish_report")
async def goldfish_report(params: GoldfishReportInput) -> str:
    """Self-contained HTML report for a completed goldfish_run/goldfish_ab
    (spec §Run reports, D11): fingerprint header, stat tiles with CIs,
    inline-SVG charts, trigger table, honesty report — no JavaScript, no
    external requests. Publish it VERBATIM as an artifact or file; never
    hand-author the numbers."""
    entry = _goldfish_get_run(params.run_id)
    if entry is None:
        live = ", ".join(_GOLDFISH_RUNS) or "(none)"
        return (f"Unknown run_id {params.run_id!r}. Live run_ids: {live}. "
                f"Completed runs are kept in a size-capped LRU (last "
                f"{GOLDFISH_MAX_RUNS}); re-running goldfish_run/goldfish_ab "
                "with the same inputs recreates the same run_id and report.")
    return _goldfish_entry_html(entry)


# ── Goldfish interactive mode (spec §Interactive mode, D3, D10) ──────────────


GOLDFISH_MAX_GAMES = 100
GOLDFISH_GAME_TTL = 24 * 3600
_GOLDFISH_ATTACH_TARGET_CAP = 8
#: Fast-forward step budget: the runner's per-turn livelock guard times the
#: engine's turn horizon (until only accepts turn:1-30 / "end" stops past 30).
_GOLDFISH_UNTIL_GUARD = 500 * 30
_GOLDFISH_UNTIL_TURN_CAP = 30
#: Response-size hygiene for until fast-forwards: echo only the LAST N
#: applied entries / log lines, with *_total carrying the full count.
_GOLDFISH_APPLIED_ECHO_CAP = 50
_GOLDFISH_LOG_DELTA_CAP = 200

#: gid -> {"game": Game, "cards": pool, "meta": {deck, annotations, combos},
#: "touched": last access}. LRU cap + idle TTL (spec §Concurrency); eviction
#: is non-destructive — every response echoes the full resume blob (D3).
_GOLDFISH_GAMES: "OrderedDict[str, dict]" = OrderedDict()

#: Every key Game.from_dict reads, plus the tool-layer "resume" meta.
_GOLDFISH_RESUME_KEYS = (
    "turn", "phase", "mana_pool", "land_drops_used", "spells_cast_this_turn",
    "combats_done", "extra_combats", "attacked_this_combat",
    "mulligans_taken", "free_mulligan_used", "kept_hand_size",
    "kept_hand_lands", "kept_hand_sources", "life_gained", "trigger_fires",
    "static_active_turn", "zones", "commander", "opponents", "won_turn",
    "combos", "combo_wins", "combo_assembled_turn", "combo_castable_turn",
    "rng_state", "log", "next_id", "resume")


def _games_evict() -> None:
    """TTL sweep then LRU cap, called on every store and access."""
    now = time.time()
    for gid in [gid for gid, v in _GOLDFISH_GAMES.items()
                if now - v["touched"] > GOLDFISH_GAME_TTL]:
        del _GOLDFISH_GAMES[gid]
    while len(_GOLDFISH_GAMES) > GOLDFISH_MAX_GAMES:
        _GOLDFISH_GAMES.popitem(last=False)


def _games_store(gid: str, game: Game, cards: dict, meta: dict) -> None:
    _GOLDFISH_GAMES[gid] = {"game": game, "cards": cards, "meta": meta,
                            "touched": time.time()}
    _GOLDFISH_GAMES.move_to_end(gid)
    _games_evict()


def _games_get(gid: str) -> dict | None:
    """Store access: evict first, then refresh the hit's LRU position."""
    _games_evict()
    entry = _GOLDFISH_GAMES.get(gid)
    if entry is not None:
        entry["touched"] = time.time()
        _GOLDFISH_GAMES.move_to_end(gid)
    return entry


def _goldfish_unknown_game(gid: str) -> str:
    return json.dumps({"error": (
        f"unknown game_id {gid!r} — the game may have been evicted (LRU cap "
        f"{GOLDFISH_MAX_GAMES} games, idle TTL 24h). Eviction is "
        "non-destructive: every response echoed the full state blob, so "
        'restart with goldfish_start(resume_state=<last echoed "state">) '
        "and the game continues with identical future draws.")})


def _goldfish_validate_resume(blob: dict) -> str | None:
    """Structural sanity for a resume blob BEFORE Game.from_dict (Task 8
    M3): required keys present, zone lists are lists of card names,
    battlefield attachment ids resolve within the blob, and the "resume"
    meta is usable for re-preparing the card pool. Returns the first
    problem found (None when sound) — a malformed blob must fail here with
    a named problem, never as a mid-step crash later."""
    missing = [k for k in _GOLDFISH_RESUME_KEYS if k not in blob]
    if missing:
        return "resume_state is missing key(s): " + ", ".join(missing)
    resume = blob["resume"]
    if not isinstance(resume, dict) or not isinstance(resume.get("deck"), str):
        return ('resume_state["resume"] must be the echoed {"deck": str, '
                '"annotations": list, "combos": list}')
    if not isinstance(resume.get("annotations"), list):
        return 'resume_state["resume"]["annotations"] must be a list'
    if not isinstance(resume.get("combos"), list):
        return 'resume_state["resume"]["combos"] must be a list'
    zones = blob["zones"]
    if not isinstance(zones, dict):
        return 'resume_state["zones"] must be a dict'
    for zone in ("library", "hand", "graveyard", "command"):
        names = zones.get(zone)
        if (not isinstance(names, list)
                or not all(isinstance(n, str) for n in names)):
            return (f'resume_state["zones"]["{zone}"] must be a list of '
                    "card names")
    battlefield = zones.get("battlefield")
    if (not isinstance(battlefield, list)
            or not all(isinstance(p, dict) for p in battlefield)):
        return ('resume_state["zones"]["battlefield"] must be a list of '
                "permanent dicts")
    ids = {p.get("id") for p in battlefield}
    for p in battlefield:
        pid = p.get("id")
        if not isinstance(pid, str) or not isinstance(p.get("name"), str):
            return ("every battlefield permanent needs string id and name "
                    "fields")
        attached = p.get("attached", [])
        if not isinstance(attached, list):
            return f"battlefield {pid}: attached must be a list of ids"
        for eq_id in attached:
            if eq_id not in ids:
                return (f"battlefield {pid}: attached id {eq_id!r} does not "
                        "resolve to any battlefield permanent in the blob")
        attached_to = p.get("attached_to")
        if attached_to is not None and attached_to not in ids:
            return (f"battlefield {pid}: attached_to id {attached_to!r} does "
                    "not resolve to any battlefield permanent in the blob")
    commander = blob["commander"]
    if (not isinstance(commander, dict)
            or not isinstance(commander.get("name"), str)):
        return ('resume_state["commander"] must be {"name": str, '
                '"cast_count": int}')
    opponents = blob["opponents"]
    if not isinstance(opponents, list) or not all(
            isinstance(o, dict) and "life" in o and "cmdr_dmg" in o
            for o in opponents):
        return ('resume_state["opponents"] must be a list of '
                '{"life", "cmdr_dmg"} dicts')
    rng_state = blob["rng_state"]
    if not isinstance(rng_state, list) or len(rng_state) != 3:
        return ('resume_state["rng_state"] must be the echoed 3-part list — '
                "it cannot be hand-written")
    return None


def _goldfish_compose_state(g: Game, meta: dict) -> dict:
    """The tool-layer state (spec §Interactive state schema): the engine's
    to_dict() plus the "resume" meta the engine deliberately doesn't carry
    (deck text, annotations, combos), so every echoed state is a complete
    goldfish_start(resume_state=...) input (D3)."""
    state = g.to_dict()
    state["resume"] = {"deck": meta["deck"],
                       "annotations": list(meta["annotations"]),
                       "combos": list(meta["combos"])}
    return state


def _goldfish_group_attach(g: Game, actions: list) -> list:
    """Response formatting ONLY (Task 8 review: pairwise attach entries hit
    124KB on token boards): collapse the engine's per-pair entries to one
    per equipment — {"type": "attach", "card": eq_id, "targets": [ids]} with
    targets capped at 8 by descending effective_power then id, plus
    "more_targets": N for the overflow. Every other action type passes
    through unchanged, in place. The engine's legal_actions stays the pinned
    ground truth, and step() still takes the engine shape
    {"type": "attach", "card": ..., "target": ...}."""
    def _target_key(pid: str):
        try:                          # engine ids are "b<N>"; anything else
            num = int(pid[1:])        # (hand-injected states) falls back to
        except ValueError:            # a plain string tiebreak, no crash
            num = 0
        return (-effective_power(g, g.perm(pid)), num, pid)

    grouped: list = []
    emitted: set[str] = set()
    for a in actions:
        if a.get("type") != "attach":
            grouped.append(a)
            continue
        eq_id = a["card"]
        if eq_id in emitted:
            continue
        emitted.add(eq_id)
        targets = [b["target"] for b in actions
                   if b.get("type") == "attach" and b["card"] == eq_id]
        targets.sort(key=_target_key)
        entry: dict = {"type": "attach", "card": eq_id,
                       "targets": targets[:_GOLDFISH_ATTACH_TARGET_CAP]}
        if len(targets) > _GOLDFISH_ATTACH_TARGET_CAP:
            entry["more_targets"] = len(targets) - _GOLDFISH_ATTACH_TARGET_CAP
        grouped.append(entry)
    return grouped


def _goldfish_game_payload(g: Game, meta: dict) -> dict:
    return {"state": _goldfish_compose_state(g, meta),
            "legal_actions": _goldfish_group_attach(g, legal_actions(g))}


class GoldfishStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deck: str | None = Field(
        default=None, min_length=1,
        description="Archidekt deck ID/URL, or decklist text ('1 Card Name' "
                    "per line, commander first or marked ' *CMDR*'). "
                    "Exactly one of deck/resume_state.")
    annotations: list[dict] = Field(
        default_factory=list,
        description="Card annotations (goldfish_annotate's DSL) for cards "
                    "the engine can't auto-derive.")
    combos: list = Field(
        default_factory=list,
        description="Declared combos: each a list of card names or "
                    '{"cards": [...], "wins": bool}.')
    seed: int = Field(default=42, description="RNG seed for the shuffle.")
    opponents: int = Field(default=1, ge=1, le=5,
                           description="Goldfish opponents (life totals).")
    interactive_mulligan: bool = Field(
        default=False,
        description="Start in the mulligan phase (legal actions: mulligan / "
                    "keep) instead of resolving mulligans automatically.")
    resume_state: dict | None = Field(
        default=None,
        description='A previously echoed "state" blob: restores an evicted '
                    "game, or rewinds to any earlier point with identical "
                    "future draws. Exactly one of deck/resume_state.")

    @model_validator(mode="after")
    def _exactly_one_source(self):
        if (self.deck is None) == (self.resume_state is None):
            raise ValueError("Provide exactly one of deck or resume_state.")
        return self


class GoldfishStepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game_id: str = Field(..., min_length=1,
                         description="Game id from goldfish_start.")
    action: dict | None = Field(
        default=None,
        description="One engine-shape action to apply, e.g. "
                    '{"type": "cast", "card": "..."} or {"type": "attach", '
                    '"card": "b1", "target": "b3"}. Omit both action and '
                    "until to let the policy choose one step.")
    until: str | None = Field(
        default=None,
        description='Fast-forward the policy to a boundary: "turn:N" (stop '
                    'at the start of turn N), "phase:main1|combat|main2", or '
                    '"end" (win or past turn 30). Already at the boundary → '
                    "applied is empty. Mutually exclusive with action.")

    @model_validator(mode="after")
    def _action_xor_until(self):
        if self.action is not None and self.until is not None:
            raise ValueError(
                "action and until are mutually exclusive — pass one.")
        return self


class GoldfishStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game_id: str = Field(..., min_length=1,
                         description="Game id from goldfish_start.")


def _goldfish_parse_until(until: str):
    """"turn:N" | "phase:X" | "end" -> (kind, value); error string otherwise."""
    if until == "end":
        return ("end", None)
    kind, sep, val = until.partition(":")
    if sep and kind == "turn":
        if val.isdigit() and 1 <= int(val) <= _GOLDFISH_UNTIL_TURN_CAP:
            return ("turn", int(val))
        return f"until turn must be 1-{_GOLDFISH_UNTIL_TURN_CAP}, got {val!r}"
    if sep and kind == "phase":
        if val in ("main1", "combat", "main2"):
            return ("phase", val)
        return ('until phase must be main1, combat, or main2 ("mulligan" '
                f'and "end" are never resting phases), got {val!r}')
    return ('until must be "turn:N", "phase:main1|combat|main2", or "end", '
            f"got {until!r}")


def _goldfish_until_done(g: Game, kind: str, value) -> bool:
    if g.won_turn is not None:
        return True
    if kind == "turn":
        return g.turn >= value        # stop at the START of turn N
    if kind == "phase":
        return g.phase == value
    return g.turn > _GOLDFISH_UNTIL_TURN_CAP       # "end" hard stop


@mcp.tool(name="goldfish_start")
async def goldfish_start(params: GoldfishStartInput) -> str:
    """Start (or resume) an interactive goldfish game. Returns JSON
    {"game_id", "state", "legal_actions"} (interactive tools return JSON,
    D4); "warnings" appears when annotations matched no deck card or names
    were not recognized by Scryfall.

    One step = one atomic action (D10): drive the game with goldfish_step,
    inspect it with goldfish_state. Every response echoes the full
    resumable state — pass it back as resume_state to restore an evicted
    game, or to rewind to any earlier point with identical future draws
    (D3): counterfactuals hold the deck order constant.

    Mulligans use the DEFAULT MulliganRules (pinned v1 — no interactive
    override): keep 7 with 3-5 mana sources (rocks count) and at least 2
    real lands, EDH free first mulligan. interactive_mulligan=true starts
    in the mulligan phase instead, with mulligan/keep as the legal actions
    (the same rules fix each keep's required bottom count).
    """
    if params.resume_state is not None:
        blob = params.resume_state
        err = _goldfish_validate_resume(blob)
        if err:
            return json.dumps({"error": err})
        resume = blob["resume"]
        try:
            (cards_pool, _library, _commander, _cmdr_note, _scopes, _approx,
             not_found, unmatched_anns) = await _goldfish_prepare(
                resume["deck"], resume["annotations"])
        except ValueError as e:
            return json.dumps({"error": str(e)})
        ensure_synthetic_cards(cards_pool)
        zones = blob["zones"]
        state_names = (set(zones["library"]) | set(zones["hand"])
                       | set(zones["graveyard"]) | set(zones["command"])
                       | {p["name"] for p in zones["battlefield"]}
                       | {blob["commander"]["name"]})
        unknown = sorted(n for n in state_names if n not in cards_pool)
        if unknown:
            return json.dumps({"error": (
                "resume_state references card(s) the rebuilt pool does not "
                "contain: " + ", ".join(unknown) + ' — resume["deck"] must '
                "be the deck this game was started from.")})
        try:
            g = Game.from_dict(blob, cards_pool)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            return json.dumps(
                {"error": f"resume_state could not be rebuilt: {e!r}"})
        meta = {"deck": resume["deck"],
                "annotations": list(resume["annotations"]),
                "combos": list(resume["combos"])}
    else:
        assert params.deck is not None  # _exactly_one_source guarantees this
        try:
            (cards_pool, library, commander, _cmdr_note, _scopes, _approx,
             not_found, unmatched_anns) = await _goldfish_prepare(
                params.deck, params.annotations)
        except ValueError as e:
            return json.dumps({"error": str(e)})   # AnnotationError verbatim
        combo_err = _goldfish_check_combos(
            params.combos, set(library) | {commander})
        if combo_err:
            return json.dumps({"error": combo_err})
        g = new_game(cards_pool, library, commander, seed=params.seed,
                     opponents=params.opponents, combos=params.combos)
        if not params.interactive_mulligan:
            auto_mulligan(g, MulliganRules())      # pinned defaults (v1)
        meta = {"deck": params.deck,
                "annotations": list(params.annotations),
                "combos": list(params.combos)}
    warnings = []
    if unmatched_anns:
        warnings.append("Annotations for cards not in deck (check "
                        "spelling): " + ", ".join(unmatched_anns))
    if not_found:
        warnings.append("Not recognized by Scryfall, skipped: "
                        + ", ".join(not_found))
    game_id = str(uuid.uuid4())
    _games_store(game_id, g, cards_pool, meta)
    payload = {"game_id": game_id, **_goldfish_game_payload(g, meta)}
    if warnings:
        payload["warnings"] = warnings
    return json.dumps(payload)


@mcp.tool(name="goldfish_step")
async def goldfish_step(params: GoldfishStepInput) -> str:
    """Advance an interactive goldfish game by atomic actions (D10): yours
    (`action`, engine shape), or the policy's (no arguments = one step;
    `until` = fast-forward to "turn:N" / "phase:X" / "end").

    Returns JSON {"applied", "state", "legal_actions", "log_delta"} —
    applied is one {"action", "reason"} (reason null for user actions, the
    fired policy rule otherwise); log_delta is only the log lines this call
    added. Under until, applied/log_delta echo just the LAST 50 entries /
    200 lines, with "applied_total"/"log_delta_total" carrying the full
    counts (the state's own log is always complete). An illegal action
    returns {"error", "state", "legal_actions"} and the game is guaranteed
    unmutated; a fast-forward that exhausts its step budget without
    reaching the boundary returns an error too, with the advanced state
    preserved. An unknown game_id names the recovery:
    goldfish_start(resume_state=...).
    """
    entry = _games_get(params.game_id)
    if entry is None:
        return _goldfish_unknown_game(params.game_id)
    g, meta = entry["game"], entry["meta"]
    log_before = len(g.log)
    if params.action is not None:
        try:
            step(g, params.action)
        except IllegalAction as e:
            return json.dumps({"error": str(e),
                               **_goldfish_game_payload(g, meta)})
        applied = {"action": params.action, "reason": None}
    elif params.until is not None:
        parsed = _goldfish_parse_until(params.until)
        if isinstance(parsed, str):
            return json.dumps({"error": parsed})
        kind, value = parsed
        applied_list: list = []
        while not _goldfish_until_done(g, kind, value):
            if len(applied_list) >= _GOLDFISH_UNTIL_GUARD:
                # Guard exhaustion must SURFACE, not masquerade as success:
                # a legal-but-degenerate annotation loop (the runner's
                # livelock case) would otherwise return 15k applied entries
                # with no signal. The advanced game stays in the store.
                return json.dumps({"error": (
                    f"stopped after {len(applied_list)} steps without "
                    f"reaching {params.until!r} — possible annotation "
                    "loop; state preserved"),
                    **_goldfish_game_payload(g, meta)})
            action, reason = choose_action(g)
            step(g, action)
            applied_list.append({"action": action, "reason": reason})
        log_delta = g.log[log_before:]
        return json.dumps({
            "applied": applied_list[-_GOLDFISH_APPLIED_ECHO_CAP:],
            "applied_total": len(applied_list),
            **_goldfish_game_payload(g, meta),
            "log_delta": log_delta[-_GOLDFISH_LOG_DELTA_CAP:],
            "log_delta_total": len(log_delta)})
    else:
        action, reason = choose_action(g)
        step(g, action)
        applied = {"action": action, "reason": reason}
    return json.dumps({"applied": applied,
                       **_goldfish_game_payload(g, meta),
                       "log_delta": g.log[log_before:]})


@mcp.tool(name="goldfish_state")
async def goldfish_state(params: GoldfishStateInput) -> str:
    """Current full state and legal actions of an interactive goldfish game
    as JSON {"game_id", "state", "legal_actions"}. The echoed state is the
    complete resume blob (D3) — keep the latest one to survive eviction."""
    entry = _games_get(params.game_id)
    if entry is None:
        return _goldfish_unknown_game(params.game_id)
    return json.dumps({"game_id": params.game_id,
                       **_goldfish_game_payload(entry["game"],
                                                entry["meta"])})


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
                                     "mystic_forge", "data", "og.png"),
                        media_type="image/png")


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
        return JSONResponse({"status": "error", "version": VERSION,
                             "db": False, "error": str(e)},
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
        "version": VERSION,
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
