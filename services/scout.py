# services/scout.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from db import PROMPT_VERSION, init_db, make_query_key, find_report_by_query_key, update_report_by_id, spend_credits
from utils.parse import (
    extract_display_md,
    extract_grades,
    extract_info_fields,
    extract_last3_games,
    extract_season_snapshot,
)
from utils.render import md_to_safe_html
from utils.stats_refresh import replace_stats_sections
from utils.prompts import load_text_prompt
import time

logger = logging.getLogger(__name__)


def _build_payload_from_report(
    *,
    report_md: str,
    player: str,
    team: str,
    league: str,
    season: str,
    model: str,
    use_web: bool,
    cached: bool,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    info_fields = extract_info_fields(report_md)
    grades, final_verdict = extract_grades(report_md)
    # Measure parse stages
    import time
    try:
        from utils.metrics import record_timing
    except Exception:
        record_timing = None

    t_parse_start = time.time()
    season_snapshot = extract_season_snapshot(report_md)
    last3_games = extract_last3_games(report_md)
    t_parse_ms = (time.time() - t_parse_start) * 1000.0
    try:
        if record_timing:
            try:
                record_timing("parse_md_ms", t_parse_ms)
            except Exception:
                pass
    except Exception:
        pass

    t_display_start = time.time()
    display_md = extract_display_md(report_md)
    t_display_ms = (time.time() - t_display_start) * 1000.0
    try:
        if record_timing:
            try:
                record_timing("display_extract_ms", t_display_ms)
            except Exception:
                pass
    except Exception:
        pass

    payload: Dict[str, Any] = {
        "player": player,
        "team": team or "",
        "league": league or "",
        "season": season or "",
        "model": model or "",
        "use_web": bool(use_web),
        "prompt_version": PROMPT_VERSION,
        "cached": bool(cached),
        # canonical
        "report_md": report_md,
        # display-only (no title/header/grades/sources/tables)
        "report_html": md_to_safe_html(display_md),
        # structured for UI
        "info_fields": info_fields,
        "grades": grades,
        "final_verdict": final_verdict,
        "season_snapshot": season_snapshot,
        "last3_games": last3_games,
    }
    # Measure HTML rendering cost
    try:
        t_render_start = time.time()
        payload["report_html"] = md_to_safe_html(display_md)
        t_render_ms = (time.time() - t_render_start) * 1000.0
        if record_timing:
            try:
                record_timing("render_html_ms", t_render_ms)
            except Exception:
                pass
    except Exception:
        # fallback if rendering fails
        payload["report_html"] = ""
        try:
            if record_timing:
                record_timing("render_html_ms", 0.0)
        except Exception:
            pass
    if created_at:
        payload["created_at"] = created_at
    return payload


def get_or_generate_scout_report(
    *,
    client,
    model: str,
    scout_instructions: str,
    player: str,
    team: str,
    league: str,
    season: str,
    use_web: bool,
    refresh: bool,
    user_id: str = None,
) -> Dict[str, Any]:
    player = (player or "").strip()
    team = (team or "").strip()
    league = (league or "").strip()
    season = (season or "").strip()

    if not refresh:
        # Build query key to look up cached report
        query_obj = {
            "player": player,
            "team": team,
            "league": league,
            "season": season,
            "use_web": use_web,
        }
        query_key = make_query_key(query_obj)
        # Try to find cached report (requires user_id for library lookup)
        if user_id:
            cached_row = find_report_by_query_key(user_id, query_key)
            if cached_row:
                report_md = cached_row.get("report_md") or ""
                cached_id = cached_row.get("id")
                updated_at_str = cached_row.get("updated_at")
                
                logger.info(f"Found cached report for {player}, updated_at={updated_at_str}")
                
                # Check if stats are stale (>24 hours since last update)
                needs_stats_refresh = False
                if updated_at_str:
                    try:
                        updated_at = datetime.fromisoformat(updated_at_str.replace('Z', '+00:00'))
                        age = datetime.now(timezone.utc) - updated_at
                        logger.info(f"Report age: {age.total_seconds():.1f} seconds")
                        if age > timedelta(hours=24):
                            needs_stats_refresh = True
                            logger.info(f"Stats are stale (>24h), triggering refresh")
                        else:
                            logger.info(f"Stats are fresh, serving cached version")
                    except Exception as e:
                        logger.warning(f"Failed to parse updated_at: {e}")
                        pass
                
                # If stats are stale, refresh them with a lightweight LLM call
                if needs_stats_refresh and client and use_web:
                    logger.info(f"Starting stats refresh for {player}")
                    try:
                        # Charge 1 credit for stats refresh; if insufficient, serve stale
                        try:
                            spend_credits(
                                user_id,
                                1,
                                reason="stats_refresh",
                                source_type="stats_refresh",
                                source_id=f"stats_refresh_{cached_id}_{int(time.time())}",
                            )
                        except ValueError as e:
                            if "INSUFFICIENT_CREDITS" in str(e):
                                logger.warning("Stats refresh skipped due to insufficient credits")
                                raise
                            raise
                        except Exception as e:
                            logger.error(f"Failed to charge credit for stats refresh: {e}")
                            raise

                        # Load stats-refresh prompt template
                        stats_prompt_template = load_text_prompt("prompts/stats_refresh.txt")
                        stats_user_prompt = stats_prompt_template.format(
                            player_name=cached_row.get("player_name") or player,
                            last_updated=updated_at_str or "unknown"
                        )
                        
                        logger.info(f"Calling LLM for stats refresh (model={model})")
                        # Call LLM for stats only (much cheaper than full report)
                        tools = [{"type": "web_search"}]
                        stats_resp = client.responses.create(
                            model=model,
                            instructions="",
                            input=stats_user_prompt,
                            tools=tools,
                        )
                        fresh_stats_md = stats_resp.output_text or ""
                        logger.info(f"LLM stats refresh returned {len(fresh_stats_md)} chars")
                        
                        # Replace only stats sections in cached report
                        updated_report_md = replace_stats_sections(report_md, fresh_stats_md)
                        
                        # Update the cached report with fresh stats
                        if updated_report_md != report_md:
                            logger.info(f"Stats sections changed, updating cache (report_id={cached_id})")
                            try:
                                # Rebuild payload from the original cached_row
                                payload_obj = cached_row.get("payload") or {}
                                update_report_by_id(
                                    user_id=user_id,
                                    report_id=cached_id,
                                    player_name=cached_row.get("player_name") or player,
                                    report_md=updated_report_md,
                                    payload=payload_obj,
                                    cached=True,
                                )
                                report_md = updated_report_md
                                logger.info(f"Cache updated successfully for report_id={cached_id}")
                            except Exception as e:
                                logger.error(f"Failed to update cache: {e}")
                                # If update fails, serve stale version
                                pass
                        else:
                            logger.info("Stats sections unchanged after refresh")
                    except Exception as e:
                        logger.error(f"Stats refresh failed: {e}")
                        # If stats refresh fails, serve stale version
                        pass
                
                payload = _build_payload_from_report(
                    report_md=report_md,
                    player=cached_row.get("player_name") or player,
                    team=cached_row.get("team") or "",
                    league=cached_row.get("league") or "",
                    season=cached_row.get("season") or "",
                    model=cached_row.get("model") or "",
                    use_web=bool(cached_row.get("use_web")),
                    cached=True,
                    created_at=cached_row.get("created_at"),
                )
                # Mark if stats were just refreshed
                if needs_stats_refresh:
                    payload["stats_refreshed"] = True
                return payload

    user_prompt = f"""
Player: {player}

Provided team (may be blank): {team if team else "(not provided)"}
Provided league (may be blank): {league if league else "(not provided)"}
Provided season (may be blank): {season if season else "(not provided)"}

Write the scouting report now.
""".strip()

    tools = [{"type": "web_search"}] if use_web else []

    from utils.metrics import increment_metric

    try:
        increment_metric("llm_calls")
    except Exception:
        pass
    # Measure LLM response time and overall scout pipeline time
    pipeline_start = time.time()
    resp_start = time.time()
    resp = client.responses.create(
        model=model,
        instructions=scout_instructions,
        input=user_prompt,
        tools=tools,
    )
    resp_elapsed_ms = (time.time() - resp_start) * 1000.0
    try:
        from utils.metrics import record_timing

        try:
            record_timing("llm_response_ms", resp_elapsed_ms)
        except Exception:
            pass
    except Exception:
        pass
    report_md = resp.output_text or ""
    
    # Capture usage information from the API response
    usage_data = {}
    try:
        if hasattr(resp, 'usage') and resp.usage:
            usage_data = {
                "input_tokens": getattr(resp.usage, 'input_tokens', 0),
                "output_tokens": getattr(resp.usage, 'output_tokens', 0),
            }
    except Exception:
        pass

    try:
        # count generated-success events when model returns non-empty output
        if report_md and len(report_md) > 0:
            try:
                increment_metric("llm_success")
            except Exception:
                pass
            try:
                # Track generation event
                from utils.analytics import track_event

                track_event(
                    user_id,
                    "report_generated",
                    {
                        "player": player,
                        "team": team,
                        "league": league,
                        "season": season,
                        "use_web": bool(use_web),
                        "cached": False,
                        "model": model,
                    },
                )
            except Exception:
                pass
    except Exception:
        pass

    # Do not persist sentinel responses where the model explicitly says the
    # player could not be verified. Persistence is handled by the caller
    # (`app.py`) so we avoid double-writing the local cache here.
    # We simply return the generated markdown and a payload; the app will
    # perform the authoritative upsert into Postgres and the local SQLite
    # cache (dual-write).

    # Build payload using canonical name when available so the UI and
    # subsequent fuzzy lookups use the correct canonical form rather than
    # the user's typed variant.
    # Derive canonical player name for the returned payload (best-effort)
    from utils.parse import extract_canonical_player

    canonical_player = extract_canonical_player(report_md) or ""
    pipeline_elapsed_ms = (time.time() - pipeline_start) * 1000.0
    try:
        from utils.metrics import record_timing

        try:
            record_timing("scout_pipeline_ms", pipeline_elapsed_ms)
        except Exception:
            pass
    except Exception:
        pass
    
    # Build payload and include usage data
    payload = _build_payload_from_report(
        report_md=report_md,
        player=canonical_player if canonical_player else player,
        team=team,
        league=league,
        season=season,
        model=model,
        use_web=use_web,
        cached=False,
    )
    
    # Add usage data to the payload for cost tracking
    if usage_data:
        payload["usage"] = usage_data
    
    return payload
