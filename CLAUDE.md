# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Mystic Forge is an MCP (Model Context Protocol) server for Magic: The Gathering. It exposes 46 tools that wrap several public MTG APIs (Scryfall, EDHRec, Archidekt, Commander Spellbook, MTGJSON) behind a unified FastMCP server, plus a deck simulator and a price watchlist of its own.

Most of it lives in `server.py`, with supporting modules alongside: `rulebook.py` (Comprehensive Rules parsing), `watchlist_db.py` / `watchlist_ingest.py` / `watchlist_pages.py`, and the `goldfish/` package (simulation engine). There is no build step, but there **is** a SQLite database backing the watchlist — path from `MYSTIC_FORGE_DB`, defaulting to `mystic_forge.db`.

## Commands

```bash
# Run locally (HTTP transport, http://localhost:8000/mcp)
python server.py

# Run over stdio (for local MCP clients that spawn the process)
python server.py --stdio

# Run in Docker
docker compose up -d          # serves http://localhost:8000/mcp

# Install deps
pip install -r requirements.txt -r requirements-dev.txt

# Run the test suite (641 tests; -m "not slow" skips the long acceptance runs)
python -m pytest -q -m "not slow"
```

There is a real test suite (`tests/`, `pytest.ini`, `asyncio_mode = auto`) and `.github/workflows/ci.yml` runs it on every push and PR. There is still no linter config.

Tools themselves are mostly covered by offline tests; to smoke-test something uncovered, run the server and call it from an MCP client, or add a throwaway `asyncio.run(...)` against the tool's underlying `_<api>_get` helper.

## Releasing — pushing here does NOT deploy

This repo is a **git submodule** of the parent `mcp-servers` project (`..` → `Kautiontape/mcp-servers`; see `../.gitmodules`, `path = mystic-forge`). Production runs whatever commit the parent's submodule pointer names. **Pushing to `main` here changes nothing in production.**

A previous `deploy.yml` in this repo ran `git submodule update --remote` plus an on-box build on every push, which bypassed that pointer entirely and shipped unreviewed work. It has been deleted. Do not reintroduce it.

To release:

1. Bump `VERSION` (semver). It feeds the image tag, the outbound `User-Agent`, and `/health`.
2. Push to `main`. `ci.yml` runs the tests; `propose-release.yml` opens or updates a PR in `mcp-servers` bumping the submodule pin.
3. Add a changelog entry in the parent repo at `landing/mtg/changelog/index.html`, tagged `data-version="<VERSION>"`, and update the teaser in `landing/mtg/index.html` to match. Push those to the release PR branch.
4. Merge the PR. That builds the image from the pin and deploys it.

`mcp-servers/scripts/check_release.py` blocks the merge unless the version has a changelog entry, the version went up, the teaser matches the newest entry, and the tool tags on the landing page match `@mcp.tool(name=...)` in `server.py`. Adding or renaming a tool means updating that page in the same release.

Verify a release with `curl -s https://mcp.kautiontape.com/mtg/health` — it reports the running version.

## Architecture

`server.py` is organized into labeled sections, one per upstream data source, separated by `═══` banner comments:

- **SCRYFALL** — card search/lookup/pricing (`scryfall_*`)
- **EDHREC** — commander recommendations & metagame (`edhrec_*`)
- **ARCHIDEKT** — deck reading & export (`archidekt_*`)
- **FORMATTING** — `format_archidekt`, generates Archidekt-importable decklist text
- **VALIDATION** — `validate_decklist`, `validate_archidekt_deck`
- **COMMANDER SPELLBOOK** — combo search (`spellbook_*`)
- **RULINGS** — `scryfall_rulings`
- **RULEBOOK** — Comprehensive Rules lookup and search (`rules_*`), parsing via `rulebook.py` against the vendored `MagicCompRules.txt`
- **PRECON DECKS** — preconstructed decks via MTGJSON (`precon_*`)
- **GOLDFISH** — deck simulation (`goldfish_*`), engine in the `goldfish/` package
- **WATCHLIST** — passphrase-named price watchlists (`watchlist_*`, `price_history`), backed by SQLite and served as HTML pages under `/w/` and `/s/`
- **ENTRYPOINT** — `mcp.run()` with transport chosen by the `--stdio` flag

Sections past RULINGS do more than wrap an API: RULEBOOK, GOLDFISH, and WATCHLIST own local state (a vendored rules file, a simulation engine, a database). They do not follow the three-layer transport/error/tool shape below.

### Consistent per-section pattern

Every source follows the same three-layer shape, so match it when adding tools:

1. **Transport helpers** — `async def _<source>_get(...)` / `_<source>_post(...)` build the request against the source's base-URL constant (`SCRYFALL_API`, `EDHREC_JSON`/`EDHREC_API`, `ARCHIDEKT_API`, `SPELLBOOK_API`, `MTGJSON_API`), always sending `USER_AGENT` (derived from `VERSION`, e.g. `MysticForge/1.1.0`) and `REQUEST_TIMEOUT`. Each opens its own `httpx.AsyncClient()`.
2. **Error formatter** — `_<source>_error(e)` converts exceptions (esp. `httpx.HTTPStatusError`) into a human-readable string. Tools return these strings rather than raising.
3. **Pydantic input model + `@mcp.tool` function** — input models subclass `BaseModel` with `Field(...)` constraints; the tool takes a single `params` argument and **returns a formatted `str`** (markdown-ish text), never raw JSON.

### Key conventions

- **Tools return human-readable strings, not JSON.** Shared `_format_*` helpers (`_format_card`, `_format_card_list`, `_format_cardlist`, `_format_combo`) build the output. Reuse them for consistency.
- **Field validation lives in the Pydantic models** (`min_length`, `max_length`, `ge`/`le`, `Enum` types like `ScryfallSearchOrder`, `TopPeriod`, `TopColor`). Keep validation there rather than inside tool bodies.
- **`format_archidekt` is privileged.** The server's `instructions=` (in the `FastMCP(...)` constructor) direct clients to always use `format_archidekt` for decklist output and to prefer these tools over web search. If you add a data source, add a matching line to `instructions=` so clients know to prefer it.
- **Caching is manual and rare.** Only the MTGJSON precon deck list is cached, via the module-global `_deck_list_cache` dict with a 24h TTL. There is no general cache layer.
- **Archidekt private decks** use optional `ARCHIDEKT_USERNAME`/`ARCHIDEKT_PASSWORD` env vars (see `.env.example`); self-host only, never deploy credentials on a shared server.

### Adding a new tool

1. Add base-URL constant if introducing a new source; add `_<source>_get`/`_error` helpers.
2. Define a `BaseModel` input with validated `Field`s.
3. Write `@mcp.tool(name="...")` returning a formatted string; wrap upstream calls in try/except that returns `_<source>_error(e)`.
4. If it's a new data source, extend the README table and the `instructions=` block.
5. Add the tool name to the Mystic Forge landing page in the parent repo (`landing/mtg/index.html`). The release gate compares that page against `@mcp.tool(name=...)` and fails the release if they diverge.

## Notes

- The `README.md` tool tables are not exhaustive. `server.py` is the source of truth for the current tool set; `landing/mtg/index.html` in the parent repo is the source of truth for what is advertised, and CI keeps the two in sync.
- Requires Python 3.14 (per Dockerfile) but only uses stdlib + `httpx`, `pydantic`, and `mcp[cli]`.
- The Dockerfile copies an **explicit file list**, not `COPY . .`. A new top-level file or data asset must be added to that line or it silently will not exist in the image.
