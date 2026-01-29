# auth.py
import os
from functools import wraps
from flask import jsonify

from supabase import create_client

_supabase = None


def _client():
    """Create a Supabase client using the server-only service role key when available.

    In production, prefer `SUPABASE_SERVICE_ROLE_KEY` for server-side verification. If
    that's not set, the function will fall back to `SUPABASE_ANON_KEY` only if present.
    """
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get(
            "SUPABASE_ANON_KEY"
        )
        if not url or not key:
            raise RuntimeError(
                "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY fallback)"
            )
        _supabase = create_client(url, key)
    return _supabase


def require_user_id(request) -> str:
    auth = request.headers.get("Authorization", "")
    # Development shortcut: when DEV_TOOLS=1, allow a fallback DEV_USER
    dev_tools = os.getenv("DEV_TOOLS") == "1"
    # Use a stable UUID when running in dev mode so Postgres UUID columns accept the value.
    dev_user = os.getenv("DEV_USER") or "00000000-0000-0000-0000-000000000000"

    if not auth.startswith("Bearer "):
        if dev_tools:
            return dev_user
        raise PermissionError("Missing bearer token")

    token = auth.split(" ", 1)[1].strip()

    try:
        result = _client().auth.get_claims(token)
    except Exception as e:
        if dev_tools:
            return dev_user
        raise PermissionError(f"Invalid token: {e}")

    # result can be different shapes depending on client version; normalize
    claims = None
    if isinstance(result, dict):
        claims = (
            result.get("claims") if isinstance(result.get("claims"), dict) else result
        )
    else:
        claims = getattr(result, "claims", None) or result

    if not isinstance(claims, dict):
        raise PermissionError("Invalid token claims")

    user_id = claims.get("sub")
    if not user_id:
        raise PermissionError("Missing user id (sub)")

    return user_id


def app_base_url(request) -> str:
    """Get the base URL for the application.
    
    Prefers explicit APP_BASE_URL env var for production (Render/proxy),
    falls back to request.host_url for local development.
    """
    return (os.getenv("APP_BASE_URL") or request.host_url).rstrip("/")


def require_admin_user(request) -> str:
    """Require the current user to be an admin for dev endpoints.

    Admins are configured via the `ADMIN_USERS` env var as a comma-separated
    list of user_ids. If `ADMIN_USERS` is not set, fall back to requiring
    `DEV_TOOLS=1` and any authenticated user.
    
    Returns:
        str: The authenticated admin user_id
        
    Raises:
        PermissionError: If user is not admin or dev tools disabled
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


def require_auth(f):
    """Decorator: Require user authentication and return 401 on PermissionError.
    
    Automatically extracts user_id from request and injects as first argument.
    Returns 401 JSON error if authentication fails.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from flask import request
        try:
            user_id = require_user_id(request)
            return f(user_id, *args, **kwargs)
        except PermissionError as e:
            return jsonify({"error": str(e)}), 401
    return decorated_function
