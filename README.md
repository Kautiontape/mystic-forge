# Mystic Forge

A unified MCP (Model Context Protocol) server for Magic: The Gathering. Combines Scryfall card search & pricing, EDHRec commander recommendations, Archidekt deck reading, and decklist validation into a single server.

## Tools

### Scryfall — Card Search & Pricing
| Tool | Description |
|---|---|
| `scryfall_search` | Full Scryfall query syntax search |
| `scryfall_named` | Look up a card by name (exact + fuzzy) |
| `scryfall_random` | Random card with optional filter |
| `scryfall_price` | Prices per printing; pin one with set code + collector number, filter by finish |
| `scryfall_price_list` | Price a decklist at the printing and finish on each line |
| `scryfall_card_text` | Exact oracle text for a whole list of cards in one call |

### EDHRec — Commander Recommendations
| Tool | Description |
|---|---|
| `edhrec_commander` | Top card recommendations for a commander |
| `edhrec_average_deck` | Average decklist for a commander |
| `edhrec_precon_upgrade` | Community cut/added cards for a precon (real add %, ranked cuts) |
| `edhrec_combos` | Popular combo lines |
| `edhrec_top_cards` | Trending cards by period and color |
| `edhrec_recommendations` | Personalized suggestions given your current cards |
| `edhrec_salt` | Saltiest (most hated) cards |

### Archidekt — Deck Reading
| Tool | Description |
|---|---|
| `archidekt_deck` | Fetch a public deck by ID or URL (`include_text` adds full oracle text) |
| `archidekt_user_decks` | List a user's public decks |
| `archidekt_export` | Export deck as importable card list |

### Validation
| Tool | Description |
|---|---|
| `validate_decklist` | Verify card names, deck size, and color identity |
| `validate_archidekt_deck` | Full validation of an Archidekt deck including categories |

### Precon Decks (MTGJSON + EDHRec)
| Tool | Description |
|---|---|
| `precon_search` | Search official precons by name or set code (MTGJSON) |
| `precon_decklist` | Full official contents of a precon (MTGJSON) |
| `precon_export` | Export a precon in Archidekt import format |
| `precon_diff` | Exact cut/added cards between a precon and a specific deck |

### Comprehensive Rules
| Tool | Description |
|---|---|
| `rules_get` | Exact CR text by rule number (`702.2b`) or keyword/glossary term |
| `rules_search` | Ranked full-text search over rules and glossary |

## Quick Start

### Docker

```bash
docker compose up -d
```

The server runs at `http://localhost:8000/mcp` (streamable HTTP transport).

### Connect to Claude Code

Add to `~/.claude/settings.local.json`:

```json
{
  "mcpServers": {
    "mystic-forge": {
      "type": "url",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

### Connect to Claude.ai

Add as a Custom Connection with the URL `http://localhost:8000/mcp`.

### stdio mode

```bash
python server.py --stdio
```

## Self-Hosting with Private Archidekt Decks

Archidekt does not support OAuth, so private deck access requires your credentials. Create a `.env` file:

```bash
cp .env.example .env
# Edit .env with your Archidekt username and password
```

**Important:** Never deploy credentials on a shared/public server. Private deck access is for self-hosted instances only.

`MYSTIC_FORGE_CR` is optional: it sets where the auto-refreshed Comprehensive Rules cache is written; it defaults to the working directory.

## Price watchlist

Personal MTG price watchlists with ~90 days of MTGJSON daily history.
Lists are identified by a passphrase (shown once at `watchlist_create`):
use it in a personal connector URL (`https://mcp.kautiontape.com/mtg/mcp/<passphrase>`)
or pass it to tools in chat. Share codes (`SC-…`) grant read-only viewing at
`https://mcp.kautiontape.com/mtg/s/<code>`. Every change is an append-only event —
see the full chain at `https://mcp.kautiontape.com/mtg/w/<passphrase>` and recover
any revision with `watchlist_clone(at_seq=N)` (mints a new passphrase).
Prices ingest nightly from MTGJSON (tcgplayer/cardkingdom/cardmarket retail).
Health: `GET /health`.

## Data Sources

- **[Scryfall](https://scryfall.com)** — Card data and prices (updated daily from TCGPlayer, Cardmarket, Cardhoarder)
- **[EDHRec](https://edhrec.com)** — Commander metagame data and recommendations
- **[Archidekt](https://archidekt.com)** — Deck building and storage
- **[MTGJSON](https://mtgjson.com)** — Daily price history and printing/uuid mapping for the watchlist

## License

MIT
