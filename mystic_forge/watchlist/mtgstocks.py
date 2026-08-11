"""MTGStocks deep links for watched cards.

MTGStocks has no name-addressable URL. `/search?query=<name>` is not a route —
it 404s — and the only card page is `/prints/<print_id>-<slug>`, where
`print_id` names one specific printing (Black Market Connections is 141924).
Nothing we already ingest carries that id: MTGJSON's Identifiers model has
fields for TCGplayer, Card Kingdom, Cardmarket, Cardsphere, SCG and more, but
none for MTGStocks. So the id can only come from MTGStocks' own JSON API, and
the only way to make a name and a print id "work together" is to look the name
up once and cache the id.

That shapes the design:

* Resolution is a **background** job (nightly ingest / post-add backfill),
  never the page path. Pages read the cache and nothing else.
* Misses are cached too, with a retry window, so an unknown name isn't
  re-queried on every cycle.
* Everything fails closed: if MTGStocks is unreachable the badge is simply
  absent. We never emit a link we know 404s.

The API is undocumented and unversioned, so the parsers below key off shape
(any object carrying an int `id` plus a `name`) rather than an exact schema.
Two details about *reaching* it are not guessable, and getting either wrong
403s every call rather than failing visibly:

* The query is a **path segment**: `/search/autocomplete/Bitterblossom`.
  `?query=` is not a route on their API gateway, which answers
  `403 {"error":"Missing Authentication Token"}` to anything it can't match.
* Their CloudFront WAF refuses a bare `MysticForge/<version>` User-Agent on
  every path, including ones that serve a browser happily. It accepts the
  `Mozilla/5.0 (compatible; …)` well-behaved-bot form, so that is what we
  send — honest about who we are, shaped the way the filter expects.

Verify the endpoint from a network that can reach MTGStocks with:

    python -m mystic_forge.watchlist.mtgstocks "Black Market Connections"

The `slow`-marked tests in tests/test_watchlist_mtgstocks.py check both of
the above against the live API; everything else there runs on fixtures.
"""

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger("mystic_forge.mtgstocks")

SITE = "https://www.mtgstocks.com"
API = "https://api.mtgstocks.com"
CONTACT = "https://mcp.kautiontape.com/mtg"
TIMEOUT = 10.0
RETRY_DAYS = 7        # how long a "not found" answer is trusted
MAX_PER_RUN = 60      # cap the calls one ingest cycle may make
GIVE_UP_AFTER = 3     # consecutive transport failures ends the run
PAUSE = 0.3           # seconds between calls — be a polite guest


def _version() -> str:
    try:
        return (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
    except OSError:
        return "0"


def _headers() -> dict:
    """The `Mozilla/5.0 (compatible; …)` prefix is load-bearing: without it
    CloudFront answers 403 on every path (see the module docstring)."""
    return {"User-Agent": (f"Mozilla/5.0 (compatible; MysticForge/{_version()};"
                           f" +{CONTACT})"),
            "Accept": "application/json"}


def disabled() -> bool:
    return bool(os.environ.get("MYSTIC_FORGE_NO_MTGSTOCKS")
                or os.environ.get("MYSTIC_FORGE_NO_INGEST"))


# ── URL shapes ──────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    out: list[str] = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def print_url(print_id, slug: str | None = None) -> str:
    """`/prints/141924-black-market-connections`. The slug is cosmetic — the
    numeric prefix is what MTGStocks routes on — but we keep it when known so
    the link reads like one a human would paste. Their own slugs already lead
    with the id (`97426-bitterblossom`), so drop that before joining or the
    number lands in the URL twice."""
    tail = ""
    if slug:
        head = f"{print_id}-"
        tail = "-" + (slug[len(head):] if slug.startswith(head) else slug)
    return f"{SITE}/prints/{print_id}{tail}"


def front_face(name: str) -> str:
    """MTGStocks indexes double-faced cards under the front face alone."""
    return name.split(" // ")[0].strip()


# ── shape-tolerant payload readers ──────────────────────────────────────────

def _walk(payload):
    """Every dict anywhere in a decoded JSON payload."""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _named(payload):
    for node in _walk(payload):
        if isinstance(node.get("id"), int) and isinstance(node.get("name"), str):
            yield node


def _pick(payload, name: str):
    """Best card-ish object for `name`: exact match wins, prefix match settles."""
    wants = [name.strip().lower(), front_face(name).lower()]
    partial = None
    for node in _named(payload):
        got = node["name"].strip().lower()
        if got in wants:
            return node
        if partial is None and any(got.startswith(w) for w in wants):
            partial = node
    return partial


def _set_code_of(node) -> str | None:
    """MTGStocks calls a set code an `abbreviation`, which matches the set
    code a watchlist entry pins — no MTGJSON name lookup needed."""
    for key in ("abbreviation", "set_code", "setCode"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    card_set = node.get("card_set")
    if isinstance(card_set, dict):
        val = card_set.get("abbreviation")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _slug_of(node) -> str | None:
    slug = node.get("slug")
    return slug if isinstance(slug, str) else None


def _set_name_of(node) -> str | None:
    for key in ("set_name", "setName", "set"):
        val = node.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, dict) and isinstance(val.get("name"), str):
            return val["name"]
    if isinstance(node.get("card_set"), dict):
        name = node["card_set"].get("name")
        if isinstance(name, str):
            return name
    return None


def _get(client, path: str, params=None):
    resp = client.get(f"{API}{path}", params=params, headers=_headers(),
                      timeout=TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


# ── API calls ───────────────────────────────────────────────────────────────

def resolve_name(client, name: str):
    """(print_id, slug) for a card name, or None if MTGStocks doesn't know it.

    The name is a path segment, not a `query` parameter — see the module
    docstring; the parameter form is not a route and 403s every time."""
    payload = _get(client, "/search/autocomplete/"
                   + urllib.parse.quote(front_face(name), safe=""))
    hit = _pick(payload, name)
    if hit is None:
        return None
    return hit["id"], _slug_of(hit) or slugify(hit["name"])


def printings(client, print_id):
    """[(print_id, slug, set_key, foil)] for every printing on a print's page.

    `set_key` is the set's code where MTGStocks gives one and its name
    otherwise, so `refresh` can match a pinned printing on either. Their
    payload lists siblings under `sets`; an unrecognised one falls back to a
    walk, yields nothing, and the name-level link stands."""
    payload = _get(client, f"/prints/{print_id}")
    nodes = payload.get("sets") if isinstance(payload, dict) else None
    if not isinstance(nodes, list):
        nodes = list(_walk(payload))
    elif isinstance(payload, dict):
        nodes = [payload, *nodes]          # the printing being viewed counts
    out, seen = [], set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        pid = node.get("id")
        key = _set_code_of(node) or _set_name_of(node)
        if not isinstance(pid, int) or not key or pid in seen:
            continue
        seen.add(pid)
        out.append((pid, _slug_of(node), key, bool(node.get("foil"))))
    return out


# ── cache ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def remember(db, card_name: str, set_code: str | None, print_id, slug,
             commit: bool = True, source: str = "ingest") -> None:
    """Store a resolution. `print_id=None` is a cached miss, not an error.

    `source='trusted'` marks an answer votes may never overwrite."""
    db.execute(
        "INSERT OR REPLACE INTO mtgstocks_prints"
        " (card_name, set_code, print_id, slug, checked_at, source)"
        " VALUES (?,?,?,?,?,?)",
        (card_name.strip().lower(), (set_code or "").strip().upper(),
         print_id, slug, _now(), source))
    if commit:
        db.commit()


def looks_like(card_name: str, slug: str | None) -> bool:
    """Does this slug plausibly belong to this card?

    MTGStocks slugs are `<id>-<slugified name>`, so a submission can be
    checked against the card it claims to be without trusting whoever sent
    it. This is what makes an open vote endpoint safe: the worst a bad
    submission achieves is a different printing of the *right* card, because
    anything naming another card fails here. Double-faced cards are indexed
    under the front face, sometimes with the back appended, so both pass."""
    if not slug:
        return False
    tail = slug.split("-", 1)[1] if slug.split("-", 1)[0].isdigit() else slug
    full, front = slugify(card_name), slugify(front_face(card_name))
    return tail in (full, front) or tail.startswith(f"{front}-")


def cached_url(db, card_name: str, set_code: str | None = None) -> str | None:
    """Deep link for a card from cache only — never a network call.

    Falls back from the pinned printing to the card's default printing, then
    to nothing. Returning None means "render no MTGStocks badge"; there is no
    generic URL to fall back to."""
    if db is None:
        return None
    keys = []
    if set_code:
        keys.append((card_name.strip().lower(), set_code.strip().upper()))
    keys.append((card_name.strip().lower(), ""))
    try:
        for cn, sc in keys:
            row = db.execute(
                "SELECT print_id, slug FROM mtgstocks_prints"
                " WHERE card_name=? AND set_code=?", (cn, sc)).fetchone()
            if row and row["print_id"]:
                return print_url(row["print_id"], row["slug"])
    except sqlite3.Error:        # pre-migration database — badge, not a 500
        return None
    return None


def voter_id(db, address: str) -> str:
    """A stable, opaque id for one voter.

    The address never lands in the database: it is hashed with a per-install
    salt, so the votes table cannot be read back as a record of who looked at
    which card. Losing the salt only costs the current tallies."""
    row = db.execute("SELECT value FROM meta WHERE key='vote_salt'").fetchone()
    salt = row["value"] if row else None
    if not salt:
        salt = secrets.token_hex(16)
        db.execute("INSERT OR REPLACE INTO meta (key, value)"
                   " VALUES ('vote_salt', ?)", (salt,))
        db.commit()
    return hashlib.sha256(f"{salt}:{address}".encode()).hexdigest()[:32]


def record_vote(db, card_name: str, set_code: str | None, print_id, slug,
                voter: str) -> bool:
    """Record one viewer's answer and re-decide the badge. False if refused.

    One vote per voter per printing, so a voter who changes their mind
    replaces their own ballot instead of stacking another. The winner is
    simply the print id with the most votes; a tie leaves the standing answer
    alone, so one dissenter never flips a settled card and an uncontested
    first vote settles it immediately. There is no quorum: one beats none."""
    try:
        print_id = int(print_id)
    except (TypeError, ValueError):
        return False
    if print_id <= 0 or not looks_like(card_name, slug):
        return False
    name, code = card_name.strip().lower(), (set_code or "").strip().upper()
    db.execute(
        "INSERT OR REPLACE INTO mtgstocks_votes"
        " (card_name, set_code, voter, print_id, slug, created_at)"
        " VALUES (?,?,?,?,?,?)", (name, code, voter, print_id, slug, _now()))
    row = db.execute(
        "SELECT print_id, source FROM mtgstocks_prints"
        " WHERE card_name=? AND set_code=?", (name, code)).fetchone()
    if row and row["source"] == "trusted":
        db.commit()                       # the vote is kept, the badge is not
        return True
    standing = row["print_id"] if row else None
    winner = db.execute(
        """SELECT print_id, slug, COUNT(*) AS votes FROM mtgstocks_votes
           WHERE card_name=? AND set_code=?
           GROUP BY print_id ORDER BY votes DESC, MIN(created_at) LIMIT 1""",
        (name, code)).fetchone()
    if winner and winner["print_id"] != standing:
        beaten = db.execute(
            "SELECT COUNT(*) FROM mtgstocks_votes WHERE card_name=?"
            " AND set_code=? AND print_id=?", (name, code, standing)).fetchone()[0]
        if winner["votes"] > beaten:
            remember(db, name, code, winner["print_id"], winner["slug"],
                     commit=False, source="vote")
    db.commit()
    return True


def _stale_before() -> str:
    return (datetime.now(timezone.utc)
            - timedelta(days=RETRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pending_names(db, limit: int):
    """Watched card names with no usable name-level answer yet."""
    return [r["card_name"] for r in db.execute(
        """SELECT MIN(wc.card_name) AS card_name FROM watchlist_current wc
           WHERE NOT EXISTS (
             SELECT 1 FROM mtgstocks_prints m
             WHERE m.card_name = LOWER(wc.card_name) AND m.set_code = ''
               AND (m.print_id IS NOT NULL OR m.checked_at > ?))
           GROUP BY LOWER(wc.card_name) LIMIT ?""", (_stale_before(), limit))]


def _pending_printings(db, limit: int):
    """Pinned (name, set_code) pairs with no printing-level answer yet."""
    return [(r["card_name"], r["set_code"]) for r in db.execute(
        """SELECT MIN(wc.card_name) AS card_name, UPPER(wc.set_code) AS set_code
           FROM watchlist_current wc
           WHERE wc.set_code IS NOT NULL AND wc.set_code <> ''
             AND NOT EXISTS (
               SELECT 1 FROM mtgstocks_prints m
               WHERE m.card_name = LOWER(wc.card_name)
                 AND m.set_code = UPPER(wc.set_code)
                 AND (m.print_id IS NOT NULL OR m.checked_at > ?))
           GROUP BY LOWER(wc.card_name), UPPER(wc.set_code) LIMIT ?""",
        (_stale_before(), limit))]


def _set_names(allprintings_path: str | None) -> dict:
    """MTGJSON set code → set name, for matching MTGStocks' set labels."""
    if not allprintings_path or not os.path.exists(allprintings_path):
        return {}
    try:
        ap = sqlite3.connect(allprintings_path)
        try:
            return {c.upper(): n for c, n in
                    ap.execute("SELECT code, name FROM sets")}
        finally:
            ap.close()
    except sqlite3.Error:
        return {}


def refresh(db, allprintings_path: str | None = None,
            limit: int = MAX_PER_RUN) -> int:
    """Fill the print-id cache for watched cards. Returns rows resolved.

    Safe to call on every cycle: it only touches names that have no fresh
    answer, and it stops early once MTGStocks starts refusing us (their API
    sits behind CloudFront and may simply not answer a server)."""
    if disabled():
        return 0
    names = _pending_names(db, limit)
    pinned = _pending_printings(db, limit)
    if not names and not pinned:
        return 0
    set_names = _set_names(allprintings_path)
    resolved, failures, refused = 0, 0, None
    with httpx.Client() as client:
        for name in names:
            if failures >= GIVE_UP_AFTER:
                break
            try:
                hit = resolve_name(client, name)
            except Exception as e:
                failures += 1
                refused = refused or e
                log.debug("mtgstocks lookup failed for %s: %s", name, e)
                continue
            failures = 0
            remember(db, name, "", hit[0] if hit else None,
                     hit[1] if hit else None)
            resolved += 1 if hit else 0
            time.sleep(PAUSE)

        for name, set_code in pinned:
            if failures >= GIVE_UP_AFTER:
                break
            base = cached_url(db, name)      # needs the name-level id first
            if not base:
                continue
            want_name = (set_names.get(set_code) or "").strip().lower()
            base_id = base.rsplit("/", 1)[1].split("-", 1)[0]
            try:
                others = printings(client, base_id)
            except Exception as e:
                failures += 1
                refused = refused or e
                log.debug("mtgstocks printings failed for %s: %s", name, e)
                continue
            failures = 0
            hits = [(pid, slug, foil) for pid, slug, key, foil in others
                    if key.strip().upper() == set_code
                    or (want_name and key.strip().lower() == want_name)]
            hits.sort(key=lambda h: h[2])    # an entry pins a set, not a finish
            match = hits[0] if hits else None
            remember(db, name, set_code,
                     match[0] if match else None, match[1] if match else None)
            resolved += 1 if match else 0
            time.sleep(PAUSE)
    if resolved:
        log.info("mtgstocks: resolved %d print id(s)", resolved)
    elif refused is not None:
        # Silence here is how a host whose whole ASN is blocked looked
        # identical to a cycle with nothing pending, across two releases.
        log.warning("mtgstocks: resolved nothing for %d pending name(s); "
                    "first refusal was %s. Viewers' browsers still vote, so "
                    "badges can fill in without this.",
                    len(names) + len(pinned), refused)
    return resolved


if __name__ == "__main__":                                # pragma: no cover
    import sys
    query = " ".join(sys.argv[1:]) or "Black Market Connections"
    with httpx.Client() as _c:
        raw = _get(_c, "/search/autocomplete/"
                   + urllib.parse.quote(front_face(query), safe=""))
        print(json.dumps(raw, indent=2)[:2000])
        found = _pick(raw, query)
        print("\npicked:", found)
        if found:
            print("url:", print_url(found["id"],
                                    found.get("slug") or slugify(found["name"])))
            print("\nprintings:")
            for row in printings(_c, found["id"])[:20]:
                print(" ", row)
