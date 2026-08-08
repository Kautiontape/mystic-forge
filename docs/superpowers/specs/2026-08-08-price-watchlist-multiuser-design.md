# Spec: Mystic Forge — Card Price History + Multi-List Watchlist

Status: approved design (2026-08-08). Supersedes the single-user draft of this spec.

## Problem

Upgrade shortlists sprawl across decks; there's no way to see "what am I planning
to buy, what does it cost today, and is it trending down" without manually checking
sites. MTGStocks has no API. Build it into Mystic Forge.

Mystic Forge is no longer single-user: several people connect to the same public
server (`kautiontape.com`) as a claude.ai custom connector. claude.ai sends **no
per-user identity** to a no-auth MCP server (verified against Anthropic connector
docs), and connectors support only "plain URL or full OAuth 2.1" — so identity
must come from something the server mints itself.

## Goals

- Per-person (actually per-list) private watchlists with optional target prices
- Identity via server-minted passphrase, usable two ways: embedded in a personal
  connector URL, or passed as a tool parameter in chat ("here's my passphrase,
  add this"). Claude memory can hold the passphrase between chats.
- Read-only share codes so friends can view (not edit) each other's lists
- Append-only history of every change, viewable on a small web page, with
  recovery by cloning any past revision to a new list
- Daily price history stored locally, backfilled ~90 days on add
- Query history + movement via MCP tools so Claude can answer "what should I buy
  this month" and chart trends in-chat

## Non-Goals

- Real OAuth identity (claude.ai supports it, but it requires DCR + PKCE +
  RFC 9728 metadata and the mcp 1.x SDK only ships token *verification*; the
  passphrase scheme can be layered under OAuth later without schema changes)
- Collection/inventory tracking (watchlist ≠ collection)
- Server-side charting or alert delivery in v1 (Claude renders charts at query
  time; alerts are P1)
- Buylist/sell prices (retail only in v1)
- Editable web UI (the side page is read-only; all mutation goes through MCP tools)

## Identity model: lists, not users

There is no user table and no auth system. The unit of identity is the
**watchlist**, named by a server-minted passphrase.

- **Passphrase format:** 3 words from the EFF large wordlist + a 2-digit number
  (e.g. `crimson-otter-codex-42`), ≈ 45 bits. Human-typeable because it doubles
  as a chat credential. Shown once at mint time; stored as SHA-256 hash.
- **Transport 1 — personal connector URL:** `https://kautiontape.com/mcp/<passphrase>`.
  ASGI middleware peels the passphrase off the path, resolves the list into
  request context, and forwards to the normal MCP app. Identity is automatic and
  invisible to Claude.
- **Transport 2 — tool parameter:** every watchlist tool takes an optional
  `passphrase`. Explicit parameter wins over URL identity. This makes a personal
  URL optional: a friend on the shared public URL can keep their passphrase in
  Claude memory.
- The bare `kautiontape.com` endpoint continues to serve all existing stateless
  tools unchanged.
- Multiple lists per person = mint more passphrases.
- **Share codes:** each list has one short read-only code (`SC-` + 6 random
  base32 chars, unique; random, not derived from the passphrase; rotatable
  independently). Grants view, history, and clone — never mutation.

Accepted trade-offs (friend-group threat model): credentials in URLs can land in
logs (MCP spec discourages this — mitigated by rotation via clone); anyone who
discovers the base URL can mint lists (light rate limit on `watchlist_create`,
e.g. 5/hour/IP); possession of a passphrase is full control of that list.

## History & recovery: append-only events, clone-only recovery

Every mutation appends an event; events are never updated or deleted. Current
state is a materialized fold of the event chain (kept for <1 s reads; replaying
events must reproduce it exactly).

- **Actions:** `create`, `add`, `remove`, `set_target`, `set_note`, `clone_init`
  (payload records source list + seq).
- **Recovery is clone-only** (owner's decision): `watchlist_clone` at any `seq`
  mints a **new** passphrase whose list is seeded from that revision. Lists are
  never rolled back in place.
- Cloning **your own** list marks the source `superseded_by = <new list>`:
  recovery. A superseded list still works, but mutating tools prepend a warning
  pointing at the successor (catches stale passphrases in Claude memory or old
  connector URLs).
- Cloning **via share code** is a fork: no supersession, lineage recorded in
  `cloned_from_*`.

## Data source: MTGJSON (unchanged from original draft)

- `AllPrices.json.gz` — rolling ~90 days of daily prices per printing. Used to
  **backfill when a card is added**.
- `AllPricesToday.json.gz` — today's prices only, much smaller. Used for the
  **nightly append**.
- Prices keyed by MTGJSON `uuid`, per provider (`tcgplayer`, `cardkingdom`,
  `cardmarket`), per finish (`normal`/`foil`), retail.
- Name→uuid mapping: `AllPrintings.sqlite` (published directly by MTGJSON —
  attach it, join on `cards.name`, keep `scryfallId` for cross-ref with existing
  Scryfall tools).
- Files are large; stream-parse (`ijson`) the .gz — never load whole JSON into
  memory. Cache downloads; ETag/If-Modified-Since.

## Storage (SQLite, WAL mode)

```sql
CREATE TABLE lists (
  id INTEGER PRIMARY KEY,
  passphrase_hash TEXT NOT NULL UNIQUE,   -- sha256 of the passphrase
  share_code TEXT NOT NULL UNIQUE,
  label TEXT,
  created_at TEXT NOT NULL,
  cloned_from_list INTEGER,               -- fork/recovery lineage
  cloned_from_seq INTEGER,
  superseded_by INTEGER                   -- set when owner clones for recovery
);
CREATE TABLE events (
  list_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,                   -- 1..n per list, append-only
  ts TEXT NOT NULL,
  action TEXT NOT NULL,                   -- create/add/remove/set_target/set_note/clone_init
  payload_json TEXT NOT NULL,
  PRIMARY KEY (list_id, seq)
);
CREATE TABLE watchlist_current (          -- materialized fold of events
  list_id INTEGER NOT NULL,
  entry_id INTEGER NOT NULL,              -- seq of the add event that created it
  card_name TEXT NOT NULL,
  uuid TEXT,                              -- NULL = track cheapest printing across sets
  target_price REAL,
  note TEXT,
  added_at TEXT NOT NULL,
  PRIMARY KEY (list_id, entry_id)
);
CREATE TABLE prices (                     -- GLOBAL: shared by all lists
  uuid TEXT NOT NULL,
  date TEXT NOT NULL,                     -- YYYY-MM-DD
  provider TEXT NOT NULL,
  finish TEXT NOT NULL,
  price REAL NOT NULL,
  PRIMARY KEY (uuid, date, provider, finish)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);  -- last_ingest, file etags
```

Ingest the **union of watched uuids across all lists** (plus all printings of
name-tracked cards). Ten friends watching the same card ingest it once. Adding a
card later still gets ~90 days of history from AllPrices. DB stays tiny.

## MCP tools

Identity-scoped tools resolve the list from (`passphrase` param) → (URL path) →
error asking the user to create or supply one.

| Tool | Input | Output |
|---|---|---|
| `watchlist_create` | label? | passphrase (shown once), personal connector URL, share code |
| `watchlist_add` | name, printing? (set+cn), target_price?, note?, passphrase? | resolved uuid(s), current price, backfill status; appends `add` event |
| `watchlist_remove` | name or entry_id, passphrase? | confirmation; appends `remove` event |
| `watchlist_list` | passphrase? | per card: current price (cheapest normal-finish, tcgplayer), Δ7d, Δ30d, target, note; sorted by Δ30d |
| `watchlist_report` | passphrase? | movers: biggest 7d drops/rises, anything at/below target |
| `watchlist_view` | share_code | read-only `watchlist_list` + movers for a shared list |
| `watchlist_history` | passphrase? or share_code, limit? | event chain: seq, ts, action, summary |
| `watchlist_clone` | passphrase? or share_code, at_seq? (default latest) | new passphrase + URL + share code; own-passphrase clone sets `superseded_by` on source |
| `price_history` | name, days=90, provider?=tcgplayer | date/price series (JSON) for cheapest printing (global data, no identity) |

## HTTP side pages (read-only)

Served via FastMCP `custom_route` alongside the MCP mount:

- `GET /w/<passphrase>` — full view: label, current list with prices, complete
  event chain (seq, timestamp, action, detail), lineage/supersession notices.
- `GET /s/<share_code>` — same page, read-only framing, no passphrase shown.
- Plain server-rendered HTML, no JS required, no forms. Cloning instructions on
  the page point at the MCP tool.

## Ingest job

In-process asyncio daily task (the server is long-running; the container has no
systemd), guarded by `meta.last_ingest`:

1. If any current watchlist entries lack backfill → download AllPrices, extract
   their uuids' 90-day history
2. Download AllPricesToday, upsert today's rows for all watched uuids
3. Record `last_ingest` in meta

Idempotent (PK upsert). Log to stdout; docker captures it.

## Deployment changes

- `docker-compose.yml`: add a named volume mounted at `/data` for the SQLite DB
  (currently state would be wiped on rebuild); DB path via `MYSTIC_FORGE_DB`
  env (default `/data/mystic_forge.db`).
- Middleware mounts: `/mcp/<passphrase>` → MCP app with list context; `/mcp` →
  MCP app without identity (public); `/w/…`, `/s/…` → side pages.

## Acceptance criteria

- [ ] Two lists cannot see or affect each other's cards through any tool
- [ ] Adding "Buster Sword" with no printing tracks the cheapest printing and
      shows ~90 days of history within one ingest cycle
- [ ] `watchlist_list` returns in <1 s (all reads local)
- [ ] Replaying a list's events reproduces `watchlist_current` exactly
- [ ] `watchlist_clone(at_seq=N)` yields a list equal to the source as of seq N
- [ ] A share code can view and clone but never mutate
- [ ] Mutating a superseded list returns a warning naming its successor
- [ ] Nightly job is idempotent — re-running same day changes nothing
- [ ] A card with target_price 30 appears in `watchlist_report` when its
      cheapest printing ≤ $30
- [ ] Ingest never loads a full MTGJSON file into memory (large-file test /
      memory cap)
- [ ] Passphrase is accepted both in the URL path and as a tool parameter, with
      the parameter taking precedence

## P1 (fast follow, not v1)

- ntfy or Fastmail push when a target price is hit (fire from the ingest job)
- `watchlist_import(deck_url, label)` — pull every `To Buy`-labeled card from an
  Archidekt deck into a list, deck name as note
- Share-code rotation tool (`watchlist_rotate_share`)

## Open questions (non-blocking)

- Foil tracking: ignore in v1 or store both finishes? (Storing both is free;
  display cheapest normal.)
- Retention: prune prices > 2 years? (Punt; DB will be MBs.)
- Rate limit numbers for `watchlist_create` (start 5/hour/IP; tune if abused)
