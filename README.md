# Mystic Forge

A unified MCP (Model Context Protocol) server for Magic: The Gathering. Combines Scryfall card search & pricing, EDHRec commander recommendations, Archidekt deck reading, and decklist validation into a single server.

## Tools

### Scryfall — Card Search & Pricing
| Tool | Description |
|---|---|
| `scryfall_search` | Full Scryfall query syntax search |
| `scryfall_named` | Look up a card by name (exact + fuzzy) |
| `scryfall_random` | Random card with optional filter |
| `scryfall_price` | Current prices across all printings |
| `scryfall_price_list` | Batch-price up to 75 cards at once |

### EDHRec — Commander Recommendations
| Tool | Description |
|---|---|
| `edhrec_commander` | Top card recommendations for a commander |
| `edhrec_average_deck` | Average decklist for a commander |
| `edhrec_combos` | Popular combo lines |
| `edhrec_top_cards` | Trending cards by period and color |
| `edhrec_recommendations` | Personalized suggestions given your current cards |
| `edhrec_salt` | Saltiest (most hated) cards |

### Archidekt — Deck Reading
| Tool | Description |
|---|---|
| `archidekt_deck` | Fetch a public deck by ID or URL |
| `archidekt_user_decks` | List a user's public decks |
| `archidekt_export` | Export deck as importable card list |

### Validation
| Tool | Description |
|---|---|
| `validate_decklist` | Verify card names, deck size, and color identity |
| `validate_archidekt_deck` | Full validation of an Archidekt deck including categories |

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

## Data Sources

- **[Scryfall](https://scryfall.com)** — Card data and prices (updated daily from TCGPlayer, Cardmarket, Cardhoarder)
- **[EDHRec](https://edhrec.com)** — Commander metagame data and recommendations
- **[Archidekt](https://archidekt.com)** — Deck building and storage

## License

MIT
