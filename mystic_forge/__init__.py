"""Mystic Forge application package.

Supporting modules for the MCP server: Comprehensive Rules parsing
(rulebook), the price watchlist (watchlist/), the deck simulator
(goldfish/), and vendored data assets (data/).

Tool declarations stay in the repo-root server.py — the release checks in
mcp-servers resolve `@mcp.tool(name=...)` from that file at any pinned
commit.
"""
