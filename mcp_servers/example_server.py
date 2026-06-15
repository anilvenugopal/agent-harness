"""Example MCP server (ADR-0016 Category A — an application-side tool server).

Exposes two tools the example packages route to over MCP:
  - lookup_policy(product)  → underwriting rules for a product
  - search(query)           → a canned knowledge-base search

Runs in either transport so you can exercise both code paths in the MCP client:
  python mcp_servers/example_server.py stdio   # launched as a subprocess
  python mcp_servers/example_server.py http     # served at :9000/mcp (Docker)

Uses the `mcp` package's FastMCP helper. This file is the only place the demo
needs the MCP SDK; the engine core does not.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("policy_service")

_POLICIES = {
    "auto": {"max_premium": 5000, "min_age": 18, "excluded_bands": ["very_high"]},
    "home": {"max_premium": 12000, "min_age": 21, "excluded_bands": []},
}


@mcp.tool()
def lookup_policy(product: str) -> dict:
    """Return underwriting rules for a product."""
    rules = _POLICIES.get(product.lower())
    if rules is None:
        return {"product": product, "found": False}
    return {"product": product, "found": True, "rules": rules}


@mcp.tool()
def search(query: str) -> dict:
    """Search the internal knowledge base (canned)."""
    return {
        "query": query,
        "results": [
            {"title": "Regional loss ratios 2025", "snippet": "Average loss ratios within tolerance."},
            {"title": "Fraud watchlist", "snippet": "No matches for this applicant."},
            {"title": "Reinsurance treaty notes", "snippet": "Standard treaty applies."},
        ],
        "count": 3,
    }


if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "http":
        # Streamable HTTP transport (the modern remote transport; SSE deprecated).
        # Allow Docker service-name Host headers (DNS rebinding protection default
        # only permits localhost/127.0.0.1 which breaks inter-container calls).
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = 9000
        mcp.settings.transport_security = TransportSecuritySettings(
            allowed_hosts=["mcp-server:*", "localhost:*", "127.0.0.1:*", "[::1]:*"],
            allowed_origins=["http://mcp-server:*", "http://localhost:*", "http://127.0.0.1:*"],
        )
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
