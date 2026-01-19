from datetime import datetime, timezone
from typing import Dict, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def increment_metric(name: str, by: int = 1) -> None:
    """Increment a named counter in the `metrics` table.

    Lazily import `connect` to avoid circular import issues.
    """
    from db import connect

    now = _utc_now_iso()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE metrics SET count = count + ?, last_seen = ? WHERE name = ?",
            (by, now, name),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO metrics (name, count, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                (name, by, now, now),
            )


def get_metric(name: str) -> Optional[int]:
    from db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT count FROM metrics WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            return None
        return int(row[0])


def list_metrics() -> Dict[str, int]:
    """Return a mapping of metric name -> count for easy consumption by UIs."""
    from db import connect

    with connect() as conn:
        rows = conn.execute("SELECT name, count FROM metrics ORDER BY name").fetchall()
        return {r[0]: int(r[1]) for r in rows}


def record_timing(name: str, ms: float) -> None:
    """Record a timing (in milliseconds) into the `timings` table.

    Adds to `total_ms` and increments `count`. Creates the row if missing.
    """
    from db import connect

    now = _utc_now_iso()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE timings SET count = count + 1, total_ms = total_ms + ?, last_ms = ?, last_seen = ? WHERE name = ?",
            (ms, ms, now, name),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO timings (name, count, total_ms, last_ms, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (name, 1, ms, ms, now, now),
            )
        # store individual sample for percentile calculations
        try:
            conn.execute(
                "INSERT INTO timing_samples (name, ms, created_at) VALUES (?, ?, ?)",
                (name, ms, now),
            )
        except Exception:
            pass


def get_timing(name: str) -> Optional[Dict[str, float]]:
    """Return timing summary for `name` or None if not present.

    Returns a dict with `count`, `total_ms`, `last_ms`, and `avg_ms`.
    """
    from db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT count, total_ms, last_ms FROM timings WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            return None
        count = int(row[0])
        total_ms = float(row[1])
        last_ms = float(row[2])
        avg_ms = total_ms / count if count > 0 else 0.0
        return {"count": count, "total_ms": total_ms, "last_ms": last_ms, "avg_ms": avg_ms}


def list_timings() -> Dict[str, Dict[str, float]]:
    """Return a mapping of timing name -> summary dict for UI consumption."""
    from db import connect

    out = {}
    with connect() as conn:
        rows = conn.execute("SELECT name, count, total_ms, last_ms FROM timings ORDER BY name").fetchall()
        for r in rows:
            name = r[0]
            cnt = int(r[1])
            total_ms = float(r[2])
            last_ms = float(r[3])
            avg_ms = total_ms / cnt if cnt > 0 else 0.0
            # collect samples for percentiles (limit recent 2000 samples)
            try:
                samples = [float(s[0]) for s in conn.execute(
                    "SELECT ms FROM timing_samples WHERE name = ? ORDER BY id DESC LIMIT 2000",
                    (name,),
                ).fetchall()]
                samples = list(reversed(samples))
                p50 = _percentile(samples, 50)
                p90 = _percentile(samples, 90)
                p99 = _percentile(samples, 99)
            except Exception:
                p50 = p90 = p99 = None
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


def _percentile(data: list[float], perc: float) -> float | None:
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
