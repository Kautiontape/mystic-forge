# Multi-List Price Watchlist Implementation Plan

> **Status: complete.** All tasks executed on branch `price_history`; the
> shipped system went beyond this plan (see the spec's "Shipped beyond this
> spec" section). Steps are checked off for the record.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Passphrase-named MTG price watchlists with event-sourced history, share codes, clone-only recovery, MTGJSON price ingest, health checks, and deploy wiring — per `docs/superpowers/specs/2026-08-08-price-watchlist-multiuser-design.md`.

**Architecture:** Two new pure-Python modules (`watchlist_db.py` for SQLite/event-sourcing, `watchlist_ingest.py` for MTGJSON streaming ingest) plus additions to `server.py` (MCP tools, ASGI passphrase middleware, health/side-page routes, uvicorn entrypoint). Parent `mcp-servers` repo gets compose volume/healthcheck + Caddy routes.

**Tech Stack:** Python 3.14, mcp SDK 1.29 (FastMCP, streamable HTTP), sqlite3 (WAL), httpx, ijson (new dep), pytest with `asyncio_mode = auto`, starlette TestClient.

**Conventions in this repo:** tools are `@mcp.tool(name=...)` async functions taking a single pydantic `params` model and returning a markdown string; tests live in `tests/`, import `server` directly, and call helpers/tools as plain functions (`conftest.py` puts repo root on `sys.path`). Commit style is `topic: Message` with NO Co-Authored-By lines. Use topic `watchlist:` for this feature, `deploy:` for the parent repo.

**Schema deltas vs the spec SQL:** `watchlist_current` gains `set_code`/`collector_number` (the user's pinned printing, event-sourced) and `uuid` stays a derived cache (resolved from MTGJSON at ingest; excluded from the replay invariant). A `card_uuids` table caches name→MTGJSON-uuid resolution. Both are implementation caches, consistent with the spec's intent.

**Two repos are touched:**
- This worktree (`/home/shawn/.herdr/worktrees/mystic-forge/price-history`, branch `price_history`) — all Python work.
- Parent repo `/home/shawn/documents/apps/kautiontape/mcp-servers` (separate git repo; this project is its submodule) — Task 11 only. Create branch `watchlist-deploy` there; NEVER push either repo.

---

### Task 1: Wordlist + DB core (connect, schema, minting, list creation)

**Files:**
- Create: `watchlist_words.txt` (EFF short wordlist, 1296 words)
- Create: `watchlist_db.py`
- Test: `tests/test_watchlist_db.py`
- Modify: `conftest.py` (shared db fixture + ingest guard)

- [x] **Step 1: Fetch and commit the wordlist**

```bash
cd /home/shawn/.herdr/worktrees/mystic-forge/price-history
curl -s https://www.eff.org/files/2016/09/08/eff_short_wordlist_1.txt | awk '{print $2}' > watchlist_words.txt
wc -l watchlist_words.txt   # expect 1296
git add watchlist_words.txt && git commit -m "watchlist: Add EFF short wordlist for passphrase minting"
```

- [x] **Step 2: Add shared fixtures to `conftest.py`**

Append to the existing `conftest.py` (keep the current sys.path lines):

```python
import os

os.environ["MYSTIC_FORGE_NO_INGEST"] = "1"  # never start the ingest loop in tests

import pytest


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Fresh initialized SQLite DB; module default path patched to it."""
    import watchlist_db
    p = str(tmp_path / "test.db")
    monkeypatch.setattr(watchlist_db, "DB_PATH", p)
    db = watchlist_db.connect(p)
    watchlist_db.init_db(db)
    db.close()
    return p


@pytest.fixture
def db(db_path):
    import watchlist_db
    conn = watchlist_db.connect(db_path)
    yield conn
    conn.close()
```

- [x] **Step 3: Write failing tests**

Create `tests/test_watchlist_db.py`:

```python
import re
import sqlite3

import watchlist_db


def test_init_db_creates_tables(db):
    names = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"lists", "events", "watchlist_current", "prices", "meta",
            "card_uuids"} <= names


def test_mint_passphrase_format():
    pp = watchlist_db.mint_passphrase()
    parts = pp.split("-")
    assert len(parts) == 5                     # 4 words + 2-digit number
    assert re.fullmatch(r"\d{2}", parts[-1])
    assert all(re.fullmatch(r"[a-z]+", w) for w in parts[:4])


def test_mint_share_code_format():
    sc = watchlist_db.mint_share_code()
    assert re.fullmatch(r"SC-[A-Z2-9]{6}", sc)


def test_create_list_roundtrip(db):
    list_id, pp, sc = watchlist_db.create_list(db, label="my deck")
    row = watchlist_db.get_list_by_passphrase(db, pp)
    assert row["id"] == list_id
    assert row["label"] == "my deck"
    assert row["share_code"] == sc
    assert watchlist_db.get_list_by_share(db, sc)["id"] == list_id
    # passphrase never stored in the clear
    raw = db.execute("SELECT passphrase_hash FROM lists").fetchone()[0]
    assert pp not in raw and len(raw) == 64


def test_wrong_passphrase_returns_none(db):
    watchlist_db.create_list(db)
    assert watchlist_db.get_list_by_passphrase(db, "nope-nope-nope-nope-00") is None


def test_create_records_create_event(db):
    list_id, _, _ = watchlist_db.create_list(db, label="x")
    ev = db.execute("SELECT * FROM events WHERE list_id=?", (list_id,)).fetchone()
    assert ev["seq"] == 1 and ev["action"] == "create"
```

- [x] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_watchlist_db.py -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'watchlist_db'`

- [x] **Step 5: Implement `watchlist_db.py`**

```python
"""SQLite storage for the multi-list price watchlist (spec 2026-08-08).

Event-sourced: every mutation appends to `events`; `watchlist_current` is a
materialized fold kept in the same transaction. `prices` is global data shared
by all lists. No user table — a list IS the identity, named by a passphrase
stored only as a SHA-256 hash.
"""

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("MYSTIC_FORGE_DB", "mystic_forge.db")
_WORDS_FILE = Path(__file__).parent / "watchlist_words.txt"
_SHARE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O/1/I/L/U confusables

SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
  id INTEGER PRIMARY KEY,
  passphrase_hash TEXT NOT NULL UNIQUE,
  share_code TEXT NOT NULL UNIQUE,
  label TEXT,
  created_at TEXT NOT NULL,
  cloned_from_list INTEGER,
  cloned_from_seq INTEGER,
  superseded_by INTEGER
);
CREATE TABLE IF NOT EXISTS events (
  list_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  action TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (list_id, seq)
);
CREATE TABLE IF NOT EXISTS watchlist_current (
  list_id INTEGER NOT NULL,
  entry_id INTEGER NOT NULL,
  card_name TEXT NOT NULL,
  set_code TEXT,
  collector_number TEXT,
  uuid TEXT,
  target_price REAL,
  note TEXT,
  added_at TEXT NOT NULL,
  PRIMARY KEY (list_id, entry_id)
);
CREATE TABLE IF NOT EXISTS prices (
  uuid TEXT NOT NULL,
  date TEXT NOT NULL,
  provider TEXT NOT NULL,
  finish TEXT NOT NULL,
  price REAL NOT NULL,
  PRIMARY KEY (uuid, date, provider, finish)
);
CREATE TABLE IF NOT EXISTS card_uuids (
  card_name TEXT NOT NULL,
  uuid TEXT NOT NULL,
  set_code TEXT,
  collector_number TEXT,
  scryfall_id TEXT,
  PRIMARY KEY (card_name, uuid)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _words() -> list[str]:
    return _WORDS_FILE.read_text().split()


def mint_passphrase() -> str:
    words = _words()
    picks = [secrets.choice(words) for _ in range(4)]
    return "-".join(picks) + f"-{secrets.randbelow(100):02d}"


def mint_share_code() -> str:
    return "SC-" + "".join(secrets.choice(_SHARE_ALPHABET) for _ in range(6))


def hash_passphrase(passphrase: str) -> str:
    return hashlib.sha256(passphrase.strip().lower().encode()).hexdigest()


def get_list_by_passphrase(db, passphrase: str):
    return db.execute("SELECT * FROM lists WHERE passphrase_hash=?",
                      (hash_passphrase(passphrase),)).fetchone()


def get_list_by_share(db, share_code: str):
    return db.execute("SELECT * FROM lists WHERE share_code=?",
                      (share_code.strip().upper(),)).fetchone()


def get_list(db, list_id: int):
    return db.execute("SELECT * FROM lists WHERE id=?", (list_id,)).fetchone()


def create_list(db, label: str | None = None,
                cloned_from_list: int | None = None,
                cloned_from_seq: int | None = None) -> tuple[int, str, str]:
    """Returns (list_id, passphrase, share_code). Passphrase shown once, here."""
    passphrase = mint_passphrase()
    while True:  # share codes are short; retry the rare collision
        share_code = mint_share_code()
        try:
            cur = db.execute(
                "INSERT INTO lists (passphrase_hash, share_code, label, created_at,"
                " cloned_from_list, cloned_from_seq) VALUES (?,?,?,?,?,?)",
                (hash_passphrase(passphrase), share_code, label, _now(),
                 cloned_from_list, cloned_from_seq))
            break
        except sqlite3.IntegrityError:
            continue
    list_id = cur.lastrowid
    append_event(db, list_id, "create", {"label": label})
    return list_id, passphrase, share_code
```

(`append_event` arrives in Task 2 — add a temporary stub so Task 1 tests pass:)

```python
def append_event(db, list_id: int, action: str, payload: dict) -> int:
    row = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE list_id=?",
                     (list_id,)).fetchone()
    seq = row[0]
    db.execute("INSERT INTO events (list_id, seq, ts, action, payload_json)"
               " VALUES (?,?,?,?,?)",
               (list_id, seq, _now(), action, json.dumps(payload)))
    db.commit()
    return seq
```

- [x] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_watchlist_db.py -v`
Expected: all PASS. Also run `pytest` (whole suite) to confirm the conftest change breaks nothing.

- [x] **Step 7: Commit**

```bash
git add watchlist_db.py tests/test_watchlist_db.py conftest.py
git commit -m "watchlist: Add DB core with passphrase-named lists"
```

---

### Task 2: Events, materialized fold, add/remove/update, replay invariant

**Files:**
- Modify: `watchlist_db.py`
- Test: `tests/test_watchlist_db.py`

- [x] **Step 1: Write failing tests** (append to `tests/test_watchlist_db.py`)

```python
def test_add_card_materializes_current(db):
    list_id, _, _ = watchlist_db.create_list(db)
    seq, entry = watchlist_db.add_card(db, list_id, "Sol Ring",
                                       target_price=1.5, note="Cloud deck")
    row = db.execute("SELECT * FROM watchlist_current WHERE list_id=?",
                     (list_id,)).fetchone()
    assert row["entry_id"] == seq == entry["entry_id"]
    assert row["card_name"] == "Sol Ring"
    assert row["target_price"] == 1.5
    assert row["note"] == "Cloud deck"


def test_add_same_name_twice_updates_instead_of_duplicating(db):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=2.0)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=1.0, note="hi")
    rows = db.execute("SELECT * FROM watchlist_current WHERE list_id=?",
                      (list_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["target_price"] == 1.0 and rows[0]["note"] == "hi"
    actions = [r["action"] for r in db.execute(
        "SELECT action FROM events WHERE list_id=? ORDER BY seq", (list_id,))]
    assert actions == ["create", "add", "set_target", "set_note"]


def test_two_printings_of_same_card_coexist(db):
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", set_code="C21",
                          collector_number="263")
    watchlist_db.add_card(db, list_id, "Sol Ring", set_code="LTC",
                          collector_number="284")
    rows = db.execute("SELECT * FROM watchlist_current WHERE list_id=?",
                      (list_id,)).fetchall()
    assert len(rows) == 2


def test_remove_by_name_and_by_entry_id(db):
    list_id, _, _ = watchlist_db.create_list(db)
    seq1, _ = watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_db.add_card(db, list_id, "Cultivate")
    removed = watchlist_db.remove_entry(db, list_id, entry_id=seq1)
    assert removed["card_name"] == "Sol Ring"
    removed = watchlist_db.remove_entry(db, list_id, name="cultivate")
    assert removed["card_name"] == "Cultivate"
    assert db.execute("SELECT COUNT(*) FROM watchlist_current WHERE list_id=?",
                      (list_id,)).fetchone()[0] == 0


def test_remove_missing_raises(db):
    list_id, _, _ = watchlist_db.create_list(db)
    import pytest
    with pytest.raises(watchlist_db.NotFound):
        watchlist_db.remove_entry(db, list_id, name="Ghost Card")


def test_lists_are_isolated(db):
    a, _, _ = watchlist_db.create_list(db)
    b, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, a, "Sol Ring")
    assert watchlist_db.current_entries(db, b) == []
    import pytest
    with pytest.raises(watchlist_db.NotFound):
        watchlist_db.remove_entry(db, b, name="Sol Ring")


def test_replay_reproduces_current(db):
    """Spec acceptance: replaying events reproduces watchlist_current exactly."""
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=2.0)
    s2, _ = watchlist_db.add_card(db, list_id, "Cultivate", note="ramp")
    watchlist_db.add_card(db, list_id, "Sol Ring", target_price=1.0)
    watchlist_db.remove_entry(db, list_id, entry_id=s2)
    replayed = watchlist_db.replay_state(db, list_id)
    current = {r["entry_id"]: dict(r) for r in
               db.execute("SELECT * FROM watchlist_current WHERE list_id=?",
                          (list_id,))}
    assert set(replayed) == set(current)
    for eid, entry in replayed.items():
        for col in ("card_name", "set_code", "collector_number",
                    "target_price", "note", "added_at"):
            assert entry[col] == current[eid][col], f"{col} diverged"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_watchlist_db.py -v`
Expected: new tests FAIL with `AttributeError: ... 'add_card'`

- [x] **Step 3: Implement in `watchlist_db.py`**

Replace the Task-1 `append_event` (drop its internal `db.commit()`) and add:

```python
class NotFound(Exception):
    pass


def append_event(db, list_id: int, action: str, payload: dict) -> int:
    row = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE list_id=?",
                     (list_id,)).fetchone()
    seq = row[0]
    db.execute("INSERT INTO events (list_id, seq, ts, action, payload_json)"
               " VALUES (?,?,?,?,?)",
               (list_id, seq, _now(), action, json.dumps(payload)))
    return seq


def current_entries(db, list_id: int) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT * FROM watchlist_current WHERE list_id=? ORDER BY entry_id",
        (list_id,))]


def _find_entry(db, list_id: int, entry_id=None, name=None,
                set_code=None, collector_number=None):
    if entry_id is not None:
        return db.execute(
            "SELECT * FROM watchlist_current WHERE list_id=? AND entry_id=?",
            (list_id, entry_id)).fetchone()
    q = "SELECT * FROM watchlist_current WHERE list_id=? AND LOWER(card_name)=LOWER(?)"
    args = [list_id, name]
    if set_code:
        q += " AND LOWER(set_code)=LOWER(?)"
        args.append(set_code)
    if collector_number:
        q += " AND collector_number=?"
        args.append(collector_number)
    return db.execute(q, args).fetchone()


def add_card(db, list_id: int, card_name: str, set_code: str | None = None,
             collector_number: str | None = None, target_price: float | None = None,
             note: str | None = None) -> tuple[int, dict]:
    """Append add (or set_target/set_note for an existing entry) and materialize.

    Returns (last_seq, entry_dict)."""
    existing = _find_entry(db, list_id, name=card_name, set_code=set_code,
                           collector_number=collector_number)
    if existing:
        seq = existing["entry_id"]
        eid = existing["entry_id"]
        if target_price is not None and target_price != existing["target_price"]:
            seq = append_event(db, list_id, "set_target",
                              {"entry_id": eid, "target_price": target_price})
            db.execute("UPDATE watchlist_current SET target_price=?"
                       " WHERE list_id=? AND entry_id=?",
                       (target_price, list_id, eid))
        if note is not None and note != existing["note"]:
            seq = append_event(db, list_id, "set_note",
                              {"entry_id": eid, "note": note})
            db.execute("UPDATE watchlist_current SET note=?"
                       " WHERE list_id=? AND entry_id=?", (note, list_id, eid))
        db.commit()
        return seq, dict(_find_entry(db, list_id, entry_id=eid))

    added_at = _now()
    payload = {"card_name": card_name, "set_code": set_code,
               "collector_number": collector_number,
               "target_price": target_price, "note": note, "added_at": added_at}
    seq = append_event(db, list_id, "add", payload)
    db.execute(
        "INSERT INTO watchlist_current (list_id, entry_id, card_name, set_code,"
        " collector_number, target_price, note, added_at) VALUES (?,?,?,?,?,?,?,?)",
        (list_id, seq, card_name, set_code, collector_number,
         target_price, note, added_at))
    db.commit()
    return seq, dict(_find_entry(db, list_id, entry_id=seq))


def remove_entry(db, list_id: int, entry_id: int | None = None,
                 name: str | None = None) -> dict:
    row = _find_entry(db, list_id, entry_id=entry_id, name=name)
    if row is None:
        raise NotFound(f"No watchlist entry matching "
                       f"{'#' + str(entry_id) if entry_id else name!r}")
    append_event(db, list_id, "remove",
                 {"entry_id": row["entry_id"], "card_name": row["card_name"]})
    db.execute("DELETE FROM watchlist_current WHERE list_id=? AND entry_id=?",
               (list_id, row["entry_id"]))
    db.commit()
    return dict(row)


def state_at(db, list_id: int, seq: int | None = None) -> dict[int, dict]:
    """Pure fold of the event chain up to (and including) seq."""
    q = "SELECT * FROM events WHERE list_id=? ORDER BY seq"
    entries: dict[int, dict] = {}
    for ev in db.execute(q, (list_id,)):
        if seq is not None and ev["seq"] > seq:
            break
        payload = json.loads(ev["payload_json"])
        if ev["action"] == "add":
            entries[ev["seq"]] = {"entry_id": ev["seq"], **payload}
        elif ev["action"] == "remove":
            entries.pop(payload["entry_id"], None)
        elif ev["action"] == "set_target":
            entries[payload["entry_id"]]["target_price"] = payload["target_price"]
        elif ev["action"] == "set_note":
            entries[payload["entry_id"]]["note"] = payload["note"]
        # create / clone_init carry no state
    return entries


def replay_state(db, list_id: int) -> dict[int, dict]:
    return state_at(db, list_id, None)
```

Also update `create_list` to `db.commit()` after its `append_event` call (since the stub no longer commits):

```python
    list_id = cur.lastrowid
    append_event(db, list_id, "create", {"label": label})
    db.commit()
    return list_id, passphrase, share_code
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_watchlist_db.py -v` — Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add watchlist_db.py tests/test_watchlist_db.py
git commit -m "watchlist: Add event-sourced add/remove with replay invariant"
```

---

### Task 3: Clone at revision (fork + recovery/supersede)

**Files:**
- Modify: `watchlist_db.py`
- Test: `tests/test_watchlist_db.py`

- [x] **Step 1: Write failing tests** (append)

```python
def test_clone_at_seq_matches_source_state(db):
    src, _, _ = watchlist_db.create_list(db, label="orig")
    watchlist_db.add_card(db, src, "Sol Ring", target_price=2.0)
    s_cult, _ = watchlist_db.add_card(db, src, "Cultivate")
    at = db.execute("SELECT MAX(seq) FROM events WHERE list_id=?",
                    (src,)).fetchone()[0]
    watchlist_db.remove_entry(db, src, entry_id=s_cult)      # after `at`
    new_id, pp, sc = watchlist_db.clone_list(db, src, at_seq=at, recovery=False)
    names = sorted(e["card_name"] for e in watchlist_db.current_entries(db, new_id))
    assert names == ["Cultivate", "Sol Ring"]
    targets = {e["card_name"]: e["target_price"]
               for e in watchlist_db.current_entries(db, new_id)}
    assert targets["Sol Ring"] == 2.0
    row = watchlist_db.get_list(db, new_id)
    assert row["cloned_from_list"] == src and row["cloned_from_seq"] == at
    assert row["label"] == "orig"


def test_clone_defaults_to_latest(db):
    src, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, src, "Sol Ring")
    new_id, _, _ = watchlist_db.clone_list(db, src, at_seq=None, recovery=False)
    assert [e["card_name"] for e in watchlist_db.current_entries(db, new_id)] \
        == ["Sol Ring"]


def test_recovery_clone_supersedes_source_but_fork_does_not(db):
    src, _, _ = watchlist_db.create_list(db)
    fork_id, _, _ = watchlist_db.clone_list(db, src, recovery=False)
    assert watchlist_db.get_list(db, src)["superseded_by"] is None
    rec_id, _, _ = watchlist_db.clone_list(db, src, recovery=True)
    assert watchlist_db.get_list(db, src)["superseded_by"] == rec_id


def test_clone_history_starts_with_clone_init_then_adds(db):
    src, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, src, "Sol Ring")
    new_id, _, _ = watchlist_db.clone_list(db, src, recovery=False)
    actions = [r["action"] for r in db.execute(
        "SELECT action FROM events WHERE list_id=? ORDER BY seq", (new_id,))]
    assert actions == ["create", "clone_init", "add"]
    # replay invariant holds for clones too
    replayed = watchlist_db.replay_state(db, new_id)
    assert [e["card_name"] for e in replayed.values()] == ["Sol Ring"]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_watchlist_db.py -v` — Expected: FAIL with `AttributeError: ... 'clone_list'`

- [x] **Step 3: Implement `clone_list` in `watchlist_db.py`**

```python
def clone_list(db, source_list_id: int, at_seq: int | None = None,
               recovery: bool = False) -> tuple[int, str, str]:
    """Mint a new list seeded from source state as of at_seq (default: latest).

    recovery=True marks the source superseded by the new list (spec: cloning
    your OWN list is recovery; cloning via share code is a fork)."""
    source = get_list(db, source_list_id)
    if source is None:
        raise NotFound(f"No list #{source_list_id}")
    if at_seq is None:
        at_seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM events"
                            " WHERE list_id=?", (source_list_id,)).fetchone()[0]
    snapshot = state_at(db, source_list_id, at_seq)
    new_id, passphrase, share_code = create_list(
        db, label=source["label"],
        cloned_from_list=source_list_id, cloned_from_seq=at_seq)
    append_event(db, new_id, "clone_init",
                 {"source_list": source_list_id, "source_seq": at_seq,
                  "source_share_code": source["share_code"],
                  "recovery": recovery})
    db.commit()
    for entry in sorted(snapshot.values(), key=lambda e: e["entry_id"]):
        add_card(db, new_id, entry["card_name"],
                 set_code=entry.get("set_code"),
                 collector_number=entry.get("collector_number"),
                 target_price=entry.get("target_price"),
                 note=entry.get("note"))
    if recovery:
        db.execute("UPDATE lists SET superseded_by=? WHERE id=?",
                   (new_id, source_list_id))
        db.commit()
    return new_id, passphrase, share_code
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_watchlist_db.py -v` — Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add watchlist_db.py tests/test_watchlist_db.py
git commit -m "watchlist: Add clone-at-revision with recovery supersession"
```

---

### Task 4: Price queries — cheapest printing, Δ7d/Δ30d, series

**Files:**
- Modify: `watchlist_db.py`
- Test: `tests/test_watchlist_prices.py` (new)

- [x] **Step 1: Write failing tests**

Create `tests/test_watchlist_prices.py`:

```python
import watchlist_db


def seed(db):
    # uuid-a: cheap printing trending down; uuid-b: expensive printing
    rows = [
        ("uuid-a", "2026-07-09", 10.0), ("uuid-a", "2026-07-25", 9.0),
        ("uuid-a", "2026-08-01", 8.0), ("uuid-a", "2026-08-08", 7.0),
        ("uuid-b", "2026-08-08", 20.0),
    ]
    for uuid, date, price in rows:
        watchlist_db.upsert_price(db, uuid, date, "tcgplayer", "normal", price)
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-08", "tcgplayer", "foil", 30.0)
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-08", "cardkingdom", "normal", 1.0)


def test_upsert_price_idempotent(db):
    watchlist_db.upsert_price(db, "u", "2026-08-08", "tcgplayer", "normal", 5.0)
    watchlist_db.upsert_price(db, "u", "2026-08-08", "tcgplayer", "normal", 6.0)
    rows = db.execute("SELECT price FROM prices").fetchall()
    assert len(rows) == 1 and rows[0][0] == 6.0


def test_price_summary_picks_cheapest_printing_default_provider(db):
    seed(db)
    s = watchlist_db.price_summary(db, ["uuid-a", "uuid-b"],
                                   today="2026-08-08")
    assert s["uuid"] == "uuid-a"          # cheapest normal-finish tcgplayer
    assert s["current"] == 7.0
    assert s["d7"] == 7.0 - 8.0           # vs at-or-before 2026-08-01
    assert s["d30"] == 7.0 - 10.0         # vs at-or-before 2026-07-09
    assert s["date"] == "2026-08-08"


def test_price_summary_none_when_no_data(db):
    assert watchlist_db.price_summary(db, ["nope"], today="2026-08-08") is None
    assert watchlist_db.price_summary(db, [], today="2026-08-08") is None


def test_price_series_for_cheapest(db):
    seed(db)
    series = watchlist_db.price_series(db, ["uuid-a", "uuid-b"], days=90,
                                       today="2026-08-08")
    assert series["uuid"] == "uuid-a"
    assert series["points"][0] == ("2026-07-09", 10.0)
    assert series["points"][-1] == ("2026-08-08", 7.0)


def test_price_series_respects_days_window(db):
    seed(db)
    series = watchlist_db.price_series(db, ["uuid-a"], days=10,
                                       today="2026-08-08")
    assert all(d >= "2026-07-29" for d, _ in series["points"])
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_watchlist_prices.py -v` — Expected: FAIL with `AttributeError: ... 'upsert_price'`

- [x] **Step 3: Implement in `watchlist_db.py`**

```python
from datetime import date as _date, timedelta


def upsert_price(db, uuid: str, date: str, provider: str, finish: str,
                 price: float, commit: bool = True) -> None:
    db.execute("INSERT OR REPLACE INTO prices (uuid, date, provider, finish,"
               " price) VALUES (?,?,?,?,?)", (uuid, date, provider, finish, price))
    if commit:
        db.commit()


def _cheapest_latest(db, uuids, provider, finish):
    """(uuid, price, date) with the lowest most-recent price, else None."""
    if not uuids:
        return None
    marks = ",".join("?" * len(uuids))
    row = db.execute(
        f"""SELECT uuid, price, date FROM (
              SELECT uuid, price, date,
                     ROW_NUMBER() OVER (PARTITION BY uuid ORDER BY date DESC) rn
              FROM prices
              WHERE provider=? AND finish=? AND uuid IN ({marks}))
            WHERE rn=1 ORDER BY price ASC LIMIT 1""",
        [provider, finish, *uuids]).fetchone()
    return (row["uuid"], row["price"], row["date"]) if row else None


def _price_at_or_before(db, uuid, provider, finish, date):
    row = db.execute(
        "SELECT price FROM prices WHERE uuid=? AND provider=? AND finish=?"
        " AND date<=? ORDER BY date DESC LIMIT 1",
        (uuid, provider, finish, date)).fetchone()
    return row["price"] if row else None


def price_summary(db, uuids, provider: str = "tcgplayer",
                  finish: str = "normal", today: str | None = None):
    """Cheapest printing's current price + 7d/30d deltas, or None if no data."""
    best = _cheapest_latest(db, list(uuids), provider, finish)
    if best is None:
        return None
    uuid, current, date = best
    today = today or _date.today().isoformat()
    out = {"uuid": uuid, "current": current, "date": date, "d7": None, "d30": None}
    for key, days in (("d7", 7), ("d30", 30)):
        ref_date = (_date.fromisoformat(today) - timedelta(days=days)).isoformat()
        ref = _price_at_or_before(db, uuid, provider, finish, ref_date)
        if ref is not None:
            out[key] = round(current - ref, 2)
    return out


def price_series(db, uuids, days: int = 90, provider: str = "tcgplayer",
                 finish: str = "normal", today: str | None = None):
    best = _cheapest_latest(db, list(uuids), provider, finish)
    if best is None:
        return None
    uuid = best[0]
    today = today or _date.today().isoformat()
    start = (_date.fromisoformat(today) - timedelta(days=days)).isoformat()
    points = [(r["date"], r["price"]) for r in db.execute(
        "SELECT date, price FROM prices WHERE uuid=? AND provider=? AND"
        " finish=? AND date>=? ORDER BY date", (uuid, provider, finish, start))]
    return {"uuid": uuid, "provider": provider, "finish": finish,
            "points": points}


def uuids_for_entry(db, entry: dict) -> list[str]:
    """MTGJSON uuids an entry tracks: its pinned printing, else all printings."""
    if entry.get("uuid"):
        return [entry["uuid"]]
    q = "SELECT uuid FROM card_uuids WHERE LOWER(card_name)=LOWER(?)"
    args = [entry["card_name"]]
    if entry.get("set_code"):
        q += " AND LOWER(set_code)=LOWER(?)"
        args.append(entry["set_code"])
    if entry.get("collector_number"):
        q += " AND collector_number=?"
        args.append(entry["collector_number"])
    return [r["uuid"] for r in db.execute(q, args)]
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_watchlist_prices.py -v` — Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add watchlist_db.py tests/test_watchlist_prices.py
git commit -m "watchlist: Add cheapest-printing price summaries and series"
```

---

### Task 5: MTGJSON ingest — resolve uuids, stream backfill, daily append

**Files:**
- Create: `watchlist_ingest.py`
- Test: `tests/test_watchlist_ingest.py`
- Modify: `requirements.txt` (add `ijson`)

- [x] **Step 1: Add dependency**

Append to `requirements.txt`: `ijson>=3.2` — then `pip install ijson`.

- [x] **Step 2: Write failing tests**

Create `tests/test_watchlist_ingest.py`:

```python
import gzip
import json
import sqlite3
import tracemalloc

import watchlist_db
import watchlist_ingest


def make_allprintings(tmp_path):
    """Minimal AllPrintings.sqlite lookalike: cards(name,uuid,setCode,number,scryfallId)."""
    p = str(tmp_path / "AllPrintings.sqlite")
    ap = sqlite3.connect(p)
    ap.execute("CREATE TABLE cards (name TEXT, uuid TEXT, setCode TEXT,"
               " number TEXT, scryfallId TEXT)")
    ap.executemany("INSERT INTO cards VALUES (?,?,?,?,?)", [
        ("Sol Ring", "uuid-a", "C21", "263", "scry-a"),
        ("Sol Ring", "uuid-b", "LTC", "284", "scry-b"),
        ("Cultivate", "uuid-c", "M21", "177", "scry-c"),
    ])
    ap.commit()
    ap.close()
    return p


def make_prices_gz(tmp_path, name, data):
    p = str(tmp_path / name)
    with gzip.open(p, "wt") as f:
        json.dump({"meta": {"version": "5"}, "data": data}, f)
    return p


PRICE_OBJ = {"paper": {"tcgplayer": {"currency": "USD", "retail": {
    "normal": {"2026-08-01": 8.0, "2026-08-08": 7.0},
    "foil": {"2026-08-08": 30.0}}}}}


def test_resolve_watched_names(db, tmp_path):
    ap = make_allprintings(tmp_path)
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_db.add_card(db, list_id, "Cultivate", set_code="M21",
                          collector_number="177")
    n = watchlist_ingest.resolve_watched(db, ap)
    assert n == 3
    uuids = {r["uuid"] for r in db.execute("SELECT uuid FROM card_uuids")}
    assert uuids == {"uuid-a", "uuid-b", "uuid-c"}
    # idempotent
    assert watchlist_ingest.resolve_watched(db, ap) == 0


def test_ingest_prices_only_watched_uuids(db, tmp_path):
    ap = make_allprintings(tmp_path)
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_ingest.resolve_watched(db, ap)
    gz = make_prices_gz(tmp_path, "AllPrices.json.gz", {
        "uuid-a": PRICE_OBJ,
        "uuid-unwatched": PRICE_OBJ,
    })
    n = watchlist_ingest.ingest_prices_file(db, gz)
    assert n == 3                       # 2 normal + 1 foil rows for uuid-a
    uuids = {r["uuid"] for r in db.execute("SELECT DISTINCT uuid FROM prices")}
    assert uuids == {"uuid-a"}


def test_ingest_idempotent(db, tmp_path):
    """Spec acceptance: re-running the same day changes nothing."""
    ap = make_allprintings(tmp_path)
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    watchlist_ingest.resolve_watched(db, ap)
    gz = make_prices_gz(tmp_path, "AllPricesToday.json.gz", {"uuid-a": PRICE_OBJ})
    watchlist_ingest.ingest_prices_file(db, gz)
    before = db.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    watchlist_ingest.ingest_prices_file(db, gz)
    assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == before


def test_streaming_never_loads_whole_file(db, tmp_path):
    """Spec acceptance: bounded memory on a large AllPrices file."""
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES ('Sol Ring','uuid-0')")
    db.commit()
    data = {f"uuid-{i}": PRICE_OBJ for i in range(5000)}
    gz = make_prices_gz(tmp_path, "big.json.gz", data)
    tracemalloc.start()
    watchlist_ingest.ingest_prices_file(db, gz)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 20 * 1024 * 1024      # far below the ~15MB decoded JSON


def test_run_ingest_records_last_ingest(db, db_path, tmp_path, monkeypatch):
    ap = make_allprintings(tmp_path)
    gz_all = make_prices_gz(tmp_path, "AllPrices.json.gz", {"uuid-a": PRICE_OBJ})
    gz_today = make_prices_gz(tmp_path, "AllPricesToday.json.gz",
                              {"uuid-a": PRICE_OBJ})
    monkeypatch.setattr(watchlist_ingest, "_download",
                        lambda url, dest, db: {"AllPrintings.sqlite": ap,
                                               "AllPrices.json.gz": gz_all,
                                               "AllPricesToday.json.gz": gz_today
                                               }[url.rsplit("/", 1)[-1]])
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.close()
    watchlist_ingest.run_ingest(db_path, str(tmp_path))
    db2 = watchlist_db.connect(db_path)
    assert db2.execute("SELECT value FROM meta WHERE key='last_ingest'"
                       ).fetchone() is not None
    assert db2.execute("SELECT COUNT(*) FROM prices").fetchone()[0] > 0
    db2.close()
```

- [x] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_watchlist_ingest.py -v` — Expected: `ModuleNotFoundError: No module named 'watchlist_ingest'`

- [x] **Step 4: Implement `watchlist_ingest.py`**

```python
"""MTGJSON ingest for the price watchlist.

Downloads are cached in DATA_DIR with ETag revalidation (meta table).
AllPrices/AllPricesToday are stream-parsed with ijson — the whole file is
never decoded into memory (spec acceptance criterion)."""

import gzip
import logging
import os
import sqlite3
import time
from datetime import date

import httpx
import ijson

import watchlist_db

log = logging.getLogger("mystic_forge.ingest")

MTGJSON = "https://mtgjson.com/api/v5"
PROVIDERS = ("tcgplayer", "cardkingdom", "cardmarket")


def _data_dir() -> str:
    return os.environ.get(
        "MYSTIC_FORGE_DATA",
        os.path.dirname(os.path.abspath(watchlist_db.DB_PATH)))


def _get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
               (key, value))
    db.commit()


def _download(url: str, dest: str, db) -> str:
    """Download url to dest unless the cached copy's ETag still matches."""
    etag_key = f"etag:{url}"
    headers = {}
    old_etag = _get_meta(db, etag_key)
    if old_etag and os.path.exists(dest):
        headers["If-None-Match"] = old_etag
    with httpx.stream("GET", url, headers=headers, timeout=300.0,
                      follow_redirects=True) as resp:
        if resp.status_code == 304:
            return dest
        resp.raise_for_status()
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
        os.replace(tmp, dest)
        if resp.headers.get("etag"):
            _set_meta(db, etag_key, resp.headers["etag"])
    return dest


def resolve_watched(db, allprintings_path: str) -> int:
    """Fill card_uuids for watched names that lack resolution. Returns rows added."""
    pending = db.execute(
        """SELECT DISTINCT card_name FROM watchlist_current wc
           WHERE NOT EXISTS (SELECT 1 FROM card_uuids cu
                             WHERE LOWER(cu.card_name)=LOWER(wc.card_name))"""
    ).fetchall()
    if not pending:
        return 0
    ap = sqlite3.connect(allprintings_path)
    ap.row_factory = sqlite3.Row
    added = 0
    for row in pending:
        for c in ap.execute(
                "SELECT name, uuid, setCode, number, scryfallId FROM cards"
                " WHERE LOWER(name)=LOWER(?)", (row["card_name"],)):
            db.execute(
                "INSERT OR IGNORE INTO card_uuids (card_name, uuid, set_code,"
                " collector_number, scryfall_id) VALUES (?,?,?,?,?)",
                (c["name"], c["uuid"], c["setCode"], c["number"],
                 c["scryfallId"]))
            added += 1
    ap.close()
    db.commit()
    return added


def watched_uuids(db) -> set[str]:
    return {r["uuid"] for r in db.execute(
        """SELECT DISTINCT cu.uuid FROM watchlist_current wc
           JOIN card_uuids cu ON LOWER(cu.card_name)=LOWER(wc.card_name)""")}


def ingest_prices_file(db, gz_path: str) -> int:
    """Stream a MTGJSON AllPrices/AllPricesToday .gz; upsert watched uuids only.

    Shape: data.<uuid>.paper.<provider>.retail.<finish>.<date> = price"""
    watched = watched_uuids(db)
    rows = 0
    with gzip.open(gz_path, "rb") as f:
        for uuid, obj in ijson.kvitems(f, "data"):
            if uuid not in watched:
                continue
            paper = (obj or {}).get("paper", {})
            for provider in PROVIDERS:
                retail = (paper.get(provider) or {}).get("retail", {})
                for finish, series in retail.items():
                    for d, price in (series or {}).items():
                        watchlist_db.upsert_price(db, uuid, d, provider, finish,
                                                  float(price), commit=False)
                        rows += 1
    db.commit()
    return rows


def _needs_backfill(db) -> bool:
    """Any watched uuid with no price rows at all?"""
    return db.execute(
        """SELECT 1 FROM watchlist_current wc
           JOIN card_uuids cu ON LOWER(cu.card_name)=LOWER(wc.card_name)
           WHERE NOT EXISTS (SELECT 1 FROM prices p WHERE p.uuid=cu.uuid)
           LIMIT 1""").fetchone() is not None


def run_ingest(db_path: str, data_dir: str | None = None) -> None:
    """Synchronous nightly ingest (call via asyncio.to_thread). Idempotent."""
    data_dir = data_dir or _data_dir()
    os.makedirs(data_dir, exist_ok=True)
    db = watchlist_db.connect(db_path)
    try:
        watchlist_db.init_db(db)
        if not db.execute("SELECT 1 FROM watchlist_current LIMIT 1").fetchone():
            _set_meta(db, "last_ingest", date.today().isoformat())
            return
        ap_path = os.path.join(data_dir, "AllPrintings.sqlite")
        week_old = (not os.path.exists(ap_path)
                    or time.time() - os.path.getmtime(ap_path) > 7 * 86400)
        if week_old or _needs_unresolved(db):
            ap_path = _download(f"{MTGJSON}/AllPrintings.sqlite", ap_path, db)
        resolve_watched(db, ap_path)
        if _needs_backfill(db):
            p = _download(f"{MTGJSON}/AllPrices.json.gz",
                          os.path.join(data_dir, "AllPrices.json.gz"), db)
            n = ingest_prices_file(db, p)
            log.info("backfill: %d rows", n)
        p = _download(f"{MTGJSON}/AllPricesToday.json.gz",
                      os.path.join(data_dir, "AllPricesToday.json.gz"), db)
        n = ingest_prices_file(db, p)
        log.info("daily: %d rows", n)
        _set_meta(db, "last_ingest", date.today().isoformat())
    finally:
        db.close()


def _needs_unresolved(db) -> bool:
    return db.execute(
        """SELECT 1 FROM watchlist_current wc
           WHERE NOT EXISTS (SELECT 1 FROM card_uuids cu
                             WHERE LOWER(cu.card_name)=LOWER(wc.card_name))
           LIMIT 1""").fetchone() is not None
```

- [x] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_watchlist_ingest.py -v` — Expected: all PASS. (The memory test streams 5000 uuids; if `peak` is unexpectedly high, check that `ijson.kvitems` is used — never `json.load`.)

- [x] **Step 6: Commit**

```bash
git add watchlist_ingest.py tests/test_watchlist_ingest.py requirements.txt
git commit -m "watchlist: Add streaming MTGJSON ingest with uuid resolution"
```

---

### Task 6: Identity plumbing — contextvar, ASGI middleware, entrypoint, ingest loop hook

**Files:**
- Modify: `server.py` (imports near top; middleware + entrypoint near bottom)
- Test: `tests/test_identity.py` (new)

- [x] **Step 1: Write failing tests**

Create `tests/test_identity.py`:

```python
import asyncio

import watchlist_db
import server


class DummyApp:
    """Records the scope it was called with; sends one empty response."""
    def __init__(self):
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.scope = scope
        self.seen_list = server._current_list.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def run_middleware(path):
    inner = DummyApp()
    mw = server.PassphraseMiddleware(inner)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "path": path, "method": "GET", "headers": []}
    asyncio.run(mw(scope, receive, send))
    return inner, sent


def test_plain_mcp_path_untouched(db_path):
    inner, _ = run_middleware("/mcp")
    assert inner.scope["path"] == "/mcp"
    assert inner.seen_list is None


def test_valid_passphrase_sets_context_and_rewrites(db_path):
    dbc = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(dbc)
    dbc.close()
    inner, _ = run_middleware(f"/mcp/{pp}")
    assert inner.scope["path"] == "/mcp"
    assert inner.seen_list == list_id


def test_invalid_passphrase_404s_without_reaching_app(db_path):
    inner, sent = run_middleware("/mcp/not-a-real-passphrase-00")
    assert inner.scope is None
    assert sent[0]["status"] == 404


def test_non_mcp_paths_pass_through(db_path):
    inner, _ = run_middleware("/health")
    assert inner.scope["path"] == "/health"


def test_context_cleared_between_requests(db_path):
    dbc = watchlist_db.connect(db_path)
    _, pp, _ = watchlist_db.create_list(dbc)
    dbc.close()
    run_middleware(f"/mcp/{pp}")
    inner, _ = run_middleware("/mcp")
    assert inner.seen_list is None
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_identity.py -v` — Expected: FAIL with `AttributeError: module 'server' has no attribute 'PassphraseMiddleware'`

- [x] **Step 3: Implement in `server.py`**

Add imports at the top (near existing imports):

```python
import asyncio
import json
import logging
import os
from contextvars import ContextVar

import watchlist_db
import watchlist_ingest
```

Add after the `mcp = FastMCP(...)` block:

```python
# ── Watchlist identity ───────────────────────────────────────────────────────
# The list id for the current request, set by PassphraseMiddleware when the
# connector URL carries a passphrase (/mcp/<passphrase>). Tools fall back to
# this when no explicit passphrase parameter is given.
_current_list: ContextVar[int | None] = ContextVar("_current_list", default=None)

_PP_RE = re.compile(r"^/mcp/(?P<pp>[a-z0-9][a-z0-9-]{6,})/?$")

PUBLIC_BASE = os.environ.get("MYSTIC_FORGE_PUBLIC_BASE",
                             "https://kautiontape.com/mtg")


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
```

Replace the entrypoint block at the bottom of `server.py`:

```python
if __name__ == "__main__":
    import sys
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(build_app(), host="0.0.0.0", port=8000)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_identity.py -v` — Expected: all PASS. Run `pytest` to confirm no regressions.

- [x] **Step 5: Commit**

```bash
git add server.py tests/test_identity.py
git commit -m "watchlist: Route passphrase URLs through ASGI identity middleware"
```

---

### Task 7: MCP tools — create, add, remove, list

**Files:**
- Modify: `server.py` (new section before ENTRYPOINT: `# WATCHLIST — ...`)
- Test: `tests/test_watchlist_tools.py` (new)

All tools follow the repo convention: pydantic input model, `@mcp.tool(name=...)`, async, return markdown string. Identity resolution order (spec): explicit `passphrase` param → URL context → helpful error.

- [x] **Step 1: Write failing tests**

Create `tests/test_watchlist_tools.py`:

```python
import pytest

import server
import watchlist_db


@pytest.fixture
def fake_scryfall(monkeypatch):
    async def _fake(endpoint, params=None):
        return {"name": "Sol Ring", "prices": {"usd": "1.23"},
                "set": "c21", "collector_number": "263"}
    monkeypatch.setattr(server, "_scryfall_get", _fake)


@pytest.fixture
def a_list(db_path):
    db = watchlist_db.connect(db_path)
    watchlist_db.init_db(db)
    list_id, pp, sc = watchlist_db.create_list(db, label="test")
    db.close()
    return list_id, pp, sc


async def test_create_returns_passphrase_and_urls(db_path):
    out = await server.watchlist_create(server.WatchlistCreateInput(label="x"))
    assert "/mtg/mcp/" in out and "/mtg/w/" in out and "SC-" in out


async def test_add_with_explicit_passphrase(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    out = await server.watchlist_add(server.WatchlistAddInput(
        name="sol ring", passphrase=pp, target_price=1.0))
    assert "Sol Ring" in out
    db = watchlist_db.connect(db_path)
    entries = watchlist_db.current_entries(db, list_id)
    db.close()
    assert len(entries) == 1 and entries[0]["card_name"] == "Sol Ring"


async def test_add_without_identity_errors_helpfully(db_path, fake_scryfall):
    out = await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring"))
    assert "watchlist_create" in out and "passphrase" in out.lower()


async def test_add_with_bad_passphrase_errors(db_path, fake_scryfall):
    out = await server.watchlist_add(server.WatchlistAddInput(
        name="Sol Ring", passphrase="bogus-bogus-bogus-bogus-00"))
    assert "passphrase" in out.lower() and "not" in out.lower()


async def test_url_context_used_when_no_param(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    token = server._current_list.set(list_id)
    try:
        await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring"))
    finally:
        server._current_list.reset(token)
    db = watchlist_db.connect(db_path)
    assert len(watchlist_db.current_entries(db, list_id)) == 1
    db.close()


async def test_remove(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    out = await server.watchlist_remove(server.WatchlistRemoveInput(
        name="Sol Ring", passphrase=pp))
    assert "Removed" in out
    db = watchlist_db.connect(db_path)
    assert watchlist_db.current_entries(db, list_id) == []
    db.close()


async def test_list_shows_prices_and_deltas(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    db = watchlist_db.connect(db_path)
    db.execute("INSERT INTO card_uuids (card_name, uuid) VALUES"
               " ('Sol Ring','uuid-a')")
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-01", "tcgplayer",
                              "normal", 8.0)
    watchlist_db.upsert_price(db, "uuid-a", "2026-08-08", "tcgplayer",
                              "normal", 7.0)
    db.close()
    out = await server.watchlist_list(server.WatchlistListInput(passphrase=pp))
    assert "Sol Ring" in out and "7.0" in out


async def test_mutating_superseded_list_warns(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    db = watchlist_db.connect(db_path)
    new_id, _, _ = watchlist_db.clone_list(db, list_id, recovery=True)
    successor = watchlist_db.get_list(db, new_id)
    db.close()
    out = await server.watchlist_add(server.WatchlistAddInput(
        name="Sol Ring", passphrase=pp))
    assert "superseded" in out.lower()
    assert successor["share_code"] in out
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_watchlist_tools.py -v` — Expected: FAIL with `AttributeError: ... 'watchlist_create'`

- [x] **Step 3: Implement the tools in `server.py`**

Add a new section before the ENTRYPOINT section:

```python
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


class WatchlistRemoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, description="Card name to remove")
    entry_id: Optional[int] = Field(None, description="Entry id (from watchlist_list/history)")
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


@mcp.tool(name="watchlist_create")
async def watchlist_create(params: WatchlistCreateInput) -> str:
    """Create a new price watchlist. Returns its passphrase (SHOWN ONLY ONCE —
    offer to remember it for the user), personal connector URL, and read-only
    share code."""
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
        try:
            card = await _scryfall_get("/cards/named", {"fuzzy": params.name})
            name = card.get("name", params.name)
            current_usd = (card.get("prices") or {}).get("usd")
        except Exception:
            pass  # offline/unknown: keep the user's spelling

        seq, entry = watchlist_db.add_card(
            db, row["id"], name, set_code=params.set_code,
            collector_number=params.collector_number,
            target_price=params.target_price, note=params.note)
        uuids = watchlist_db.uuids_for_entry(db, entry)
        summary = watchlist_db.price_summary(db, uuids) if uuids else None
        backfill = ("history ready" if summary
                    else "history pending next nightly ingest")
        lines = [warning + f"Added **{name}** (entry #{entry['entry_id']}) — {backfill}."]
        if current_usd:
            lines.append(f"Scryfall market price now: ${current_usd}")
        if entry.get("target_price") is not None:
            lines.append(f"Target: {_fmt_price(entry['target_price'])}")
        if uuids:
            lines.append(f"Tracking {len(uuids)} printing(s).")
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
                db, row["id"], entry_id=params.entry_id, name=params.name)
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
        s = watchlist_db.price_summary(db, watchlist_db.uuids_for_entry(db, e))
        rows.append((e, s))
    rows.sort(key=lambda t: (t[1] is None,
                             t[1]["d30"] if t[1] and t[1]["d30"] is not None else 0))
    for e, s in rows:
        printing = f" [{e['set_code']} {e['collector_number']}]" \
            if e.get("set_code") else ""
        lines.append(
            f"| {e['entry_id']} | {e['card_name']}{printing} "
            f"| {_fmt_price(s['current']) if s else '—'} "
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
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_watchlist_tools.py -v` — Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add server.py tests/test_watchlist_tools.py
git commit -m "watchlist: Add create/add/remove/list MCP tools"
```

---

### Task 8: MCP tools — report, view, history, clone, price_history

**Files:**
- Modify: `server.py` (same WATCHLIST section)
- Test: `tests/test_watchlist_tools.py`

- [x] **Step 1: Write failing tests** (append to `tests/test_watchlist_tools.py`)

```python
def _seed_prices(db_path, name="Sol Ring", uuid="uuid-a"):
    db = watchlist_db.connect(db_path)
    db.execute("INSERT OR IGNORE INTO card_uuids (card_name, uuid) VALUES (?,?)",
               (name, uuid))
    watchlist_db.upsert_price(db, uuid, "2026-07-09", "tcgplayer", "normal", 10.0)
    watchlist_db.upsert_price(db, uuid, "2026-08-01", "tcgplayer", "normal", 8.0)
    watchlist_db.upsert_price(db, uuid, "2026-08-08", "tcgplayer", "normal", 7.0)
    db.close()


async def test_report_flags_target_hits(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(
        name="Sol Ring", passphrase=pp, target_price=30.0))
    _seed_prices(db_path)
    out = await server.watchlist_report(server.WatchlistListInput(passphrase=pp))
    assert "Sol Ring" in out and "target" in out.lower()


async def test_view_by_share_code_is_readonly_surface(db_path, a_list,
                                                      fake_scryfall):
    list_id, pp, sc = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    out = await server.watchlist_view(server.WatchlistViewInput(share_code=sc))
    assert "Sol Ring" in out
    out = await server.watchlist_view(server.WatchlistViewInput(
        share_code="SC-ZZZZZZ"))
    assert "not" in out.lower()          # unknown code


async def test_history_lists_chain(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    await server.watchlist_remove(server.WatchlistRemoveInput(
        name="Sol Ring", passphrase=pp))
    out = await server.watchlist_history(server.WatchlistHistoryInput(
        passphrase=pp))
    assert "add" in out and "remove" in out and "#3" in out


async def test_clone_own_list_is_recovery(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    out = await server.watchlist_clone(server.WatchlistCloneInput(passphrase=pp))
    assert "Passphrase" in out
    db = watchlist_db.connect(db_path)
    assert watchlist_db.get_list(db, list_id)["superseded_by"] is not None
    db.close()


async def test_clone_via_share_code_is_fork(db_path, a_list, fake_scryfall):
    list_id, pp, sc = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    out = await server.watchlist_clone(server.WatchlistCloneInput(share_code=sc))
    assert "Passphrase" in out
    db = watchlist_db.connect(db_path)
    assert watchlist_db.get_list(db, list_id)["superseded_by"] is None
    db.close()


async def test_price_history_series(db_path, a_list, fake_scryfall):
    list_id, pp, _ = a_list
    await server.watchlist_add(server.WatchlistAddInput(name="Sol Ring",
                                                        passphrase=pp))
    _seed_prices(db_path)
    out = await server.price_history(server.PriceHistoryInput(name="Sol Ring"))
    assert "2026-08-08" in out and "7.0" in out
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_watchlist_tools.py -v` — Expected: new tests FAIL (`watchlist_report` missing).

- [x] **Step 3: Implement** (append to the WATCHLIST section in `server.py`)

```python
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
            s = watchlist_db.price_summary(db, watchlist_db.uuids_for_entry(db, e))
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


@mcp.tool(name="price_history")
async def price_history(params: PriceHistoryInput) -> str:
    """Daily price series (cheapest printing) for a card from the local price
    DB. Data exists for cards someone watches; global, needs no passphrase."""
    db = _wl_db()
    try:
        uuids = [r["uuid"] for r in db.execute(
            "SELECT uuid FROM card_uuids WHERE LOWER(card_name)=LOWER(?)",
            (params.name,))]
        series = watchlist_db.price_series(db, uuids, days=params.days,
                                           provider=params.provider)
        if not series or not series["points"]:
            return (f"No local history for '{params.name}'. It appears after a "
                    f"watchlist add + nightly ingest; for a spot price use "
                    f"scryfall_price.")
        pts = "\n".join(f"{d}: {p}" for d, p in series["points"])
        return (f"# {params.name} — {params.provider}, cheapest printing "
                f"({series['uuid']})\n```\n{pts}\n```")
    finally:
        db.close()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_watchlist_tools.py -v` — Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add server.py tests/test_watchlist_tools.py
git commit -m "watchlist: Add report/view/history/clone/price_history tools"
```

---

### Task 9: HTTP surface — /health + /w + /s pages

**Files:**
- Modify: `server.py`
- Test: `tests/test_http_surface.py` (new)

- [x] **Step 1: Write failing tests**

Create `tests/test_http_surface.py`:

```python
from starlette.testclient import TestClient

import server
import watchlist_db


def client():
    return TestClient(server.build_app())


def test_health_ok(db_path):
    with client() as c:
        r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "last_ingest" in body and "lists" in body


def test_health_reports_stale_ingest(db_path):
    db = watchlist_db.connect(db_path)
    list_id, _, _ = watchlist_db.create_list(db)
    watchlist_db.add_card(db, list_id, "Sol Ring")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest','2020-01-01')")
    db.commit()
    db.close()
    with client() as c:
        body = c.get("/health").json()
    assert body["ingest_stale"] is True and body["status"] == "degraded"


def test_watch_page_shows_list_and_history(db_path):
    db = watchlist_db.connect(db_path)
    list_id, pp, _ = watchlist_db.create_list(db, label="My Deck")
    watchlist_db.add_card(db, list_id, "Sol Ring", note="<script>")
    db.close()
    with client() as c:
        r = c.get(f"/w/{pp}")
    assert r.status_code == 200
    assert "My Deck" in r.text and "Sol Ring" in r.text
    assert "<script>" not in r.text          # escaped
    assert "&lt;script&gt;" in r.text


def test_watch_page_404_on_bad_passphrase(db_path):
    with client() as c:
        assert c.get("/w/bogus-bogus-bogus-bogus-00").status_code == 404


def test_share_page_readonly_no_passphrase_leak(db_path):
    db = watchlist_db.connect(db_path)
    _, pp, sc = watchlist_db.create_list(db, label="Shared")
    db.close()
    with client() as c:
        r = c.get(f"/s/{sc}")
    assert r.status_code == 200
    assert "Shared" in r.text
    assert pp not in r.text                  # passphrase never on share page
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_http_surface.py -v` — Expected: 404s / assertion failures (routes don't exist yet).

- [x] **Step 3: Implement routes in `server.py`** (after the tools, before ENTRYPOINT)

```python
from html import escape as _esc

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse


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


def _render_page(db, row, editable: bool) -> str:
    title = _esc(row["label"] or "Watchlist")
    parts = [f"<!doctype html><meta charset=utf-8><title>{title}</title>",
             "<style>body{font:15px system-ui;margin:2rem auto;max-width:52rem;"
             "padding:0 1rem}table{border-collapse:collapse;width:100%}"
             "td,th{border-bottom:1px solid #ccc;padding:.4rem;text-align:left}"
             "code{background:#eee;padding:.1rem .3rem}</style>",
             f"<h1>{title}</h1>"]
    if row["superseded_by"]:
        parts.append("<p>⚠️ This list was superseded by a recovery clone.</p>")
    if not editable:
        parts.append(f"<p>Read-only view via share code "
                     f"<code>{_esc(row['share_code'])}</code></p>")
    parts.append("<h2>Current cards</h2><table><tr><th>#</th><th>Card</th>"
                 "<th>Price</th><th>Target</th><th>Note</th></tr>")
    for e in watchlist_db.current_entries(db, row["id"]):
        s = watchlist_db.price_summary(db, watchlist_db.uuids_for_entry(db, e))
        parts.append(
            f"<tr><td>{e['entry_id']}</td><td>{_esc(e['card_name'])}</td>"
            f"<td>{_fmt_price(s['current']) if s else '—'}</td>"
            f"<td>{_fmt_price(e['target_price'])}</td>"
            f"<td>{_esc(e['note'] or '')}</td></tr>")
    parts.append("</table><h2>History</h2><table><tr><th>#</th><th>When</th>"
                 "<th>Action</th><th>Detail</th></tr>")
    for ev in db.execute("SELECT * FROM events WHERE list_id=? ORDER BY seq DESC",
                         (row["id"],)):
        payload = json.loads(ev["payload_json"])
        detail = payload.get("card_name") or payload.get("label") or ""
        parts.append(f"<tr><td>{ev['seq']}</td><td>{_esc(ev['ts'])}</td>"
                     f"<td>{_esc(ev['action'])}</td><td>{_esc(str(detail))}</td></tr>")
    parts.append("</table><p>Recover any revision in chat: "
                 "<code>watchlist_clone(at_seq=N)</code></p>")
    return "".join(parts)


@mcp.custom_route("/w/{passphrase}", methods=["GET"])
async def watch_page(request: Request):
    db = _wl_db()
    try:
        row = watchlist_db.get_list_by_passphrase(
            db, request.path_params["passphrase"])
        if row is None:
            return HTMLResponse("unknown passphrase", status_code=404)
        return HTMLResponse(_render_page(db, row, editable=True))
    finally:
        db.close()


@mcp.custom_route("/s/{share_code}", methods=["GET"])
async def share_page(request: Request):
    db = _wl_db()
    try:
        row = watchlist_db.get_list_by_share(
            db, request.path_params["share_code"])
        if row is None:
            return HTMLResponse("unknown share code", status_code=404)
        return HTMLResponse(_render_page(db, row, editable=False))
    finally:
        db.close()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_http_surface.py -v` — Expected: all PASS. Then full suite: `pytest`.

- [x] **Step 5: Commit**

```bash
git add server.py tests/test_http_surface.py
git commit -m "watchlist: Add health endpoint and read-only history pages"
```

---

### Task 10: Packaging — Dockerfile, dev compose, README, server instructions

**Files:**
- Modify: `Dockerfile`, `docker-compose.yml`, `README.md`, `server.py` (instructions string)

- [x] **Step 1: Dockerfile — ship the new modules**

Replace `COPY server.py .` with:

```dockerfile
COPY server.py watchlist_db.py watchlist_ingest.py watchlist_words.txt ./
```

- [x] **Step 2: Dev compose parity** (`docker-compose.yml` in this repo)

```yaml
services:
  mystic-forge:
    build: .
    container_name: mystic_forge
    ports:
      - "8000:8000"
    environment:
      - MYSTIC_FORGE_DB=/data/mystic_forge.db
    volumes:
      - mystic_forge_data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status==200 else 1)"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 15s
    restart: always

volumes:
  mystic_forge_data:
```

- [x] **Step 3: Append watchlist guidance to the FastMCP `instructions` string** in `server.py` (inside the existing parenthesized string):

```python
        "Watchlist: price watchlists are identified by a passphrase. When the "
        "user gives one (or uses a personal connector URL) pass it through; "
        "after watchlist_create, show the passphrase once and offer to "
        "remember it for future chats. Share codes (SC-…) are read-only."
```

- [x] **Step 4: README** — add a `## Price watchlist` section after the existing tool docs:

```markdown
## Price watchlist

Personal MTG price watchlists with ~90 days of MTGJSON daily history.
Lists are identified by a passphrase (shown once at `watchlist_create`):
use it in a personal connector URL (`https://kautiontape.com/mtg/mcp/<passphrase>`)
or pass it to tools in chat. Share codes (`SC-…`) grant read-only viewing at
`https://kautiontape.com/mtg/s/<code>`. Every change is an append-only event —
see the full chain at `https://kautiontape.com/mtg/w/<passphrase>` and recover
any revision with `watchlist_clone(at_seq=N)` (mints a new passphrase).
Prices ingest nightly from MTGJSON (tcgplayer/cardkingdom/cardmarket retail).
Health: `GET /health`.
```

- [x] **Step 5: Verify + commit**

Run: `pytest` (full suite) and `docker build -t mf-test . && docker run --rm mf-test python -c "import server"` if docker is available locally; otherwise pytest only.

```bash
git add Dockerfile docker-compose.yml README.md server.py
git commit -m "watchlist: Package modules, dev volume, docs, instructions"
```

---

### Task 11: Parent repo — volume, healthcheck, Caddy routes

**Files (in `/home/shawn/documents/apps/kautiontape/mcp-servers` — separate repo!):**
- Modify: `docker-compose.yml`
- Modify: `Caddyfile`

- [x] **Step 1: Create a branch** (do NOT touch main; do NOT push)

```bash
cd /home/shawn/documents/apps/kautiontape/mcp-servers
git status   # confirm clean before branching; stop and report if dirty
git checkout -b watchlist-deploy
```

- [x] **Step 2: compose — replace the mystic-forge service block**

```yaml
  mystic-forge:
    build: ./mystic-forge
    image: ghcr.io/kautiontape/mcp-servers-mystic-forge:latest
    container_name: mcp_mystic_forge
    environment:
      - MYSTIC_FORGE_DB=/data/mystic_forge.db
    volumes:
      - mystic_forge_data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status==200 else 1)"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 15s
    restart: always
    networks:
      - mcp
```

And extend the top-level volumes block:

```yaml
volumes:
  actual_data:
  mystic_forge_data:
```

- [x] **Step 3: Caddyfile — add watchlist page + health routes** (after the `@mtg_mcp` handle block):

```
    # Mystic Forge — watchlist pages + health (public, no auth)
    @mtg_pages path /mtg/w/* /mtg/s/* /mtg/health
    handle @mtg_pages {
        uri strip_prefix /mtg
        reverse_proxy mystic-forge:8000
    }
```

- [x] **Step 4: Validate + commit**

```bash
docker compose config -q && echo compose-ok
git add docker-compose.yml Caddyfile
git commit -m "deploy: Add mystic-forge data volume, healthcheck, page routes"
git checkout main   # leave the checkout back on main for the user
```

(If `docker compose config` is unavailable, note it and rely on YAML review.)

---

### Task 12: Full verification + live smoke test

- [x] **Step 1: Full suite in the worktree**

Run: `cd /home/shawn/.herdr/worktrees/mystic-forge/price-history && pytest -v`
Expected: everything passes, including the pre-existing precon tests.

- [x] **Step 2: Live smoke test** (real server, real streamable HTTP client)

```bash
cd /home/shawn/.herdr/worktrees/mystic-forge/price-history
MYSTIC_FORGE_NO_INGEST=1 MYSTIC_FORGE_DB=/tmp/claude-smoke.db python server.py &
sleep 3
curl -s http://localhost:8000/health          # expect {"status":"ok",...}
```

Then exercise the passphrase URL end-to-end (this validates ContextVar
propagation through the real streamable-HTTP stack — the one integration risk):

```bash
python - <<'EOF'
import asyncio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    # 1. create a list over the public endpoint
    async with streamablehttp_client("http://localhost:8000/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = await s.call_tool("watchlist_create", {"params": {"label": "smoke"}})
            text = out.content[0].text
            print(text)
            pp = text.split("`")[1]
    # 2. add via the personal URL — no passphrase param
    async with streamablehttp_client(f"http://localhost:8000/mcp/{pp}") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = await s.call_tool("watchlist_add", {"params": {"name": "Sol Ring"}})
            print(out.content[0].text)
            out = await s.call_tool("watchlist_list", {"params": {}})
            print(out.content[0].text)
            assert "Sol Ring" in out.content[0].text, "URL identity failed!"
    print("SMOKE OK")

asyncio.run(main())
EOF
kill %1
```

Expected: `SMOKE OK`. **If the URL-identity assert fails** (ContextVar not
propagating through the session manager's task group), fall back to stamping
`scope["mystic_list_id"]` in the middleware and reading it in `_resolve_list_row`
via `mcp.get_context().request_context.request.scope` — then re-run this smoke
test until it passes, and update the identity tests to match.

- [x] **Step 3: Check off spec acceptance criteria** in the spec doc, commit any doc updates, and stop. Merging `price_history` → main and pushing are the user's call (use superpowers:finishing-a-development-branch).

---

## Self-review notes (already applied)

- Spec coverage: identity (T6/T7), share codes (T1/T8/T9), events+replay (T2), clone/supersede (T3/T7/T8), prices+deltas (T4), MTGJSON streaming+idempotent+memory (T5), tools (T7/T8), pages+health (T9), deploy volume/healthcheck/Caddy (T10/T11), acceptance criteria mapped in T12.
- Type consistency: `create_list`/`clone_list` return `(id, passphrase, share_code)` everywhere; tools take `params` models; `price_summary` returns dict-or-None and every caller guards None.
- Known deliberate divergence: `watchlist_current` carries `set_code`/`collector_number`/`uuid` beyond the spec SQL (documented in header).
