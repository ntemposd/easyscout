"""
services/reports.py

Report Management Service 
=========================

This service handles all report-related operations including:
- Report generation orchestration (/api/scout)
- Suggestion acceptance workflow
- Library management and deduplication
- Similarity matching (fuzzy + embeddings)
- Stats refresh prompts and updates
- Credit management for report operations

Note: The actual LLM generation logic lives in services/scout.py.
This service orchestrates the full workflow: auth, credits, caching, 
similarity matching, persistence, and analytics.

Routes Registered:
- POST /api/scout - Main report generation endpoint (requires auth, costs 1 credit)
- POST /api/save_suggestion - Save suggested report to library (free operation)
"""

import logging
import time
import uuid
import json
from datetime import datetime, timezone, timedelta
from flask import jsonify, request

# Import database operations
from db import (
    _get_pool,
    find_report_by_query_key,
    get_balance,
    get_report,
    get_report_by_id,
    insert_report,
    make_query_key,
    refund_credits,
    spend_credits,
    update_report_by_id,
    insert_cost_tracking,
)
import db

# Import Scout LLM generation service
from services.scout import get_or_generate_scout_report

# Import utilities
from utils.parse import (
    extract_display_md,
    extract_grades,
    extract_info_fields,
    extract_last3_games,
    extract_season_snapshot,
    _split_height_weight,
    extract_canonical_player,
)
from utils.render import md_to_safe_html, ensure_parsed_payload
from utils.normalize import normalize_name
from utils.stats_refresh import replace_stats_sections
from utils.prompts import load_text_prompt
from utils.metrics import increment_metric
from utils.name_matching import names_match

# Import similarity matching helpers
from utils.similarity_matching import (
    _best_similar_report,
    _find_by_embedding_similarity,
)
from utils.payload_handler import fetch_report_payload
from utils.analytics import track_event
from utils.cost_pricing import estimate_cost, get_model_prices

# Import embeddings
from utils.embeddings import embed_text, store_embedding

logger = logging.getLogger(__name__)


# ============================================================================
# HELPER FUNCTIONS (Module-level for clarity)
# ============================================================================

def _canonical_player_name(name: str) -> str:
    """Canonicalize player name for deduplication using normalization.
    
    Normalizes the name and removes extra whitespace to create a consistent
    canonical form for matching across the database.
    
    Args:
        name: Raw player name
        
    Returns:
        Normalized player name with extra whitespace removed
    """
    norm = normalize_name(name, transliterate=True)
    parts = [p for p in norm.split() if p]
    return " ".join(parts)


def _handle_suggestion_acceptance(user_id: int, suggestion_report_id: int, player: str, team: str, league: str, season: str):
    """
    Handle workflow when user accepts a suggested match.
    
    Charges 1 credit only if it's a new cross-user report.
    Returns FREE if user already owns the report or if it's their own report.
    
    Returns:
        tuple: (response_dict, status_code) or None if not applicable
    """
    logger.info(f"[ACCEPT_SUGGESTION] Starting for report_id={suggestion_report_id}, player='{player}'")
    
    # Fetch the source report from database
    logger.info(f"[ACCEPT_SUGGESTION] Fetching source report from Postgres...")
    source_report = None
    try:
        source_report = get_report_by_id(suggestion_report_id)
        logger.info(f"[ACCEPT_SUGGESTION] Postgres fetch: {'SUCCESS' if source_report else 'NOT_FOUND'}")
    except Exception:
        source_report = None

    if not source_report:
        logger.error(f"[ACCEPT_SUGGESTION] Report {suggestion_report_id} not found")
        return ({"error": "Suggested report not found"}, 404)
    
    # If the suggestion points to a report owned by the same user,
    # do NOT charge — just return the existing report for FREE
    try:
        source_owner = source_report.get("source_user_id")
    except Exception:
        source_owner = None
    
    if source_owner and str(source_owner) == str(user_id):
        logger.info(
            f"[ACCEPT_SUGGESTION] Same-user source (user_id={user_id}, report_id={suggestion_report_id}) → returning FREE"
        )
        existing_payload = dict(source_report)
        try:
            payload = ensure_parsed_payload(existing_payload)
        except Exception:
            payload = existing_payload
        payload["cached"] = True
        payload["report_id"] = suggestion_report_id
        payload["library_id"] = suggestion_report_id
        payload["credits_remaining"] = get_balance(user_id)
        
        # Ensure HTML is present
        try:
            display_md = extract_display_md(payload.get("report_md", "") or "")
            payload["report_html"] = md_to_safe_html(display_md)
        except Exception:
            payload.setdefault("report_html", "")
        return (payload, 200)
    
    # Check if user already has a report with the SOURCE report's canonical name
    logger.info(f"[ACCEPT_SUGGESTION] Checking if user already has this report...")
    source_player_name = source_report.get("player", "")

    # Use SOURCE report's player name (the correct one without typos)
    canonical_query_player = _canonical_player_name(source_player_name)
    logger.info(f"[ACCEPT_SUGGESTION] Checking for existing report with canonical_player='{canonical_query_player}'")
    
    existing_query_obj = {
        "player": canonical_query_player,
        "team": team,
        "league": league,
        "season": season,
        "use_web": True,
    }
    existing_query_key = make_query_key(existing_query_obj)
    existing_by_key = find_report_by_query_key(user_id, existing_query_key)
    
    if existing_by_key:
        # User already has this report (by canonical name) — return FREE without charging
        logger.info(f"[ACCEPT_SUGGESTION] User already has this report (id={existing_by_key.get('id')}) → returning FREE")
        existing_payload = existing_by_key.get("payload") or {}
        existing_payload["report_md"] = existing_by_key.get("report_md") or existing_payload.get("report_md", "")
        try:
            payload = ensure_parsed_payload(existing_payload)
        except Exception:
            payload = existing_payload
        
        payload["cached"] = True
        payload["created_at"] = existing_by_key.get("created_at")
        payload["report_id"] = existing_by_key.get("id")
        payload["library_id"] = existing_by_key.get("id")
        payload["credits_remaining"] = get_balance(user_id)
        
        # Ensure HTML is present
        try:
            display_md = extract_display_md(existing_payload.get("report_md", "") or "")
            payload["report_html"] = md_to_safe_html(display_md)
        except Exception:
            payload.setdefault("report_html", "")
        
        return (payload, 200)
    
    # No existing report with this canonical name — charge 1 credit and save as new
    logger.info(f"[ACCEPT_SUGGESTION] User doesn't have this report → charging 1 credit...")
    try:
        new_balance = spend_credits(
            user_id,
            1,
            reason="accept_suggestion",
            source_type="scout_request",
            source_id=f"accept_suggestion_{suggestion_report_id}",
        )
    except ValueError as e:
        if "INSUFFICIENT_CREDITS" in str(e):
            return ({"error": "Insufficient credits"}, 402)
        raise
    
    # Save the suggestion to the current user's library
    try:
        source_payload = source_report or {}
        source_md = source_payload.get("report_md", "")
        
        # Prepare payload for insertion - use SOURCE report's proper name
        payload = dict(source_payload)
        payload["cached"] = False  # User paid 1 credit
        payload["report_md"] = source_md
        source_player_name = source_report.get("player") or player
        payload["player"] = source_player_name
        payload["player_name"] = source_player_name
        payload["team"] = team
        
        # Parse structured fields from markdown if missing
        try:
            payload = ensure_parsed_payload(payload)
        except Exception:
            pass
        
        insert_query_obj = {
            "player": canonical_query_player,
            "team": team,
            "league": league,
            "season": season,
            "use_web": True,
        }
        
        pg_id = insert_report(
            user_id=user_id,
            player_name=source_player_name,
            query_obj=insert_query_obj,
            report_md=source_md,
            payload=payload,
            cached=False,
        )
        
        # Fetch the newly saved report to get the fresh created_at timestamp
        try:
            saved_report = get_report(user_id, int(pg_id))
            if saved_report and saved_report.get("created_at"):
                payload["created_at"] = saved_report["created_at"]
        except Exception as e:
            logger.warning(f"Failed to fetch created_at for saved suggestion: {e}")
            payload["created_at"] = source_report.get("created_at")
        
        # Update payload with IDs and credits for return
        payload["report_id"] = pg_id
        payload["library_id"] = pg_id
        payload["credits_remaining"] = new_balance
        
        # Ensure HTML is present
        try:
            display_md = extract_display_md(source_md or "")
            payload["report_html"] = md_to_safe_html(display_md)
        except Exception:
            payload.setdefault("report_html", "")
        
        return (payload, 200)
    except Exception as e:
        logger.error("Failed to save accepted suggestion: %s", e)
        # Refund the credit on failure
        try:
            refund_credits(
                user_id,
                1,
                reason="refund_suggestion_save_failed",
                source_type="scout_request_refund",
                source_id=f"accept_suggestion_{suggestion_report_id}:refund",
            )
        except Exception:
            pass
        return ({"error": f"Failed to save suggestion: {str(e)}"}, 500)


def _check_user_library(user_id: int, player: str, team: str, league: str, season: str, use_web: bool):
    """
    Check if user already has a report for this player (using canonical name matching).
    
    Uses normalize_name() and names_match() utils to handle typos, transliterations, and nicknames.
    
    Returns:
        tuple: (existing_report_dict or None, canonical_player_name, query_key, query_obj)
    """
    canonical_query_player = _canonical_player_name(player)
    logger.info(f"[FLOW] Canonical player='{canonical_query_player}'")

    # Build query object for library lookup
    query_obj = {
        "player": canonical_query_player,
        "team": team,
        "use_web": True,  # Always True since server generates with web search
    }
    query_key = make_query_key(query_obj)

    # Check if user already has ANY report for this canonical player name
    logger.info("[FLOW] Player-name check start")
    player_only_check = None
    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, payload, report_md, player_name, created_at, updated_at, cached, query
                FROM public.reports
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (user_id,),
            )
            rows = cur.fetchall()
            
            for row in rows:
                rid, payload, report_md, player_name, created_at, updated_at, cached, query_json = row
                try:
                    query_dict = json.loads(query_json) if isinstance(query_json, str) else query_json
                    existing_canonical = query_dict.get("player", "").strip()
                    
                    if existing_canonical and names_match(canonical_query_player, existing_canonical):
                        player_only_check = {
                            "id": int(rid),
                            "payload": payload,
                            "report_md": report_md or "",
                            "player_name": player_name or "",
                            "created_at": created_at.isoformat() if created_at else None,
                            "updated_at": updated_at.isoformat() if updated_at else (created_at.isoformat() if created_at else None),
                            "cached": bool(cached),
                        }
                        logger.info(f"[PLAYER CHECK] Found existing report for '{canonical_query_player}' → matched '{existing_canonical}' (id={rid})")
                        break
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"[PLAYER CHECK] Lookup failed: {e}")
    
    existing = player_only_check if player_only_check else find_report_by_query_key(user_id, query_key)
    logger.info(f"[FLOW] Player-name check: {'HIT' if player_only_check else 'MISS'}")
    logger.info(f"[MATCH] Query key lookup: {'HIT' if existing else 'MISS'}")
    
    return existing, canonical_query_player, query_key, query_obj


def _handle_cached_report(user_id: int, existing: dict, player: str, refresh_stats: bool, client, model: str):
    """
    Handle cached report workflow: parse fields, check staleness, optionally refresh stats.
    
    Returns:
        tuple: (response_dict, status_code)
    """
    owned_payload = existing.get("payload") or {}
    owned_payload["report_md"] = existing.get("report_md") or owned_payload.get("report_md", "")

    try:
        display_md = extract_display_md(owned_payload.get("report_md", "") or "")
        owned_payload["report_html"] = md_to_safe_html(display_md)
    except Exception:
        owned_payload.setdefault("report_html", "")

    try:
        report_md_local = owned_payload.get("report_md", "") or ""
        
        if not owned_payload.get("info_fields"):
            try:
                owned_payload["info_fields"] = extract_info_fields(report_md_local)
            except Exception:
                owned_payload["info_fields"] = {}
        
        try:
            _split_height_weight(owned_payload.get("info_fields", {}))
        except Exception:
            pass

        if not owned_payload.get("grades"):
            try:
                grades_local, final_verdict_local = extract_grades(report_md_local)
                owned_payload["grades"] = grades_local
                owned_payload["final_verdict"] = final_verdict_local
            except Exception:
                owned_payload["grades"] = []
                owned_payload.setdefault("final_verdict", "")

        if not owned_payload.get("season_snapshot"):
            try:
                owned_payload["season_snapshot"] = extract_season_snapshot(report_md_local)
            except Exception:
                owned_payload["season_snapshot"] = {}

        if not owned_payload.get("last3_games"):
            try:
                owned_payload["last3_games"] = extract_last3_games(report_md_local)
            except Exception:
                owned_payload["last3_games"] = []
    except Exception:
        pass

    try:
        owned_payload = ensure_parsed_payload(owned_payload)
    except Exception:
        pass

    owned_payload["cached"] = True
    owned_payload["created_at"] = existing.get("created_at")
    owned_payload["report_id"] = existing.get("id")

    try:
        increment_metric("cache_hits")
    except Exception:
        pass
    owned_payload["credits_remaining"] = get_balance(user_id)
    
    # Check if stats are stale (>20s)
    report_md_for_refresh = existing.get("report_md") or ""
    updated_at_str = existing.get("updated_at")
    stats_are_stale = False
    report_age_seconds = 0
    
    if updated_at_str:
        try:
            updated_at = datetime.fromisoformat(updated_at_str.replace('Z', '+00:00'))
            age = datetime.now(timezone.utc) - updated_at
            report_age_seconds = age.total_seconds()
            if age > timedelta(seconds=20):
                stats_are_stale = True
        except Exception:
            pass
    
    if stats_are_stale and not refresh_stats:
        owned_payload["stats_stale"] = True
        owned_payload["stats_age_seconds"] = report_age_seconds
        return (owned_payload, 200)
    
    if stats_are_stale and refresh_stats and client:
        logger.info(f"[STATS_REFRESH] Starting stats refresh for {player}")
        try:
            new_balance = spend_credits(user_id, 1, reason="stats_refresh", source_type="stats_refresh", source_id=f"stats_refresh_{existing.get('id')}_{int(time.time())}")
            owned_payload["credits_remaining"] = new_balance
            logger.info(f"[STATS_REFRESH] Credit charged, new balance: {new_balance}")

            logger.info(f"[STATS_REFRESH] Loading stats refresh prompt template")
            stats_prompt_template = load_text_prompt("prompts/stats_refresh.txt")
            stats_user_prompt = stats_prompt_template.format(player_name=existing.get("player_name") or player, last_updated=updated_at_str or "unknown")
            logger.info(f"[STATS_REFRESH] Prompt prepared ({len(stats_user_prompt)} chars)")
            
            logger.info(f"[STATS_REFRESH] Calling LLM for stats update (model={model})")
            # Use OpenAI API format
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a basketball statistics expert. Update the stats sections with the latest available data."},
                    {"role": "user", "content": stats_user_prompt}
                ],
                temperature=0.7,
                max_completion_tokens=2000
            )
            logger.info(f"[STATS_REFRESH] LLM call completed successfully")
            fresh_stats_md = response.choices[0].message.content or ""
            logger.info(f"[STATS_REFRESH] LLM returned {len(fresh_stats_md)} characters")
            
            # Track cost for stats refresh
            try:
                usage = response.usage
                if usage:
                    input_tokens = usage.prompt_tokens
                    output_tokens = usage.completion_tokens
                    prices = get_model_prices(model)
                    estimated_cost = estimate_cost({"input_tokens": input_tokens, "output_tokens": output_tokens}, prices)
                    insert_cost_tracking(
                        user_id=user_id,
                        report_id=existing.get("id"),
                        model=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        estimated_cost=estimated_cost,
                        player_name=existing.get("player_name") or player,
                        operation_type="refresh"
                    )
                    logger.info(f"[STATS_REFRESH] Cost tracked: ${estimated_cost:.6f} ({input_tokens} in, {output_tokens} out)")
            except Exception as e:
                logger.warning(f"[STATS_REFRESH] Failed to track cost: {e}")
            
            logger.info(f"[STATS_REFRESH] Replacing stats sections in report")
            updated_report_md = replace_stats_sections(report_md_for_refresh, fresh_stats_md)
            logger.info(f"[STATS_REFRESH] Stats sections replaced")

            if updated_report_md != report_md_for_refresh:
                logger.info(f"[STATS_REFRESH] Report changed, updating database for report_id={existing.get('id')}")
                payload_obj = existing.get("payload") or {}
                # Persist refresh timestamp in payload for library loads
                payload_obj["stats_refreshed_at"] = datetime.now(timezone.utc).isoformat()
                update_report_by_id(
                    user_id=user_id,
                    report_id=existing.get("id"),
                    player_name=existing.get("player_name") or player,
                    report_md=updated_report_md,
                    payload=payload_obj,
                    cached=True,
                    update_generated_at=False,  # Don't change generation time
                    update_stats_updated_at=True,  # Update stats timestamp
                )
                logger.info(f"[STATS_REFRESH] Database updated successfully")

                # Re-fetch full payload to get fresh extracted fields (season_snapshot, last3_games, etc.)
                try:
                    from utils.payload_handler import fetch_report_payload
                    refreshed_payload = fetch_report_payload(user_id, existing.get("id"))
                    if refreshed_payload:
                        owned_payload = refreshed_payload
                        # Set stats_refreshed_at to current time (not old updated_at)
                        owned_payload["stats_refreshed_at"] = datetime.now(timezone.utc).isoformat()
                        logger.info(f"[STATS_REFRESH] Payload refreshed with fresh extracted fields")
                    else:
                        # Fallback: manually update if fetch fails
                        owned_payload["report_md"] = updated_report_md
                        owned_payload["stats_refreshed_at"] = datetime.now(timezone.utc).isoformat()
                        display_md = extract_display_md(updated_report_md)
                        owned_payload["report_html"] = md_to_safe_html(display_md)
                        logger.info(f"[STATS_REFRESH] HTML regenerated (manual fallback)")
                except Exception as e:
                    logger.warning(f"[STATS_REFRESH] Failed to refresh payload, using manual update: {e}")
                    # Fallback: manually update
                    owned_payload["report_md"] = updated_report_md
                    owned_payload["stats_refreshed_at"] = datetime.now(timezone.utc).isoformat()
                    try:
                        display_md = extract_display_md(updated_report_md)
                        owned_payload["report_html"] = md_to_safe_html(display_md)
                        logger.info(f"[STATS_REFRESH] HTML regenerated (exception fallback)")
                    except Exception as e2:
                        logger.warning(f"[STATS_REFRESH] Failed to regenerate HTML: {e2}")
            else:
                logger.warning(f"[STATS_REFRESH] LLM returned no changes to stats sections")
        except ValueError as e:
            logger.error(f"[STATS_REFRESH] ValueError during stats refresh: {e}")
            if "INSUFFICIENT_CREDITS" in str(e):
                return ({"error": "Insufficient credits"}, 402)
            raise  # Re-raise if not insufficient credits
        except Exception as e:
            logger.error(f"[STATS_REFRESH] Exception during stats refresh: {type(e).__name__}: {e}", exc_info=True)
            # Return error to frontend instead of silently failing
            return ({"error": f"Stats refresh failed: {str(e)}"}, 500)
    
    # Log what's being returned for badge debugging
    logger.info(f"[STATS_REFRESH] Returning payload with stats_refreshed_at={owned_payload.get('stats_refreshed_at')}, updated_at={owned_payload.get('updated_at')}, created_at={owned_payload.get('created_at')}")
    return (owned_payload, 200)


def _try_similarity_matching(user_id: int, player: str, team: str, league: str, client, refresh: bool, query_key: str, query_obj: dict):
    """
    Try similarity matching via embeddings and fuzzy logic.
    
    Returns:
        tuple: (response_dict, status_code) or (None, None) if no match
    """
    if refresh:
        return None, None
        
    logger.info("[FLOW] Similarity matching start (embeddings → fuzzy)")
    try:
        # STEP 1: Try embedding-based similarity
        try:
            if league and league.strip():
                embed_auto, embed_suggest = 0.95, 0.75
            else:
                embed_auto, embed_suggest = 0.95, 0.78
            
            embed_similar = _find_by_embedding_similarity("*", player, team=team, league=league, client=client, auto_threshold=embed_auto, suggest_threshold=embed_suggest, max_scan=50)
            
            if embed_similar:
                try:
                    if embed_similar.get("type") == "auto":
                        increment_metric("fuzzy_auto_hits")
                    else:
                        increment_metric("fuzzy_suggests")
                except Exception:
                    pass
                
                if embed_similar.get("type") == "auto":
                    payload = embed_similar.get("payload") or {}
                    payload["auto_matched"] = True
                    try:
                        payload = ensure_parsed_payload(payload)
                    except Exception:
                        pass
                    payload["credits_remaining"] = get_balance(user_id)
                    return (payload, 200)
                elif embed_similar.get("type") == "suggest":
                    suggestion_payload = fetch_report_payload(user_id, int(embed_similar.get("report_id")))
                    return ({
                        "match_suggestion": {
                            "report_id": embed_similar.get("report_id"),
                            "player_name": embed_similar.get("player_name"),
                            "team": suggestion_payload.get("team") if suggestion_payload else team,
                            "league": suggestion_payload.get("league") if suggestion_payload else league,
                            "score": embed_similar.get("score"),
                        },
                        "auto_matched": False,
                        "credits_remaining": get_balance(user_id),
                        "note": "Similar player found in your library",
                    }, 200)
        except Exception:
            pass
        
        # STEP 2: Fuzzy matching
        if league and league.strip():
            pre_auto, pre_suggest = 78, 68
        else:
            pre_auto, pre_suggest = 88, 75

        pre_similar = _best_similar_report("*", player, team=team, league=league, client=client, auto_threshold=pre_auto, suggest_threshold=pre_suggest, max_scan=200, transliterate=True)
        
        if pre_similar:
            try:
                if pre_similar.get("type") == "auto":
                    increment_metric("fuzzy_auto_hits")
                else:
                    increment_metric("fuzzy_suggests")
            except Exception:
                pass
            
            if pre_similar.get("type") == "auto":
                payload = pre_similar.get("payload") or {}
                payload["auto_matched"] = True
                try:
                    payload = ensure_parsed_payload(payload)
                except Exception:
                    pass
                payload["credits_remaining"] = get_balance(user_id)
                return (payload, 200)
            elif pre_similar.get("type") == "suggest" and pre_similar.get("score") == 100:
                return _handle_exact_match_suggestion(user_id, pre_similar, query_key, query_obj)
            elif pre_similar.get("type") == "suggest":
                suggestion_payload = fetch_report_payload(user_id, int(pre_similar.get("report_id")))
                return ({
                    "match_suggestion": {
                        "report_id": pre_similar.get("report_id"),
                        "player_name": pre_similar.get("player_name"),
                        "team": suggestion_payload.get("team") if suggestion_payload else None,
                        "league": suggestion_payload.get("league") if suggestion_payload else None,
                        "score": pre_similar.get("score"),
                        "report_payload": suggestion_payload,
                    },
                    "auto_matched": False,
                    "credits_remaining": get_balance(user_id),
                }, 200)
    except Exception:
        pass
    
    return None, None


def _handle_exact_match_suggestion(user_id: int, pre_similar: dict, query_key: str, query_obj: dict):
    """Handle exact match (score=100) by charging and saving to library."""
    suggestion_payload = fetch_report_payload(user_id, int(pre_similar.get("report_id")))
    
    if suggestion_payload:
        try:
            new_balance = spend_credits(user_id, 1, reason="scout_exact_match", source_type="scout_request", source_id=f"exact_match_{pre_similar.get('report_id')}")
        except ValueError as e:
            if "INSUFFICIENT_CREDITS" in str(e):
                return ({"error": "Insufficient credits. Please top up.", "credits": get_balance(user_id)}, 402)
            raise
        
        user_report_id = None
        try:
            existing_copy = find_report_by_query_key(user_id, query_key)
            if existing_copy:
                user_report_id = existing_copy.get("id")
            else:
                source_md = suggestion_payload.get("report_md", "")
                payload = dict(suggestion_payload)
                payload["cached"] = True
                payload["report_md"] = source_md
                if not payload.get("player"):
                    payload["player"] = pre_similar.get("player_name")
                
                try:
                    payload = ensure_parsed_payload(payload)
                except Exception:
                    pass
                
                user_report_id = insert_report(user_id=user_id, player_name=pre_similar.get("player_name"), query_obj=query_obj, report_md=source_md, payload=payload, cached=True)
                payload["report_id"] = user_report_id
                payload["cached"] = True
                
                try:
                    saved_report = get_report(user_id, int(user_report_id))
                    if saved_report and saved_report.get("created_at"):
                        payload["created_at"] = saved_report["created_at"]
                except Exception:
                    pass
        except Exception:
            pass
        
        suggestion_payload["auto_matched"] = True
        suggestion_payload["credits_remaining"] = new_balance
        if user_report_id:
            suggestion_payload["report_id"] = user_report_id
            return (suggestion_payload, 200)
        return (suggestion_payload, 200)
    return (None, None)


def _check_global_cache(user_id: int, player: str, query_key: str, query_obj: dict, refresh: bool, canonical_query_player: str):
    """
    Check if ANY user has this report (global cache).
    
    Returns:
        tuple: (response_dict, status_code) or (None, None) if no cache hit
    """
    if refresh:
        return None, None
    
    logger.info("[FLOW] Global cache stage")
    global_cached_report = None
    try:
        pool = _get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, payload, report_md, player_name, created_at, cached FROM public.reports WHERE query_key = %s ORDER BY created_at DESC, id DESC LIMIT 1", (query_key,))
            row = cur.fetchone()
            if row:
                rid, payload, report_md, player_name, created_at, cached = row
                global_cached_report = {
                    "id": int(rid),
                    "payload": payload,  
                    "report_md": report_md or "",
                    "player_name": player_name or "",
                    "created_at": created_at.isoformat() if created_at else None,
                    "cached": bool(cached),
                }
            
            if not global_cached_report:
                try:
                    cur.execute("SELECT id, payload, report_md, player_name, created_at, cached FROM public.reports ORDER BY created_at DESC, id DESC LIMIT 100")
                    rows = cur.fetchall() or []
                except Exception:
                    rows = []
                
                player_norm = normalize_name(player, transliterate=True)
                for rid, payload, report_md, player_name, created_at, cached in rows:
                    try:
                        if names_match(player_norm, player_name or ""):
                            global_cached_report = {
                                "id": int(rid),
                                "payload": payload,
                                "report_md": (report_md or ""),
                                "player_name": (player_name or ""),
                                "created_at": created_at.isoformat() if created_at else None,
                                "cached": bool(cached),
                            }
                            break
                    except Exception:
                        continue
    except Exception:
        pass
    
    if global_cached_report:
        try:
            new_balance = spend_credits(user_id, 1, reason="scout_global_cache", source_type="scout_request", source_id=f"global_cache_{global_cached_report['id']}")
        except ValueError as e:
            if "INSUFFICIENT_CREDITS" in str(e):
                return ({"error": "Insufficient credits. Please top up.", "credits": get_balance(user_id)}, 402)
            raise
        
        try:
            source_md = global_cached_report.get("report_md", "")
            source_payload = global_cached_report.get("payload") or {}
            payload = dict(source_payload)
            payload["cached"] = True
            payload["report_md"] = source_md
            payload["player"] = global_cached_report.get("player_name")
            
            try:
                payload = ensure_parsed_payload(payload)
            except Exception:
                pass
            
            user_report_id = insert_report(user_id=user_id, player_name=global_cached_report.get("player_name"), query_obj=query_obj, report_md=source_md, payload=payload, cached=True)
            payload["report_id"] = user_report_id
            payload["cached"] = True
            payload["credits_remaining"] = new_balance
            
            try:
                saved_report = get_report(user_id, int(user_report_id))
                if saved_report and saved_report.get("created_at"):
                    payload["created_at"] = saved_report["created_at"]
            except Exception:
                pass
            
            try:
                track_event(user_id, "report_gen_cached", {"player": player, "source_report_id": global_cached_report["id"], "cached": True})
            except Exception:
                pass
            
            return (payload, 200)
        except Exception:
            pass
    
    return None, None


def _generate_report_with_llm(user_id: int, client, MODEL: str, SCOUT_INSTRUCTIONS: str, player: str, team: str, league: str, season: str, refresh: bool):
    """
    Generate a new report using LLM.
    
    Returns:
        tuple: (payload_dict, status_code) or raises exception
    """
    try:
        track_event(user_id, "report_gen_started", {"player": player, "team": team, "league": league, "use_web": True, "refresh": refresh})
    except Exception:
        pass

    if client is None:
        return ({"error": "OpenAI generation disabled (set ENABLE_OPENAI=1 to enable)."}, 503)

    try:
        payload = get_or_generate_scout_report(client=client, model=MODEL, scout_instructions=SCOUT_INSTRUCTIONS, player=player, team=team, league=league, season=season, use_web=True, refresh=refresh, user_id=user_id)
    except Exception as e:
        try:
            track_event(user_id, "generation_failed", {"player": player, "error": str(e), "error_type": type(e).__name__})
        except Exception:
            pass
        raise

    report_md = (payload.get("report_md") or "").strip()
    if report_md.startswith("PLAYER_NOT_FOUND:"):
        try:
            track_event(user_id, "generation_failed", {"player": player, "error": report_md, "error_type": "player_not_found"})
        except Exception:
            pass
        
        if league and league.strip():
            fb_auto, fb_suggest = 88, 75
        else:
            fb_auto, fb_suggest = 92, 78

        fb = _best_similar_report(user_id, player, team=team, league=league, client=client, auto_threshold=fb_auto, suggest_threshold=fb_suggest, max_scan=300, transliterate=True)
        
        if fb:
            if fb.get("type") == "auto":
                payload = fb.get("payload") or {}
                payload["auto_matched"] = True
                try:
                    payload = ensure_parsed_payload(payload)
                except Exception:
                    pass
                payload["credits_remaining"] = get_balance(user_id)
                return (payload, 200)
            elif fb.get("type") == "suggest":
                fb_payload = None
                try:
                    fb_payload = get_report(user_id, int(fb.get("report_id")))
                except Exception:
                    pass
                
                return ({
                    "match_suggestion": {
                        "report_id": fb.get("report_id"),
                        "player_name": fb.get("player_name"),
                        "team": fb_payload.get("team") if fb_payload else None,
                        "league": fb_payload.get("league") if fb_payload else None,
                        "score": fb.get("score"),
                    },
                    "auto_matched": False,
                    "credits_remaining": get_balance(user_id),
                    "note": "Original generation returned PLAYER_NOT_FOUND; a close cached match was found.",
                }, 200)

        return ({"error": report_md}, 400)
    
    return (payload, 200)


def _persist_and_charge_report(user_id: int, payload: dict, player: str, canonical_query_player: str, query_obj: dict, refresh: bool, report_id_to_update: int, request_id: str, MODEL: str, client):
    """
    Charge credit and persist report to database.
    
    Returns:
        tuple: (response_dict, status_code)
    """
    report_md = payload.get("report_md", "")
    
    try:
        new_balance = spend_credits(user_id, 1, reason="report", source_type="scout_request", source_id=request_id)
    except ValueError as e:
        if str(e) == "INSUFFICIENT_CREDITS":
            return ({"error": "Insufficient credits. Please top up.", "credits": get_balance(user_id)}, 402)
        return ({"error": str(e)}, 500)

    try:
        payload["cached"] = False
        cached_flag = False

        insert_query_obj = dict(query_obj)
        insert_query_obj["use_web"] = True

        canonical_player = (payload.get("player") or payload.get("player_name") or player).strip()
        insert_query_obj["player"] = canonical_query_player

        payload.setdefault("queried_player", player)

        # POST-LLM CANONICAL DEDUP
        if not refresh and not report_id_to_update:
            try:
                pool = _get_pool()
                with pool.connection() as conn, conn.cursor() as cur:
                    cur.execute("SELECT id, payload, report_md, player_name, created_at, cached FROM public.reports WHERE user_id = %s AND player_name = %s ORDER BY created_at DESC, id DESC LIMIT 1", (user_id, canonical_player))
                    existing_row = cur.fetchone()
                    if existing_row:
                        existing_id = existing_row[0]
                        
                        try:
                            refund_credits(user_id, 1, reason="post_llm_dedup", source_type="scout_request_refund", source_id=f"{request_id}:post_llm_dedup")
                        except Exception:
                            pass
                        
                        existing_payload_dict = existing_row[1] or {}
                        existing_payload_dict["report_md"] = existing_row[2] or ""
                        try:
                            existing_payload_dict = ensure_parsed_payload(existing_payload_dict)
                        except Exception:
                            pass
                        existing_payload_dict["cached"] = bool(existing_row[5])
                        existing_payload_dict["report_id"] = int(existing_id)
                        existing_payload_dict["library_id"] = int(existing_id)
                        existing_payload_dict["credits_remaining"] = get_balance(user_id)
                        return (existing_payload_dict, 200)
            except Exception:
                pass

        try:
            if report_id_to_update and refresh:
                pg_id = update_report_by_id(user_id=user_id, report_id=int(report_id_to_update), player_name=canonical_player, report_md=report_md, payload=payload, cached=cached_flag)
                payload["refreshed"] = True
            else:
                pg_id = insert_report(user_id=user_id, player_name=canonical_player, query_obj=insert_query_obj, report_md=report_md, payload=payload, cached=cached_flag)
            
            try:
                saved_report = get_report(user_id, int(pg_id))
                if saved_report and saved_report.get("created_at"):
                    payload["created_at"] = saved_report["created_at"]
            except Exception:
                pass
            
            payload["library_id"] = int(pg_id)
        except Exception as e:
            try:
                refund_credits(user_id, 1, reason="refund_postgres_failed", source_type="scout_request_refund", source_id=f"{request_id}:refund_pg")
            except Exception:
                pass
            return ({"error": f"Failed to save report: {e}"}, 500)

        try:
            embed_text_input = f"{canonical_player} {query_obj.get('team') or ''} {query_obj.get('league') or ''}".strip()
            embedding_vector = embed_text(client, embed_text_input)
            target_id = int(pg_id)
            store_embedding(target_id, embedding_vector)
        except Exception:
            pass

        try:
            usage = payload.get("usage", {})
            if usage and usage.get("input_tokens") and usage.get("output_tokens"):
                prices = get_model_prices(MODEL)
                estimated_cost = estimate_cost(usage, prices)
                insert_cost_tracking(user_id=user_id, report_id=int(pg_id), model=MODEL, input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"], estimated_cost=estimated_cost, player_name=payload.get("player") or payload.get("player_name") or player)
        except Exception:
            pass

        payload["credits_remaining"] = new_balance
        return (payload, 200)
    except Exception as e:
        try:
            refund_credits(user_id, 1, reason="refund_persist_failed", source_type="scout_request_refund", source_id=f"{request_id}:refund")
        except Exception:
            pass
        return ({"error": str(e)}, 500)


def create_reports_routes(app, require_user_id, client, MODEL, SCOUT_INSTRUCTIONS):
    """
    Register report-related routes with the Flask app.
    
    Args:
        app: Flask application instance
        require_user_id: Authentication function that extracts user_id from request
        client: OpenAI client instance for LLM generation
        MODEL: Model name string (e.g., "claude-sonnet-4")
        SCOUT_INSTRUCTIONS: System prompt for LLM report generation
    """

    @app.post("/api/save_suggestion")
    def save_suggestion():
        """
        Accept a suggested report from another user and save it to the current user's library.
        
        This is a FREE operation (doesn't charge credits) - used when a user wants to save
        a report they found through the similarity matching system but doesn't want to accept
        it as a match for their current query.
        
        Request Body:
            report_id (int): The ID of the report to save
            
        Returns:
            200: Report saved successfully with new report_id
            400: Invalid or missing report_id
            401: Authentication failed
            404: Report not found
            500: Failed to save report
        """
        try:
            user_id = require_user_id(request)
        except PermissionError as e:
            return jsonify({"error": str(e)}), 401

        data = request.get_json(force=True) or {}
        
        report_id = data.get("report_id")
        if not report_id:
            return jsonify({"error": "Missing report_id"}), 400
        
        try:
            report_id = int(report_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid report_id"}), 400
        
        # Fetch the report (from any user, but verify it exists)
        try:
            report = get_report(report_id)
        except Exception:
            return jsonify({"error": f"Report {report_id} not found"}), 404
        
        if not report:
            return jsonify({"error": f"Report {report_id} not found"}), 404
        
        # Extract key fields from the source report
        player_name = report.get("player_name") or report.get("player") or ""
        report_md = report.get("report_md", "")
        payload = report.get("payload", {})
        
        # Create a copy for this user without charging credits
        try:
            query_obj = {
                "player": player_name,
                "team": (payload.get("team") or "").strip(),
                "league": (payload.get("league") or "").strip(),
                "season": (payload.get("season") or "").strip(),
                "use_web": True,
            }
            
            # Save to this user's library using upsert (won't create duplicate if same query_key)
            pg_id = insert_report(
                user_id=user_id,
                player_name=player_name,
                query_obj=query_obj,
                report_md=report_md,
                payload=payload,
                cached=True,  # Mark as cached since it came from a suggestion
            )
            
            return jsonify({
                "success": True,
                "report_id": pg_id,
                "message": f"Report saved to your library",
            })
        except Exception as e:
            logger.error("Failed to save suggested report: %s", e)
            return jsonify({"error": f"Failed to save report: {str(e)}"}), 500

    @app.post("/api/scout")
    def scout():
        """
        Main report generation endpoint - orchestrates the full report workflow.
        
        Workflow:
        1. Suggestion acceptance (if accept_suggestion=true)
        2. User library check (canonical name matching)
        3. Stats refresh check (if cached and stale >20s)
        4. Global cache check (all users' reports)
        5. Similarity matching (embeddings → fuzzy)
        6. LLM generation (if no matches found)
        7. Persistence and credit management
        
        Request Body:
            player (str, required): Player name to scout
            team (str, optional): Team name
            league (str, optional): League name
            season (str, optional): Season identifier
            use_web (bool, default=True): Use web search for generation
            refresh (bool, default=False): Force regeneration ignoring cache
            refresh_stats (bool, default=False): User confirmed stats refresh
            report_id (int, optional): Report ID to update when refreshing
            accept_suggestion (bool, default=False): Accepting a suggested match
            suggestion_report_id (int, optional): Source report ID when accepting
            
        Returns:
            200: Report generated/retrieved successfully with payload
            400: Invalid request or player not found
            401: Authentication failed
            402: Insufficient credits
            500: Generation or persistence failed
        """
        try:
            user_id = require_user_id(request)
        except PermissionError as e:
            return jsonify({"error": str(e)}), 401

        data = request.get_json(force=True) or {}

        # Extract request parameters
        player = (data.get("player") or "").strip()
        if not player:
            return jsonify({"error": "Missing required field: player"}), 400

        team = (data.get("team") or "").strip()
        league = (data.get("league") or "").strip()
        season = (data.get("season") or "").strip()

        # Default to True so cached reports can be refreshed with live stats
        use_web = bool(data.get("use_web", True))
        refresh = bool(data.get("refresh", False))
        refresh_stats = bool(data.get("refresh_stats", False))  # User confirmed stats refresh

        logger.info(
            f"[FLOW] /api/scout start player='{player}' team='{team}' league='{league}' refresh={refresh}"
        )
        report_id_to_update = data.get("report_id")  # For regenerating existing reports
        accept_suggestion = bool(data.get("accept_suggestion", False))  # For accepting suggestions
        suggestion_report_id = data.get("suggestion_report_id")  # Source report ID when accepting

        # ========================================================================
        # FAST PATH: STATS REFRESH FOR KNOWN REPORT ID
        # ========================================================================
        # When refreshing stats for a report already in the user's library,
        # skip canonicalization and go directly to refresh
        if refresh_stats and report_id_to_update and not refresh:
            try:
                report_id_to_update = int(report_id_to_update)
                logger.info(f"[FLOW] Fast path: Stats refresh for report_id={report_id_to_update}")
                
                # Fetch the existing report directly by ID
                from utils.payload_handler import fetch_report_payload
                existing_report = fetch_report_payload(user_id, report_id_to_update)
                
                if not existing_report:
                    logger.warning(f"[FLOW] Report {report_id_to_update} not found for user {user_id}")
                    return jsonify({"error": "Report not found"}), 404
                
                # Build minimal existing dict for _handle_cached_report
                existing_dict = {
                    "id": report_id_to_update,
                    "player_name": existing_report.get("player") or existing_report.get("player_name") or player,
                    "report_md": existing_report.get("report_md", ""),
                    "payload": existing_report,
                    "created_at": existing_report.get("created_at"),
                    "updated_at": existing_report.get("updated_at")
                }
                
                result, status_code = _handle_cached_report(
                    user_id, existing_dict, player, refresh_stats=True, client=client, model=MODEL
                )
                return jsonify(result), status_code
                
            except ValueError:
                logger.error(f"[FLOW] Invalid report_id: {report_id_to_update}")
                return jsonify({"error": "Invalid report_id"}), 400

        # ========================================================================
        # SECTION 1: SUGGESTION ACCEPTANCE WORKFLOW
        # ========================================================================
        # When a user accepts a suggested match, check if they already have it,
        # then charge only if it's a new cross-user report
        if accept_suggestion and suggestion_report_id:
            try:
                suggestion_report_id = int(suggestion_report_id)
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid suggestion_report_id"}), 400
            
            result, status_code = _handle_suggestion_acceptance(
                user_id, suggestion_report_id, player, team, league, season
            )
            return jsonify(result), status_code

        # ========================================================================
        # SECTION 2: CANONICAL NAME NORMALIZATION & LIBRARY CHECK
        # ========================================================================
        existing, canonical_query_player, query_key, query_obj = _check_user_library(
            user_id, player, team, league, season, use_web
        )
        
        # ========================================================================
        # SECTION 3: CACHED REPORT WITH STATS REFRESH
        # ========================================================================
        if existing and not (report_id_to_update and refresh):
            result, status_code = _handle_cached_report(
                user_id, existing, player, refresh_stats, client, MODEL
            )
            return jsonify(result), status_code

        # ========================================================================
        # SECTION 4: SIMILARITY MATCHING (EMBEDDINGS → FUZZY)
        # ========================================================================
        result, status_code = _try_similarity_matching(user_id, player, team, league, client, refresh, query_key, query_obj)
        if result:
            return jsonify(result), status_code

        # ========================================================================
        # SECTION 5: GLOBAL CACHE CHECK (BEFORE LLM GENERATION)
        # ========================================================================
        request_id = str(uuid.uuid4())

        # Pre-check balance
        try:
            if get_balance(user_id) < 1:
                return jsonify({"error": "Insufficient credits. Please top up.", "credits": get_balance(user_id)}), 402
        except Exception:
            return jsonify({"error": "Could not verify credits"}), 500

        result, status_code = _check_global_cache(user_id, player, query_key, query_obj, refresh, canonical_query_player)
        if result:
            return jsonify(result), status_code

        # ========================================================================
        # SECTION 6: LLM GENERATION
        # ========================================================================
        payload, status_code = _generate_report_with_llm(
            user_id, client, MODEL, SCOUT_INSTRUCTIONS, player, team, league, season, refresh
        )
        if status_code != 200:
            return jsonify(payload), status_code

        # ========================================================================
        # SECTION 7: CHARGE & PERSIST REPORT
        # ========================================================================
        result, status_code = _persist_and_charge_report(
            user_id, payload, player, canonical_query_player, query_obj, 
            refresh, report_id_to_update, request_id, MODEL, client
        )
        return jsonify(result), status_code

