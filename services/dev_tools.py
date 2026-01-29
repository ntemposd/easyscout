"""Development tools and debugging endpoints."""
import os
import uuid
from difflib import SequenceMatcher
from flask import jsonify, request, render_template

try:
    from rapidfuzz import fuzz
    _token_sort_ratio = getattr(fuzz, "token_sort_ratio", None)
    _token_set_ratio = getattr(fuzz, "token_set_ratio", None)
    _HAS_RAPIDFUZZ = True
except Exception:
    _token_sort_ratio = None
    _token_set_ratio = None
    _HAS_RAPIDFUZZ = False


def create_dev_routes(app, require_user_id, require_admin_user, 
                     refund_credits, list_reports, get_report,
                     normalize_name, send_email, list_metrics, list_timings):
    """Register all dev/debug-related routes with the Flask app.
    
    Args:
        app: Flask application instance
        require_user_id: Auth function to get user ID from request
        require_admin_user: Auth function to verify admin access
        refund_credits: Function to add credits to user account
        list_reports: Function to fetch user's reports
        get_report: Function to get a specific report
        normalize_name: Function to normalize player names
        send_email: Function to send emails
        list_metrics: Function to list metrics
        list_timings: Function to list timing data
    """
    
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


    @app.get("/api/dev/metrics")
    def dev_metrics():
        try:
            require_admin_user()
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
            require_admin_user()
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
            require_admin_user()
        except PermissionError as e:
            return (render_template("landing.html", error=str(e)), 403)

        return render_template("dev_metrics.html")


    @app.post("/api/dev/seed_metrics")
    def dev_seed_metrics():
        try:
            require_admin_user()
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403

        try:
            # Lazy import to avoid startup cycles
            from utils.metrics import increment_metric

            for _ in range(3):
                increment_metric("llm_calls")
            for _ in range(5):
                increment_metric("cache_hits")
            increment_metric("embedding_calls", 2)
            increment_metric("report_saves", 1)

            return jsonify({"ok": True, "metrics": list_metrics()})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
