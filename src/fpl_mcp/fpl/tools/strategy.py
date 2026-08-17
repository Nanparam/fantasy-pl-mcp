# src/fpl_mcp/fpl/tools/strategy.py
"""Squad strategy advisor tools (ported from fpl-mcp-server).

These operate on the authenticated user's squad and recommend transfers,
chip timing, and flag underperforming players. All return structured dicts.
Data comes from the target project's existing data layer:
  - authenticated squad/transfers/chips via auth_manager.get_my_team
  - player metadata via cache.get_player_map
  - per-player fixtures via resources.fixtures.get_player_fixtures
  - per-player gameweek history via api.get_player_summary
"""
import logging
from typing import Any, Dict, List, Optional

from ..api import api
from ..auth_manager import get_auth_manager
from ..cache import get_player_map
from ..resources.fixtures import get_player_fixtures
from ..utils.concurrency import gather_limited
from ..utils.gameweek import get_current_gameweek_id
from ..utils.params import unwrap

logger = logging.getLogger(__name__)

_POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# status codes FPL uses on the bootstrap element
_STATUS_LABELS = {
    "a": "available",
    "i": "injured",
    "d": "doubtful",
    "s": "suspended",
    "u": "unavailable",
    "n": "not eligible",
}


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def _authed_team_id() -> Optional[int]:
    auth_manager = get_auth_manager()
    team_id = auth_manager.team_id
    return int(team_id) if team_id else None


def register_tools(mcp):
    """Register strategy advisor tools with the MCP server"""

    @mcp.tool()
    async def analyze_squad_recent_performance(num_gameweeks: int = 5) -> Dict[str, Any]:
        """Analyze recent gameweek performance for your current squad.

        Fetches the last N gameweeks for each of your players and buckets them
        into underperformers / solid / stars, with a form trend and net
        transfer-market sentiment per player. Requires FPL authentication.

        Args:
            num_gameweeks: Number of recent gameweeks to analyze (default 5)

        Returns:
            Per-player stats, summary counts, and transfer priorities
        """
        logger.info(f"Tool called: analyze_squad_recent_performance({num_gameweeks})")
        num_gameweeks = unwrap(num_gameweeks, "num_gameweeks", default=5)
        try:
            num_gameweeks = max(1, int(num_gameweeks))
        except (TypeError, ValueError):
            num_gameweeks = 5

        team_id = await _authed_team_id()
        if not team_id:
            return {
                "error": "No authenticated team found",
                "suggestion": "Run 'fpl-mcp-config setup' to store your FPL credentials",
            }

        auth_manager = get_auth_manager()
        try:
            my_team = await auth_manager.get_my_team(team_id)
        except Exception as e:
            return {"error": f"Could not fetch your squad: {e}"}

        picks = my_team.get("picks", [])
        if not picks:
            return {"error": "No squad found for your team"}

        player_map = await get_player_map()
        player_ids = [p.get("element") for p in picks if p.get("element")]

        # Fetch each player's element-summary concurrently
        summaries = await gather_limited(
            (api.get_player_summary(pid) for pid in player_ids),
            limit=5,
            return_exceptions=True,
        )

        players_out: List[Dict[str, Any]] = []
        for pid, summary in zip(player_ids, summaries):
            info = player_map.get(pid, {})
            name = info.get("web_name", f"Player {pid}")

            if isinstance(summary, Exception) or not summary:
                logger.warning(f"Could not fetch history for player {pid}: {summary}")
                continue

            history = summary.get("history", []) or []
            recent = history[-num_gameweeks:]
            if not recent:
                continue

            points = [entry.get("total_points", 0) for entry in recent]
            minutes = [entry.get("minutes", 0) for entry in recent]
            total_points = sum(points)
            games_played = sum(1 for m in minutes if m > 0)
            avg_points = round(total_points / len(recent), 2)
            avg_minutes = round(sum(minutes) / len(recent), 1)

            # Form trend: last 3 vs the ones before
            trend = "stable"
            if len(points) >= 4:
                last3 = points[-3:]
                prior = points[:-3]
                last3_avg = sum(last3) / len(last3)
                prior_avg = sum(prior) / len(prior) if prior else 0
                if prior_avg and last3_avg > prior_avg * 1.2:
                    trend = "improving"
                elif prior_avg and last3_avg < prior_avg * 0.8:
                    trend = "declining"

            # Net transfer-market sentiment over the window
            net_transfers = sum(entry.get("transfers_balance", 0) for entry in recent)
            if net_transfers > 100_000:
                sentiment = "heavy_buying"
            elif net_transfers > 10_000:
                sentiment = "buying"
            elif net_transfers < -100_000:
                sentiment = "heavy_selling"
            elif net_transfers < -10_000:
                sentiment = "selling"
            else:
                sentiment = "stable"

            # Category bucket
            if avg_points < 2.5:
                category = "underperformer"
            elif avg_points < 5:
                category = "solid"
            else:
                category = "star"

            players_out.append(
                {
                    "id": pid,
                    "name": name,
                    "position": _POSITIONS.get(info.get("element_type"), "UNK"),
                    "avg_points": avg_points,
                    "total_points": total_points,
                    "avg_minutes": avg_minutes,
                    "games_played": games_played,
                    "form_trend": trend,
                    "transfer_sentiment": sentiment,
                    "net_transfers": net_transfers,
                    "category": category,
                    "status": _STATUS_LABELS.get(info.get("status"), info.get("status")),
                    "news": info.get("news", ""),
                }
            )

        # Worst first
        players_out.sort(key=lambda p: p["avg_points"])

        underperformers = [p for p in players_out if p["category"] == "underperformer"]
        summary = {
            "underperformers": len(underperformers),
            "solid": sum(1 for p in players_out if p["category"] == "solid"),
            "stars": sum(1 for p in players_out if p["category"] == "star"),
        }

        return {
            "gameweeks_analyzed": num_gameweeks,
            "players": players_out,
            "summary": summary,
            "transfer_priorities": [
                {"name": p["name"], "reason": f"avg {p['avg_points']} pts, {p['form_trend']}"}
                for p in underperformers
            ],
        }

    @mcp.tool()
    async def recommend_transfers() -> Dict[str, Any]:
        """Recommend which of your players to transfer out.

        Scores each owned player on a transfer-out priority (injuries/suspension,
        did-not-play, hard upcoming fixtures, poor form, low minutes) and reports
        your free transfers plus points-hit economics. Requires authentication.

        Returns:
            Free transfers, ranked transfer-out candidates, and advice
        """
        logger.info("Tool called: recommend_transfers()")

        team_id = await _authed_team_id()
        if not team_id:
            return {
                "error": "No authenticated team found",
                "suggestion": "Run 'fpl-mcp-config setup' to store your FPL credentials",
            }

        auth_manager = get_auth_manager()
        try:
            my_team = await auth_manager.get_my_team(team_id)
        except Exception as e:
            return {"error": f"Could not fetch your squad: {e}"}

        picks = my_team.get("picks", [])
        if not picks:
            return {"error": "No squad found for your team"}

        transfers_state = my_team.get("transfers", {})
        limit = transfers_state.get("limit")
        if limit is None:
            return {
                "error": "Transfer recommendations are not available before Gameweek 1.",
                "free_transfers": None,
            }
        free_transfers = max(0, limit - (transfers_state.get("made") or 0))

        player_map = await get_player_map()
        player_ids = [p.get("element") for p in picks if p.get("element")]

        # Per-player next-3 fixtures for difficulty scoring
        fixtures_lists = await gather_limited(
            (get_player_fixtures(pid, 3) for pid in player_ids),
            limit=5,
            return_exceptions=True,
        )

        candidates: List[Dict[str, Any]] = []
        for pid, player_fixtures in zip(player_ids, fixtures_lists):
            info = player_map.get(pid)
            if not info:
                continue
            if isinstance(player_fixtures, Exception):
                player_fixtures = []

            score = 0
            reasons: List[str] = []

            status = info.get("status", "a")
            if status != "a":
                score += 100
                reasons.append(_STATUS_LABELS.get(status, status))

            form = _to_float(info.get("form"))
            if form < 2:
                score += 25
                reasons.append("very poor form")
            elif form < 3:
                score += 10
                reasons.append("poor form")

            minutes = info.get("minutes", 0) or 0
            if minutes < 200:
                score += 20
                reasons.append("low minutes")

            if player_fixtures:
                avg_diff = sum(f.get("difficulty", 3) for f in player_fixtures) / len(player_fixtures)
                if avg_diff >= 4:
                    score += 30
                    reasons.append("very hard fixtures")
                elif avg_diff >= 3.5:
                    score += 15
                    reasons.append("hard fixtures")

            if score >= 100:
                priority = "urgent"
            elif score >= 50:
                priority = "high"
            elif score >= 30:
                priority = "medium"
            else:
                priority = "low"

            candidates.append(
                {
                    "id": pid,
                    "name": info.get("web_name", f"Player {pid}"),
                    "position": _POSITIONS.get(info.get("element_type"), "UNK"),
                    "priority_score": score,
                    "priority": priority,
                    "reasons": reasons,
                    "form": form,
                    "status": _STATUS_LABELS.get(status, status),
                }
            )

        candidates.sort(key=lambda c: c["priority_score"], reverse=True)

        return {
            "free_transfers": free_transfers,
            "candidates_out": candidates[:5],
            "advice": {
                "points_hit_economics": (
                    "Each extra transfer beyond your free ones costs 4 points. Only "
                    "take a hit if the incoming player is expected to out-score the "
                    "outgoing by more than 4 points over the coming weeks."
                ),
                "timing": (
                    "Bank a transfer (up to the cap) when no player is urgent, and "
                    "act immediately on injured/suspended players (score >= 100)."
                ),
            },
        }

    @mcp.tool()
    async def recommend_chip_strategy() -> Dict[str, Any]:
        """Recommend timing for your available FPL chips.

        Detects upcoming double gameweeks (a team playing 2+ times) and blank
        gameweeks (fewer than 60% of teams playing) over the next ~10 gameweeks
        and advises on each chip you still have. Requires authentication.

        Returns:
            Available chips, per-chip recommendations, and fixture outlook
        """
        logger.info("Tool called: recommend_chip_strategy()")

        team_id = await _authed_team_id()
        if not team_id:
            return {
                "error": "No authenticated team found",
                "suggestion": "Run 'fpl-mcp-config setup' to store your FPL credentials",
            }

        auth_manager = get_auth_manager()
        try:
            my_team = await auth_manager.get_my_team(team_id)
        except Exception as e:
            return {"error": f"Could not fetch your squad: {e}"}

        available_chips = [
            c.get("name")
            for c in my_team.get("chips", [])
            if c.get("status_for_entry") == "available"
        ]

        current_gw = await get_current_gameweek_id()
        if current_gw is None:
            current_gw = 1

        all_fixtures = await api.get_fixtures()
        teams = await api.get_teams()
        total_teams = len(teams) or 20

        # Build a fixture outlook for the next 10 gameweeks
        horizon = range(current_gw, current_gw + 10)
        fixture_outlook: List[Dict[str, Any]] = []
        double_gws: List[int] = []
        blank_gws: List[int] = []

        for gw in horizon:
            gw_fixtures = [f for f in all_fixtures if f.get("event") == gw]
            team_counts: Dict[int, int] = {}
            teams_playing = set()
            for f in gw_fixtures:
                for tkey in ("team_h", "team_a"):
                    tid = f.get(tkey)
                    if tid:
                        team_counts[tid] = team_counts.get(tid, 0) + 1
                        teams_playing.add(tid)

            is_double = any(count >= 2 for count in team_counts.values())
            is_blank = bool(gw_fixtures) and len(teams_playing) < total_teams * 0.6

            if is_double:
                double_gws.append(gw)
            if is_blank:
                blank_gws.append(gw)

            fixture_outlook.append(
                {
                    "gameweek": gw,
                    "teams_playing": len(teams_playing),
                    "double_gameweek": is_double,
                    "blank_gameweek": is_blank,
                }
            )

        recommendations: List[Dict[str, Any]] = []
        chip_lower = {c.lower(): c for c in available_chips if c}

        def _next(gws: List[int]) -> Optional[int]:
            return gws[0] if gws else None

        # Bench Boost / Triple Captain -> favor double gameweeks
        for key, label in (("bboost", "Bench Boost"), ("3xc", "Triple Captain")):
            name = chip_lower.get(key)
            if not name:
                continue
            target = _next(double_gws)
            recommendations.append(
                {
                    "chip": name,
                    "priority": "high" if target else "low",
                    "suggested_gameweek": target,
                    "reason": (
                        f"Best used in a double gameweek (next: GW{target})."
                        if target
                        else "No double gameweek detected in the next 10 GWs; hold."
                    ),
                }
            )

        # Free Hit -> favor blank gameweeks
        if "freehit" in chip_lower:
            target = _next(blank_gws)
            recommendations.append(
                {
                    "chip": chip_lower["freehit"],
                    "priority": "high" if target else "low",
                    "suggested_gameweek": target,
                    "reason": (
                        f"Best used to navigate a blank gameweek (next: GW{target})."
                        if target
                        else "No blank gameweek detected in the next 10 GWs; hold."
                    ),
                }
            )

        # Wildcard -> favor a double gameweek run or when many players are unavailable
        if "wildcard" in chip_lower:
            wc_player_map = await get_player_map()
            injured = sum(
                1
                for p in my_team.get("picks", [])
                if wc_player_map.get(p.get("element"), {}).get("status", "a") != "a"
            )
            target = _next(double_gws)
            priority = "high" if (target or injured >= 3) else "medium"
            recommendations.append(
                {
                    "chip": chip_lower["wildcard"],
                    "priority": priority,
                    "suggested_gameweek": target,
                    "reason": (
                        f"{injured} squad players currently unavailable; "
                        if injured
                        else ""
                    )
                    + (
                        f"consider restructuring ahead of the double gameweek in GW{target}."
                        if target
                        else "use to fix your squad structure when fixtures swing."
                    ),
                }
            )

        priority_rank = {"high": 0, "medium": 1, "low": 2}
        recommendations.sort(key=lambda r: priority_rank.get(r["priority"], 3))

        return {
            "available_chips": available_chips,
            "recommendations": recommendations,
            "fixture_outlook": fixture_outlook,
            "double_gameweeks": double_gws,
            "blank_gameweeks": blank_gws,
        }
