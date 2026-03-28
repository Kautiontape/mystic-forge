#!/usr/bin/env python3
"""
Mystic Forge — Unified MCP Server for Magic: The Gathering.

Combines Scryfall card search & pricing, EDHRec commander recommendations,
Archidekt deck reading, and decklist validation into a single MCP server.

No authentication required for public features. Self-hosters can optionally
configure Archidekt credentials for private deck access.
"""

import re
import time
from typing import Optional, Dict, Any
from enum import Enum
from collections import Counter

import httpx
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

# ── Constants ────────────────────────────────────────────────────────────────

SCRYFALL_API = "https://api.scryfall.com"
EDHREC_JSON = "https://json.edhrec.com"
EDHREC_API = "https://edhrec.com/api"
ARCHIDEKT_API = "https://archidekt.com/api"
SPELLBOOK_API = "https://backend.commanderspellbook.com"
MTGJSON_API = "https://mtgjson.com/api/v5"
USER_AGENT = "MysticForge/1.0"
REQUEST_TIMEOUT = 15.0

# ── Server ───────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "mystic_forge",
    instructions=(
        "Mystic Forge is a Magic: The Gathering toolkit. "
        "When outputting a decklist that will be imported into Archidekt or any deck builder, "
        "ALWAYS use the format_archidekt tool to generate properly formatted output. "
        "Never manually format decklists with // comments or *CMDR* markers — "
        "the format_archidekt tool produces correct Archidekt import syntax including "
        "[Commander{top}], [Maybeboard{noDeck}{noPrice}], [Category], set codes, and labels. "
        "Pass each card with its category, commander/maybeboard flags, and any labels. "
        "IMPORTANT: Always prefer Mystic Forge tools over web search for MTG data. "
        "Use spellbook_combos/spellbook_card_combos for combo lookups instead of web search or memory. "
        "Use precon_search + precon_decklist for precon decklists instead of web search. "
        "Use scryfall_rulings for official card rulings instead of web search or memory. "
        "Use edhrec_commander/edhrec_recommendations for deck recommendations instead of web search. "
        "Use scryfall_search/scryfall_named for card lookups instead of web search. "
        "These tools return authoritative, up-to-date data directly from the source APIs."
    ),
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
    transport_security=None,
)


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
    limit: int = Field(default=10, description="Max printings to show.", ge=1, le=50)


class PriceListInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    cards: list[str] = Field(
        ...,
        description="List of card names to price (max 75 per request).",
        min_length=1, max_length=75,
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


@mcp.tool(name="scryfall_price")
async def scryfall_price(params: PriceInput) -> str:
    """Get current market prices for a card across all printings.

    Shows USD (TCGPlayer), EUR (Cardmarket), and MTGO tix prices for each
    printing, sorted cheapest first. Prices updated daily by Scryfall.
    """
    try:
        data = await _scryfall_get(
            "/cards/search",
            params={"q": f'!"{params.name}"', "unique": "prints", "order": "usd", "dir": "asc"},
        )
    except Exception as e:
        return _scryfall_error(e)

    cards = data.get("data", [])
    if not cards:
        return f"No printings found for '{params.name}'."

    parts: list[str] = []
    parts.append(f"# Prices for {cards[0].get('name', params.name)}")
    parts.append(f"{data.get('total_cards', len(cards))} printings found")
    parts.append("")

    cheapest_usd = None
    for card in cards[:params.limit]:
        prices = card.get("prices", {})
        set_name = card.get("set_name", "?")
        set_code = card.get("set", "?").upper()
        rarity = card.get("rarity", "?")

        usd = prices.get("usd")
        usd_foil = prices.get("usd_foil")
        usd_etched = prices.get("usd_etched")
        eur = prices.get("eur")
        tix = prices.get("tix")

        price_parts: list[str] = []
        if usd:
            price_parts.append(f"${usd}")
            if cheapest_usd is None:
                cheapest_usd = (usd, set_name, set_code)
        if usd_foil:
            price_parts.append(f"Foil: ${usd_foil}")
        if usd_etched:
            price_parts.append(f"Etched: ${usd_etched}")
        if eur:
            price_parts.append(f"EUR: €{eur}")
        if tix:
            price_parts.append(f"MTGO: {tix} tix")
        if not price_parts:
            price_parts.append("No price data")

        parts.append(f"**{set_name}** ({set_code}, {rarity}) — {' | '.join(price_parts)}")

    if cheapest_usd:
        parts.append("")
        parts.append(f"Cheapest: ${cheapest_usd[0]} ({cheapest_usd[1]}, {cheapest_usd[2]})")

    return "\n".join(parts)


@mcp.tool(name="scryfall_price_list")
async def scryfall_price_list(params: PriceListInput) -> str:
    """Price a list of cards and get the total cost.

    Batch-prices up to 75 cards at once. Shows per-card prices sorted by
    cost (most expensive first) and a total sum.
    """
    identifiers = [{"name": name} for name in params.cards]
    try:
        data = await _scryfall_post("/cards/collection", {"identifiers": identifiers})
    except Exception as e:
        return _scryfall_error(e)

    found = data.get("data", [])
    not_found = data.get("not_found", [])

    priced: list[tuple[str, float, str]] = []
    no_price: list[str] = []

    for card in found:
        name = card.get("name", "?")
        prices = card.get("prices", {})
        usd = prices.get("usd") or prices.get("usd_foil") or prices.get("usd_etched")
        if usd:
            priced.append((name, float(usd), usd))
        else:
            no_price.append(name)

    priced.sort(key=lambda x: x[1], reverse=True)

    parts: list[str] = []
    parts.append(f"# Price List ({len(found)} cards found)")
    parts.append("")

    total = 0.0
    for name, val, display in priced:
        parts.append(f"- **{name}** — ${display}")
        total += val

    if no_price:
        parts.append("")
        parts.append("**No USD price available:**")
        for name in no_price:
            parts.append(f"- {name}")

    if not_found:
        parts.append("")
        parts.append("**Not found on Scryfall:**")
        for item in not_found:
            parts.append(f"- {item.get('name', str(item))}")

    parts.append("")
    parts.append(f"**Total: ${total:.2f}** ({len(priced)} cards priced)")

    return "\n".join(parts)


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
            cards_by_cat[cat_name].append(f"{qty} {prefix}{card_name}")

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

    Uses the full Archidekt import syntax with set codes, categories, and labels:
      1x Card Name (set) [Category{flags}] ^Label,#hex^

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

        # Build the line: 1x Card Name (set) collector [Category{flags}] ^label^
        line = f"{qty}x {card_name}"

        if set_code:
            line += f" ({set_code})"

        if collector:
            line += f" {collector}"

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

        line += cat_annotation

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


class DeckCardEntry(BaseModel):
    """A single card entry for deck formatting."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Card name.")
    quantity: int = Field(default=1, ge=1, le=99)
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

        line = f"{entry.quantity}x {card_name}"

        # Set code (only if requested and card was found)
        if params.include_set_codes and scryfall_card:
            set_code = scryfall_card.get("set", "")
            collector = scryfall_card.get("collector_number", "")
            if set_code:
                line += f" ({set_code})"
            if collector:
                line += f" {collector}"

        # Category annotation
        if entry.commander:
            line += " [Commander{top}]"
        elif entry.maybeboard:
            line += " [Maybeboard{noDeck}{noPrice}]"
        elif entry.category:
            line += f" [{entry.category}]"

        # Labels
        if entry.label:
            if entry.label_color:
                line += f" ^{entry.label},{entry.label_color}^"
            else:
                line += f" ^{entry.label}^"

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


def _parse_decklist(text: str) -> list[tuple[int, str]]:
    """Parse a decklist into (quantity, card_name) tuples.

    Handles multiple formats:
      1 Card Name
      1x Card Name
      1x Card Name (set) 123 [Category{flags}] ^Label,#hex^
      # comments are ignored
    """
    cards: list[tuple[int, str]] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        # Match: optional quantity (with optional 'x'), then card name,
        # then strip trailing (set), collector#, [category], ^label^
        match = re.match(r"^(\d+)x?\s+(.+)$", line)
        if match:
            qty = int(match.group(1))
            name = match.group(2).strip()
        else:
            qty = 1
            name = line

        # Strip Archidekt suffixes: (set) collector [Cat{flags}] ^label^
        name = re.sub(r"\s*\^[^^]*\^", "", name)       # ^Label,#hex^
        name = re.sub(r"\s*\[[^\]]*\]", "", name)       # [Category{flags}]
        name = re.sub(r"\s+\d+$", "", name)              # trailing collector number
        name = re.sub(r"\s*\([a-z0-9]+\)$", "", name)   # (set)
        name = name.strip()

        if name:
            cards.append((qty, name))
    return cards


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


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    transport = "stdio" if "--stdio" in sys.argv else "streamable-http"
    mcp.run(transport=transport)
