# app.py
# ruff: noqa: E402
from dotenv import load_dotenv

load_dotenv(override=True)

import logging
import os
from flask import Flask, jsonify, render_template, request

from auth import require_user_id, app_base_url, require_admin_user, require_auth
from db import (
    get_balance,
    get_report,
    init_db,
    list_reports,
    count_reports,
    record_stripe_event,
    record_stripe_purchase,
    refund_credits,
    initialize_user_with_welcome_credits,
)
from services.pdf_export import generate_pdf_from_report, generate_pdf_filename
from services.billing import create_billing_routes
from services.dev_tools import create_dev_routes
from services.analytics import create_analytics_routes
from services.reports import create_reports_routes
from services.config import initialize_sentry, initialize_stripe, initialize_openai, setup_compression
from utils.metrics import list_metrics, list_timings
from utils.parse import extract_display_md
from utils.prompts import load_text_prompt
from utils.render import md_to_safe_html
from utils.normalize import normalize_name

from utils.analytics import (
    track_event,
    alias_user,
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
import re

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", os.urandom(32).hex())
app.config["SESSION_COOKIE_SECURE"] = os.getenv("DEV_TOOLS") != "1"  # HTTPS only in production
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Setup optional middleware
setup_compression(app)

@app.after_request
def add_security_headers(response):
    """Add security headers to all responses"""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
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
    """Global error handler with optional traceback in dev mode."""
    logger.exception(e)
    if os.getenv("DEV_TOOLS") == "1":
        import traceback
        tb = traceback.format_exc()
        return jsonify({"error": str(e), "traceback": tb}), 500
    return jsonify({"error": "internal server error"}), 500

# --------------------
# Logging Setup (must be early to use logger in config)
# --------------------
logging.basicConfig(level=logging.INFO)
# Reduce verbosity from noisy libraries in normal runs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Initialize third-party services
initialize_sentry()
stripe = initialize_stripe()
client = initialize_openai()

# --------------------
# Config
# --------------------
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")

init_db()
SCOUT_INSTRUCTIONS = load_text_prompt("prompts/scout_instructions.txt")

# Register service routes
create_billing_routes(app, stripe, require_user_id, record_stripe_event, 
                     record_stripe_purchase, refund_credits, app_base_url)
create_dev_routes(app, require_user_id, require_admin_user, refund_credits,
                 list_reports, get_report, normalize_name, send_email,
                 list_metrics, list_timings)
create_analytics_routes(app, require_user_id, track_event, alias_user)
create_reports_routes(app, require_user_id, client, MODEL, SCOUT_INSTRUCTIONS)

# --------------------
# Pages
# --------------------
@app.get("/")
def landing():
    # New landing page
    return render_template("landing.html")

@app.get("/app")
def app_page():
    # Existing app UI moved here
    return render_template("index.html")

@app.get("/login")
def login_page():
    return render_template("login.html")

@app.get("/auth/callback")
def auth_callback_page():
    return render_template("auth_callback.html")

@app.get("/billing/success")
def billing_success():
    return render_template("billing_success.html")

@app.get("/privacy")
def privacy_page():
    return render_template("privacy.html")

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/api/render_md")
@require_auth
def api_render_md(user_id):
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
@require_auth
def api_credits(user_id):
    # Grant welcome bonus on first-ever call for this user
    try:
        initialize_user_with_welcome_credits(user_id)
    except Exception:
        # Silently fail if already granted or DB error; user still gets balance
        pass
    try:
        return jsonify({"credits": get_balance(user_id)})
    except Exception as e:
        logger.exception(e)
        # Graceful fallback: show zero credits when Postgres is unavailable
        return jsonify({"credits": 0, "error": "credits_unavailable"}), 200


@app.get("/api/reports")
@require_auth
def api_reports(user_id):

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
        logger.exception(e)
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
@require_auth
def api_report(user_id, report_id: int):

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

_playwright_installed = False

def ensure_playwright_browsers():
    """Ensure Playwright browsers are installed (runtime check for Render)."""
    global _playwright_installed
    if _playwright_installed:
        return
    try:
        import subprocess
        logger.info("Installing Playwright browsers (first-time runtime setup)...")
        subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True, timeout=120)
        _playwright_installed = True
        logger.info("Playwright browsers installed successfully.")
    except Exception as e:
        logger.warning(f"Playwright browser install failed (may already be installed): {e}")
        _playwright_installed = True  # Assume installed to avoid retries

@app.get("/api/reports/<int:report_id>/pdf")
@require_auth
def api_report_pdf(user_id, report_id: int):
    """Generate and download a scouting report as PDF using Playwright (Chromium)."""
    
    # Ensure browsers are installed (lazy init for Render)
    ensure_playwright_browsers()

    # Fetch report
    payload = None
    try:
        payload = get_report(user_id, report_id)
    except Exception:
        payload = None

    if not payload:
        return jsonify({"error": "Not found"}), 404

    # Use service to generate PDF
    try:
        from flask import send_file
        pdf_bytes = generate_pdf_from_report(payload)
        filename = generate_pdf_filename(payload.get("player", "Report"))
        
        from io import BytesIO
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

@app.post("/api/alias")
@require_auth
def api_alias(user_id):
    """Validate and store player name alias.
    
    Safety guards against accidental aliasing of nicknames to wrong players.
    Currently disabled—player dedup now handled via names_match logic.
    """
    data = request.get_json(force=True) or {}
    queried = (data.get("queried_player") or "").strip()
    player = (data.get("player") or "").strip()

    if not queried or not player:
        return jsonify({"error": "missing fields (queried_player, player)"}), 400

    try:
        from utils.phonetic import phonetic_key

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

            if len(q_parts) == 1 and p_last:
                pk = phonetic_key(p_last) or ""
                qk = phonetic_key(q_last) or ""
                if p_last != q_last and pk != qk:
                    return (
                        jsonify({
                            "error": "Alias looks like a nickname or moniker. Please confirm by providing the full name (first + last) when creating an alias.",
                        }),
                        400,
                    )
        except Exception:
            pass

        return jsonify({"ok": True})
    except Exception as e:
        logger.exception(f"Error in /api/alias: {e}")
        return jsonify({"error": str(e)}), 500

@app.context_processor
def inject_supabase():
    return {
        "supabase_url": os.environ.get("SUPABASE_URL"),
        "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY"),
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # Run without Flask debugger for better performance and fewer logs.
    # For production, run under a WSGI server (gunicorn / waitress) instead.
    app.run(host="0.0.0.0", port=port, debug=True)
