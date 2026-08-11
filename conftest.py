import os
import sys

# Make the repo-root modules (server.py) importable from tests/.
sys.path.insert(0, os.path.dirname(__file__))

os.environ["MYSTIC_FORGE_NO_INGEST"] = "1"  # never start the ingest loop in tests

import pytest


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Fresh initialized SQLite DB; module default path patched to it."""
    from mystic_forge.watchlist import db as watchlist_db
    p = str(tmp_path / "test.db")
    monkeypatch.setattr(watchlist_db, "DB_PATH", p)
    db = watchlist_db.connect(p)
    watchlist_db.init_db(db)
    db.close()
    return p


@pytest.fixture
def db(db_path):
    from mystic_forge.watchlist import db as watchlist_db
    conn = watchlist_db.connect(db_path)
    yield conn
    conn.close()
