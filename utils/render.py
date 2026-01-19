# utils/render.py
from __future__ import annotations

import bleach
from markdown_it import MarkdownIt

from utils.clean import scrub_urls_preserve_newlines
from typing import Dict
from utils.parse import extract_display_md


def ensure_parsed_payload(payload: Dict) -> Dict:
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

# IMPORTANT: linkify=False so markdown-it doesn't turn domains into <a>
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": True})

_ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
    "p",
    "pre",
    "code",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "br",
    "blockquote",
    "ul",
    "ol",
    "li",
    "span",
    "div",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
]

# Disallow links entirely (you said users should not see them)
_ALLOWED_ATTRS = {
    # keep empty for now; (no "a")
}

_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def md_to_safe_html(md_text: str) -> str:
    """
    Render markdown to sanitized HTML for UI.
    - strips URLs/domains before rendering
    - disallows <a> tags
    """
    # Preserve newlines when scrubbing URLs so markdown headings/lists stay on
    # their own lines and render correctly.
    md_text = scrub_urls_preserve_newlines(md_text or "")

    raw_html = _md.render(md_text)

    clean_html = bleach.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return clean_html
