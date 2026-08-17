# src/fpl_mcp/fpl/tools/injuries.py
"""Injury and predicted-lineup tools backed by the RotoWire scraper.

Ported from fpl-mcp-server. These tools need no FPL authentication — they
read RotoWire's public lineup predictions and return structured dicts.
"""
import logging
from typing import Any, Dict

from ..resources.rotowire import get_lineup_statuses
from ..utils.params import unwrap

logger = logging.getLogger(__name__)

_SOURCE_NOTE = (
    "Scraped from RotoWire predicted lineups; updates as lineups are confirmed."
)


def register_tools(mcp):
    """Register injury/lineup tools with the MCP server"""

    @mcp.tool()
    async def get_injury_and_lineup_predictions() -> Dict[str, Any]:
        """Get predicted lineups and injury status for upcoming Premier League matches.

        Sourced from RotoWire. Lists players currently flagged OUT or DOUBTFUL
        with a confidence rating. No FPL authentication required.

        Returns:
            Counts plus OUT and DOUBTFUL player lists, or an error dict
        """
        logger.info("Tool called: get_injury_and_lineup_predictions()")

        statuses = await get_lineup_statuses()
        if not statuses:
            return {
                "error": "No lineup predictions available at this time.",
                "suggestion": "RotoWire may not have published lineups yet, or its "
                              "page layout changed. Try again closer to kickoff.",
                "source": "RotoWire",
            }

        out = sorted(
            [s for s in statuses if s["status"] == "OUT"], key=lambda s: s["team"]
        )
        doubtful = sorted(
            [s for s in statuses if s["status"] == "DOUBTFUL"], key=lambda s: s["team"]
        )

        return {
            "counts": {"out": len(out), "doubtful": len(doubtful)},
            "out": out,
            "doubtful": doubtful,
            "source": "RotoWire",
            "note": _SOURCE_NOTE,
        }

    @mcp.tool()
    async def get_players_to_avoid() -> Dict[str, Any]:
        """Get players to avoid for transfers based on injury/lineup status.

        Returns players who are OUT (high risk) or DOUBTFUL (medium risk),
        sourced from RotoWire predicted lineups. No FPL authentication required.

        Returns:
            High-risk and medium-risk player lists with a total count
        """
        logger.info("Tool called: get_players_to_avoid()")

        statuses = await get_lineup_statuses()

        high_risk = []
        medium_risk = []
        for s in statuses:
            if s["status"] == "OUT":
                high_risk.append(
                    {
                        "player_name": s["player_name"],
                        "team": s["team"],
                        "reason": f"OUT - {s['reason']}",
                        "risk_level": "high",
                    }
                )
            elif s["status"] == "DOUBTFUL":
                medium_risk.append(
                    {
                        "player_name": s["player_name"],
                        "team": s["team"],
                        "reason": f"DOUBTFUL - {s['reason']}",
                        "risk_level": "medium",
                    }
                )

        return {
            "total": len(high_risk) + len(medium_risk),
            "high_risk": high_risk,
            "medium_risk": medium_risk,
            "source": "RotoWire",
            "note": _SOURCE_NOTE,
        }

    @mcp.tool()
    async def check_player_availability(player_name: str) -> Dict[str, Any]:
        """Check whether a specific player is available based on RotoWire lineups.

        Case-insensitive partial match against scraped player names. No FPL
        authentication required.

        Args:
            player_name: Player name (partial match accepted)

        Returns:
            Availability verdict: "avoid" (OUT), "risky" (DOUBTFUL), or
            "available" (not flagged); plus any matched entries
        """
        logger.info(f"Tool called: check_player_availability({player_name})")

        player_name = unwrap(player_name, "player_name", "name", default=None)
        if not player_name:
            return {"error": "player_name is required"}

        statuses = await get_lineup_statuses()
        query = player_name.lower()
        matches = [s for s in statuses if query in s["player_name"].lower()]

        # No match against the injury/lineup feed => not flagged => likely available
        if not matches:
            return {
                "player_name": player_name,
                "found": False,
                "verdict": "available",
                "message": (
                    f"{player_name} not found in RotoWire injury/lineup reports. "
                    "Likely available to play."
                ),
                "matches": [],
                "source": "RotoWire",
            }

        # Verdict derives from the most severe matched status
        has_out = any(m["status"] == "OUT" for m in matches)
        verdict = "avoid" if has_out else "risky"
        return {
            "player_name": player_name,
            "found": True,
            "verdict": verdict,
            "matches": matches,
            "source": "RotoWire",
            "note": _SOURCE_NOTE,
        }
