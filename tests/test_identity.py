import asyncio

import watchlist_db
import server


class DummyApp:
    """Records the scope it was called with; sends one empty response."""
    def __init__(self):
        self.scope = None
        self.seen_list = None

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
