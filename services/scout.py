# services/scout.py
from __future__ import annotations

from typing import Any, Dict, Optional

from db import PROMPT_VERSION, get_cached_report
from utils.parse import (
    extract_display_md,
    extract_grades,
    extract_info_fields,
    extract_last3_games,
    extract_season_snapshot,
)
from utils.render import md_to_safe_html
import time


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
) -> Dict[str, Any]:
    player = (player or "").strip()
    team = (team or "").strip()
    league = (league or "").strip()
    season = (season or "").strip()

    if not refresh:
        cached_row = get_cached_report(
            player=player,
            team=team,
            league=league,
            season=season,
            use_web=use_web,
        )
        if cached_row:
            report_md = cached_row.get("report_md") or ""
            return _build_payload_from_report(
                report_md=report_md,
                player=cached_row.get("player") or player,
                team=cached_row.get("team") or "",
                league=cached_row.get("league") or "",
                season=cached_row.get("season") or "",
                model=cached_row.get("model") or "",
                use_web=bool(cached_row.get("use_web")),
                cached=True,
                created_at=cached_row.get("created_at"),
            )

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

    try:
        # count generated-success events when model returns non-empty output
        if report_md and len(report_md) > 0:
            try:
                increment_metric("llm_success")
            except Exception:
                pass
            try:
                # Track generation event (anonymous if caller doesn't provide user)
                from utils.app_helpers import track_event

                track_event(
                    None,
                    "scout_generated",
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
    return _build_payload_from_report(
        report_md=report_md,
        player=canonical_player if canonical_player else player,
        team=team,
        league=league,
        season=season,
        model=model,
        use_web=use_web,
        cached=False,
    )
