# db_pg.py
import json
import os
from typing import Any, Dict, List, Optional

from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

# ----------------------------
# Pool
# ----------------------------

_pool: Optional[ConnectionPool] = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        dsn = os.environ["DATABASE_URL"]
        _pool = ConnectionPool(dsn, min_size=1, max_size=5, timeout=10)
    return _pool


# ----------------------------
# Credits
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
            update public.user_credits
               set balance = balance + %s,
                   updated_at = now()
             where user_id = %s
         returning balance
            """,
            (amount, user_id),
        )
        new_balance = int(cur.fetchone()[0])

        try:
            cur.execute(
                """
                insert into public.credit_ledger(user_id, delta, reason, source_type, source_id)
                values (%s, %s, %s, %s, %s)
                """,
                (user_id, amount, reason, source_type, source_id),
            )
            conn.commit()
            return new_balance
        except UniqueViolation:
            # already granted => rollback to undo the balance increment
            conn.rollback()
            cur.execute(
                "select balance from public.user_credits where user_id = %s", (user_id,)
            )
            row2 = cur.fetchone()
            return int(row2[0]) if row2 else 0


# ----------------------------
# Stripe bookkeeping
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
# Reports (library)
# ----------------------------


def _canonical_query_key(query_obj: Dict[str, Any]) -> str:
    # Deterministic representation: same object => same key
    # IMPORTANT: keep spaces so it matches Postgres jsonb::text formatting
    return json.dumps(query_obj, sort_keys=True, ensure_ascii=False)


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


def list_reports(user_id: str, q: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    q = (q or "").strip()
    limit = max(1, min(int(limit or 20), 100))

    where = "user_id = %s"
    params: List[Any] = [user_id]

    if q:
        where += " and (player_name ilike %s or query ilike %s)"
        like = f"%{q}%"
        params += [like, like]

    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            select id, player_name, created_at, cached
            from public.reports
            where {where}
            order by created_at desc, id desc
            limit %s
            """,
            (*params, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "id": int(r[0]),
            "player_name": r[1],
            "created_at": r[2].isoformat() if r[2] else None,
            "cached": bool(r[3]),
        }
        for r in rows
    ]


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
