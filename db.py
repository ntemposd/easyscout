import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from utils.normalize import normalize_name

DB_PATH = os.getenv("DB_PATH", "scout_reports.db")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Better concurrent behavior (reads while writes) for light multi-user
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


# In-memory TTL cache to reduce frequent DB reads for cached reports
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = int(os.getenv("CACHE_TTL_SECS", "60"))


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player TEXT NOT NULL,
                player_norm TEXT NOT NULL,
                queried_player TEXT,
                queried_player_norm TEXT,
                team TEXT,
                team_norm TEXT,
                league TEXT,
                league_norm TEXT,
                season TEXT,
                season_norm TEXT,
                use_web INTEGER NOT NULL DEFAULT 0,
                model TEXT,
                prompt_version TEXT NOT NULL,
                report_md TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # Ensure new columns exist for upgrades of existing DB files
        cur = conn.execute("PRAGMA table_info('reports')")
        cols = {r[1] for r in cur.fetchall()}  # second column is name

        # Add missing columns if needed (SQLite supports ADD COLUMN)
        if "queried_player" not in cols:
            try:
                conn.execute("ALTER TABLE reports ADD COLUMN queried_player TEXT;")
            except Exception:
                pass
            cols.add("queried_player")

        if "queried_player_norm" not in cols:
            try:
                conn.execute("ALTER TABLE reports ADD COLUMN queried_player_norm TEXT;")
            except Exception:
                pass
            cols.add("queried_player_norm")

        # Create indexes (only after ensuring columns exist)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reports_player_norm ON reports(player_norm);"
        )
        if "queried_player_norm" in cols:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reports_queried_player_norm ON reports(queried_player_norm);"
            )
            # aliases table: maps queried_player_norm -> canonical player_norm
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS player_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_norm TEXT NOT NULL,
                    queried_player TEXT NOT NULL,
                    queried_player_norm TEXT NOT NULL UNIQUE,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_aliases_player_norm ON player_aliases(player_norm);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_aliases_queried_norm ON player_aliases(queried_player_norm);"
            )
            # embeddings table: store vector embeddings for reports (optional)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS report_embeddings (
                    report_id INTEGER PRIMARY KEY,
                    embedding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_embeddings_report_id ON report_embeddings(report_id);"
            )
            # cache for query embeddings to avoid recomputing per user action
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_embeddings (
                    query_hash TEXT PRIMARY KEY,
                    query_text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_query_embeddings_hash ON query_embeddings(query_hash);"
            )
            # basic metrics table for instrumentation
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics (
                    name TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name);"
            )
            # timings table for recording duration measurements (milliseconds)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS timings (
                    name TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0,
                    total_ms REAL NOT NULL DEFAULT 0,
                    last_ms REAL NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timings_name ON timings(name);")
            # store individual timing samples for percentile calculations
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS timing_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_timing_samples_name ON timing_samples(name);"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reports_lookup ON reports(player_norm, team_norm, league_norm, season_norm, use_web, prompt_version);"
        )


def norm(s: str | None) -> str:
    # use the shared normalize_name for consistent behavior (transliteration, diacritics)
    return normalize_name(s or "", transliterate=True)


def get_cached_report(
    player: str,
    team: str = "",
    league: str = "",
    season: str = "",
    use_web: bool = False,
) -> dict | None:
    """
    Cache strategy:
    - Always match by player_norm.
    - If team/league/season are provided, require exact match on those too.
    - If requested use_web=True, prefer a cached report with use_web=1, but allow fallback.
    - Always match prompt_version so you can evolve prompts without mixing old/new.
    """
    player_n = norm(player)
    team_n = norm(team)
    league_n = norm(league)
    season_n = norm(season)
    use_web_flag = 1 if use_web else 0

    # Try in-memory cache first
    cache_key = (
        f"{player_n}|{team_n}|{league_n}|{season_n}|{use_web_flag}|{PROMPT_VERSION}"
    )
    now = time.time()
    try:
        with _CACHE_LOCK:
            ent = _CACHE.get(cache_key)
            if ent and (now - ent[0]) < _CACHE_TTL:
                return dict(ent[1])
            elif ent:
                del _CACHE[cache_key]
    except Exception:
        pass

    where = ["player_norm = ?", "prompt_version = ?"]
    params = [player_n, PROMPT_VERSION]

    if team_n:
        where.append("team_norm = ?")
        params.append(team_n)
    if league_n:
        where.append("league_norm = ?")
        params.append(league_n)
    if season_n:
        where.append("season_norm = ?")
        params.append(season_n)

    base_sql = f"""
        SELECT *
        FROM reports
        WHERE {" AND ".join(where)}
    """

    # 1) If use_web requested, try to find a web-sourced cached report first
    with connect() as conn:
        if use_web:
            row = conn.execute(
                base_sql + " AND use_web = 1 ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
            if row:
                try:
                    from utils.metrics import increment_metric

                    try:
                        increment_metric("report_db_reads")
                    except Exception:
                        pass
                except Exception:
                    pass
                out = dict(row)
                try:
                    with _CACHE_LOCK:
                        _CACHE[cache_key] = (time.time(), out)
                except Exception:
                    pass
                return out

        # 2) Otherwise return the newest cached report that matches
        row = conn.execute(
            base_sql + " ORDER BY created_at DESC LIMIT 1",
            params,
        ).fetchone()
        if row:
            try:
                from utils.metrics import increment_metric

                try:
                    increment_metric("report_db_reads")
                except Exception:
                    pass
            except Exception:
                pass
            out = dict(row)
            try:
                with _CACHE_LOCK:
                    _CACHE[cache_key] = (time.time(), out)
            except Exception:
                pass
            return out

        return None


def save_report(
    player: str,
    report_md: str,
    team: str = "",
    league: str = "",
    season: str = "",
    use_web: bool = False,
    model: str = "",
    queried_player: str | None = None,
) -> int:
    """
    Save a report to SQLite. If a report already exists for the same `player_norm`
    (and matching team/league/season when provided) then update that row and
    return its id instead of inserting a duplicate. This avoids needing schema
    changes and prevents duplicate rows for the same canonical player.
    """
    p = player.strip()
    q = (queried_player or "").strip()
    team_s = team.strip()
    league_s = league.strip()
    season_s = season.strip()

    p_norm = norm(p)
    q_norm = norm(q)
    team_norm = norm(team_s)
    league_norm = norm(league_s)
    season_norm = norm(season_s)

    with connect() as conn:
        import time as _time
        _save_start = _time.time()
        # Try to find an existing report for this canonical player (and prompt version)
        where = ["player_norm = ?", "prompt_version = ?"]
        params = [p_norm, PROMPT_VERSION]

        if team_norm:
            where.append("team_norm = ?")
            params.append(team_norm)
        if league_norm:
            where.append("league_norm = ?")
            params.append(league_norm)
        if season_norm:
            where.append("season_norm = ?")
            params.append(season_norm)

        sel_sql = f"SELECT id FROM reports WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT 1"
        row = conn.execute(sel_sql, params).fetchone()

        now = _utc_now_iso()

        if row:
            # Update existing row to refresh report content and metadata
            report_id = int(row[0])
            conn.execute(
                """
                UPDATE reports SET
                    player = ?, player_norm = ?, queried_player = ?, queried_player_norm = ?,
                    team = ?, team_norm = ?, league = ?, league_norm = ?, season = ?, season_norm = ?,
                    use_web = ?, model = ?, prompt_version = ?, report_md = ?, created_at = ?
                WHERE id = ?
                """,
                (
                    p,
                    p_norm,
                    q,
                    q_norm,
                    team_s,
                    team_norm,
                    league_s,
                    league_norm,
                    season_s,
                    season_norm,
                    1 if use_web else 0,
                    model,
                    PROMPT_VERSION,
                    report_md,
                    now,
                    report_id,
                ),
            )
            # Record alias for queried_player
            try:
                _upsert_player_alias(conn, p_norm, q, q_norm)
            except Exception:
                pass
            try:
                from utils.metrics import increment_metric
                try:
                    increment_metric("report_saves")
                except Exception:
                    pass
            except Exception:
                pass
            # invalidate cache entries for this player
            try:
                with _CACHE_LOCK:
                    keys = [
                        k for k in list(_CACHE.keys()) if k.startswith(f"{p_norm}|")
                    ]
                    for k in keys:
                        _CACHE.pop(k, None)
            except Exception:
                pass
            try:
                from utils.metrics import record_timing
                try:
                    _save_ms = (_time.time() - _save_start) * 1000.0
                    record_timing("db_save_ms", _save_ms)
                except Exception:
                    pass
            except Exception:
                pass
            return report_id

        # No existing row — insert a new one
        cur = conn.execute(
            """
            INSERT INTO reports (
                player, player_norm, queried_player, queried_player_norm, team, team_norm, league, league_norm, season, season_norm,
                use_web, model, prompt_version, report_md, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p,
                p_norm,
                q,
                q_norm,
                team_s,
                team_norm,
                league_s,
                league_norm,
                season_s,
                season_norm,
                1 if use_web else 0,
                model,
                PROMPT_VERSION,
                report_md,
                now,
            ),
        )
        new_id = cur.lastrowid
        try:
            # record timing for DB save (best-effort)
            from utils.metrics import record_timing
            try:
                _save_ms = (_time.time() - _save_start) * 1000.0
                record_timing("db_save_ms", _save_ms)
            except Exception:
                pass
        except Exception:
            pass
        # Record alias for queried_player
        try:
            _upsert_player_alias(conn, p_norm, q, q_norm)
        except Exception:
            pass
        try:
            from utils.metrics import increment_metric

            try:
                increment_metric("report_saves")
            except Exception:
                pass
        except Exception:
            pass
        try:
            with _CACHE_LOCK:
                keys = [k for k in list(_CACHE.keys()) if k.startswith(f"{p_norm}|")]
                for k in keys:
                    _CACHE.pop(k, None)
        except Exception:
            pass
        return new_id


def _upsert_player_alias(
    conn: sqlite3.Connection,
    player_norm: str,
    queried_player: str,
    queried_player_norm: str,
) -> None:
    """Insert or update an alias row linking `queried_player_norm` -> `player_norm`.
    `conn` must be an open sqlite3.Connection. This function is idempotent.
    """
    now = _utc_now_iso()
    # Try to update existing alias (match by queried_player_norm)
    cur = conn.execute(
        """
        UPDATE player_aliases
           SET player_norm = ?, queried_player = ?, last_seen = ?, count = count + 1
         WHERE queried_player_norm = ?
        """,
        (player_norm, queried_player, now, queried_player_norm),
    )
    if cur.rowcount == 0:
        # Insert new alias
        conn.execute(
            """
            INSERT INTO player_aliases (player_norm, queried_player, queried_player_norm, first_seen, last_seen, count)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (player_norm, queried_player, queried_player_norm, now, now),
        )


def find_canonical_by_alias(queried_player: str) -> dict | None:
    """Return the canonical mapping for a queried_player if present.
    Returns a dict with keys `player_norm` and `queried_player` or None.
    """
    qn = norm(queried_player)
    with connect() as conn:
        row = conn.execute(
            "SELECT player_norm, queried_player FROM player_aliases WHERE queried_player_norm = ? LIMIT 1",
            (qn,),
        ).fetchone()
        if not row:
            return None
        return {"player_norm": row[0], "queried_player": row[1]}
        


def list_local_reports(limit: int = 200) -> list:
    """Return recent reports from the local SQLite cache (not Postgres).

    This is used as a fast first-pass candidate set for fuzzy matching to
    avoid a round-trip to Postgres on common cache hits.
    """
    out = []
    try:
        with connect() as conn:
            cur = conn.execute(
                "SELECT id, player, created_at, use_web FROM reports ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            )
            rows = cur.fetchall()
            for r in rows:
                out.append(
                    {
                        "id": int(r[0]),
                        "player_name": r[1] or "",
                        "created_at": r[2] or None,
                        "cached": bool(r[3]),
                    }
                )
    except Exception:
        return []
    return out
