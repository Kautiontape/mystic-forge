"""Mystic Forge application package.

Supporting modules for the MCP server: Comprehensive Rules parsing
(rulebook), the price watchlist (watchlist/), the deck simulator
(goldfish/), and vendored data assets (data/).

Tool declarations stay in the repo-root server.py — the release checks in
mcp-servers resolve `@mcp.tool(name=...)` from that file at any pinned
commit.
"""

import os

# VERSION sits at the repo root, outside this package, next to server.py; the
# Dockerfile copies it to /app/VERSION beside /app/mystic_forge/. One level up
# from the package therefore resolves it in a source checkout and in the image
# alike.
_VERSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")


def version() -> str:
    """The running release version.

    Lives here rather than in server.py because the watchlist ingest needs it
    too, and importing server from inside the package would be a cycle. Read
    on each call, not captured at import: the value is used a handful of times
    a day and a stale copy is worth more trouble than the file read costs.
    """
    with open(_VERSION_FILE, encoding="utf-8") as f:
        return f.read().strip()
