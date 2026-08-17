"""Guard the public MCP surface: the __main__.py split must not change
which tools are registered — clients bind to tool names."""

import pytest

EXPECTED_TOOLS = {
    # team.py
    "get_team",
    "get_my_team",
    "get_manager",
    "check_fpl_authentication",
    "update_fpl_credentials",
    # managers.py
    "get_manager_info",
    # leagues.py
    "get_league_standings",
    "get_league_analytics",
    # players.py
    "get_player_information",
    "search_fpl_players",
    # gameweeks.py
    "get_gameweek_status",
    "get_blank_gameweeks",
    "get_double_gameweeks",
    # fixtures.py
    "analyze_player_fixtures",
    "analyze_fixtures",
    # analysis.py
    "analyze_players",
    "compare_players",
    # live.py
    "get_gameweek_live_scores",
    "get_dream_team",
    # advice.py
    "suggest_captain",
    # new additions to team.py / managers.py / players.py
    "get_my_current_team",
    "get_manager_transfer_history",
    "get_price_changes",
    # ported from fpl-mcp-server: make_transfers (team.py)
    "make_transfers",
    # reverse-engineered from the my-team endpoint: set captain / vice-captain
    "set_captaincy",
    # reverse-engineered from the my-team endpoint: apply / cancel a chip
    "set_active_chip",
    # reverse-engineered from the my-team endpoint: bench / lineup substitution
    "substitute_players",
    # ported: injuries.py (RotoWire lineup predictions)
    "get_injury_and_lineup_predictions",
    "get_players_to_avoid",
    "check_player_availability",
    # ported: strategy.py (authenticated squad advisors)
    "analyze_squad_recent_performance",
    "recommend_transfers",
    "recommend_chip_strategy",
    # ported: name-based league/manager tools (leagues.py)
    "get_league_standings_by_name",
    "get_manager_gameweek_team",
    "compare_managers",
}

EXPECTED_PROMPTS = {
    "transfer_advice_prompt",
    "player_analysis_prompt",
    "team_rating_prompt",
    "differential_players_prompt",
    "chip_strategy_prompt",
}


async def test_all_tools_registered():
    from fpl_mcp.__main__ import mcp

    tools = await mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_all_prompts_registered():
    from fpl_mcp.__main__ import mcp

    prompts = await mcp.list_prompts()
    assert {p.name for p in prompts} == EXPECTED_PROMPTS


async def test_all_resources_registered():
    from fpl_mcp.__main__ import mcp

    resources = await mcp.list_resources()
    templates = await mcp.list_resource_templates()
    uris = {str(r.uri) for r in resources} | {t.uriTemplate for t in templates}

    assert uris == {
        "fpl://static/players",
        "fpl://static/players/{name}",
        "fpl://static/teams",
        "fpl://static/teams/{name}",
        "fpl://gameweeks/current",
        "fpl://gameweeks/all",
        "fpl://gameweeks/blank",
        "fpl://gameweeks/double",
        "fpl://fixtures",
        "fpl://fixtures/gameweek/{gameweek_id}",
        "fpl://fixtures/team/{team_name}",
        "fpl://players/{player_name}/fixtures",
    }
