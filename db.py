# db.py
# Unified database module: combines production functions (from db_pg.py) and cache functions (from db_cache.py)
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

from utils.normalize import normalize_name

# Prompt version tag to distinguish cached reports across prompt iterations.
# Increment this when changing the scouting prompt meaningfully.
PROMPT_VERSION = 1

# ----------------------------
# Pool
# ----------------------------

_pool: Optional[ConnectionPool] = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ["DATABASE_URL"]
        
        # Convert to Transaction mode (port 6543) to avoid Session mode client limits
        if "pooler.supabase.com:5432" in dsn:
            dsn = dsn.replace("pooler.supabase.com:5432", "pooler.supabase.com:6543")
        
        # Add connect_timeout to DSN to prevent DNS/SSL hangs
        if "?" in dsn:
            dsn += "&connect_timeout=5"
        else:
            dsn += "?connect_timeout=5"
        
        _pool = ConnectionPool(
            dsn,
            min_size=2,
            max_size=20,  # Increased from 10 to handle concurrent onboarding
            timeout=10,
            max_lifetime=180,
            max_idle=60,
            reconnect_timeout=15,
            kwargs={
                "options": "-c statement_timeout=30s",
                # Disable prepared statement caching to avoid conflicts with dynamic SQL
                "prepare_threshold": None,
            },
        )
    return _pool


# ----------------------------
# Initialization (for backwards compatibility)
# ----------------------------

def init_db() -> None:
    """Placeholder for backwards compatibility - PostgreSQL setup is handled by migrations"""
    pass


# ----------------------------
# Utility Functions
# ----------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(s: str | None) -> str:
    """Normalize names for consistent matching"""
    return normalize_name(s or "", transliterate=True)


def _canonical_query_key(query_obj: Dict[str, Any]) -> str:
    # Deterministic representation: same object => same key
    # IMPORTANT: keep spaces so it matches Postgres jsonb::text formatting
    return json.dumps(query_obj, sort_keys=True, ensure_ascii=False)


# ----------------------------
# Credits (Production)
# ----------------------------


def _ensure_user_row(cur, user_id: str) -> None:
    cur.execute(
        """
        insert into public.user_credits(user_id, balance)
        values (%s, 0)
        on conflict (user_id) do nothing
        """,
        (user_id,),
    )


def initialize_user_with_welcome_credits(user_id: str) -> int:
    """
    Grant 3 welcome credits to a new user (one-time only).
    Returns the new balance after granting credits.
    Uses idempotent source_id so repeated calls won't double-grant.
    """
    return refund_credits(
        user_id,
        3,
        reason="welcome_bonus",
        source_type="onboarding",
        source_id=f"welcome_bonus_{user_id}",
    )


def get_balance(user_id: str) -> int:
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "select balance from public.user_credits where user_id = %s", (user_id,)
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def spend_credits(
    user_id: str,
    amount: int,
    *,
    reason: str,
    source_type: str,
    source_id: str,
) -> int:
    """
    Atomically subtract `amount` credits if balance is sufficient.
    Writes a ledger row with delta=-amount.

    Returns: new balance
    Raises: ValueError("INSUFFICIENT_CREDITS") when not enough balance.
    """
    if amount <= 0:
        raise ValueError("amount must be > 0")
    if not source_type or not source_id:
        raise ValueError("source_type and source_id are required")

    with _get_pool().connection() as conn, conn.cursor() as cur:
        _ensure_user_row(cur, user_id)

        cur.execute(
            """
            update public.user_credits
               set balance = balance - %s,
                   updated_at = now()
             where user_id = %s
               and balance >= %s
         returning balance
            """,
            (amount, user_id, amount),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise ValueError("INSUFFICIENT_CREDITS")

        new_balance = int(row[0])

        try:
            cur.execute(
                """
                insert into public.credit_ledger(user_id, delta, reason, source_type, source_id)
                values (%s, %s, %s, %s, %s)
                """,
                (user_id, -amount, reason, source_type, source_id),
            )
            conn.commit()
            return new_balance
        except UniqueViolation:
            # already applied => revert (rollback) and return current balance
            conn.rollback()
            cur.execute(
                "select balance from public.user_credits where user_id = %s", (user_id,)
            )
            row2 = cur.fetchone()
            return int(row2[0]) if row2 else 0


def refund_credits(
    user_id: str,
    amount: int,
    *,
    reason: str,
    source_type: str,
    source_id: str,
) -> int:
    """
    Adds `amount` credits and writes ledger row with delta=+amount.
    Idempotent if (source_type, source_id) is unique in credit_ledger.
    """
    if amount <= 0:
        raise ValueError("amount must be > 0")
    if not source_type or not source_id:
        raise ValueError("source_type and source_id are required")

    with _get_pool().connection() as conn, conn.cursor() as cur:
        _ensure_user_row(cur, user_id)

        cur.execute(
            """
            insert into public.credit_ledger(user_id, delta, reason, source_type, source_id)
            values (%s, %s, %s, %s, %s)
            on conflict (source_type, source_id) do nothing
            returning 1
            """,
            (user_id, amount, reason, source_type, source_id),
        )
        inserted = cur.fetchone()

        if not inserted:
            conn.commit()
            cur.execute(
                "select balance from public.user_credits where user_id = %s", (user_id,)
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

        cur.execute(
            """
            update public.user_credits
               set balance = balance + %s,
                   updated_at = now()
             where user_id = %s
         returning balance
            """,
            (amount, user_id),
        )
        row2 = cur.fetchone()
        conn.commit()
        return int(row2[0]) if row2 else 0


# ----------------------------
# Stripe bookkeeping (Production)
# ----------------------------


def record_stripe_event(event_id: str, event_type: str, payload: dict) -> bool:
    """Returns True only the first time we see this event_id."""
    if not event_id:
        return True

    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into public.stripe_events(event_id, event_type, payload)
            values (%s, %s, %s::jsonb)
            on conflict (event_id) do nothing
            """,
            (
                event_id,
                event_type or "",
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
        )
        inserted = cur.rowcount == 1
        conn.commit()
        return inserted


def record_stripe_purchase(
    *,
    user_id: str,
    session_id: str,
    amount_cents: int,
    currency: str,
    credits: int,
) -> None:
    if not session_id:
        return

    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into public.stripe_purchases(user_id, checkout_session_id, amount_cents, currency, credits)
            values (%s, %s, %s, %s, %s)
            on conflict (checkout_session_id) do nothing
            """,
            (
                user_id,
                session_id,
                int(amount_cents or 0),
                (currency or "eur").lower(),
                int(credits or 0),
            ),
        )
        conn.commit()


# ----------------------------
# Reports (Production Library)
# ----------------------------


def make_query_key(query_obj: Dict[str, Any]) -> str:
    return _canonical_query_key(query_obj)


def find_report_by_query_key(user_id: str, query_key: str) -> Optional[Dict[str, Any]]:
    """
    Exact match in the user's library (free-load in /api/scout when refresh=false).
    """
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select id, payload, report_md, player_name, created_at, cached
            from public.reports
            where user_id = %s and query_key = %s
            order by created_at desc, id desc
            limit 1
            """,
            (user_id, query_key),
        )
        row = cur.fetchone()

    if not row:
        return None

    rid, payload, report_md, player_name, created_at, cached = row
    return {
        "id": int(rid),
        "payload": payload,  # jsonb -> dict (psycopg) or None
        "report_md": report_md or "",
        "player_name": player_name or "",
        "created_at": created_at.isoformat() if created_at else None,
        "cached": bool(cached),
    }


def upsert_report(
    user_id: str,
    player_name: str,
    query_obj: Dict[str, Any],
    report_md: str,
    payload: Dict[str, Any],
    cached: bool,
) -> int:
    """
    Inserts OR updates the user's report for this query_key.
    Requires unique index: (user_id, query_key).
    """
    query_key = _canonical_query_key(query_obj)
    q_text = query_key
    p_text = json.dumps(payload or {}, ensure_ascii=False, default=str)

    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into public.reports (user_id, player_name, query, query_key, report_md, payload, cached)
            values (%s, %s, %s, %s, %s, %s::jsonb, %s)
            on conflict (user_id, query_key) do update
              set player_name = excluded.player_name,
                  query       = excluded.query,
                  report_md   = excluded.report_md,
                  payload     = excluded.payload,
                  cached      = excluded.cached
            returning id
            """,
            (user_id, player_name, q_text, query_key, report_md, p_text, bool(cached)),
        )
        (rid,) = cur.fetchone()
        conn.commit()
        return int(rid)


def update_report_by_id(
    user_id: str,
    report_id: int,
    player_name: str,
    report_md: str,
    payload: Dict[str, Any],
    cached: bool,
) -> int:
    """
    Updates an existing report by ID (for regenerations).
    Ensures the report belongs to the user before updating.
    """
    p_text = json.dumps(payload or {}, ensure_ascii=False, default=str)

    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            update public.reports
            set player_name = %s,
                report_md = %s,
                payload = %s::jsonb,
                cached = %s,
                created_at = now()
            where id = %s and user_id = %s
            returning id
            """,
            (player_name, report_md, p_text, bool(cached), report_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Report {report_id} not found or does not belong to user {user_id}")
        conn.commit()
        return int(row[0])


# Backwards-compatible name (your app.py uses insert_report)
def insert_report(
    user_id: str,
    player_name: str,
    query_obj: Dict[str, Any],
    report_md: str,
    payload: Dict[str, Any],
    cached: bool,
) -> int:
    return upsert_report(
        user_id=user_id,
        player_name=player_name,
        query_obj=query_obj,
        report_md=report_md,
        payload=payload,
        cached=cached,
    )


def list_reports(user_id: str, q: str = "", limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    q = (q or "").strip()
    # Allow larger result sets; capped to avoid unbounded queries
    limit = max(1, min(int(limit or 20), 1000))
    offset = max(0, int(offset or 0))

    # Special case: "*" means search ALL users' reports (for global suggestions)
    if user_id == "*":
        where = "1=1"  # No user_id filter
        params: List[Any] = []
    else:
        where = "user_id = %s"
        params: List[Any] = [user_id]

    if q:
        # Search across key fields: player, league, team, position
        where += """ and (
            player_name ilike %s 
            or (payload->>'league') ilike %s
            or (payload->'info_fields'->>'League') ilike %s
            or (payload->'info_fields'->>'Team') ilike %s
            or (payload->'info_fields'->>'Position') ilike %s
        )"""
        like = f"%{q}%"
        params += [like, like, like, like, like]

    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            try:
                # Fetch only minimal payload fields for list performance
                cur.execute(
                    f"""
                    select id, player_name, created_at, cached,
                           jsonb_build_object(
                               'league', payload->>'league',
                               'team', payload->>'team',
                               'team_name', payload->>'team_name',
                               'info_fields', payload->'info_fields'
                           ) as payload
                    from public.reports
                    where {where}
                    order by created_at desc, id desc
                    limit %s offset %s
                    """,
                    (*params, limit, offset),
                )
                rows = cur.fetchall()
            except Exception:
                # If query times out or fails, rollback to clean connection state
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
    except Exception as e:
        # Log timeout errors but don't crash — return empty results as fallback
        if "statement timeout" in str(e).lower() or "timeout" in str(e).lower():
            return []
        raise

    results = []
    for r in rows:
        payload = r[4] if r[4] and isinstance(r[4], dict) else {}
        # Try top-level first, then fall back to info_fields
        team = (payload.get("team") or payload.get("team_name") or "").strip()
        league = (payload.get("league") or "").strip()
        
        # Try to extract from info_fields if not found
        info_fields = payload.get("info_fields", {}) or {}
        if not team:
            team = (info_fields.get("Team") or "").strip()
        if not league:
            league = (info_fields.get("League") or "").strip()
        
        # Extract position from info_fields
        position = (info_fields.get("Position") or "").strip()
        
        results.append({
            "id": int(r[0]),
            "player_name": r[1],
            "created_at": r[2].isoformat() if r[2] else None,
            "cached": bool(r[3]),
            "team": team,
            "league": league,
            "position": position,
        })
    
    return results


def count_reports(user_id: str, q: str = "") -> int:
    """Return total reports matching user/q for pagination and badge counts."""
    q = (q or "").strip()

    where = "user_id = %s"
    params: List[Any] = [user_id]

    if q:
        where += """ and (
            player_name ilike %s 
            or (payload->>'league') ilike %s
            or (payload->'info_fields'->>'League') ilike %s
            or (payload->'info_fields'->>'Team') ilike %s
            or (payload->'info_fields'->>'Position') ilike %s
        )"""
        like = f"%{q}%"
        params += [like, like, like, like, like]

    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    f"""
                    select count(*)
                    from public.reports
                    where {where}
                    """,
                    tuple(params),
                )
                row = cur.fetchone()
            except Exception:
                # If query times out or fails, rollback to clean connection state
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

        return int(row[0] or 0)
    except Exception as e:
        # Log timeout errors but don't crash — return 0 as fallback
        if "statement timeout" in str(e).lower() or "timeout" in str(e).lower():
            return 0
        raise


def get_report(user_id: str, report_id: int) -> Optional[Dict[str, Any]]:
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select payload, report_md, player_name, created_at, cached
            from public.reports
            where id = %s and user_id = %s
            """,
            (int(report_id), user_id),
        )
        row = cur.fetchone()

    if not row:
        return None

    payload, report_md, player_name, created_at, cached = row

    # If payload exists (jsonb), return it as the main object
    if payload:
        # Ensure the consumer has access to the markdown too
        if isinstance(payload, dict) and "report_md" not in payload:
            payload["report_md"] = report_md or ""
        if isinstance(payload, dict) and "cached" not in payload:
            payload["cached"] = bool(cached)
        if isinstance(payload, dict) and "created_at" not in payload and created_at:
            payload["created_at"] = created_at.isoformat()
        return payload

    # fallback: minimal
    return {
        "player": player_name,
        "report_md": report_md or "",
        "cached": bool(cached),
        "created_at": created_at.isoformat() if created_at else None,
    }


def get_report_by_id(report_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a report by ID without user_id filtering (for cross-user operations like accepting suggestions)"""
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select payload, report_md, player_name, created_at, cached, user_id
            from public.reports
            where id = %s
            """,
            (int(report_id),),
        )
        row = cur.fetchone()

    if not row:
        return None

    payload, report_md, player_name, created_at, cached, source_user_id = row

    # If payload exists (jsonb), return it as the main object
    if payload:
        # Ensure the consumer has access to the markdown too
        if isinstance(payload, dict) and "report_md" not in payload:
            payload["report_md"] = report_md or ""
        if isinstance(payload, dict) and "cached" not in payload:
            payload["cached"] = bool(cached)
        if isinstance(payload, dict) and "created_at" not in payload and created_at:
            payload["created_at"] = created_at.isoformat()
        if isinstance(payload, dict) and "source_user_id" not in payload:
            payload["source_user_id"] = source_user_id
        return payload

    # fallback: minimal
    return {
        "player": player_name,
        "report_md": report_md or "",
        "cached": bool(cached),
        "created_at": created_at.isoformat() if created_at else None,
    }


# ----------------------------
# Cost Tracking (Production)
# ----------------------------


def insert_cost_tracking(
    user_id: str,
    report_id: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost: float,
    player_name: str | None = None,
) -> None:
    """Record cost tracking data for a report generation.
    
    Args:
        user_id: User who generated the report
        report_id: ID of the generated report
        model: Model name used for generation
        input_tokens: Number of input tokens used
        output_tokens: Number of output tokens used
        estimated_cost: Calculated cost in dollars
    """
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.cost_tracking (
                user_id,
                report_id,
                model,
                input_tokens,
                output_tokens,
                estimated_cost,
                player_name,
                timestamp
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                user_id,
                report_id,
                model,
                input_tokens,
                output_tokens,
                estimated_cost,
                player_name or "",
            ),
        )
        conn.commit()


def get_cost_stats(user_id: str = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieve cost tracking statistics.
    
    Args:
        user_id: Optional user_id to filter by specific user
        limit: Maximum number of records to return
        
    Returns:
        List of cost tracking records
    """
    with _get_pool().connection() as conn, conn.cursor() as cur:
        if user_id:
            cur.execute(
                """
                SELECT 
                    id,
                    user_id,
                    report_id,
                    model,
                    input_tokens,
                    output_tokens,
                    estimated_cost,
                    player_name,
                    timestamp
                FROM public.cost_tracking
                WHERE user_id = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT 
                    id,
                    user_id,
                    report_id,
                    model,
                    input_tokens,
                    output_tokens,
                    estimated_cost,
                    player_name,
                    timestamp
                FROM public.cost_tracking
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (limit,),
            )
        
        rows = cur.fetchall()
        
        return [
            {
                "id": row[0],
                "user_id": row[1],
                "report_id": row[2],
                "model": row[3],
                "input_tokens": row[4],
                "output_tokens": row[5],
                "estimated_cost": float(row[6]),
                "player_name": row[7] or "",
                "timestamp": row[8].isoformat() if row[8] else None,
            }
            for row in rows
        ]


def get_cost_summary(user_id: str = None) -> Dict[str, Any]:
    """Get aggregated cost statistics.
    
    Args:
        user_id: Optional user_id to filter by specific user
        
    Returns:
        Dict with total cost, token counts, and report count
    """
    with _get_pool().connection() as conn, conn.cursor() as cur:
        if user_id:
            cur.execute(
                """
                SELECT 
                    COUNT(*) as report_count,
                    SUM(input_tokens) as total_input_tokens,
                    SUM(output_tokens) as total_output_tokens,
                    SUM(estimated_cost) as total_cost
                FROM public.cost_tracking
                WHERE user_id = %s
                """,
                (user_id,),
            )
        else:
            cur.execute(
                """
                SELECT 
                    COUNT(*) as report_count,
                    SUM(input_tokens) as total_input_tokens,
                    SUM(output_tokens) as total_output_tokens,
                    SUM(estimated_cost) as total_cost
                FROM public.cost_tracking
                """
            )
        
        row = cur.fetchone()
        
        if not row:
            return {
                "report_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost": 0.0,
            }
        
        return {
            "report_count": int(row[0]) if row[0] else 0,
            "total_input_tokens": int(row[1]) if row[1] else 0,
            "total_output_tokens": int(row[2]) if row[2] else 0,
            "total_cost": float(row[3]) if row[3] else 0.0,
        }


# ----------------------------
# Cache Functions (formerly in db_cache.py)
# ----------------------------

# ==================== Player Aliases ====================

def upsert_player_alias(
    queried_player: str,
    canonical_player: str,
    team: str = "",
    league: str = "",
) -> None:
    """Store a player name alias mapping in PostgreSQL"""
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.player_aliases 
                (queried_player, queried_player_norm, canonical_player, canonical_player_norm, team_norm, league_norm, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                queried_player,
                norm(queried_player),
                canonical_player,
                norm(canonical_player),
                norm(team),
                norm(league),
                _utc_now_iso(),
            ),
        )


def find_canonical_by_alias(queried_player: str) -> Optional[dict]:
    """Find canonical player name from alias in PostgreSQL"""
    queried_norm = norm(queried_player)
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical_player, canonical_player_norm, team_norm, league_norm
            FROM public.player_aliases
            WHERE queried_player_norm = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (queried_norm,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "canonical_player": row[0],
            "canonical_player_norm": row[1],
            "team_norm": row[2],
            "league_norm": row[3],
        }


# ==================== Report Embeddings ====================

def save_report_embedding(report_id: int, embedding: list[float]) -> None:
    """Store report embedding in PostgreSQL"""
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.report_embeddings (report_id, embedding_json, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (report_id) DO UPDATE
            SET embedding_json = EXCLUDED.embedding_json, created_at = EXCLUDED.created_at
            """,
            (report_id, json.dumps(embedding), _utc_now_iso()),
        )


def get_report_embedding(report_id: int) -> Optional[list[float]]:
    """Retrieve report embedding from PostgreSQL"""
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT embedding_json FROM public.report_embeddings WHERE report_id = %s",
            (report_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        # PostgreSQL JSONB returns already-parsed data
        return row[0] if isinstance(row[0], list) else json.loads(row[0])


def get_all_report_embeddings() -> list[tuple[int, list[float]]]:
    """Get all report embeddings for similarity search"""
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT report_id, embedding_json FROM public.report_embeddings")
        # PostgreSQL JSONB returns already-parsed data
        return [(row[0], row[1] if isinstance(row[1], list) else json.loads(row[1])) for row in cur.fetchall()]


# ==================== Query Embeddings ====================

def save_query_embedding(query_hash: str, query_text: str, embedding: list[float]) -> None:
    """Cache query embedding in PostgreSQL"""
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.query_embeddings (query_hash, query_text, embedding_json, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (query_hash) DO UPDATE
            SET embedding_json = EXCLUDED.embedding_json, created_at = EXCLUDED.created_at
            """,
            (query_hash, query_text, json.dumps(embedding), _utc_now_iso()),
        )


def get_query_embedding(query_hash: str) -> Optional[list[float]]:
    """Retrieve cached query embedding from PostgreSQL"""
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT embedding_json FROM public.query_embeddings WHERE query_hash = %s",
            (query_hash,),
        )
        row = cur.fetchone()
        if not row:
            return None
        # PostgreSQL JSONB returns already-parsed data
        return row[0] if isinstance(row[0], list) else json.loads(row[0])


# ==================== Metrics ====================

def increment_metric(name: str, by: int = 1) -> None:
    """Increment a named counter in PostgreSQL metrics table"""
    pool = _get_pool()
    now = _utc_now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.metrics (name, count, first_seen, last_seen)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE
            SET count = public.metrics.count + EXCLUDED.count,
                last_seen = EXCLUDED.last_seen
            """,
            (name, by, now, now),
        )


def get_metric(name: str) -> Optional[int]:
    """Get metric count from PostgreSQL"""
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count FROM public.metrics WHERE name = %s", (name,))
        row = cur.fetchone()
        if not row:
            return None
        return int(row[0])


def list_metrics() -> dict[str, int]:
    """List all metrics from PostgreSQL"""
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, count FROM public.metrics ORDER BY name")
        return {row[0]: int(row[1]) for row in cur.fetchall()}


# ==================== Timings ====================

def record_timing(name: str, ms: float) -> None:
    """Record timing measurement in PostgreSQL"""
    pool = _get_pool()
    now = _utc_now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        # Update aggregate timings
        cur.execute(
            """
            INSERT INTO public.timings (name, count, total_ms, last_ms, first_seen, last_seen)
            VALUES (%s, 1, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE
            SET count = public.timings.count + 1,
                total_ms = public.timings.total_ms + EXCLUDED.total_ms,
                last_ms = EXCLUDED.last_ms,
                last_seen = EXCLUDED.last_seen
            """,
            (name, ms, ms, now, now),
        )
        # Store individual sample
        cur.execute(
            "INSERT INTO public.timing_samples (name, ms, created_at) VALUES (%s, %s, %s)",
            (name, ms, now),
        )


def get_timing(name: str) -> Optional[dict]:
    """Get timing summary from PostgreSQL"""
    pool = _get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count, total_ms, last_ms FROM public.timings WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
        if not row:
            return None
        count = int(row[0])
        total_ms = float(row[1])
        last_ms = float(row[2])
        avg_ms = total_ms / count if count > 0 else 0.0
        return {"count": count, "total_ms": total_ms, "last_ms": last_ms, "avg_ms": avg_ms}


def list_timings() -> dict[str, dict]:
    """List all timings with percentiles from PostgreSQL"""
    pool = _get_pool()
    out = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, count, total_ms, last_ms FROM public.timings ORDER BY name")
        for row in cur.fetchall():
            name = row[0]
            cnt = int(row[1])
            total_ms = float(row[2])
            last_ms = float(row[3])
            avg_ms = total_ms / cnt if cnt > 0 else 0.0
            
            # Get percentiles from samples
            cur.execute(
                "SELECT ms FROM public.timing_samples WHERE name = %s ORDER BY id DESC LIMIT 2000",
                (name,),
            )
            samples = [float(s[0]) for s in cur.fetchall()]
            samples.reverse()
            
            p50 = _percentile(samples, 50)
            p90 = _percentile(samples, 90)
            p99 = _percentile(samples, 99)
            
            out[name] = {
                "count": cnt,
                "total_ms": total_ms,
                "last_ms": last_ms,
                "avg_ms": avg_ms,
                "p50_ms": p50,
                "p90_ms": p90,
                "p99_ms": p99,
            }
    return out


def _percentile(data: list[float], perc: float) -> Optional[float]:
    """Calculate percentile from sorted data"""
    if not data:
        return None
    data_sorted = sorted(data)
    k = (len(data_sorted) - 1) * (perc / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(data_sorted):
        return float(data_sorted[-1])
    d0 = data_sorted[f] * (c - k)
    d1 = data_sorted[c] * (k - f)
    return float(d0 + d1)
