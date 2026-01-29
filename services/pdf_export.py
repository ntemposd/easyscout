# services/pdf_export.py
"""PDF export service for generating printable scout reports."""

import asyncio
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, Any

from playwright.async_api import async_playwright

from utils.parse import extract_display_md
from utils.render import md_to_safe_html


def generate_pdf_from_report(payload: Dict[str, Any]) -> bytes:
    """Generate PDF from report payload.
    
    Args:
        payload: Report data including report_md, player name, and structured fields
        
    Returns:
        PDF file content as bytes
    """
    player_name = (payload.get("player") or "Report").strip() or "Report"
    report_md = payload.get("report_md", "") or ""

    # Render markdown to HTML using existing helpers
    try:
        display_md = extract_display_md(report_md)
        report_html = md_to_safe_html(display_md)
    except Exception:
        report_html = "<p>No report content available.</p>"

    # Add structured tables (season snapshot, last games, info fields, grades) when present
    def render_kv_table(title: str, data: dict) -> str:
        """Render a simple key/value table with a heading outside the table element."""
        if not data:
            return ""
        rows = "".join(
            f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in data.items() if v is not None and v != ""
        )
        if not rows:
            return ""
        return f"<div class=\"section-block\"><h2 class=\"table-title\">{title}</h2><table>{rows}</table></div>"

    def render_list_table(title: str, items: list) -> str:
        """Render a list of dicts as a table with a separate heading block."""
        if not items:
            return ""
        # Determine headers from first item keys for consistency
        first = items[0]
        if not isinstance(first, dict):
            return ""
        headers = list(first.keys())
        header_html = "".join(f"<th>{h}</th>" for h in headers)
        body_html = "".join(
            "<tr>" + "".join(f"<td>{(it or {}).get(h, '')}</td>" for h in headers) + "</tr>"
            for it in items
            if isinstance(it, dict)
        )
        return (
            f"<div class=\"section-block\">"
            f"<h2 class=\"table-title\">{title}</h2>"
            f"<table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"
            f"</div>"
        )

    extra_sections = []

    season_snapshot = payload.get("season_snapshot") or {}
    extra_sections.append(render_kv_table("Season snapshot", season_snapshot))

    last3 = payload.get("last3_games") or []
    extra_sections.append(render_list_table("Last 3 games", last3))

    info_fields = payload.get("info_fields") or {}
    extra_sections.append(render_kv_table("Player info", info_fields))

    grades = payload.get("grades") or []
    if grades and isinstance(grades, list):
        # grades may be list of dicts with keys like category/grade/notes
        if isinstance(grades[0], dict):
            extra_sections.append(render_list_table("Grades", grades))

    extra_html = "".join(s for s in extra_sections if s)

    # Inline minimal print-friendly styles plus Tailwind (if present) for richer styling
    base_css = """
    body { font-family: Arial, sans-serif; color: #1f2937; line-height: 1.6; margin: 0; padding: 0; }
    .page { max-width: 820px; margin: 20px auto; padding: 0 24px 32px 24px; }
    .header { text-align: center; border-bottom: 3px solid #6FD06B; padding: 18px 0 14px 0; margin-bottom: 24px; }
    .header .title { color: #6FD06B; font-size: 22px; font-weight: 700; margin: 0 0 6px 0; }
    .header .player { color: #0E2018; font-size: 18px; font-weight: 700; margin: 0; }
    h1 { color: #0E2018; font-size: 18px; margin: 22px 0 10px 0; border-bottom: 2px solid #6FD06B; padding-bottom: 6px; }
    h2 { color: #0E2018; font-size: 15px; margin: 18px 0 8px 0; }
    h3 { color: #0E2018; font-size: 13px; margin: 40px 0 6px 0; }
    p { margin: 8px 0; font-size: 11.5px; line-height: 1.6; }
    ul { margin: 10px 0 10px 18px; padding: 0; }
    li { margin: 5px 0; font-size: 11.5px; }
    .section-block { margin: 18px 0 14px 0; }
    .table-title { margin: 0 0 8px 0; font-size: 14px; color: #0E2018; }
    table { width: 100%; border-collapse: collapse; margin: 6px 0 12px 0; font-size: 11px; }
    td, th { border: 1px solid #e5e7eb; padding: 8px; text-align: left; }
    th { background: #f3f4f6; color: #111827; font-weight: 600; }
    .footer { margin-top: 28px; padding-top: 12px; border-top: 1px solid #e5e7eb; font-size: 10px; color: #6b7280; text-align: center; }
    """

    tailwind_css = ""
    try:
        tw_path = Path("static/tailwind.css")
        if tw_path.exists():
            tailwind_css = tw_path.read_text(encoding="utf-8")
    except Exception:
        tailwind_css = ""

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8" />
        <style>{base_css}\n{tailwind_css}</style>
    </head>
    <body>
        <div class="page">
            <div class="header">
                <div class="title">Scout Report — {player_name}</div>
                <div class="player">{player_name}</div>
            </div>
            <div class="content">{report_html}{extra_html}</div>
            <div class="footer">Generated by Easyscout — Scout Reports made Easy</div>
        </div>
    </body>
    </html>
    """

    async def render_pdf(html: str) -> bytes:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            pdf_bytes = await page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"},
            )
            await browser.close()
            return pdf_bytes

    pdf_bytes = asyncio.run(render_pdf(html_content))
    return pdf_bytes


def generate_pdf_filename(player_name: str) -> str:
    """Generate sanitized filename for PDF export."""
    safe_name = re.sub(r"[^a-zA-Z0-9\s\-]", "", player_name).strip() or "report"
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{date_str} {safe_name} - by Easyscout.pdf"
