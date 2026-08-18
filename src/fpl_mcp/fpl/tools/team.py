# src/fpl_mcp/fpl/tools/team.py
import logging
import time
from typing import Dict, Any, Optional, List

from ..auth_manager import get_auth_manager
from ..api import api
from ..cache import cache
from ..utils.gameweek import get_current_gameweek_id

logger = logging.getLogger(__name__)

async def get_team_for_gameweek(gameweek: Optional[int] = None, team_id: int = 0) -> Dict[str, Any]:
    """
    Get any FPL team for a specific gameweek with rich data
    
    Args:
        gameweek: The gameweek number (defaults to current)
        team_id: FPL team ID to look up (required)
        
    Returns:
        Detailed team information including player details
    """
    # Get auth manager for API access
    auth_manager = get_auth_manager()
    
    # Check that we have a valid team ID
    if not team_id:
        return {
            "error": "No team ID specified",
            "suggestion": "Please provide a valid team_id parameter"
        }
    
    logger.info(f"Getting team data for team {team_id}, gameweek {gameweek}")
    
    # Use current gameweek if not specified
    if gameweek is None:
        current_gw_data = await api.get_current_gameweek()
        gameweek = current_gw_data.get("id", 1)  # Extract just the ID
    
    # Ensure gameweek is an integer
    try:
        gameweek = int(gameweek)
    except (ValueError, TypeError):
        logger.error(f"Invalid gameweek value: {gameweek}")
        return {"error": f"Invalid gameweek value: {gameweek}"}
    
    # Get team data for the gameweek
    try:
        gw_picks_data = await auth_manager.get_team_for_gameweek(team_id, gameweek)
    except Exception as e:
        logger.error(f"Error fetching team data: {e}")
        return {
            "error": f"Failed to retrieve team data for gameweek {gameweek}: {str(e)}"
        }
    
    # Get player data to enrich team information
    # Use the players, teams, and position resources for better caching
    all_players = await api.get_players()
    all_teams = await api.get_teams()
    
    # Create lookup dictionaries
    players = {p["id"]: p for p in all_players}
    teams = {t["id"]: t for t in all_teams}
    
    # Process team data
    picks = gw_picks_data.get("picks", [])
    entry_history = gw_picks_data.get("entry_history", {})
    
    # Format each player
    formatted_picks = []
    captain_id = None
    vice_captain_id = None
    
    # Find captain and vice captain
    for pick in picks:
        if pick.get("is_captain"):
            captain_id = pick.get("element")
        if pick.get("is_vice_captain"):
            vice_captain_id = pick.get("element")
    
    # Format players with detailed info
    for pick in picks:
        player_id = pick.get("element")
        player_data = players.get(player_id, {})
        
        if not player_data:
            logger.warning(f"Player {player_id} not found in bootstrap data")
            continue
        
        # Get team ID from player data
        player_team_id = player_data.get("team")
        
        # Look up team details using the team ID
        team_data = teams.get(player_team_id, {})
        team_name = team_data.get("name", "Unknown")
        team_short = team_data.get("short_name", "UNK")
        
        # Extract position from player data
        position = player_data.get("element_type")
        
        # Convert position ID to position code
        position_map = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
        position_code = position_map.get(position, "UNK")
        
        # Create enriched player data
        formatted_player = {
            "id": player_id,
            "position_order": pick.get("position"),
            "multiplier": pick.get("multiplier"),
            "is_captain": pick.get("is_captain", False),
            "is_vice_captain": pick.get("is_vice_captain", False),
            
            # Player details - using field names from players.py resource
            "web_name": player_data.get("web_name", "Unknown"),
            "full_name": f"{player_data.get('first_name', '')} {player_data.get('second_name', '')}".strip() or "Unknown",
            "price": player_data.get("now_cost", 0) / 10.0 if player_data.get("now_cost") else 0,
            "form": player_data.get("form", "0.0"),
            "points_per_game": player_data.get("points_per_game", "0.0"),
            "total_points": player_data.get("total_points", 0),
            "minutes": player_data.get("minutes", 0),
            "goals": player_data.get("goals_scored", 0),
            "assists": player_data.get("assists", 0),
            "clean_sheets": player_data.get("clean_sheets", 0),
            "bonus": player_data.get("bonus", 0),
            "status": player_data.get("status"),
            "news": player_data.get("news", ""),
            
            # Team details
            "team": team_name,
            "team_short": team_short,
            
            # Position details
            "position": position_code,
        }
        
        formatted_picks.append(formatted_player)
    
    # Sort by position order
    formatted_picks.sort(key=lambda p: p["position_order"])
    
    # Split into active and bench
    active_players = [p for p in formatted_picks if p["multiplier"] > 0]
    bench_players = [p for p in formatted_picks if p["multiplier"] == 0]
    
    # Try to get team manager information
    try:
        async def fetch_manager_info():
            entry_data = await auth_manager.get_entry_data(team_id)
            return {
                "team_name": entry_data.get("name", "Unknown"),
                "manager_name": f"{entry_data.get('player_first_name', '')} {entry_data.get('player_last_name', '')}".strip(),
                "manager_region": entry_data.get("player_region_name", ""),
                "overall_rank": entry_data.get("summary_overall_rank", 0),
                "overall_points": entry_data.get("summary_overall_points", 0),
            }

        manager_info = await cache.get_or_fetch(
            f"team_manager_info_{team_id}",
            fetch_func=fetch_manager_info,
            ttl=3600,
        )
    except Exception as e:
        logger.warning(f"Could not get manager info for team {team_id}: {e}")
        manager_info = {
            "team_name": "Unknown",
            "manager_name": "Unknown",
        }
    
    # Build full result
    result = {
        "gameweek": gameweek,
        "team_id": team_id,
        "team_name": manager_info.get("team_name", "Unknown"),
        "manager_name": manager_info.get("manager_name", "Unknown"),
        "active": active_players,
        "bench": bench_players,
        "captain": next((p for p in formatted_picks if p["is_captain"]), None),
        "vice_captain": next((p for p in formatted_picks if p["is_vice_captain"]), None),
    }
    
    # Add gameweek history data if available
    if entry_history:
        result["points"] = entry_history.get("points", 0)
        result["total_points"] = entry_history.get("total_points", 0)
        result["rank"] = entry_history.get("rank", None)
        result["overall_rank"] = entry_history.get("overall_rank", None) or manager_info.get("overall_rank", 0)
        result["bank"] = entry_history.get("bank", 0) / 10.0
        result["team_value"] = entry_history.get("value", 0) / 10.0
        result["transfers"] = {
            "made": entry_history.get("event_transfers", 0),
            "cost": entry_history.get("event_transfers_cost", 0),
        }
    
    return result

async def get_manager_info(team_id: int) -> Dict[str, Any]:
    """
    Get detailed information about a team manager
    
    Args:
        team_id: FPL team ID to look up
        
    Returns:
        Manager information including history, name, and team details
    """
    # Get auth manager
    auth_manager = get_auth_manager()

    try:
        async def fetch_manager_info():
            entry_data = await auth_manager.get_entry_data(team_id)
            return {
                "team_id": team_id,
                "team_name": entry_data.get("name", "Unknown"),
                "manager_name": f"{entry_data.get('player_first_name', '')} {entry_data.get('player_last_name', '')}".strip(),
                "started_event": entry_data.get("started_event"),
                "overall_rank": entry_data.get("summary_overall_rank"),
                "overall_points": entry_data.get("summary_overall_points"),
                "value": entry_data.get("last_deadline_value") / 10.0 if entry_data.get("last_deadline_value") else 0,
                "bank": entry_data.get("last_deadline_bank") / 10.0 if entry_data.get("last_deadline_bank") else 0,
                "kit": entry_data.get("kit"),
                "region": entry_data.get("player_region_name"),
                "joined_time": entry_data.get("joined_time"),
                "leagues": {
                    "classic": entry_data.get("leagues", {}).get("classic", []),
                    "h2h": entry_data.get("leagues", {}).get("h2h", []),
                    "cup": entry_data.get("leagues", {}).get("cup", {})
                }
            }

        # 1 hour cache
        return await cache.get_or_fetch(
            f"manager_info_{team_id}",
            fetch_func=fetch_manager_info,
            ttl=3600,
        )
    except Exception as e:
        logger.error(f"Error fetching manager info for team {team_id}: {e}")
        return {"error": f"Failed to retrieve manager info: {str(e)}"}

# Register these as MCP tools
def register_tools(mcp):
    @mcp.tool()
    async def get_team(team_id: int, gameweek: Optional[int] = None) -> Dict[str, Any]:
        """Get any team's players, captain, and other details for a specific gameweek
        
        Args:
            team_id: FPL team ID (required)
            gameweek: Gameweek number (defaults to current gameweek)
            
        Returns:
            Detailed team information including player details, captain, and value
        """
        try:
            # Always use the specified team_id, no default
            return await get_team_for_gameweek(gameweek, team_id)
        except Exception as e:
            logger.error(f"Error in get_team: {e}")
            return {"error": str(e)}
    
    @mcp.tool()
    async def get_my_team(gameweek: Optional[int] = None) -> Dict[str, Any]:
        """Get your own FPL team for a specific gameweek
        
        Args:
            gameweek: Gameweek number (defaults to current gameweek)
            
        Returns:
            Detailed team information including player details, captain, and value
            
        Note:
            This uses your authenticated team ID from the FPL credentials.
            To get another team's details, use get_team and provide a team_id.
        """
        try:
            # Get the authenticated user's team ID
            auth_manager = get_auth_manager()
            team_id = auth_manager.team_id
            
            if not team_id:
                return {
                    "error": "No default team ID found in credentials",
                    "suggestion": "Check your authentication settings or use get_team with an explicit team_id"
                }
                
            logger.info(f"Getting authenticated user's team: {team_id}")
            return await get_team_for_gameweek(gameweek, team_id)
        except Exception as e:
            logger.error(f"Error in get_my_team: {e}")
            return {"error": str(e)}
            
    @mcp.tool()
    async def get_manager(team_id: int) -> Dict[str, Any]:
        """Get detailed information about an FPL manager

        Args:
            team_id: FPL team ID to look up

        Returns:
            Manager information including history, name, team details, and leagues
        """
        try:
            return await get_manager_info(team_id)
        except Exception as e:
            logger.error(f"Error in get_manager: {e}")
            return {"error": str(e)}

    @mcp.tool()
    async def get_my_current_team() -> Dict[str, Any]:
        """Get your current team as shown on the transfers page, including
        selling prices, chips, and transfer state (requires authentication)

        Unlike get_my_team, this uses the authenticated my-team endpoint, so
        it reflects pending changes and per-player purchase/selling prices.

        Returns:
            Current squad with prices, available chips, and transfer status
        """
        from ..cache import get_player_map

        try:
            auth_manager = get_auth_manager()
            team_id = auth_manager.team_id

            if not team_id:
                return {
                    "error": "No team ID found in credentials",
                    "setup_instructions": "Run 'fpl-mcp-config setup' to configure your FPL credentials"
                }

            data = await auth_manager.get_my_team(int(team_id))
            player_map = await get_player_map()
            positions = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

            picks = []
            for pick in data.get("picks", []):
                pid = pick.get("element")
                info = player_map.get(pid, {})
                picks.append({
                    "id": pid,
                    "name": info.get("web_name", f"Player {pid}"),
                    "position": positions.get(info.get("element_type"), "UNK"),
                    "purchase_price": pick.get("purchase_price", 0) / 10.0,
                    "selling_price": pick.get("selling_price", 0) / 10.0,
                    "is_captain": pick.get("is_captain", False),
                    "is_vice_captain": pick.get("is_vice_captain", False),
                    "on_bench": pick.get("position", 0) > 11,
                })

            transfers = data.get("transfers", {})
            return {
                "team_id": int(team_id),
                "picks": picks,
                "chips": [
                    {"name": c.get("name"), "status": c.get("status_for_entry")}
                    for c in data.get("chips", [])
                ],
                "transfers": {
                    "free_transfers_available": transfers.get("limit"),
                    "made_this_gameweek": transfers.get("made", 0),
                    "bank": transfers.get("bank", 0) / 10.0,
                    "team_value": transfers.get("value", 0) / 10.0,
                },
            }
        except Exception as e:
            logger.error(f"Error in get_my_current_team: {e}")
            return {
                "error": str(e),
                "suggestion": "This endpoint requires valid FPL credentials; run 'fpl-mcp-config setup'"
            }

    @mcp.tool()
    async def check_fpl_authentication() -> Dict[str, Any]:
        """Check if FPL authentication is working correctly

        Returns:
            Authentication status and basic team information
        """
        try:
            auth_manager = get_auth_manager()
            team_id = auth_manager.team_id

            if not team_id:
                return {
                    "authenticated": False,
                    "error": "No team ID found in credentials",
                    "setup_instructions": "Run 'fpl-mcp-config setup' to configure your FPL credentials"
                }

            # Try to get basic team info as authentication test
            try:
                entry_data = await auth_manager.get_entry_data()

                return {
                    "authenticated": True,
                    "team_name": entry_data.get("name"),
                    "manager_name": f"{entry_data.get('player_first_name')} {entry_data.get('player_last_name')}",
                    "overall_rank": entry_data.get("summary_overall_rank"),
                    "team_id": team_id
                }
            except Exception as e:
                return {
                    "authenticated": False,
                    "error": f"Authentication failed: {str(e)}",
                    "setup_instructions": "Check your FPL credentials and ensure they are correct"
                }

        except Exception as e:
            logger.error(f"Authentication check failed: {e}")
            return {
                "authenticated": False,
                "error": str(e),
                "setup_instructions": "Run 'fpl-mcp-config setup' to configure your FPL credentials"
            }

    @mcp.tool()
    async def update_fpl_credentials(refresh_token: str, team_id: str = "") -> Dict[str, Any]:
        """Store a new FPL refresh token when the current one has expired.

        Use this when authenticated FPL tools fail with an invalid/expired refresh
        token error. Ask the user to fetch a fresh token first: log in at
        https://fantasy.premierleague.com, open the DevTools Console (F12), run

            copy(JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.startsWith('oidc.user:')))).refresh_token)

        (typing 'allow pasting' first if Chrome refuses), and paste the clipboard
        contents here. Copying the whole 'oidc.user:...' JSON value from DevTools ->
        Application -> Local storage works too.

        Args:
            refresh_token: The copied oidc.user JSON value (or just its
                refresh_token field)
            team_id: FPL team ID; omit to keep the currently stored one

        Returns:
            Whether the new token was stored and validated against the FPL API
        """
        from ..credential_manager import extract_refresh_token

        token = extract_refresh_token(refresh_token)
        if not token:
            return {
                "updated": False,
                "error": "No refresh token found in the provided value. Pass the "
                         "full oidc.user JSON or its refresh_token field."
            }

        auth_manager = get_auth_manager()
        team_id = team_id.strip() or auth_manager.team_id
        if not team_id:
            return {
                "updated": False,
                "error": "No team ID stored. Ask the user for their FPL team ID "
                         "(the number in their team page URL) and pass it as team_id."
            }

        auth_manager.set_credentials(token, str(team_id))

        # Validate immediately: the exchange claims the token before the user's
        # browser can rotate it away, and /my-team/ proves the API accepts it.
        try:
            team_data = await auth_manager.get_my_team()
        except Exception as e:
            return {
                "updated": False,
                "error": f"Token was stored but failed validation: {e}. Ask the "
                         "user to copy a fresh oidc.user value and try again."
            }

        return {
            "updated": True,
            "team_id": team_id,
            "squad_picks": len(team_data.get("picks", [])),
            "note": "Token validated and stored. The copy in the user's browser "
                    "has been superseded; their browser session will recover on "
                    "its own."
        }

    @mcp.tool()
    async def make_transfers(
        player_names_out: List[str],
        player_names_in: List[str],
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """Execute transfers on your FPL team by player name. IRREVERSIBLE.

        Resolves the given player names to IDs, builds the transfer payload from
        your current squad's selling prices and incoming players' prices, and —
        only when confirm=True — POSTs it to FPL. Requires authentication.

        Safety: with confirm=False (the default) this performs a DRY RUN and
        returns a preview without submitting anything. Review the preview, then
        call again with confirm=True to actually execute.

        Args:
            player_names_out: Names of players to transfer OUT (must be in your squad)
            player_names_in: Names of players to transfer IN (same count as out)
            confirm: Must be True to actually submit the transfers

        Returns:
            A dry-run preview, or the result of the executed transfer
        """
        from ...config import FPL_API_BASE_URL
        from ..resources.players import find_players_by_name
        from ..utils.params import unwrap

        logger.info(
            f"Tool called: make_transfers(out={player_names_out}, "
            f"in={player_names_in}, confirm={confirm})"
        )

        player_names_out = unwrap(player_names_out, "player_names_out", default=player_names_out)
        player_names_in = unwrap(player_names_in, "player_names_in", default=player_names_in)
        confirm = unwrap(confirm, "confirm", default=confirm)

        if not player_names_out or not player_names_in:
            return {"error": "Both player_names_out and player_names_in are required"}
        if len(player_names_out) != len(player_names_in):
            return {
                "error": "player_names_out and player_names_in must have the same length",
                "out_count": len(player_names_out),
                "in_count": len(player_names_in),
            }

        auth_manager = get_auth_manager()
        team_id = auth_manager.team_id
        if not team_id:
            return {
                "error": "No team ID found in credentials",
                "setup_instructions": "Run 'fpl-mcp-config setup' to configure your FPL credentials",
            }

        # Current squad: gives us selling prices, the active event, and the bank
        try:
            my_team = await auth_manager.get_my_team(int(team_id))
        except Exception as e:
            return {"error": f"Could not fetch your squad: {e}"}

        picks = my_team.get("picks", [])
        selling_price_by_id = {p.get("element"): p.get("selling_price", 0) for p in picks}
        owned_ids = set(selling_price_by_id)

        transfers_state = my_team.get("transfers", {})
        event = transfers_state.get("event")
        if event is None:
            # Fall back to current gameweek if the my-team payload omits it
            event = await get_current_gameweek_id()
        bank = transfers_state.get("bank", 0)  # in tenths

        # Resolve names -> element IDs
        async def _resolve(name: str) -> Dict[str, Any]:
            matches = await find_players_by_name(name)
            if not matches:
                return {"error": f"No player found matching '{name}'"}
            return matches[0]

        transfers = []
        preview = []
        spend = 0  # tenths: cost of incoming
        proceeds = 0  # tenths: selling value of outgoing
        for name_out, name_in in zip(player_names_out, player_names_in):
            out_player = await _resolve(name_out)
            if "error" in out_player:
                return out_player
            in_player = await _resolve(name_in)
            if "error" in in_player:
                return in_player

            out_id = out_player["id"]
            in_id = in_player["id"]

            if out_id not in owned_ids:
                return {
                    "error": f"'{out_player['name']}' is not in your squad",
                    "suggestion": "You can only transfer out players you own",
                }

            selling_price = selling_price_by_id.get(out_id, 0)
            # find_players_by_name returns price in millions; convert to tenths
            purchase_price = int(round(float(in_player.get("price", 0)) * 10))

            spend += purchase_price
            proceeds += selling_price

            transfers.append(
                {
                    "element_in": in_id,
                    "element_out": out_id,
                    "purchase_price": purchase_price,
                    "selling_price": selling_price,
                }
            )
            preview.append(
                {
                    "out": {"name": out_player["name"], "id": out_id,
                            "selling_price": selling_price / 10.0},
                    "in": {"name": in_player["name"], "id": in_id,
                           "purchase_price": purchase_price / 10.0},
                }
            )

        net_cost = spend - proceeds  # tenths; positive means money needed from bank
        affordable = net_cost <= bank

        payload = {
            "entry": int(team_id),
            "event": event,
            "transfers": transfers,
            "chip": None,
        }

        # Dry run unless explicitly confirmed
        if not confirm:
            return {
                "status": "dry_run",
                "would_transfer": preview,
                "bank_before": bank / 10.0,
                "net_cost": net_cost / 10.0,
                "affordable": affordable,
                "event": event,
                "requires": "Call again with confirm=true to execute (IRREVERSIBLE)",
            }

        if not affordable:
            return {
                "status": "error",
                "error": "Insufficient funds for this transfer",
                "net_cost": net_cost / 10.0,
                "bank": bank / 10.0,
            }

        url = f"{FPL_API_BASE_URL}/transfers/"
        try:
            response = await auth_manager.make_authed_post(url, payload)
        except Exception as e:
            return {
                "status": "error",
                "error": f"Transfer request failed: {e}",
                "payload": payload,
            }

        return {
            "status": "ok",
            "transfers": preview,
            "net_cost": net_cost / 10.0,
            "event": event,
            "response": response,
        }

    @mcp.tool()
    async def set_captaincy(
        captain: str,
        vice_captain: Optional[str] = None,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """Set your team's captain (and optionally vice-captain) by player name.

        Resolves the given player name(s) to your squad, rebuilds the full
        pick list with the new (vice-)captain flags, and — only when
        confirm=True — POSTs it to FPL. Requires authentication.

        Both players must already be in your squad (this does not transfer
        players in). The captain and vice-captain must be two different players.

        Safety: with confirm=False (the default) this performs a DRY RUN and
        returns a preview without submitting anything. Review the preview, then
        call again with confirm=True to actually save.

        Args:
            captain: Name of the player to captain (must be in your squad)
            vice_captain: Name of the player to vice-captain (optional; kept as-is
                if omitted). Must differ from the captain.
            confirm: Must be True to actually submit the change

        Returns:
            A dry-run preview, or the result of the saved change
        """
        from ...config import FPL_API_BASE_URL
        from ..cache import get_player_map
        from ..resources.players import find_players_by_name
        from ..utils.params import unwrap

        logger.info(
            f"Tool called: set_captaincy(captain={captain}, "
            f"vice_captain={vice_captain}, confirm={confirm})"
        )

        captain = unwrap(captain, "captain", "name", default=None)
        vice_captain = unwrap(vice_captain, "vice_captain", default=None)
        confirm = unwrap(confirm, "confirm", default=confirm)

        if not captain:
            return {"error": "captain is required"}

        auth_manager = get_auth_manager()
        team_id = auth_manager.team_id
        if not team_id:
            return {
                "error": "No team ID found in credentials",
                "setup_instructions": "Run 'fpl-mcp-config setup' to configure your FPL credentials",
            }

        # Current squad: the my-team endpoint returns picks with position order
        # and the chip currently active, both of which we must echo back.
        try:
            my_team = await auth_manager.get_my_team(int(team_id))
        except Exception as e:
            return {"error": f"Could not fetch your squad: {e}"}

        picks = my_team.get("picks", [])
        if not picks:
            return {"error": "No squad found for your team"}

        owned_ids = {p.get("element") for p in picks}
        player_map = await get_player_map()

        def _name_of(pid):
            return player_map.get(pid, {}).get("web_name", f"Player {pid}")

        # Resolve captain (and vice-captain) names to element IDs
        async def _resolve(name: str) -> Dict[str, Any]:
            matches = await find_players_by_name(name)
            if not matches:
                return {"error": f"No player found matching '{name}'"}
            return matches[0]

        cap = await _resolve(captain)
        if "error" in cap:
            return cap
        captain_id = cap["id"]
        if captain_id not in owned_ids:
            return {
                "error": f"'{cap['name']}' is not in your squad",
                "suggestion": "You can only captain a player you own",
            }

        # Vice-captain: use the resolved name, or keep the current one
        if vice_captain:
            vc = await _resolve(vice_captain)
            if "error" in vc:
                return vc
            vice_id = vc["id"]
            if vice_id not in owned_ids:
                return {
                    "error": f"'{vc['name']}' is not in your squad",
                    "suggestion": "You can only vice-captain a player you own",
                }
        else:
            vice_id = next(
                (p.get("element") for p in picks if p.get("is_vice_captain")), None
            )

        if vice_id is not None and vice_id == captain_id:
            return {
                "error": "Captain and vice-captain must be different players",
                "captain": _name_of(captain_id),
            }

        # Rebuild the full picks payload, echoing position and flipping the flags.
        # FPL requires the complete 15-player list on every my-team POST.
        new_picks = []
        for p in picks:
            pid = p.get("element")
            new_picks.append(
                {
                    "element": pid,
                    "position": p.get("position"),
                    "is_captain": pid == captain_id,
                    "is_vice_captain": pid == vice_id,
                }
            )

        # Preserve any currently-active chip (e.g. Triple Captain / Bench Boost)
        active_chip = next(
            (c.get("name") for c in my_team.get("chips", [])
             if c.get("status_for_entry") == "active"),
            None,
        )

        payload = {"chip": active_chip, "picks": new_picks}

        preview = {
            "captain": _name_of(captain_id),
            "vice_captain": _name_of(vice_id) if vice_id is not None else None,
            "active_chip": active_chip,
        }

        # Dry run unless explicitly confirmed
        if not confirm:
            return {
                "status": "dry_run",
                "would_set": preview,
                "requires": "Call again with confirm=true to save",
            }

        url = f"{FPL_API_BASE_URL}/my-team/{int(team_id)}/"
        try:
            response = await auth_manager.make_authed_post(url, payload)
        except Exception as e:
            return {
                "status": "error",
                "error": f"Set captaincy request failed: {e}",
            }

        # A successful my-team POST returns an empty body; invalidate the cache
        # so a follow-up read reflects the change.
        try:
            cache.clear(f"my_team_{int(team_id)}")
        except Exception:
            pass

        return {
            "status": "ok",
            "set": preview,
            "response": response,
        }

    @mcp.tool()
    async def set_active_chip(
        chip: Optional[str] = None,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """Apply or cancel an FPL chip for the current gameweek. Requires authentication.

        Chips are toggled via the same my-team endpoint that saves your team:
        applying a chip sets it active for this gameweek; passing no chip (or
        "none"/"cancel") de-applies whatever chip is currently pending.

        Only one chip can be active per gameweek. The chip must be available on
        your account (Free Hit and Wildcard are the transfer-page chips; Bench
        Boost and Triple Captain are the pick-team chips). Your captain, vice-
        captain, and bench order are preserved unchanged.

        Safety: with confirm=False (the default) this performs a DRY RUN and
        returns a preview without submitting anything. Review the preview, then
        call again with confirm=True to actually apply/cancel.

        Args:
            chip: Chip to apply. Accepts friendly names or FPL codes:
                "bench boost"/"bboost", "triple captain"/"3xc",
                "free hit"/"freehit", "wildcard". Pass None, "none", or
                "cancel" to de-apply the current chip.
            confirm: Must be True to actually submit the change

        Returns:
            A dry-run preview, or the result of the applied/cancelled chip
        """
        from ...config import FPL_API_BASE_URL
        from ..utils.params import unwrap

        logger.info(f"Tool called: set_active_chip(chip={chip}, confirm={confirm})")

        chip = unwrap(chip, "chip", "name", default=None)
        confirm = unwrap(confirm, "confirm", default=confirm)

        # Normalize the chip name to the FPL internal code (or None to cancel)
        _CHIP_CODES = {
            "bboost": "bboost", "bench boost": "bboost", "benchboost": "bboost",
            "bb": "bboost",
            "3xc": "3xc", "triple captain": "3xc", "triplecaptain": "3xc",
            "tc": "3xc", "3c": "3xc",
            "freehit": "freehit", "free hit": "freehit", "fh": "freehit",
            "wildcard": "wildcard", "wc": "wildcard",
        }
        _CANCEL_TOKENS = {None, "", "none", "null", "cancel", "off", "remove"}

        raw = chip.strip().lower() if isinstance(chip, str) else chip
        if raw in _CANCEL_TOKENS:
            chip_code = None
            action = "cancel"
        else:
            chip_code = _CHIP_CODES.get(raw)
            if chip_code is None:
                return {
                    "error": f"Unknown chip '{chip}'",
                    "valid_chips": ["bench boost", "triple captain", "free hit", "wildcard"],
                    "note": "Pass no chip (or 'cancel') to de-apply the current chip.",
                }
            action = "apply"

        auth_manager = get_auth_manager()
        team_id = auth_manager.team_id
        if not team_id:
            return {
                "error": "No team ID found in credentials",
                "setup_instructions": "Run 'fpl-mcp-config setup' to configure your FPL credentials",
            }

        try:
            my_team = await auth_manager.get_my_team(int(team_id))
        except Exception as e:
            return {"error": f"Could not fetch your squad: {e}"}

        picks = my_team.get("picks", [])
        if not picks:
            return {"error": "No squad found for your team"}

        # Availability check against the account's chip list, when applying
        chips_info = my_team.get("chips", [])
        available = {
            c.get("name"): c.get("status_for_entry") for c in chips_info
        }
        currently_active = next(
            (c.get("name") for c in chips_info if c.get("status_for_entry") == "active"),
            None,
        )

        if action == "apply":
            status = available.get(chip_code)
            if status is not None and status not in ("available", "active"):
                return {
                    "error": f"Chip '{chip_code}' is not available (status: {status})",
                    "chips": available,
                }
        else:  # cancel
            if currently_active is None:
                return {
                    "status": "noop",
                    "message": "No chip is currently active to cancel.",
                    "chips": available,
                }

        # Rebuild the full picks payload, preserving position and captaincy.
        new_picks = [
            {
                "element": p.get("element"),
                "position": p.get("position"),
                "is_captain": bool(p.get("is_captain")),
                "is_vice_captain": bool(p.get("is_vice_captain")),
            }
            for p in picks
        ]

        payload = {"chip": chip_code, "picks": new_picks}

        preview = {
            "action": action,
            "chip": chip_code,
            "previously_active": currently_active,
        }

        # Dry run unless explicitly confirmed
        if not confirm:
            return {
                "status": "dry_run",
                "would": preview,
                "requires": "Call again with confirm=true to submit",
            }

        url = f"{FPL_API_BASE_URL}/my-team/{int(team_id)}/"
        try:
            response = await auth_manager.make_authed_post(url, payload)
        except Exception as e:
            return {
                "status": "error",
                "error": f"Chip request failed: {e}",
            }

        # Invalidate the cached my-team read so a follow-up reflects the change.
        try:
            cache.clear(f"my_team_{int(team_id)}")
        except Exception:
            pass

        return {
            "status": "ok",
            "result": preview,
            "response": response,
        }

    @mcp.tool()
    async def substitute_players(
        player_out: str,
        player_in: str,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """Swap a starter with a bench player (or reorder the bench) by name.

        FPL encodes your lineup with a position number per player: 1-11 are the
        starting XI, 12 is the reserve goalkeeper, and 13-15 are the outfield
        bench in substitution order. This tool swaps the two named players'
        positions, preserving everyone else, plus captain/vice-captain and any
        active chip.

        Use it to (a) bring a bench player into the XI in place of a starter,
        (b) send a starter to the bench, or (c) reorder two bench players. The
        two players must be swappable: a goalkeeper can only be exchanged with
        the other goalkeeper, and the resulting XI must be a legal formation
        (1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD).

        Safety: with confirm=False (the default) this performs a DRY RUN and
        returns a preview without submitting anything. Review the preview, then
        call again with confirm=True to actually save.

        Args:
            player_out: Name of the player leaving their current slot (partial
                match accepted)
            player_in: Name of the player taking that slot (partial match accepted)
            confirm: Must be True to actually submit the change

        Returns:
            A dry-run preview, or the result of the saved lineup
        """
        from ...config import FPL_API_BASE_URL
        from ..cache import get_player_map
        from ..resources.players import find_players_by_name
        from ..utils.params import unwrap

        logger.info(
            f"Tool called: substitute_players(out={player_out}, "
            f"in={player_in}, confirm={confirm})"
        )

        player_out = unwrap(player_out, "player_out", "out", default=None)
        player_in = unwrap(player_in, "player_in", "in", default=None)
        confirm = unwrap(confirm, "confirm", default=confirm)

        if not player_out or not player_in:
            return {"error": "Both player_out and player_in are required"}

        auth_manager = get_auth_manager()
        team_id = auth_manager.team_id
        if not team_id:
            return {
                "error": "No team ID found in credentials",
                "setup_instructions": "Run 'fpl-mcp-config setup' to configure your FPL credentials",
            }

        try:
            my_team = await auth_manager.get_my_team(int(team_id))
        except Exception as e:
            return {"error": f"Could not fetch your squad: {e}"}

        picks = my_team.get("picks", [])
        if not picks:
            return {"error": "No squad found for your team"}

        player_map = await get_player_map()
        by_id = {p.get("element"): p for p in picks}

        def _name_of(pid):
            return player_map.get(pid, {}).get("web_name", f"Player {pid}")

        # Resolve both names to squad members
        async def _resolve(name: str) -> Dict[str, Any]:
            matches = await find_players_by_name(name)
            if not matches:
                return {"error": f"No player found matching '{name}'"}
            pid = matches[0]["id"]
            if pid not in by_id:
                return {
                    "error": f"'{matches[0]['name']}' is not in your squad",
                    "suggestion": "You can only substitute players you own",
                }
            return {"id": pid, "name": matches[0]["name"]}

        out_r = await _resolve(player_out)
        if "error" in out_r:
            return out_r
        in_r = await _resolve(player_in)
        if "error" in in_r:
            return in_r

        out_id, in_id = out_r["id"], in_r["id"]
        if out_id == in_id:
            return {"error": "player_out and player_in must be different players"}

        out_pos = by_id[out_id].get("position")
        in_pos = by_id[in_id].get("position")

        # Goalkeeper rule: a GK (element_type 1) can only swap with a GK.
        out_type = player_map.get(out_id, {}).get("element_type")
        in_type = player_map.get(in_id, {}).get("element_type")
        if (out_type == 1) != (in_type == 1):
            return {
                "error": "A goalkeeper can only be swapped with the other goalkeeper",
                "player_out": {"name": _name_of(out_id), "is_gk": out_type == 1},
                "player_in": {"name": _name_of(in_id), "is_gk": in_type == 1},
            }

        # Build the new picks list with the two positions swapped.
        new_picks = []
        for p in picks:
            pid = p.get("element")
            pos = p.get("position")
            if pid == out_id:
                pos = in_pos
            elif pid == in_id:
                pos = out_pos
            new_picks.append(
                {
                    "element": pid,
                    "position": pos,
                    "is_captain": bool(p.get("is_captain")),
                    "is_vice_captain": bool(p.get("is_vice_captain")),
                }
            )

        # Validate the resulting starting XI (positions 1-11) forms a legal
        # formation. FPL: exactly 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD.
        counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for p in new_picks:
            if p["position"] <= 11:
                etype = player_map.get(p["element"], {}).get("element_type")
                if etype in counts:
                    counts[etype] += 1
        legal = (
            counts[1] == 1
            and 3 <= counts[2] <= 5
            and 2 <= counts[3] <= 5
            and 1 <= counts[4] <= 3
        )
        formation = {
            "GKP": counts[1], "DEF": counts[2], "MID": counts[3], "FWD": counts[4],
        }
        if not legal:
            return {
                "error": "That swap would produce an illegal formation",
                "resulting_xi": formation,
                "rule": "Starting XI must be 1 GKP, 3-5 DEF, 2-5 MID, 1-3 FWD",
            }

        # Preserve any currently-active chip
        active_chip = next(
            (c.get("name") for c in my_team.get("chips", [])
             if c.get("status_for_entry") == "active"),
            None,
        )
        payload = {"chip": active_chip, "picks": new_picks}

        def _slot(pos):
            if pos <= 11:
                return f"starting XI (slot {pos})"
            if pos == 12:
                return "bench (reserve GK)"
            return f"bench (sub {pos - 12})"

        preview = {
            "player_out": {"name": _name_of(out_id), "from": _slot(out_pos), "to": _slot(in_pos)},
            "player_in": {"name": _name_of(in_id), "from": _slot(in_pos), "to": _slot(out_pos)},
            "resulting_formation": formation,
            "active_chip": active_chip,
        }

        # Dry run unless explicitly confirmed
        if not confirm:
            return {
                "status": "dry_run",
                "would_swap": preview,
                "requires": "Call again with confirm=true to save",
            }

        url = f"{FPL_API_BASE_URL}/my-team/{int(team_id)}/"
        try:
            response = await auth_manager.make_authed_post(url, payload)
        except Exception as e:
            return {
                "status": "error",
                "error": f"Substitution request failed: {e}",
            }

        try:
            cache.clear(f"my_team_{int(team_id)}")
        except Exception:
            pass

        return {
            "status": "ok",
            "swapped": preview,
            "response": response,
        }