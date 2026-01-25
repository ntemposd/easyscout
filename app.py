# app.py
# ruff: noqa: E402
from dotenv import load_dotenv

load_dotenv(override=True)

import logging
import os
import uuid
from datetime import datetime, timezone

try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None

try:
    import stripe
except Exception:
    # Fallback stub for environments without `stripe` installed (dev/test)
    import types

    stripe = types.SimpleNamespace()
    stripe.api_key = ""
from flask import Flask, jsonify, render_template, request
try:
    from openai import OpenAI
except Exception:
    # Provide a minimal stub so the app can import when `openai` isn't installed.
    class OpenAI:  # type: ignore
        def __init__(self, *args, **kwargs):
            pass

try:
    from sentry_sdk.integrations.excepthook import ExcepthookIntegration
    from sentry_sdk.integrations.flask import FlaskIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
except ImportError:
    ExcepthookIntegration = FlaskIntegration = LoggingIntegration = None

import db_pg
from auth import require_user_id
from db import init_db  # your existing SQLite cache init
from db import find_canonical_by_alias, get_cached_report
import db
from db_pg import (
    find_report_by_query_key,
    get_balance,
    get_report,
    get_report_by_id,
    insert_report,
    list_reports,
    count_reports,
    make_query_key,
    record_stripe_event,
    record_stripe_purchase,
    refund_credits,
    spend_credits,
    initialize_user_with_welcome_credits,
    update_report_by_id,
)
from services.scout import get_or_generate_scout_report
from utils.metrics import increment_metric, list_metrics, list_timings
from utils.parse import extract_display_md
from utils.prompts import load_text_prompt
from utils.render import md_to_safe_html, ensure_parsed_payload
from utils.normalize import normalize_name
from utils.name_variants import NICKNAME_MAP

from utils.app_helpers import (
    _best_similar_report,
    _find_by_embedding_similarity,
    _HAS_RAPIDFUZZ,
    _token_sort_ratio,
    _token_set_ratio,
    track_event,
    alias_user,
    analytics_enabled,
    shutdown_analytics,
)
from utils.email import send_email

import atexit

# Ensure analytics client is cleanly shutdown on process exit to avoid
# background threads/sockets being left in an invalid state on Windows.
try:
    atexit.register(shutdown_analytics)
except Exception:
    pass
from difflib import SequenceMatcher
import re

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", os.urandom(32).hex())
app.config["SESSION_COOKIE_SECURE"] = os.getenv("DEV_TOOLS") != "1"  # HTTPS only in production
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


@app.after_request
def add_security_headers(response):
    """Add security headers to all responses"""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    # Only add HSTS if in production (HTTPS)
    if os.getenv("DEV_TOOLS") != "1":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.route("/robots.txt")
def robots_txt():
    return app.send_static_file("robots.txt")


@app.route("/sitemap.xml")
def sitemap():
    return app.send_static_file("sitemap.xml")


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")

# 404 handler: render 404 template without ERROR logs
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

# 405 handler: method not allowed (typically bot probing) — return 405, not 500
@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "method not allowed"}), 405

# Development-friendly global error handler: log exception and return JSON
@app.errorhandler(Exception)
def _handle_exception(e):
    try:
        logger.exception(e)
    except Exception:
        pass
    # In dev mode, include the traceback for easier debugging
    if os.getenv("DEV_TOOLS") == "1":
        import traceback

        tb = traceback.format_exc()
        return jsonify({"error": str(e), "traceback": tb}), 500
    return jsonify({"error": "internal server error"}), 500

# Optional HTTP response compression when `flask_compress` is installed
try:
    from flask_compress import Compress

    Compress(app)
except Exception:
    pass

# --------------------
# Logging Setup (must be early to use logger in config)
# --------------------
logging.basicConfig(level=logging.INFO)
# Reduce verbosity from noisy libraries in normal runs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --------------------
# Config
# --------------------
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

# Safety: avoid using a live Stripe secret in development mode
try:
    if os.getenv("DEV_TOOLS") == "1" and isinstance(stripe.api_key, str) and stripe.api_key.startswith("sk_live"):
        logger.warning("DEV_TOOLS=1 and a live Stripe secret detected — clearing `stripe.api_key` to avoid accidental live charges.")
        stripe.api_key = ""
except Exception:
    pass

_env_enable = os.getenv("ENABLE_OPENAI")
# If ENABLE_OPENAI explicitly provided, honor it. Otherwise, enable automatically
# when an `OPENAI_API_KEY` is present in the environment so the server can
# generate reports when needed without requiring an extra opt-in step.
if _env_enable is not None:
    ENABLE_OPENAI = _env_enable.lower() in ("1", "true", "yes")
else:
    ENABLE_OPENAI = bool(os.getenv("OPENAI_API_KEY"))

# Initialize client if enabled
client = OpenAI() if ENABLE_OPENAI else None

# Sentry error monitoring (no-op if SENTRY_DSN is unset)
if sentry_sdk:
    SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05"))
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN", ""),
        integrations=[
            FlaskIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ExcepthookIntegration(),
        ],
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        environment=os.getenv("SENTRY_ENV", os.getenv("ENV", "development")),
        send_default_pii=False,
    )
if ENABLE_OPENAI and client is not None:
    logger.info(
        "OpenAI generation ENABLED (client initialized). Set ENABLE_OPENAI=0 to disable."
    )
elif ENABLE_OPENAI and client is None:
    logger.warning(
        "OpenAI generation requested via ENABLE_OPENAI but client failed to initialize."
    )
else:
    logger.info(
        "OpenAI generation DISABLED. Set OPENAI_API_KEY or ENABLE_OPENAI=1 to enable."
    )

init_db()
SCOUT_INSTRUCTIONS = load_text_prompt("prompts/scout_instructions.txt")


def app_base_url() -> str:
    # Prefer explicit env for production (Render/proxy), fallback to request.host_url locally
    return (os.getenv("APP_BASE_URL") or request.host_url).rstrip("/")


# --------------------
# Pages
# --------------------
@app.get("/")
def landing():
    # New landing page
    return render_template(
        "landing.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
    )


@app.get("/app")
def app_page():
    # Existing app UI moved here
    return render_template(
        "index.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
    )


@app.get("/login")
def login_page():
    return render_template(
        "login.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
    )


@app.get("/auth/callback")
def auth_callback_page():
    return render_template(
        "auth_callback.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
    )


@app.get("/billing/success")
def billing_success():
    return render_template(
        "billing_success.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
    )


@app.get("/privacy")
def privacy_page():
    return render_template(
        "privacy.html",
        supabase_url=SUPABASE_URL,
        supabase_anon_key=SUPABASE_ANON_KEY,
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/render_md")
def api_render_md():
    try:
        _ = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    data = request.get_json(force=True) or {}
    md = data.get("md") or ""
    try:
        display_md = extract_display_md(md)
        html = md_to_safe_html(display_md)
        return jsonify({"html": html})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------
# Credits
# --------------------
@app.get("/api/credits")
def api_credits():
    try:
        user_id = require_user_id(request)
        # Grant welcome bonus on first-ever call for this user
        try:
            initialize_user_with_welcome_credits(user_id)
        except Exception:
            # Silently fail if already granted or DB error; user still gets balance
            pass
        return jsonify({"credits": get_balance(user_id)})
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        try:
            logger.exception(e)
        except Exception:
            pass
        # Graceful fallback: show zero credits when Postgres is unavailable
        return jsonify({"credits": 0, "error": "credits_unavailable"}), 200


@app.post("/api/dev/grant_credits")
def dev_grant_credits():
    # Enable only when DEV_TOOLS=1
    if os.getenv("DEV_TOOLS") != "1":
        return jsonify({"error": "disabled"}), 404

    try:
        user_id = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    data = request.get_json(force=True) or {}
    amount = int(data.get("amount", 0))
    if amount <= 0 or amount > 1000:
        return jsonify({"error": "amount must be 1..1000"}), 400

    new_balance = refund_credits(
        user_id,
        amount,
        reason="dev_grant",
        source_type="dev",
        source_id=f"dev_grant:{uuid.uuid4()}",
    )
    return jsonify({"credits": new_balance})


@app.get("/api/dev/inspect_reports")
def dev_inspect_reports():
    # DEV only: inspect the user's saved reports and compute similarity to a query
    if os.getenv("DEV_TOOLS") != "1":
        return jsonify({"error": "disabled"}), 404

    try:
        user_id = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or "200")
    except ValueError:
        limit = 200

    # Fetch recent reports metadata
    try:
        items = list_reports(user_id, q="", limit=limit)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    results = []
    for it in items:
        rid = int(it.get("id"))
        try:
            payload = get_report(user_id, rid) or {}
        except Exception:
            payload = {}

        player_name = it.get("player_name") or ""

        # compute similarity to q if provided
        score = None
        if q:
            try:
                a = normalize_name(q, transliterate=True)
                b = normalize_name(player_name, transliterate=True)
                if _HAS_RAPIDFUZZ and _token_sort_ratio is not None:
                    s1 = int(_token_sort_ratio(a, b) or 0)
                    s2 = 0
                    if _token_set_ratio is not None:
                        try:
                            s2 = int(_token_set_ratio(a, b) or 0)
                        except Exception:
                            s2 = 0
                    score = max(s1, s2)
                else:
                    score = int(SequenceMatcher(None, a, b).ratio() * 100)
            except Exception:
                score = None

        results.append(
            {
                "id": rid,
                "player_name": player_name,
                "created_at": it.get("created_at"),
                "cached": bool(it.get("cached")),
                "score_to_query": score,
                "payload": payload,
            }
        )

    # sort by score desc if q provided, else by created_at desc
    if q:
        results.sort(key=lambda x: (x.get("score_to_query") or 0), reverse=True)
    else:
        results.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    return jsonify({"items": results})


@app.post("/api/dev/send_email")
def dev_send_email():
    # Enable only when DEV_TOOLS=1
    if os.getenv("DEV_TOOLS") != "1":
        return jsonify({"error": "disabled"}), 404

    # Gate behind auth for consistency with other dev endpoints
    try:
        _ = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    data = request.get_json(force=True) or {}
    to = (data.get("to") or "").strip()
    subject = (data.get("subject") or "").strip()
    html = data.get("html")
    text = data.get("text")

    if not to or not subject or not (html or text):
        return jsonify({"error": "missing to, subject, and body (html or text)"}), 400

    try:
        resp = send_email(to=to, subject=subject, html=html, text=text)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    message_id = None
    try:
        message_id = resp.get("MessageId")  # type: ignore[attr-defined]
    except Exception:
        pass
    return jsonify({"ok": True, "message_id": message_id})


@app.get("/api/reports")
def api_reports():
    try:
        user_id = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or "50")  # Default to 50 for pagination
    except ValueError:
        limit = 50

    try:
        offset = int(request.args.get("offset") or "0")
    except ValueError:
        offset = 0

    # Prevent unbounded offsets and reasonable max limit
    offset = max(offset, 0)
    limit = min(limit, 100)  # Cap at 100 per request for performance

    try:
        items = list_reports(user_id, q=q, limit=limit, offset=offset)
        total = count_reports(user_id, q=q)
    except Exception as e:
        # Fallback to local SQLite cache in dev/offline to avoid blank UI
        try:
            logger.exception(e)
        except Exception:
            pass
        try:
            from db import list_local_reports

            items = list_local_reports(limit=limit)
            total = len(items)
        except Exception:
            return jsonify({"error": "reports_unavailable"}), 503

    # Lightweight caching hints for clients
    resp = jsonify({"items": items, "total": total})
    try:
        resp.headers["Cache-Control"] = "private, max-age=5"
    except Exception:
        pass
    return resp


@app.get("/api/reports/<int:report_id>")
def api_report(report_id: int):
    try:
        user_id = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    # Try Postgres library first (user-scoped). If that fails (missing row
    # or Postgres errors because the environment uses a dev user id), fall
    # back to local SQLite cache by id so client-suggested local rows can be
    # loaded directly without a separate endpoint.
    payload = None
    try:
        try:
            payload = get_report(user_id, report_id)
        except Exception:
            payload = None
    except Exception:
        payload = None

    if not payload:
        # Try local SQLite cache by id
        try:
            from db import connect

            conn = connect()
            cur = conn.execute(
                "SELECT player, report_md, use_web, model, created_at FROM reports WHERE id = ? LIMIT 1",
                (int(report_id),),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                payload = {
                    "player": row[0] or "",
                    "report_md": row[1] or "",
                    "use_web": bool(row[2]),
                    "model": row[3] or "",
                    "created_at": row[4] or None,
                    "cached": True,
                }
            else:
                return jsonify({"error": "Not found"}), 404
        except Exception:
            return jsonify({"error": "Not found"}), 404

    # Ensure rendered HTML is present for library-open flow
    try:
        report_md = payload.get("report_md", "") or ""
        display_md = extract_display_md(report_md)
        payload["report_html"] = md_to_safe_html(display_md)
    except Exception:
        payload.setdefault("report_html", "")

    # If structured fields are missing (e.g., payload came from Postgres with
    # minimal JSON), attempt to parse them from the stored markdown so the
    # client can render tables (season snapshot, last3_games, grades, info_fields).
    try:
        # Only populate if absent to avoid overwriting explicit payloads
        from utils.parse import (
            extract_grades,
            extract_info_fields,
            extract_last3_games,
            extract_season_snapshot,
            _split_height_weight,
        )

        report_md = payload.get("report_md", "") or ""

        if not payload.get("info_fields"):
            try:
                payload["info_fields"] = extract_info_fields(report_md)
            except Exception:
                payload["info_fields"] = {}
        
        # Post-process existing info_fields to split Height/Weight if needed
        try:
            _split_height_weight(payload.get("info_fields", {}))
        except Exception:
            pass

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
        # parsing failed — leave payload as-is
        pass

    # Helpful flags for UI
    payload["report_id"] = report_id
    payload["from_library"] = True
    return jsonify(payload)


@app.get("/api/reports/<int:report_id>/pdf")
def api_report_pdf(report_id: int):
    """Generate and download a scouting report as PDF using Playwright (Chromium)."""
    try:
        user_id = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    # Fetch report same way as /api/reports/<id>
    payload = None
    try:
        try:
            payload = get_report(user_id, report_id)
        except Exception:
            payload = None
    except Exception:
        payload = None

    if not payload:
        # Try local SQLite cache by id
        try:
            from db import connect

            conn = connect()
            cur = conn.execute(
                "SELECT player, report_md, use_web, model, created_at FROM reports WHERE id = ? LIMIT 1",
                (int(report_id),),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                payload = {
                    "player": row[0] or "",
                    "report_md": row[1] or "",
                    "use_web": bool(row[2]),
                    "model": row[3] or "",
                    "created_at": row[4] or None,
                    "cached": True,
                }
            else:
                return jsonify({"error": "Not found"}), 404
        except Exception:
            return jsonify({"error": "Not found"}), 404

    try:
        import asyncio
        import re
        from io import BytesIO
        from pathlib import Path
        from playwright.async_api import async_playwright

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
            if not data:
                return ""
            rows = "".join(
                f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in data.items() if v is not None and v != ""
            )
            if not rows:
                return ""
            return f"<h2>{title}</h2><table>{rows}</table>"

        def render_list_table(title: str, items: list) -> str:
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
            return f"<h2>{title}</h2><table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"

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
        table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 11px; }
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
            <meta charset=\"UTF-8\" />
            <style>{base_css}\n{tailwind_css}</style>
        </head>
        <body>
            <div class=\"page\">
                <div class=\"header\">
                    <div class=\"title\">Scout Report — {player_name}</div>
                    <div class=\"player\">{player_name}</div>
                </div>
                <div class=\"content\">{report_html}{extra_html}</div>
                <div class=\"footer\">Generated by Easyscout — Scout Reports made Easy</div>
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

        # Sanitize filename and prepend generation date (UTC)
        safe_name = re.sub(r"[^a-zA-Z0-9\s\-]", "", player_name).strip() or "report"
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"{date_str} {safe_name} - by Easyscout.pdf"

        from flask import send_file

        return send_file(
            BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"PDF generation failed: {str(e)}"}), 500


@app.post('/api/analytics')
def api_analytics():
    try:
        # allow analytics even when require_user_id falls back in dev
        user_id = None
        try:
            user_id = require_user_id(request)
        except Exception:
            user_id = None

        data = request.get_json(force=True) or {}
        event = data.get('event')
        props = data.get('properties') or {}
        if not event:
            return jsonify({'error': 'missing event'}), 400

        # Use distinct_id from frontend if provided, otherwise fallback to authenticated user_id
        distinct_id = props.pop('distinct_id', None) or user_id

        # Handle identity linking (merge anonymous with authenticated)
        if event == '$alias' and 'previous_id' in props:
            previous_id = props.pop('previous_id')
            if distinct_id and previous_id and distinct_id != previous_id:
                try:
                    alias_user(previous_id, distinct_id)
                except Exception:
                    pass
            return jsonify({'ok': True})

        try:
            track_event(distinct_id, event, props)
        except Exception:
            pass

        return jsonify({'ok': True})
    except PermissionError as e:
        return jsonify({'error': str(e)}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.get('/api/analytics_status')
def api_analytics_status():
    try:
        status = analytics_enabled()
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.post('/api/analytics_debug')
def api_analytics_debug():
    """Send a direct, one-off event using a fresh PostHog client and immediately shutdown.

    Useful to validate ingestion key, host, and network without relying on the
    app-level analytics client or background threads.
    """
    try:
        data = request.get_json(force=True) or {}
        event = data.get('event')
        properties = data.get('properties') or {}
        distinct_id = data.get('distinct_id') or None

        if not event:
            return jsonify({'error': 'missing event'}), 400

        key = os.getenv('POSTHOG_API_KEY')
        host = os.getenv('POSTHOG_HOST') or 'https://app.posthog.com'
        if not key:
            return jsonify({'error': 'missing POSTHOG_API_KEY in env'}), 500

        # Use a fresh PostHog client instance so we can shutdown immediately
        try:
            from posthog import Posthog as PH
            ph = PH(project_api_key=key, host=host)
            try:
                # Preferred signature: capture(event, properties=..., distinct_id=...)
                ph.capture(event, properties=properties, distinct_id=distinct_id or 'anonymous')
            except TypeError:
                # Fallback for older/newer module-level API
                try:
                    import posthog as ph_mod
                    ph_mod.capture(distinct_id or 'anonymous', event, properties=properties)
                except Exception:
                    raise
            finally:
                try:
                    ph.shutdown()
                except Exception:
                    pass

            return jsonify({'ok': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.post('/api/analytics_debug_raw')
def api_analytics_debug_raw():
    """Directly POST to the PostHog `/capture` HTTP endpoint and return the raw response.

    This helps verify ingestion at the HTTP level and shows the exact response body
    returned by PostHog for debugging.
    """
    try:
        data = request.get_json(force=True) or {}
        event = data.get('event')
        properties = data.get('properties') or {}
        distinct_id = data.get('distinct_id') or 'anonymous'

        if not event:
            return jsonify({'error': 'missing event'}), 400

        key = os.getenv('POSTHOG_API_KEY')
        host = os.getenv('POSTHOG_HOST') or 'https://app.posthog.com'
        if not key:
            return jsonify({'error': 'missing POSTHOG_API_KEY in env'}), 500

        import requests

        url = host.rstrip('/') + '/capture'
        payload = {
            'api_key': key,
            'event': event,
            'properties': {**properties, 'distinct_id': distinct_id},
        }

        r = requests.post(url, json=payload, timeout=10)
        try:
            body = r.json()
        except Exception:
            body = r.text

        return jsonify({'status_code': r.status_code, 'body': body})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.get("/api/dev/metrics")
def dev_metrics():
    try:
        _require_admin_user()
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    try:
        metrics = list_metrics()
        # Short human-readable descriptions for dashboard rows
        METRIC_DESCRIPTIONS = {
            "llm_calls": "Number of LLM generation requests attempted",
            "llm_success": "Successful LLM responses recorded",
            "embedding_calls": "Embedding generation operations",
            "cache_hits": "Local cache hit count for reports",
            "alias_hits": "Alias-based cache hits",
            "report_saves": "Number of reports saved to local DB",
            "report_db_reads": "Report reads from DB",
            "query_embedding_cache_hits": "Cached query embedding hits",
            "query_embedding_stores": "Stored query embeddings",
            "report_embedding_loads": "Loaded report embeddings",
            "report_embedding_stores": "Stored report embeddings",
            "fuzzy_auto_hits": "Fuzzy matching auto-accepts",
            "fuzzy_suggests": "Fuzzy match suggestions shown",
        }

        return jsonify({"metrics": metrics, "descriptions": METRIC_DESCRIPTIONS})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/metrics")
def metrics_endpoint():
    """Return both counter metrics and timing summaries for admins."""
    try:
        _require_admin_user()
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    try:
        counters = list_metrics()
        timings = list_timings()
        return jsonify({"metrics": counters, "timings": timings})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/dev/dashboard")
def dev_dashboard():
    try:
        _require_admin_user()
    except PermissionError as e:
        return (render_template("landing.html", error=str(e)), 403)

    return render_template("dev_metrics.html")


@app.post("/api/dev/seed_metrics")
def dev_seed_metrics():
    try:
        _require_admin_user()
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    try:
        # Lazy import to avoid startup cycles
        from utils.metrics import increment_metric, list_metrics

        for _ in range(3):
            increment_metric("llm_calls")
        for _ in range(5):
            increment_metric("cache_hits")
        increment_metric("embedding_calls", 2)
        increment_metric("report_saves", 1)

        return jsonify({"ok": True, "metrics": list_metrics()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/alias")
def api_alias():
    try:
        require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    data = request.get_json(force=True) or {}
    queried = (data.get("queried_player") or "").strip()
    player = (data.get("player") or "").strip()

    if not queried or not player:
        return jsonify({"error": "missing fields (queried_player, player)"}), 400

    try:
        # Import DB helpers locally to avoid circular import concerns
        from datetime import datetime, timezone

        from db import PROMPT_VERSION, _upsert_player_alias, connect, norm
        from utils.phonetic import phonetic_key

        p_norm = norm(player)
        q_norm = norm(queried)
        now = datetime.now(timezone.utc).isoformat()

        # Safety guard: avoid creating aliases when the queried name looks
        # like a moniker/nickname (no last-name token) that doesn't share
        # a last name with the canonical player. This prevents accidental
        # aliasing like mapping "Greek Freak" -> "Evan Mehdi Fournier".
        try:
            # extract last tokens
            p_parts = [t for t in re.sub(r"[^\w\s]", " ", player).split() if t]
            q_parts = [t for t in re.sub(r"[^\w\s]", " ", queried).split() if t]
            p_last = p_parts[-1].lower() if len(p_parts) >= 1 else ""
            q_last = q_parts[-1].lower() if len(q_parts) >= 1 else ""

            # If queried has only one token (likely a nickname) and that token
            # does not match the canonical last name (by exact or phonetic),
            # refuse to create the alias and ask user to confirm with full name.
            if len(q_parts) == 1 and p_last:
                pk = phonetic_key(p_last) or ""
                qk = phonetic_key(q_last) or ""
                if p_last != q_last and pk != qk:
                    return (
                        jsonify(
                            {
                                "error": "Alias looks like a nickname or moniker. Please confirm by providing the full name (first + last) when creating an alias.",
                            }
                        ),
                        400,
                    )
        except Exception:
            # If any of the checks fail, continue with the upsert as before
            pass

        with connect() as conn:
            # upsert alias
            _upsert_player_alias(conn, p_norm, queried, q_norm)

            # Also update the canonical reports row's queried_player to reflect
            # the most-recent accepted query so the UI shows the latest example.
            # Update the newest report for that player_norm and prompt_version.
            conn.execute(
                """
                UPDATE reports SET queried_player = ?, queried_player_norm = ?, created_at = ?
                WHERE id = (
                    SELECT id FROM reports WHERE player_norm = ? AND prompt_version = ? ORDER BY created_at DESC LIMIT 1
                )
                """,
                (queried, q_norm, now, p_norm, PROMPT_VERSION),
            )

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------
# Stripe Checkout
# --------------------
@app.post("/api/stripe/create-checkout-session")
def stripe_create_checkout_session():
    if not stripe.api_key:
        return (
            jsonify({"error": "Stripe not configured (missing STRIPE_SECRET_KEY)"}),
            500,
        )

    try:
        user_id = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    data = request.get_json(force=True) or {}
    credits = int(data.get("credits", 0))
    if credits <= 0 or credits > 1000:
        return jsonify({"error": "credits must be between 1 and 1000"}), 400

    b = app_base_url()

    # 1 credit = €1 => 100 cents, quantity = credits
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "eur",
                    "unit_amount": 100,
                    "product_data": {"name": "Scoutbot report credits"},
                },
                "quantity": credits,
            }
        ],
        success_url=f"{b}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{b}/app",
        client_reference_id=user_id,
        metadata={"user_id": user_id, "credits": str(credits)},
    )

    return jsonify({"url": session.url})


@app.post("/api/stripe/webhook")
def stripe_webhook():
    whsec = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not whsec:
        return (
            jsonify(
                {
                    "error": "Stripe webhook not configured (missing STRIPE_WEBHOOK_SECRET)"
                }
            ),
            500,
        )

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, whsec)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # Convert StripeObject -> plain dict for storage (safe for JSONB)
    try:
        event_dict = event.to_dict()  # stripe-python objects support to_dict()
    except Exception:
        event_dict = dict(event) if isinstance(event, dict) else {"raw": str(event)}

    # Idempotent event processing: only act the first time we see this event_id
    try:
        first_time = record_stripe_event(
            event_dict.get("id", ""), event_dict.get("type", ""), event_dict
        )
    except Exception:
        # If logging fails, don't block Stripe retries
        first_time = True

    if not first_time:
        return {"ok": True}

    etype = event_dict.get("type")

    if etype in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    ):
        session = (event_dict.get("data") or {}).get("object") or {}
        payment_status = session.get("payment_status")
        if payment_status in ("paid", "no_payment_required"):
            meta = session.get("metadata") or {}
            user_id = meta.get("user_id") or session.get("client_reference_id")
            credits_str = meta.get("credits")
            session_id = session.get("id")

            if user_id and credits_str and session_id:
                credits = int(credits_str)

                # Optional bookkeeping
                try:
                    amount_total = int(session.get("amount_total") or 0)
                    currency = (session.get("currency") or "eur").lower()
                    record_stripe_purchase(
                        user_id=user_id,
                        session_id=session_id,
                        amount_cents=amount_total,
                        currency=currency,
                        credits=credits,
                    )
                except Exception:
                    pass

                # Grant credits (idempotent via credit_ledger unique index on source_type/source_id)
                try:
                    refund_credits(
                        user_id,
                        credits,
                        reason="purchase",
                        source_type="stripe_session",
                        source_id=session_id,
                    )
                except Exception:
                    # Don't fail webhook; Stripe will retry if needed
                    pass

    return {"ok": True}


@app.post("/api/stripe/confirm")
def stripe_confirm():
    if not stripe.api_key:
        return (
            jsonify({"error": "Stripe not configured (missing STRIPE_SECRET_KEY)"}),
            500,
        )

    try:
        user_id = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    data = request.get_json(force=True) or {}
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"error": "missing session_id"}), 400

    session = stripe.checkout.Session.retrieve(session_id)

    if session.get("payment_status") not in ("paid", "no_payment_required"):
        return jsonify({"error": "not paid yet"}), 409

    meta = session.get("metadata") or {}
    if (meta.get("user_id") or "") != user_id:
        return jsonify({"error": "session does not belong to this user"}), 403

    credits = int(meta.get("credits", "0") or "0")
    if credits <= 0:
        return jsonify({"error": "invalid credits"}), 400

    # Optional bookkeeping
    try:
        amount_total = int(session.get("amount_total") or 0)
        currency = (session.get("currency") or "eur").lower()
        record_stripe_purchase(
            user_id=user_id,
            session_id=session_id,
            amount_cents=amount_total,
            currency=currency,
            credits=credits,
        )
    except Exception:
        pass

    new_balance = refund_credits(
        user_id,
        credits,
        reason="purchase",
        source_type="stripe_session",
        source_id=session_id,  # same idempotency key as webhook
    )
    return jsonify({"credits": new_balance})


@app.context_processor
def inject_supabase():
    return {
        "supabase_url": os.environ.get("SUPABASE_URL"),
        "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY"),
    }


def _require_admin_user():
    """Require the current user to be an admin for dev endpoints.

    Admins are configured via the `ADMIN_USERS` env var as a comma-separated
    list of user_ids. If `ADMIN_USERS` is not set, fall back to requiring
    `DEV_TOOLS=1` and any authenticated user.
    """
    if os.getenv("ADMIN_USERS"):
        try:
            user_id = require_user_id(request)
        except PermissionError as e:
            raise PermissionError(str(e))
        admins = [
            s.strip() for s in os.getenv("ADMIN_USERS", "").split(",") if s.strip()
        ]
        if user_id not in admins:
            raise PermissionError("not an admin")
        return user_id

    # No explicit admin list configured; require DEV_TOOLS=1 and authenticated
    if os.getenv("DEV_TOOLS") != "1":
        raise PermissionError("dev tools disabled")
    return require_user_id(request)


# --------------------
# Save Suggested Report (free, saves another user's report to current user's library)
# --------------------


@app.post("/api/save_suggestion")
def save_suggestion():
    """
    Accept a suggested report from another user and save it to the current user's library.
    This is a FREE operation (doesn't charge credits).
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


# --------------------
# Scout (requires login + costs 1 credit)
# --------------------


@app.post("/api/scout")
def scout():
    try:
        user_id = require_user_id(request)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401

    data = request.get_json(force=True) or {}

    player = (data.get("player") or "").strip()
    if not player:
        return jsonify({"error": "Missing required field: player"}), 400

    team = (data.get("team") or "").strip()
    league = (data.get("league") or "").strip()
    season = (data.get("season") or "").strip()

    use_web = bool(data.get("use_web", False))
    refresh = bool(data.get("refresh", False))
    report_id_to_update = data.get("report_id")  # For regenerating existing reports
    accept_suggestion = bool(data.get("accept_suggestion", False))  # For accepting suggestions
    suggestion_report_id = data.get("suggestion_report_id")  # Source report ID when accepting

    # HANDLE SUGGESTION ACCEPTANCE: Check if already have it first, then charge only if new
    if accept_suggestion and suggestion_report_id:
        logger.info(f"[ACCEPT_SUGGESTION] Starting for report_id={suggestion_report_id}, player='{player}'")
        try:
            suggestion_report_id = int(suggestion_report_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid suggestion_report_id"}), 400
        
        # Fetch the source report (Postgres first, then local SQLite fallback)
        logger.info(f"[ACCEPT_SUGGESTION] Fetching source report from Postgres...")
        source_report = None
        try:
            source_report = get_report_by_id(suggestion_report_id)
            logger.info(f"[ACCEPT_SUGGESTION] Postgres fetch: {'SUCCESS' if source_report else 'NOT_FOUND'}")
        except Exception:
            source_report = None

        if not source_report:
            # Fallback: try local SQLite cache (may happen if embedding suggestions reference local IDs)
            logger.info(f"[ACCEPT_SUGGESTION] Postgres fetch failed, trying SQLite fallback...")
            try:
                from db import connect

                conn = connect()
                row = conn.execute(
                    "SELECT player, report_md, team, league, season, use_web, model, created_at FROM reports WHERE id = ? LIMIT 1",
                    (suggestion_report_id,),
                ).fetchone()
                conn.close()
                if row:
                    logger.info(f"[ACCEPT_SUGGESTION] SQLite fallback: SUCCESS")
                    source_report = {
                        "player": row[0] or "",
                        "report_md": row[1] or "",
                        "team": row[2] or "",
                        "league": row[3] or "",
                        "season": row[4] or "",
                        "use_web": bool(row[5]),
                        "model": row[6] or "",
                        "created_at": row[7] or None,
                        "cached": False,
                    }
                else:
                    logger.info(f"[ACCEPT_SUGGESTION] SQLite fallback: NOT_FOUND")
            except Exception as e:
                logger.error(f"[ACCEPT_SUGGESTION] SQLite fallback error: {e}")
                source_report = None

        if not source_report:
            logger.error(f"[ACCEPT_SUGGESTION] Source report not found anywhere")
            return jsonify({"error": f"Source report not found"}), 404
        
        # Check if user already has a report with the SOURCE report's canonical name
        # (not the user's typed query, which might have typos)
        # Use the source report's player name since fuzzy matching already determined they're the same player
        logger.info(f"[ACCEPT_SUGGESTION] Checking if user already has this report...")
        source_player_name = source_report.get("player", "")
        
        def _canonical_query_name(name: str) -> str:
            norm = normalize_name(name, transliterate=True)
            parts = norm.split()
            if not parts:
                return norm
            first = parts[0]
            first_canon = NICKNAME_MAP.get(first, first)
            parts[0] = first_canon
            return " ".join(parts)

        # Use SOURCE report's player name (the correct one without typos)
        canonical_query_player = _canonical_query_name(source_player_name)
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
            
            return jsonify(payload)
        
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
                return jsonify({"error": "Insufficient credits"}), 402
            raise
        
        # Save the suggestion to the current user's library (reusing the existing report data)
        try:
            # get_report_by_id returns the payload directly (not wrapped)
            source_payload = source_report or {}
            source_md = source_payload.get("report_md", "")
            
            # Prepare payload for insertion - ensure it has all structured fields
            # Use the SOURCE report's proper name, not the user's typed query
            payload = dict(source_payload)  # Make a copy
            payload["cached"] = False  # User paid 1 credit for this cross-user suggestion
            payload["report_md"] = source_md
            # Use SOURCE report's player name (proper-cased), not user's query
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
                "player": canonical_query_player,  # Use canonical name for deduplication
                "team": team,
                "league": league,
                "season": season,
                "use_web": True,
            }
            
            pg_id = insert_report(
                user_id=user_id,
                player_name=source_player_name,  # Use SOURCE report's proper-cased name
                query_obj=insert_query_obj,
                report_md=source_md,
                payload=payload,  # Use enhanced payload
                cached=False,  # User paid 1 credit
            )
            
            # Update payload with IDs and credits for return
            payload["created_at"] = source_report.get("created_at")
            payload["report_id"] = pg_id
            payload["library_id"] = pg_id
            payload["credits_remaining"] = new_balance
            
            # Ensure HTML is present
            try:
                display_md = extract_display_md(source_md or "")
                payload["report_html"] = md_to_safe_html(display_md)
            except Exception:
                payload.setdefault("report_html", "")
            
            return jsonify(payload)
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
            return jsonify({"error": f"Failed to save suggestion: {str(e)}"}), 500

    # Canonicalize player for deduplication (nickname → formal)
    def _canonical_query_name(name: str) -> str:
        norm = normalize_name(name, transliterate=True)
        parts = norm.split()
        if not parts:
            return norm
        first = parts[0]
        first_canon = NICKNAME_MAP.get(first, first)
        parts[0] = first_canon
        return " ".join(parts)

    canonical_query_player = _canonical_query_name(player)

    # This defines "same report" for the user's library.
    # (Keep refresh inside if you want refresh=true to be a different saved report;
    # if you DON'T want that, tell me and I’ll adjust.)
    query_obj = {
        "player": canonical_query_player,
        "team": team,
        "league": league,
        "season": season,
        "use_web": True,  # Always True since server generates with web search
    }
    query_key = make_query_key(query_obj)

    # ✅ FREE if already in the user's library (don't charge a credit)
    # If the exact same query_key exists for this user, return it immediately.
    # But skip if this is an explicit regeneration (report_id provided + refresh=true)
    existing = find_report_by_query_key(user_id, query_key)
    logger.info(f"[MATCH] Query key lookup: {'HIT' if existing else 'MISS'}")
    if existing and not (report_id_to_update and refresh):
        owned_payload = existing.get("payload") or {}
        # Ensure canonical markdown is present
        owned_payload["report_md"] = existing.get("report_md") or owned_payload.get(
            "report_md", ""
        )

        # If older records didn't store pre-rendered HTML, render it now from the stored markdown
        try:
            display_md = extract_display_md(owned_payload.get("report_md", "") or "")
            # Recompute HTML from markdown to ensure any stored/old HTML is refreshed
            # (don't trust potentially stale `report_html` in older DB rows).
            owned_payload["report_html"] = md_to_safe_html(display_md)
        except Exception:
            owned_payload.setdefault("report_html", "")

        # Parse structured pieces so the client can render tables (season snapshot,
        # last3_games, grades, info_fields). Some old payloads only stored
        # raw markdown in Postgres and didn't include these fields.
        parsed_ok = False
        try:
            from utils.parse import (
                extract_grades,
                extract_info_fields,
                extract_last3_games,
                extract_season_snapshot,
                _split_height_weight,
            )

            report_md_local = owned_payload.get("report_md", "") or ""
            if not owned_payload.get("info_fields"):
                try:
                    owned_payload["info_fields"] = extract_info_fields(report_md_local)
                except Exception:
                    owned_payload["info_fields"] = {}
            
            # Post-process existing info_fields to split Height/Weight if needed
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
                    owned_payload["season_snapshot"] = extract_season_snapshot(
                        report_md_local
                    )
                except Exception:
                    owned_payload["season_snapshot"] = {}

            if not owned_payload.get("last3_games"):
                try:
                    owned_payload["last3_games"] = extract_last3_games(report_md_local)
                except Exception:
                    owned_payload["last3_games"] = []
        except Exception:
            # If parsing fails just continue — we already provided report_html
            parsed_ok = False
        finally:
            owned_payload["parsed_from_md"] = bool(parsed_ok)

        # Ensure structured fields are present (best-effort)
        try:
            owned_payload = ensure_parsed_payload(owned_payload)
        except Exception:
            pass

        owned_payload["cached"] = True  # it's a library hit
        owned_payload["created_at"] = existing.get("created_at")
        owned_payload["report_id"] = existing.get("id")
        try:
            increment_metric("cache_hits")
        except Exception:
            pass
        owned_payload["credits_remaining"] = get_balance(user_id)
        return jsonify(owned_payload)

    # QUICK LOCAL CACHE: consult SQLite cache index to find if player exists, then fetch from Postgres
    if not refresh:
        try:
            # Check if player exists in cache (by name or alias)
            alias = find_canonical_by_alias(player)
            logger.info(f"[MATCH] Alias lookup: {'HIT → ' + alias.get('player_norm') if alias and alias.get('player_norm') else 'MISS'}")
            if alias and alias.get("player_norm"):
                canonical_name = alias.get("player_norm")
                try:
                    increment_metric("alias_hits")
                except Exception:
                    pass
            else:
                # Check if exact player exists in cache
                local = get_cached_report(
                    player, team=team, league=league, season=season, use_web=False
                )
                if local:
                    canonical_name = local.get("player") or player
                else:
                    canonical_name = None
            
            # If found in cache, fetch from Postgres using the canonical name
            if canonical_name:
                try:
                    # Build query key for Postgres lookup
                    cache_query_obj = {
                        "player": canonical_name,
                        "team": team,
                        "league": league,
                        "season": season,
                        "use_web": True,
                        "refresh": False,
                    }
                    cache_query_key = make_query_key(cache_query_obj)
                    
                    # Try to find in user's Postgres library
                    pg_report = find_report_by_query_key(user_id, cache_query_key)
                    if pg_report:
                        owned_payload = pg_report.get("payload") or {}
                        owned_payload["report_md"] = pg_report.get("report_md") or owned_payload.get("report_md", "")
                        
                        # Ensure HTML and structured fields are present
                        try:
                            display_md = extract_display_md(owned_payload.get("report_md", "") or "")
                            owned_payload["report_html"] = md_to_safe_html(display_md)
                        except Exception:
                            owned_payload.setdefault("report_html", "")
                        
                        try:
                            owned_payload = ensure_parsed_payload(owned_payload)
                        except Exception:
                            pass
                        
                        try:
                            increment_metric("cache_hits")
                        except Exception:
                            pass
                        
                        owned_payload["cached"] = True
                        owned_payload["created_at"] = pg_report.get("created_at")
                        owned_payload["report_id"] = pg_report.get("id")
                        owned_payload["credits_remaining"] = get_balance(user_id)
                        return jsonify(owned_payload)
                except Exception as e:
                    # If Postgres lookup fails, continue to generation
                    logger.warning(f"Cache hit but Postgres fetch failed: {e}")
                    pass
        except Exception:
            pass

    # If user did not request a forced refresh, try similarity matching
    # Priority: Embeddings (fast, semantic) → Fuzzy (token-based) → LLM
    if not refresh:
        try:
            # STEP 1: Try embedding-based similarity (fast, semantic)
            # Embeddings run FIRST because they're fast and catch semantic equivalents
            try:
                if league and league.strip():
                    embed_auto, embed_suggest = 0.95, 0.75
                else:
                    embed_auto, embed_suggest = 0.95, 0.78
                
                embed_similar = _find_by_embedding_similarity(
                    user_id,
                    player,
                    team=team,
                    league=league,
                    client=client,
                    auto_threshold=embed_auto,
                    suggest_threshold=embed_suggest,
                    max_scan=50,
                )
                if embed_similar:
                    logger.info(f"[MATCH] Embedding match: type={embed_similar.get('type')}, score={embed_similar.get('score')}, report_id={embed_similar.get('report_id')}")
                else:
                    logger.info("[MATCH] Embedding match: MISS")
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
                        return jsonify(payload)
                    elif embed_similar.get("type") == "suggest":
                        # Return suggestion from embedding match
                        suggestion_report_id = embed_similar.get("report_id")
                        suggestion_payload = None
                        try:
                            from db_pg import get_report
                            suggestion_payload = get_report(user_id, int(suggestion_report_id))
                        except Exception:
                            suggestion_payload = None
                        
                        return jsonify(
                            {
                                "match_suggestion": {
                                    "report_id": suggestion_report_id,
                                    "player_name": embed_similar.get("player_name"),
                                    "team": suggestion_payload.get("team") if suggestion_payload else team,
                                    "league": suggestion_payload.get("league") if suggestion_payload else league,
                                    "score": embed_similar.get("score"),
                                },
                                "auto_matched": False,
                                "credits_remaining": get_balance(user_id),
                                "note": "Similar player found in your library",
                            }
                        )
            except Exception as e:
                logger.debug(f"Embedding similarity check failed: {e}")
                pass  # Fall through to fuzzy matching
            
            # STEP 2: Fall back to fuzzy-match lookup if embeddings didn't match
            if league and league.strip():
                pre_auto, pre_suggest = 78, 68
            else:
                pre_auto, pre_suggest = 88, 75

            pre_similar = _best_similar_report(
                user_id,
                player,
                team=team,
                league=league,
                client=client,
                auto_threshold=pre_auto,
                suggest_threshold=pre_suggest,
                max_scan=200,
                transliterate=True,
            )
            if pre_similar:
                logger.info(f"[MATCH] Fuzzy match: type={pre_similar.get('type')}, score={pre_similar.get('score')}, report_id={pre_similar.get('report_id')}")
            else:
                logger.info("[MATCH] Fuzzy match: MISS")
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
                    return jsonify(payload)
                elif pre_similar.get("type") == "suggest":
                    # Try to fetch the suggested report payload (Postgres or local)
                    suggestion_payload = None
                    try:
                        # Prefer Postgres get_report, fallback to local SQLite by id
                        try:
                            suggestion_payload = get_report(
                                user_id, int(pre_similar.get("report_id"))
                            )
                        except Exception:
                            # Fallback: try reading from local SQLite first
                            from db import connect

                            conn = connect()
                            row = conn.execute(
                                "SELECT player, report_md, team, league, season, use_web, model, created_at FROM reports WHERE id = ? LIMIT 1",
                                (int(pre_similar.get("report_id")),),
                            ).fetchone()
                            conn.close()
                            if row:
                                report_md = row[1] or ""
                                from utils.parse import (
                                    extract_display_md,
                                    extract_grades,
                                    extract_info_fields,
                                    extract_last3_games,
                                    extract_season_snapshot,
                                )

                                display_md = extract_display_md(report_md)
                                suggestion_payload = {
                                    "player": row[0] or "",
                                    "report_md": report_md,
                                    "report_html": md_to_safe_html(display_md),
                                    "team": row[2] or "",
                                    "league": row[3] or "",
                                    "season": row[4] or "",
                                    "use_web": bool(row[5]),
                                    "model": row[6] or "",
                                    "created_at": row[7] or None,
                                    "cached": True,
                                    "info_fields": extract_info_fields(report_md),
                                    "grades": (lambda g: g[0])(
                                        extract_grades(report_md)
                                    ),
                                    "final_verdict": (lambda g: g[1])(
                                        extract_grades(report_md)
                                    ),
                                    "season_snapshot": extract_season_snapshot(
                                        report_md
                                    ),
                                    "last3_games": extract_last3_games(report_md),
                                }
                                try:
                                    suggestion_payload = ensure_parsed_payload(
                                        suggestion_payload
                                    )
                                except Exception:
                                    pass
                                # Persist Postgres-sourced suggestion into local SQLite so
                                # the library-open codepath is identical.
                                try:
                                    sp_player = (
                                        suggestion_payload.get("player")
                                        or suggestion_payload.get("player_name")
                                        or pre_similar.get("player_name")
                                    )
                                    sp_report_md = (
                                        suggestion_payload.get("report_md") or ""
                                    )
                                    sp_team = suggestion_payload.get("team") or ""
                                    sp_league = suggestion_payload.get("league") or ""
                                    sp_season = suggestion_payload.get("season") or ""
                                    sp_use_web = bool(suggestion_payload.get("use_web"))
                                    sp_model = suggestion_payload.get("model") or ""
                                    if sp_player and sp_report_md:
                                        try:
                                            new_id = db.save_report(
                                                sp_player,
                                                sp_report_md,
                                                team=sp_team,
                                                league=sp_league,
                                                season=sp_season,
                                                use_web=sp_use_web,
                                                model=sp_model,
                                                queried_player=player,
                                            )
                                            suggestion_payload["report_id"] = new_id
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            else:
                                # Try a direct Postgres read that doesn't rely on psycopg get_report wrapper.
                                try:
                                    pool = db_pg._get_pool()
                                    with pool.connection() as conn_pg, conn_pg.cursor() as cur:
                                        cur.execute(
                                            "SELECT payload, report_md, player_name, created_at, cached FROM public.reports WHERE id = %s LIMIT 1",
                                            (int(pre_similar.get("report_id")),),
                                        )
                                        prow = cur.fetchone()
                                    if prow:
                                        payload_row, report_md = prow[0], prow[1] or ""
                                        if payload_row:
                                            suggestion_payload = payload_row
                                            if (
                                                isinstance(suggestion_payload, dict)
                                                and "report_md"
                                                not in suggestion_payload
                                            ):
                                                suggestion_payload["report_md"] = (
                                                    report_md
                                                )
                                        else:
                                            from utils.parse import extract_display_md

                                            display_md = extract_display_md(report_md)
                                            suggestion_payload = {
                                                "player": prow[2] or "",
                                                "report_md": report_md,
                                                "report_html": md_to_safe_html(
                                                    display_md
                                                ),
                                                "created_at": prow[3] or None,
                                                "cached": bool(prow[4]),
                                            }
                                        try:
                                            suggestion_payload = ensure_parsed_payload(
                                                suggestion_payload
                                            )
                                        except Exception:
                                            pass
                                    # Persist Postgres-sourced suggestion into local SQLite so
                                    # the library-open codepath is identical.
                                    try:
                                        sp_player = (
                                                suggestion_payload.get("player")
                                                or suggestion_payload.get("player_name")
                                                or pre_similar.get("player_name")
                                        )
                                        sp_report_md = (
                                            suggestion_payload.get("report_md") or ""
                                        )
                                        sp_team = suggestion_payload.get("team") or ""
                                        sp_league = (
                                            suggestion_payload.get("league") or ""
                                        )
                                        sp_season = (
                                            suggestion_payload.get("season") or ""
                                        )
                                        sp_use_web = bool(
                                            suggestion_payload.get("use_web")
                                        )
                                        sp_model = suggestion_payload.get("model") or ""
                                        if sp_player and sp_report_md:
                                            try:
                                                new_id = db.save_report(
                                                    sp_player,
                                                    sp_report_md,
                                                    team=sp_team,
                                                    league=sp_league,
                                                    season=sp_season,
                                                    use_web=sp_use_web,
                                                    model=sp_model,
                                                    queried_player=player,
                                                )
                                                suggestion_payload["report_id"] = new_id
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                except Exception:
                                    pass
                    except Exception:
                        suggestion_payload = None

                    return jsonify(
                        {
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
                        }
                    )

            # Fallback to the previous (slightly stricter) check for suggestions
            if league and league.strip():
                sim_auto, sim_suggest = 84, 74
            else:
                sim_auto, sim_suggest = 88, 75

            similar = _best_similar_report(
                user_id,
                player,
                team=team,
                league=league,
                client=client,
                auto_threshold=sim_auto,
                suggest_threshold=sim_suggest,
                max_scan=300,
                transliterate=True,
            )
            if similar:
                if similar.get("type") == "auto":
                    payload = similar.get("payload") or {}
                    payload["auto_matched"] = True
                    try:
                        payload = ensure_parsed_payload(payload)
                    except Exception:
                        pass
                    payload["credits_remaining"] = get_balance(user_id)
                    return jsonify(payload)
                elif similar.get("type") == "suggest":
                    # As above, attach the full report payload when available
                    suggestion_payload = None
                    try:
                        try:
                            suggestion_payload = get_report(
                                user_id, int(similar.get("report_id"))
                            )
                        except Exception:
                            # Fallback: try reading from local SQLite first
                            from db import connect

                            conn = connect()
                            row = conn.execute(
                                "SELECT player, report_md, team, league, season, use_web, model, created_at FROM reports WHERE id = ? LIMIT 1",
                                (int(similar.get("report_id")),),
                            ).fetchone()
                            conn.close()
                            if row:
                                report_md = row[1] or ""
                                from utils.parse import (
                                    extract_display_md,
                                    extract_grades,
                                    extract_info_fields,
                                    extract_last3_games,
                                    extract_season_snapshot,
                                )

                                display_md = extract_display_md(report_md)
                                suggestion_payload = {
                                    "player": row[0] or "",
                                    "report_md": report_md,
                                    "report_html": md_to_safe_html(display_md),
                                    "team": row[2] or "",
                                    "league": row[3] or "",
                                    "season": row[4] or "",
                                    "use_web": bool(row[5]),
                                    "model": row[6] or "",
                                    "created_at": row[7] or None,
                                    "cached": True,
                                    "info_fields": extract_info_fields(report_md),
                                    "grades": (lambda g: g[0])(
                                        extract_grades(report_md)
                                    ),
                                    "final_verdict": (lambda g: g[1])(
                                        extract_grades(report_md)
                                    ),
                                    "season_snapshot": extract_season_snapshot(
                                        report_md
                                    ),
                                    "last3_games": extract_last3_games(report_md),
                                }
                            else:
                                # Try a direct Postgres read that doesn't rely on psycopg get_report wrapper.
                                try:
                                    pool = db_pg._get_pool()
                                    with pool.connection() as conn_pg, conn_pg.cursor() as cur:
                                        cur.execute(
                                            "SELECT payload, report_md, player_name, created_at, cached FROM public.reports WHERE id = %s LIMIT 1",
                                            (int(similar.get("report_id")),),
                                        )
                                        prow = cur.fetchone()
                                    if prow:
                                        payload_row, report_md = prow[0], prow[1] or ""
                                        if payload_row:
                                            suggestion_payload = payload_row
                                            if (
                                                isinstance(suggestion_payload, dict)
                                                and "report_md"
                                                not in suggestion_payload
                                            ):
                                                suggestion_payload["report_md"] = (
                                                    report_md
                                                )
                                        else:
                                            from utils.parse import (
                                                extract_display_md,
                                            )

                                            display_md = extract_display_md(report_md)
                                            suggestion_payload = {
                                                "player": prow[2] or "",
                                                "report_md": report_md,
                                                "report_html": md_to_safe_html(
                                                    display_md
                                                ),
                                                "created_at": prow[3] or None,
                                                "cached": bool(prow[4]),
                                            }
                                except Exception:
                                    pass
                    except Exception:
                        suggestion_payload = None

                    return jsonify(
                        {
                            "match_suggestion": {
                                "report_id": similar.get("report_id"),
                                "player_name": similar.get("player_name"),
                                "team": suggestion_payload.get("team") if suggestion_payload else None,
                                "league": suggestion_payload.get("league") if suggestion_payload else None,
                                "score": similar.get("score"),
                                "report_payload": suggestion_payload,
                            },
                            "auto_matched": False,
                            "credits_remaining": get_balance(user_id),
                        }
                    )
        except Exception:
            # If fuzzy lookup fails, continue to normal flow
            pass

    # Otherwise: check balance, generate report, then charge only on success
    request_id = str(uuid.uuid4())

    # Pre-check balance to avoid generating when user has no credits
    try:
        if get_balance(user_id) < 1:
            return (
                jsonify(
                    {
                        "error": "Insufficient credits. Please top up.",
                        "credits": get_balance(user_id),
                    }
                ),
                402,
            )
    except Exception:
        # If balance check fails, fail fast
        return jsonify({"error": "Could not verify credits"}), 500

    # Server-side override: always use web search when generating a new report
    generation_use_web = True

    # Track generation start
    try:
        track_event(user_id, "report_gen_started", {
            "player": player,
            "team": team,
            "league": league,
            "use_web": generation_use_web,
            "refresh": refresh
        })
    except Exception:
        pass

    # Safety: avoid making OpenAI generation calls unless explicitly enabled.
    if client is None:
        return (
            jsonify(
                {"error": "OpenAI generation disabled (set ENABLE_OPENAI=1 to enable)."}
            ),
            503,
        )

    try:
        payload = get_or_generate_scout_report(
            client=client,
            model=MODEL,
            scout_instructions=SCOUT_INSTRUCTIONS,
            player=player,
            team=team,
            league=league,
            season=season,
            use_web=generation_use_web,
            refresh=refresh,
            user_id=user_id,
        )
    except Exception as e:
        # Track generation failure
        try:
            track_event(user_id, "generation_failed", {
                "player": player,
                "error": str(e),
                "error_type": type(e).__name__
            })
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500

    # Detect prompt-specified sentinel for missing player
    report_md = (payload.get("report_md") or "").strip()
    if report_md.startswith("PLAYER_NOT_FOUND:"):
        # Track player not found
        try:
            track_event(user_id, "generation_failed", {
                "player": player,
                "error": report_md,
                "error_type": "player_not_found"
            })
        except Exception:
            pass
        
        # Attempt a fallback fuzzy-match against the user's saved reports
        try:
            # Fallback fuzzy-match after a PLAYER_NOT_FOUND sentinel from the model.
            if league and league.strip():
                fb_auto, fb_suggest = 88, 75
            else:
                fb_auto, fb_suggest = 92, 78

            fb = _best_similar_report(
                user_id,
                player,
                team=team,
                league=league,
                client=client,
                auto_threshold=fb_auto,
                suggest_threshold=fb_suggest,
                max_scan=300,
                transliterate=True,
            )
            if fb:
                if fb.get("type") == "auto":
                    payload = fb.get("payload") or {}
                    payload["auto_matched"] = True
                    try:
                        payload = ensure_parsed_payload(payload)
                    except Exception:
                        pass
                    payload["credits_remaining"] = get_balance(user_id)
                    # Return cached payload (no charge)
                    return jsonify(payload)
                elif fb.get("type") == "suggest":
                    # Fetch team/league info for the suggestion
                    fb_team = None
                    fb_league = None
                    try:
                        from db_pg import get_report
                        fb_payload = get_report(user_id, int(fb.get("report_id")))
                        if fb_payload:
                            fb_team = fb_payload.get("team")
                            fb_league = fb_payload.get("league")
                    except Exception:
                        pass
                    
                    return jsonify(
                        {
                            "match_suggestion": {
                                "report_id": fb.get("report_id"),
                                "player_name": fb.get("player_name"),
                                "team": fb_team,
                                "league": fb_league,
                                "score": fb.get("score"),
                            },
                            "auto_matched": False,
                            "credits_remaining": get_balance(user_id),
                            "note": "Original generation returned PLAYER_NOT_FOUND; a close cached match was found.",
                        }
                    )
        except Exception:
            pass

        return jsonify({"error": report_md}), 400

    # Charge now that generation succeeded
    logger.info(f"[GENERATION] LLM generated report for '{player}' → charging 1 credit")
    try:
        new_balance = spend_credits(
            user_id,
            1,
            reason="report",
            source_type="scout_request",
            source_id=request_id,
        )
    except ValueError as e:
        if str(e) == "INSUFFICIENT_CREDITS":
            return (
                jsonify(
                    {
                        "error": "Insufficient credits. Please top up.",
                        "credits": get_balance(user_id),
                    }
                ),
                402,
            )
        return jsonify({"error": str(e)}), 500

    # Save/Upsert into Postgres library (1 row per user/query_key)
    try:
        # Force newly generated reports to be marked non-cached both in the DB flag
        # and in the returned payload so the UI doesn't show "from your library".
        payload["cached"] = False
        cached_flag = False

        # Persist with `use_web=True` since generation used web search
        insert_query_obj = dict(query_obj)
        insert_query_obj["use_web"] = generation_use_web

        # Prefer canonical player name extracted from the payload when available
        canonical_player = (
            payload.get("player") or payload.get("player_name") or player
        ).strip()
        # Use canonical query player (from user input) for query_key deduplication, not LLM's player name
        insert_query_obj["player"] = canonical_query_player

        # Ensure the stored payload includes the original queried name
        payload.setdefault("queried_player", player)

        # Save into local SQLite and return its id as report_id (local cache)
        try:
            saved_id = db.save_report(
                player=canonical_query_player,
                queried_player=player,
                team=team,
                league=league,
                season=season,
                use_web=generation_use_web,
                model=payload.get("model", MODEL),
                report_md=report_md,
            )
        except Exception as e:
            # On local persist failure, refund credit and error
            try:
                refund_credits(
                    user_id,
                    1,
                    reason="refund_sqlite_persist_failed",
                    source_type="scout_request_refund",
                    source_id=f"{request_id}:refund",
                )
            except Exception:
                pass
            return jsonify({"error": f"Failed to save local cache: {e}"}), 500

        payload["report_id"] = int(saved_id) if saved_id is not None else None
        # Always write to Postgres as the single source of truth for reports
        try:
            # If regenerating an existing report, update it by ID instead of creating new
            if report_id_to_update and refresh:
                pg_id = update_report_by_id(
                    user_id=user_id,
                    report_id=int(report_id_to_update),
                    player_name=canonical_player,
                    report_md=report_md,
                    payload=payload,
                    cached=cached_flag,
                )
                payload["refreshed"] = True  # Mark as refreshed for UI messaging
            else:
                pg_id = insert_report(
                    user_id=user_id,
                    player_name=canonical_player,
                    query_obj=insert_query_obj,
                    report_md=report_md,
                    payload=payload,
                    cached=cached_flag,
                )
                logger.info(f"[SAVE] Saved report to Postgres: id={pg_id}, canonical_player='{canonical_player}', query_key_player='{canonical_query_player}'")
            payload["library_id"] = int(pg_id)
        except Exception as e:
            # If Postgres write fails, this is a critical error - refund credit
            logger.error("Failed to save report to Postgres: %s", e)
            try:
                refund_credits(
                    user_id,
                    1,
                    reason="refund_postgres_failed",
                    source_type="scout_request_refund",
                    source_id=f"{request_id}:refund_pg",
                )
            except Exception:
                pass
            return jsonify({"error": f"Failed to save report: {e}"}), 500

        # Generate and store embeddings for future similarity matching
        try:
            from utils.embeddings import embed_text, store_embedding
            from db import connect
            
            # Create embedding for the player name
            embed_text_input = f"{canonical_player} {team or ''} {league or ''}".strip()
            embedding_vector = embed_text(client, embed_text_input)
            
            # Store embedding in local SQLite cache
            conn = None
            try:
                conn = connect()
                # Use Postgres ID for embeddings so suggestions resolve correctly
                target_id = int(pg_id)
                store_embedding(conn, target_id, embedding_vector)
                conn.commit()
                logger.info(f"[EMBEDDING] Stored embedding for report_id={target_id}")
            except Exception as e:
                logger.warning(f"Failed to store embedding for report {target_id}: {e}")
            finally:
                # Ensure connection is always closed
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
        except Exception as e:
            # Embedding storage failure should not block report save
            logger.warning(f"Failed to generate/store embedding: {e}")

        # Track cost for this report generation
        try:
            from db_pg import insert_cost_tracking
            from utils.app_helpers import estimate_cost, get_model_prices
            
            usage = payload.get("usage", {})
            if usage and usage.get("input_tokens") and usage.get("output_tokens"):
                prices = get_model_prices(MODEL)
                estimated_cost = estimate_cost(usage, prices)
                
                insert_cost_tracking(
                    user_id=user_id,
                    report_id=int(pg_id),
                    model=MODEL,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    estimated_cost=estimated_cost,
                    player_name=payload.get("player") or payload.get("player_name") or player,
                )
                
                logger.info(
                    "Cost tracked for report %s: $%.4f (input: %d, output: %d tokens)",
                    pg_id, estimated_cost, usage["input_tokens"], usage["output_tokens"]
                )
        except Exception as e:
            # Cost tracking failure should not break the flow
            logger.warning("Failed to track cost: %s", e)

        payload["credits_remaining"] = new_balance
        return jsonify(payload)
    except Exception as e:
        # Refund credit on unexpected failure
        try:
            refund_credits(
                user_id,
                1,
                reason="refund_persist_failed",
                source_type="scout_request_refund",
                source_id=f"{request_id}:refund",
            )
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # Run without Flask debugger for better performance and fewer logs.
    # For production, run under a WSGI server (gunicorn / waitress) instead.
    app.run(host="0.0.0.0", port=port, debug=True)
