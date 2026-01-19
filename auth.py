# auth.py
import os

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
