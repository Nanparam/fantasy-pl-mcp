"""RotoWire scraper for Premier League lineup predictions and injury status.

Ported from fpl-mcp-server. Scrapes RotoWire's public soccer lineups page and
returns plain dicts (not dataclasses) so MCP tools can serialize them directly.
The scrape is cached (10 min) to avoid re-hitting RotoWire on every tool call.
"""

import logging
from typing import Any, Dict, List

import httpx
from bs4 import BeautifulSoup

from ..cache import cache

logger = logging.getLogger(__name__)

ROTOWIRE_URL = "https://www.rotowire.com/soccer/lineups.php"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# TTL for the cached scrape (seconds). Lineups change slowly relative to how
# often the injury tools may be called in a single session.
_SCRAPE_TTL = 600


def _map_status(injury_status: str) -> Dict[str, Any]:
    """Map a RotoWire injury label to our normalized status/reason/confidence."""
    if injury_status == "OUT":
        return {"status": "OUT", "reason": "Listed as OUT on RotoWire", "confidence": 0.95}
    if injury_status in ("QUES", "DOUBTFUL"):
        return {
            "status": "DOUBTFUL",
            "reason": "Listed as QUESTIONABLE on RotoWire",
            "confidence": 0.75,
        }
    if injury_status == "SUS":
        return {"status": "OUT", "reason": "Suspended", "confidence": 1.0}
    return {
        "status": "DOUBTFUL",
        "reason": f"Listed as {injury_status} on RotoWire",
        "confidence": 0.6,
    }


def _parse_lineup_data(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Parse the RotoWire lineup page into a list of player-status dicts.

    Only players carrying an injury/suspension indicator (span.lineup__inj)
    are returned; healthy players are skipped.
    """
    statuses: List[Dict[str, Any]] = []

    try:
        player_entries = soup.find_all("li", class_="lineup__player")
        logger.info(f"Found {len(player_entries)} total player entries")

        for player_entry in player_entries:
            try:
                name_link = player_entry.find("a")
                if not name_link:
                    continue

                # Prefer the full name from the title attribute
                player_name = (name_link.get("title") or "").strip()
                if not player_name:
                    player_name = name_link.get_text(strip=True)
                if not player_name:
                    continue

                # Team abbreviation via the enclosing game box (div.lineup).
                # Each box holds two lineup__list (home, away) and two
                # lineup__abbr; the player's list index picks home vs away.
                team = "Unknown"
                parent_ul = player_entry.find_parent("ul", class_="lineup__list")
                game_box = player_entry.find_parent("div", class_="lineup")
                if parent_ul and game_box:
                    team_abbrs = game_box.find_all("div", class_="lineup__abbr")
                    all_lists = game_box.find_all("ul", class_="lineup__list")
                    if len(all_lists) >= 2 and len(team_abbrs) >= 2:
                        try:
                            list_index = all_lists.index(parent_ul)
                        except ValueError:
                            list_index = 0
                        if list_index < len(team_abbrs):
                            team = team_abbrs[list_index].get_text(strip=True)
                        else:
                            team = team_abbrs[0].get_text(strip=True)

                # Only keep players flagged with an injury/suspension marker
                injury_element = player_entry.find("span", class_="lineup__inj")
                if not injury_element:
                    continue

                injury_status = injury_element.get_text(strip=True)
                mapped = _map_status(injury_status)
                statuses.append(
                    {
                        "player_name": player_name,
                        "team": team,
                        "status": mapped["status"],
                        "reason": mapped["reason"],
                        "confidence": mapped["confidence"],
                    }
                )
            except Exception as e:  # pragma: no cover - defensive per-entry guard
                logger.warning(f"Error parsing player entry: {e}")
                continue

        logger.info(f"Parsed {len(statuses)} player statuses from RotoWire")
        return statuses

    except Exception as e:
        logger.error(f"Error parsing RotoWire lineup data: {e}")
        return []


async def _scrape_lineups() -> List[Dict[str, Any]]:
    """Fetch and parse the RotoWire Premier League lineups page (uncached)."""
    logger.info("Scraping RotoWire Premier League lineups...")
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(ROTOWIRE_URL, headers=_HEADERS)

        if response.status_code != 200:
            logger.error(f"Failed to fetch RotoWire page: HTTP {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        return _parse_lineup_data(soup)
    except Exception as e:
        logger.error(f"Failed to scrape RotoWire lineups: {e}")
        return []


async def get_lineup_statuses() -> List[Dict[str, Any]]:
    """Get RotoWire injury/lineup statuses (cached).

    Returns:
        List of dicts: {player_name, team, status, reason, confidence}.
        Empty list if RotoWire is unreachable or its markup changed.
    """
    return await cache.get_or_fetch(
        "rotowire_lineups",
        fetch_func=_scrape_lineups,
        ttl=_SCRAPE_TTL,
    )
