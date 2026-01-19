"""Markdown-related helpers extracted from app_helpers.

Contains helpers that turn report markdown into sanitized HTML and
extract structured fields when missing.
"""
from typing import Dict

from utils.parse import extract_display_md
from utils.render import md_to_safe_html


def ensure_parsed_payload(payload: Dict) -> Dict:
    """Populate derived fields from markdown when missing and ensure `report_html`.

    This mirrors the previous `_ensure_parsed_payload` but uses a clearer name
    and lives in `utils.markdown_helpers`.
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
        pass

    return payload
