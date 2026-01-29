"""Report payload processing and parsing utilities.

Handles populating derived fields (HTML rendering, parsed metadata),
fetching reports from database, and ensuring payloads have all required fields.
"""

import logging

from utils.normalize import normalize_name
from utils.parse import extract_display_md
from utils.render import md_to_safe_html

logger = logging.getLogger(__name__)


def _ensure_parsed_payload(payload: dict) -> dict:
    """Populate derived fields from markdown when missing and ensure `report_html`.

    Best-effort helper used by cached/library/suggestion paths so the client
    receives structured `info_fields`, `grades`, `season_snapshot`,
    `last3_games`, and `report_html` when possible.
    """
    if not isinstance(payload, dict):
        return payload

    report_md = (payload.get("report_md") or "")
    try:
        display_md = extract_display_md(report_md)
        payload.setdefault("report_html", md_to_safe_html(display_md))
    except Exception:
        payload.setdefault("report_html", "")

    try:
        from utils.parse import (
            extract_info_fields,
            extract_grades,
            extract_season_snapshot,
            extract_last3_games,
        )

        if not payload.get("info_fields"):
            try:
                payload["info_fields"] = extract_info_fields(report_md)
            except Exception:
                payload["info_fields"] = {}

        if not payload.get("grades"):
            try:
                grades, final_verdict = extract_grades(report_md)
                payload["grades"] = grades
                payload["final_verdict"] = final_verdict
            except Exception:
                payload["grades"] = []
                payload.setdefault("final_verdict", "")

        if not payload.get("season_snapshot"):
            try:
                payload["season_snapshot"] = extract_season_snapshot(report_md)
            except Exception:
                payload["season_snapshot"] = {}

        if not payload.get("last3_games"):
            try:
                payload["last3_games"] = extract_last3_games(report_md)
            except Exception:
                payload["last3_games"] = []
    except Exception:
        # If parsing helpers are unavailable, leave payload as-is
        pass

    return payload


def fetch_report_payload(user_id: str, report_id: int):
    """Fetch report payload, trying get_report first, then direct Postgres query.
    
    Returns the payload dict or None if not found.
    Handles report_md reconstruction from split columns.
    """
    suggestion_payload = None
    try:
        from db import get_report
        suggestion_payload = get_report(user_id, report_id)
    except Exception:
        pass
    
    # If not found, try a direct Postgres read
    if not suggestion_payload:
        try:
            import db
            pool = db._get_pool()
            with pool.connection() as conn_pg, conn_pg.cursor() as cur:
                cur.execute(
                    "SELECT payload, report_md, report_narrative_md, stats_md, player_name, created_at, updated_at, cached FROM public.reports WHERE id = %s LIMIT 1",
                    (report_id,),
                )
                prow = cur.fetchone()
            if prow:
                payload_row = prow[0]
                report_md = prow[1] or ""
                narrative_md = prow[2]
                stats_md = prow[3]
                
                # Reconstruct report_md from split columns if they exist
                if narrative_md and stats_md:
                    report_md = narrative_md + "\n\n" + stats_md
                
                if payload_row:
                    suggestion_payload = payload_row
                    if (
                        isinstance(suggestion_payload, dict)
                        and "report_md" not in suggestion_payload
                    ):
                        suggestion_payload["report_md"] = report_md
                    if isinstance(suggestion_payload, dict):
                        # Always trust DB timestamps to keep them current
                        suggestion_payload["created_at"] = prow[5] or None
                        suggestion_payload["updated_at"] = prow[6] or (prow[5] or None)
                else:
                    display_md = extract_display_md(report_md)
                    suggestion_payload = {
                        "player": prow[4] or "",
                        "report_md": report_md,
                        "report_html": md_to_safe_html(display_md),
                        "created_at": prow[5] or None,
                        "updated_at": prow[6] or (prow[5] or None),
                        "cached": bool(prow[7]),
                    }
                try:
                    from utils.render import ensure_parsed_payload
                    suggestion_payload = ensure_parsed_payload(suggestion_payload)
                except Exception:
                    pass
        except Exception:
            pass
    
    return suggestion_payload
