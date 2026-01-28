# utils/stats_refresh.py
"""Helper functions for extracting and replacing stats sections in markdown reports."""

import re


def extract_stats_sections(markdown: str) -> tuple[str | None, str | None]:
    """
    Extract Season snapshot and Last 3 games sections from markdown.
    Only returns the section heading and its content, nothing else.
    
    Returns:
        (season_snapshot_section, last_3_games_section) or (None, None) if not found
    """
    # Match from "### Season snapshot" to the next "###" heading (non-greedy)
    season_pattern = r'(### Season snapshot\s*\n.*?)(?=\n###|\Z)'
    season_match = re.search(season_pattern, markdown, re.DOTALL)
    season_section = season_match.group(1).strip() if season_match else None
    
    # Match from "### Last 3 games" to the next "###" heading (non-greedy)
    games_pattern = r'(### Last 3 games\s*\n.*?)(?=\n###|\Z)'
    games_match = re.search(games_pattern, markdown, re.DOTALL)
    games_section = games_match.group(1).strip() if games_match else None
    
    return season_section, games_section


def replace_stats_sections(
    original_markdown: str,
    fresh_stats_markdown: str
) -> str:
    """
    Replace Season snapshot and Last 3 games sections in original markdown
    with fresh versions from stats-refresh LLM output.
    
    Args:
        original_markdown: Full cached report
        fresh_stats_markdown: LLM output with only updated stats sections
    
    Returns:
        Updated markdown with fresh stats
    """
    # Extract fresh sections
    fresh_season, fresh_games = extract_stats_sections(fresh_stats_markdown)
    
    if not fresh_season or not fresh_games:
        # If extraction failed, return original unchanged
        return original_markdown
    
    result = original_markdown
    
    # Replace Season snapshot section
    # Pattern: "### Season snapshot" followed by anything until the next "###" heading (or end of string)
    season_pattern = r'### Season snapshot\s*\n.*?(?=\n###|\Z)'
    season_before = result
    result = re.sub(season_pattern, fresh_season, result, count=1, flags=re.DOTALL)
    
    # Verify replacement actually happened to avoid duplicates
    if result == season_before:
        # Fallback: try without the trailing content match (in case of malformed markdown)
        season_pattern = r'### Season snapshot[^\n]*\n.*?(?=\n###|\Z)'
        result = re.sub(season_pattern, fresh_season, result, count=1, flags=re.DOTALL)
    
    # Replace Last 3 games section
    # Pattern: "### Last 3 games" followed by anything until the next "###" heading (or end of string)
    games_pattern = r'### Last 3 games\s*\n.*?(?=\n###|\Z)'
    games_before = result
    result = re.sub(games_pattern, fresh_games, result, count=1, flags=re.DOTALL)
    
    # Verify replacement actually happened to avoid duplicates
    if result == games_before:
        # Fallback: try without the trailing content match (in case of malformed markdown)
        games_pattern = r'### Last 3 games[^\n]*\n.*?(?=\n###|\Z)'
        result = re.sub(games_pattern, fresh_games, result, count=1, flags=re.DOTALL)
    
    return result
