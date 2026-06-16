# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP policy_service (ADR-0016 Category A — application-side tool server).

Exposes three tools the underwriting packages route to over MCP:
  - property_data(address)              → third-party property enrichment
  - lookup_appetite(line, occupancy, construction, state)
                                        → carrier appetite & binding authority
  - pull_loss_runs(named_insured)       → 5-year claims history

Runs in either transport so you can exercise both code paths in the MCP client:
  python mcp_servers/example_server.py stdio   # launched as a subprocess
  python mcp_servers/example_server.py http     # served at :9000/mcp (Docker)

Uses the `mcp` package's FastMCP helper.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("policy_service")

# ── property enrichment ───────────────────────────────────────────────────────

_PROPERTY_DB: list[tuple[list[str], dict]] = [
    # match on substrings in the address — first match wins
    (
        ["chicago", " il"],
        {
            "wind_zone":            "none",
            "flood_zone":           "X",
            "distance_to_coast_mi": 800,
            "prior_cat":            False,
            "protection_class":     4,
            "fire_district":        "Chicago FD",
        },
    ),
    (
        ["tampa", " fl"],
        {
            "wind_zone":            "high",
            "flood_zone":           "AE",
            "distance_to_coast_mi": 3,
            "prior_cat":            True,
            "protection_class":     8,
            "fire_district":        "Hillsborough County FD",
        },
    ),
    # generic fallback
    (
        [],
        {
            "wind_zone":            "moderate",
            "flood_zone":           "X",
            "distance_to_coast_mi": 150,
            "prior_cat":            False,
            "protection_class":     6,
            "fire_district":        "Unknown",
        },
    ),
]


@mcp.tool()
def property_data(address: str) -> dict:
    """Retrieve third-party property enrichment for an address."""
    addr_lower = address.lower()
    for keywords, data in _PROPERTY_DB:
        if all(k in addr_lower for k in keywords):
            return {"address": address, **data}
    _, generic = _PROPERTY_DB[-1]
    return {"address": address, **generic}


# ── appetite & authority ──────────────────────────────────────────────────────

def _appetite_key(occupancy: str, construction: str, state: str) -> dict:
    occ   = occupancy.lower()
    constr = construction.lower()
    st    = state.upper()

    # Florida frame/warehouse: still in appetite but near authority limits so TIV breach
    if st == "FL" and constr == "frame" and occ == "warehouse":
        return {
            "in_appetite":       True,
            "authority_tiv":     5_000_000,
            "authority_premium": 25_000,
            "referral_triggers": ["frame_construction_fl", "wind_zone_high", "cat_exposed"],
            "notes":             "Frame construction in FL wind territory triggers referral.",
        }

    # Illinois restaurant — standard in-appetite
    if st == "IL" and occ == "restaurant":
        return {
            "in_appetite":       True,
            "authority_tiv":     3_000_000,
            "authority_premium": 15_000,
            "referral_triggers": [],
            "notes":             "",
        }

    # Default commercial property appetite
    return {
        "in_appetite":       True,
        "authority_tiv":     2_000_000,
        "authority_premium": 10_000,
        "referral_triggers": [],
        "notes":             "Standard limits apply.",
    }


@mcp.tool()
def lookup_appetite(line: str, occupancy: str, construction: str, state: str) -> dict:
    """Check carrier underwriting appetite and binding authority for a risk."""
    result = _appetite_key(occupancy, construction, state)
    return {"line": line, "occupancy": occupancy, "construction": construction,
            "state": state, **result}


# ── loss runs ─────────────────────────────────────────────────────────────────

_LOSS_HISTORY: dict[str, dict] = {
    "lakeview bistro": {
        "loss_count":     1,
        "incurred_total": 45_000,
        "earned_premium": 300_000,
        "loss_ratio":     round(45_000 / 300_000, 4),
        "claims": [
            {"year": 2023, "cause": "kitchen_fire", "incurred": 45_000, "status": "closed"},
        ],
    },
    "gulfside storage": {
        "loss_count":     4,
        "incurred_total": 820_000,
        "earned_premium": 1_000_000,
        "loss_ratio":     round(820_000 / 1_000_000, 4),
        "claims": [
            {"year": 2022, "cause": "hurricane_ian",  "incurred": 600_000, "status": "closed"},
            {"year": 2022, "cause": "wind_hail",      "incurred": 95_000,  "status": "closed"},
            {"year": 2023, "cause": "water_intrusion","incurred": 85_000,  "status": "closed"},
            {"year": 2024, "cause": "vandalism",      "incurred": 40_000,  "status": "closed"},
        ],
    },
}


@mcp.tool()
def pull_loss_runs(named_insured: str) -> dict:
    """Retrieve 5-year claims history for a named insured."""
    key = named_insured.lower().strip()
    for k, data in _LOSS_HISTORY.items():
        if k in key or key in k:
            return {"named_insured": named_insured, **data}
    return {
        "named_insured":  named_insured,
        "loss_count":     0,
        "incurred_total": 0,
        "earned_premium": 0,
        "loss_ratio":     0.0,
        "claims":         [],
    }


# ── entrypoint ────────────────────────────────────────────────────────────────

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
