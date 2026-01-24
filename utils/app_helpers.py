# utils/app_helpers.py
"""Helper utilities extracted from app.py to keep the Flask app lean.

This module contains non-request-facing helpers only; imports are local
where needed to avoid circular imports with `app.py`.
"""
from difflib import SequenceMatcher
import os
import re
import sys
import time

try:
    from rapidfuzz import fuzz

    _token_sort_ratio = getattr(fuzz, "token_sort_ratio", None)
    _token_set_ratio = getattr(fuzz, "token_set_ratio", None)
    if _token_sort_ratio is None:
        from rapidfuzz.fuzz import token_sort_ratio as _token_sort_ratio

        try:
            from rapidfuzz.fuzz import token_set_ratio as _token_set_ratio
        except Exception:
            _token_set_ratio = None
    _HAS_RAPIDFUZZ = True
except Exception:
    _token_sort_ratio = None
    _token_set_ratio = None
    _HAS_RAPIDFUZZ = False

from utils.phonetic import phonetic_key
import logging
from utils.normalize import normalize_name
from utils.parse import extract_display_md
from utils.render import md_to_safe_html
import os

# --- Analytics (optional) ---
try:
    try:
        # Newer SDKs expose Client; alias it to Posthog for compatibility
        from posthog import Client as Posthog
    except Exception:
        # Older SDKs expose Posthog directly
        from posthog import Posthog  # type: ignore

    _POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
    _POSTHOG_HOST = os.getenv("POSTHOG_HOST") or "https://app.posthog.com"
    if _POSTHOG_API_KEY and Posthog:
        _analytics_client = Posthog(project_api_key=_POSTHOG_API_KEY, host=_POSTHOG_HOST)
        try:
            logging.getLogger("posthog").info("PostHog analytics initialized")
        except Exception:
            pass
    else:
        _analytics_client = None
except Exception:
    _analytics_client = None


def track_event(distinct_id: str | None, event: str, properties: dict | None = None) -> None:
    """Safely send an event to PostHog if configured. No-op when unavailable.

    `distinct_id` may be `None` for anonymous events.
    """
    logger = logging.getLogger("hoopscout.analytics")
    try:
        if not _analytics_client:
            logger.info("analytics disabled - dropping event %s", event)
            return

        immediate = os.getenv("POSTHOG_IMMEDIATE_FLUSH") == "1"

        if immediate:
            # Use a fresh short-lived client so we can flush immediately without
            # affecting the global client state.
            try:
                from posthog import Client as PH

                ph = PH(project_api_key=os.getenv("POSTHOG_API_KEY"), host=os.getenv("POSTHOG_HOST") or "https://app.posthog.com")
                try:
                    ph.capture(distinct_id=distinct_id or "anonymous", event=event, properties=properties or {})
                except TypeError:
                    import posthog as ph_mod

                    ph_mod.capture(distinct_id or "anonymous", event, properties=properties or {})
                try:
                    ph.shutdown()
                except Exception:
                    pass
                logger.info("event flushed immediately: %s with properties: %s", event, properties or {})
                return
            except Exception as e:
                logger.exception("Immediate flush failed, falling back to pooled client: %s", e)

        # Normal path: use pooled client
        logger.info("tracking event %s for %s: %s", event, distinct_id or "anonymous", properties or {})
        try:
            _analytics_client.capture(distinct_id=distinct_id or "anonymous", event=event, properties=properties or {})
            logger.info("event queued (client.capture): %s", event)
            return
        except TypeError:
            try:
                import posthog as posthog_module

                logger.info("falling back to module-level posthog.capture for event %s", event)
                posthog_module.capture(distinct_id or "anonymous", event, properties=properties or {})
                logger.info("event queued (module.capture): %s", event)
                return
            except Exception as e2:
                logger.exception("Fallback posthog.capture failed: %s", e2)
                return
    except Exception as e:
        logger.exception("Error sending analytics event: %s", e)
        # Do not allow analytics failures to affect app behavior
        return


def alias_user(previous_id: str, distinct_id: str) -> None:
    """Link anonymous ID with authenticated user ID in PostHog.
    
    Automatically merges all events from previous_id into distinct_id's profile.
    """
    logger = logging.getLogger("hoopscout.analytics")
    try:
        if not _analytics_client:
            logger.info("analytics disabled - skipping alias")
            return

        logger.info("aliasing user: %s -> %s", previous_id, distinct_id)
        try:
            _analytics_client.alias(previous_id, distinct_id)
            logger.info("user aliased successfully")
        except Exception as e:
            logger.exception("Failed to alias user: %s", e)
    except Exception as e:
        logger.exception("Error aliasing user: %s", e)


def shutdown_analytics() -> None:
    """Flush and shutdown the PostHog analytics client on app exit.
    
    Critical for Render's ephemeral dynos to ensure queued events aren't lost on restart.
    """
    try:
        if _analytics_client:
            # Flush any pending events before shutdown
            if hasattr(_analytics_client, "flush"):
                _analytics_client.flush()
            # Then shutdown the client
            if hasattr(_analytics_client, "shutdown"):
                _analytics_client.shutdown()
            logger = logging.getLogger("hoopscout.analytics")
            logger.info("PostHog analytics flushed and shutdown on exit")
    except Exception as e:
        logger = logging.getLogger("hoopscout.analytics")
        logger.exception("Error during analytics shutdown: %s", e)


def analytics_enabled() -> dict:
    """Return a small dict describing analytics client state for debugging."""
    try:
        return {
            "enabled": bool(_analytics_client),
            "host": os.getenv("POSTHOG_HOST") or "https://app.posthog.com",
            "has_key": bool(os.getenv("POSTHOG_API_KEY")),
        }
    except Exception:
        return {"enabled": False, "host": None, "has_key": False}

# Import name variant mappings from dedicated file
from utils.name_variants import NICKNAME_MAP

# Matching thresholds (configurable via env)
FIRSTNAME_REQUIRE = int(os.getenv("FIRSTNAME_REQUIRE", "90"))
FIRSTNAME_SECONDARY = int(os.getenv("FIRSTNAME_SECONDARY", "70"))
LASTNAME_HIGH = int(os.getenv("LASTNAME_HIGH", "95"))


def _ensure_parsed_payload(payload: dict) -> dict:
    """Populate derived fields from markdown when missing and ensure `report_html`.

    Best-effort helper used by cached/library/suggestion paths so the client
    receives structured `info_fields`, `grades`, `season_snapshot`,
    `last3_games`, and `report_html` when possible.
    """
    if not isinstance(payload, dict):
        return payload

    report_md = (payload.get("report_md") or "")
    try:
        display_md = extract_display_md(report_md)
        payload.setdefault("report_html", md_to_safe_html(display_md))
    except Exception:
        payload.setdefault("report_html", "")

    try:
        from utils.parse import (
            extract_info_fields,
            extract_grades,
            extract_season_snapshot,
            extract_last3_games,
        )

        if not payload.get("info_fields"):
            try:
                payload["info_fields"] = extract_info_fields(report_md)
            except Exception:
                payload["info_fields"] = {}

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
        # If parsing helpers are unavailable, leave payload as-is
        pass

    return payload


def _find_by_embedding_similarity(
    user_id: str,
    player: str,
    team: str = "",
    league: str = "",
    client=None,
    auto_threshold: float = 0.86,
    suggest_threshold: float = 0.78,
    max_scan: int = 50,
):
    """Find similar reports using embedding vectors (FAST, semantic)
    
    Returns: {"type": "auto" | "suggest", "report_id": ..., "score": ...} or None
    """
    if not player or not client:
        return None
    
    conn = None
    try:
        from db import connect
        from utils.embeddings import find_nearest
        
        conn = connect()
        player_norm = normalize_name(player, transliterate=True)
        league_norm = (league or "").strip().lower()
        team_norm = (team or "").strip().lower()
        
        # Find nearest neighbors by embedding
        tops = find_nearest(conn, client, player, top_k=5)
        
        if not tops:
            return None
        
        best_rid, best_sim = tops[0]
        
        # Check league/team constraints
        try:
            from db_pg import get_report
            payload = get_report(user_id, int(best_rid))
            if not payload:
                return None
            
            cand_league = (payload.get("league") or "").strip().lower()
            cand_team = (payload.get("team") or payload.get("team_name") or "").strip().lower()
            
            if league_norm and cand_league and league_norm != cand_league:
                # Try next best
                if len(tops) > 1:
                    best_rid, best_sim = tops[1]
                    payload = get_report(user_id, int(best_rid))
                    if not payload:
                        return None
                    cand_league = (payload.get("league") or "").strip().lower()
                    if league_norm and cand_league and league_norm != cand_league:
                        return None
                else:
                    return None
            
            if not league_norm and team_norm and cand_team and team_norm != cand_team:
                return None  # Team provided and doesn't match
        except Exception:
            return None
        
        # Check first/last name alignment (safety check)
        try:
            pn_parts = player_norm.split()
            nn = payload.get("player") or ""
            nn_parts = normalize_name(nn).split()
            
            if pn_parts and nn_parts:
                pn_first = pn_parts[0]
                pn_last = pn_parts[-1]
                nn_first = nn_parts[0]
                nn_last = nn_parts[-1]
                
                # Last names must align (exact or phonetic)
                if not _last_names_align(player_norm, normalize_name(nn)):
                    return None
                
                # First names: check nickname canonicalization
                pn_first_canon = NICKNAME_MAP.get(pn_first, pn_first)
                nn_first_canon = NICKNAME_MAP.get(nn_first, nn_first)
                
                if pn_first_canon == nn_first_canon:
                    # Exact first name match (or nickname equiv)
                    pass  # Allow it
                else:
                    # Compute first name similarity
                    fname_sim = 0
                    if _HAS_RAPIDFUZZ and _token_set_ratio is not None:
                        try:
                            fname_sim = int(_token_set_ratio(pn_first, nn_first) or 0)
                        except Exception:
                            fname_sim = 0
                    else:
                        try:
                            fname_sim = int(SequenceMatcher(None, pn_first, nn_first).ratio() * 100)
                        except Exception:
                            fname_sim = 0
                    
                    # Allow if embedding is very strong OR first name is reasonably similar
                    if not (
                        fname_sim >= FIRSTNAME_REQUIRE
                        or (best_sim >= 0.95 and fname_sim >= FIRSTNAME_SECONDARY)
                    ):
                        return None  # First name too different
        except Exception:
            pass  # Safety check failed, reject
            return None
        
        # Return based on similarity threshold
        if best_sim >= auto_threshold:
            payload["cached"] = True
            payload["report_id"] = int(best_rid)
            payload["matched_player_name"] = payload.get("player")
            payload["matched_score"] = int(best_sim * 100)
            from db_pg import get_balance
            payload["credits_remaining"] = get_balance(user_id)
            return {"type": "auto", "payload": payload, "score": int(best_sim * 100)}
        
        elif best_sim >= suggest_threshold:
            return {
                "type": "suggest",
                "report_id": int(best_rid),
                "player_name": payload.get("player"),
                "score": int(best_sim * 100),
            }
    except Exception as e:
        logger.warning(f"Embedding similarity check failed: {e}")
    finally:
        # Always close SQLite connection
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    
    return None


def _best_similar_report(
    user_id: str,
    player: str,
    team: str = "",
    league: str = "",
    client=None,
    auto_threshold: int = 95,
    suggest_threshold: int = 85,
    max_scan: int = 200,
    transliterate: bool = True,
):
    """Scan user's recent reports and return either an `auto` payload, 
    a `suggest` dict with `report_id`, or None.
    """
    if not player or not player.strip():
        return None
    player_norm = normalize_name(player, transliterate=transliterate)
    started = time.monotonic()
    max_secs = float(os.getenv("FUZZY_TIMEOUT_SECS", "3.0"))

    candidates = []
    # Search Postgres FIRST (where current reports live)
    # Do NOT fallback to SQLite — that's old/stale data and may include other users' reports
    try:
        from db_pg import list_reports
        candidates = list_reports(user_id, q="", limit=max_scan)
    except Exception:
        candidates = []

    # If no Postgres candidates, return None (don't search SQLite)
    # SQLite is only for embeddings storage, not for matching

    
    if not candidates:
        # No reports in Postgres for this user → no fuzzy matches possible
        return None

    # Normalize provided league/team for quick checks
    league_norm = (league or "").strip().lower()
    team_norm = (team or "").strip().lower()

    # EXACT MATCH CHECK: If we find an exact normalized match from another user,
    # return it as a suggestion with score=100 so it auto-accepts and saves to their library
    # This includes nickname-aware matching (e.g., "Kostas" matches "Konstantinos")
    player_parts = player_norm.split()
    player_first = player_parts[0] if player_parts else ""
    player_last = player_parts[-1] if player_parts else ""
    player_first_canon = NICKNAME_MAP.get(player_first, player_first)

    def _last_names_align(a_norm: str, b_norm: str) -> bool:
        """Require last names to agree (exact, phonetic, or fuzzy) for suggestions.

        Tolerates 1-2 char typos (e.g., Papanikolaoy vs Papanikolaou).
        Prevents cross-player reuse when surnames are genuinely different.
        """
        try:
            a_last = (a_norm.split()[-1] if a_norm else "").strip()
            b_last = (b_norm.split()[-1] if b_norm else "").strip()
            if not a_last or not b_last:
                return False
            if a_last == b_last:
                return True
            # Normalize both for diacritic/case comparison
            try:
                import unicodedata
                a_clean = ''.join(c for c in unicodedata.normalize('NFD', a_last) if unicodedata.category(c) != 'Mn').lower()
                b_clean = ''.join(c for c in unicodedata.normalize('NFD', b_last) if unicodedata.category(c) != 'Mn').lower()
                if a_clean == b_clean:
                    return True
            except Exception:
                pass
            try:
                pa = phonetic_key(a_last)
                pb = phonetic_key(b_last)
                if pa and pb and pa == pb:
                    return True
            except Exception:
                pass
            # Fuzzy match for typos: allow if similarity >= 90% and length difference <= 2
            try:
                if _HAS_RAPIDFUZZ and _token_set_ratio is not None:
                    sim = int(_token_set_ratio(a_last.lower(), b_last.lower()) or 0)
                else:
                    sim = int(SequenceMatcher(None, a_last.lower(), b_last.lower()).ratio() * 100)
                
                len_diff = abs(len(a_last) - len(b_last))
                if sim >= 90 and len_diff <= 2:
                    return True
            except Exception:
                pass
        except Exception:
            return False
        return False
    
    for c in candidates:
        try:
            if time.monotonic() - started > max_secs:
                return None
        except Exception:
            pass
        name_raw = (c.get("player_name") or c.get("player") or "").strip()
        if not name_raw:
            continue
        name_norm = normalize_name(name_raw, transliterate=transliterate)
        
        # Check for exact match (including nickname equivalence)
        is_exact_match = False
        if player_norm == name_norm:
            is_exact_match = True
        else:
            # Check nickname-canonicalized match
            name_parts = name_norm.split()
            name_first = name_parts[0] if name_parts else ""
            name_last = name_parts[-1] if name_parts else ""
            name_first_canon = NICKNAME_MAP.get(name_first, name_first)
            
            # Match if: same last name + same canonical first name
            # Also allow phonetic last name match for common typos (e.g., Farid vs Faried)
            last_name_match = player_last == name_last
            if not last_name_match:
                try:
                    # Check phonetic similarity for last names (handles 1-letter typos)
                    from utils.phonetic import phonetic_key
                    p_phonetic = phonetic_key(player_last)
                    n_phonetic = phonetic_key(name_last)
                    if p_phonetic and n_phonetic and p_phonetic == n_phonetic:
                        last_name_match = True
                    # Also check string distance for very close matches
                    elif player_last and name_last:
                        # Allow 1-char difference for names >= 5 chars
                        if len(player_last) >= 5 and len(name_last) >= 5:
                            from difflib import SequenceMatcher
                            sim = SequenceMatcher(None, player_last, name_last).ratio()
                            if sim >= 0.85:  # 85% similarity
                                last_name_match = True
                except Exception:
                    pass
            
            if last_name_match and player_first_canon == name_first_canon:
                is_exact_match = True
        
        if is_exact_match:
            # Check league/team constraints
            try:
                cand_league = (c.get("league") or "").strip().lower()
                cand_team = (c.get("team") or "").strip().lower()
                if league_norm and cand_league and league_norm != cand_league:
                    continue  # League mismatch, keep looking
                if team_norm and cand_team and team_norm != cand_team:
                    continue  # Team mismatch, keep looking
            except Exception:
                pass

            # Safety: ensure surnames align before suggesting
            if not _last_names_align(player_norm, name_norm):
                continue
            
            # Exact match found! Return as suggestion with score 100 (will trigger auto-accept)
            return {
                "type": "suggest",
                "report_id": int(c.get("id")),
                "player_name": name_raw,
                "score": 100,
            }

    # Helper: compute first/last alignment and similarities accounting for
    # reversed input (e.g., "White Derrick") and nickname canonicalization.
    def _compute_alignment(p_norm: str, n_norm: str):
        p_parts = p_norm.split()
        n_parts = n_norm.split()
        first_p = p_parts[0] if p_parts else ""
        last_p = p_parts[-1] if p_parts else ""
        first_n = n_parts[0] if n_parts else ""
        last_n = n_parts[-1] if n_parts else ""

        def _sim(a, b):
            try:
                if _HAS_RAPIDFUZZ and _token_set_ratio is not None:
                    return int(_token_set_ratio(a, b) or 0)
                return int(SequenceMatcher(None, a, b).ratio() * 100)
            except Exception:
                return 0

        # Direct alignment (p_first vs n_first, p_last vs n_last)
        first_sim_direct = _sim(first_p, first_n)
        last_sim_direct = _sim(last_p, last_n)
        score_direct = last_sim_direct * 2 + first_sim_direct

        # Cross alignment (p_first vs n_last, p_last vs n_first)
        first_sim_cross = _sim(first_p, last_n)
        last_sim_cross = _sim(last_p, first_n)
        score_cross = last_sim_cross * 2 + first_sim_cross

        if score_cross > score_direct:
            # Use cross alignment
            return {
                "first_p": first_p,
                "first_n": last_n,
                "last_p": last_p,
                "last_n": first_n,
                "first_sim": first_sim_cross,
                "last_sim": last_sim_cross,
            }
        return {
            "first_p": first_p,
            "first_n": first_n,
            "last_p": last_p,
            "last_n": last_n,
            "first_sim": first_sim_direct,
            "last_sim": last_sim_direct,
        }

    # Embedding pre-check (local-first then OpenAI fallback)
    try:
        from db import connect
        from utils import embeddings as emb_utils
        from utils.embeddings import find_nearest

        conn = connect()

        def _handle_top(best_rid, best_sim):
            if best_sim >= float(os.getenv("EMBED_AUTO_THRESHOLD", "0.86")):
                try:
                    from db_pg import get_report

                    payload = get_report(user_id, int(best_rid))
                    if payload:
                        try:
                            cand_league = ((payload.get("league") or "").strip().lower())
                            cand_team = ((payload.get("team") or payload.get("team_name") or "").strip().lower())
                            if league_norm and cand_league and league_norm != cand_league:
                                return None
                            if not league_norm and team_norm and cand_team and team_norm != cand_team:
                                return None
                        except Exception:
                            pass
                        # Apply surname-firstname safety check similar to fuzzy
                        try:
                            pn_parts = player_norm.split()
                            nn = payload.get("player") or ""
                            nn_parts = normalize_name(nn).split()
                            if (
                                pn_parts
                                and nn_parts
                                and pn_parts[-1] == nn_parts[-1]
                            ):
                                first_p = pn_parts[0]
                                first_n = nn_parts[0]
                                fname_sim = 0
                                if _HAS_RAPIDFUZZ and _token_set_ratio is not None:
                                    try:
                                        fname_sim = int(
                                            _token_set_ratio(first_p, first_n) or 0
                                        )
                                    except Exception:
                                        fname_sim = 0
                                else:
                                    try:
                                        fname_sim = int(
                                            SequenceMatcher(None, first_p, first_n).ratio()
                                            * 100
                                        )
                                    except Exception:
                                        fname_sim = 0

                                first_p_canon = NICKNAME_MAP.get(first_p, first_p)
                                first_n_canon = NICKNAME_MAP.get(first_n, first_n)

                                # If this is a surname-only strong embedding match
                                # (first names dissimilar), cap/downgrade to a
                                # suggestion-level score so it won't auto-match.
                                if not (
                                    fname_sim >= FIRSTNAME_REQUIRE
                                    or (
                                        int(best_sim * 100) >= LASTNAME_HIGH
                                        and fname_sim >= FIRSTNAME_SECONDARY
                                    )
                                ):
                                    return None

                        except Exception:
                            pass

                        payload["cached"] = True
                        payload["report_id"] = int(best_rid)
                        payload["matched_player_name"] = payload.get("player")
                        payload["matched_score"] = int(best_sim * 100)
                        from db_pg import get_balance
                        payload["credits_remaining"] = get_balance(user_id)

                        return {
                            "type": "auto",
                            "payload": payload,
                            "score": int(best_sim * 100),
                        }
                except Exception:
                    return None
            if best_sim >= float(os.getenv("EMBED_SUGGEST_THRESHOLD", "0.78")):
                try:
                        from db_pg import get_report
                        # Always check first-name similarity for embedding suggestions
                        # to avoid surname-only false matches (e.g., Okaro → Derrick).
                        try:
                            payload = get_report(user_id, int(best_rid))
                            if payload:
                                pn_parts = player_norm.split()
                                nn = payload.get("player") or ""
                                nn_parts = normalize_name(nn).split()
                                if pn_parts and nn_parts and pn_parts[-1] == nn_parts[-1]:
                                    first_p = pn_parts[0]
                                    first_n = nn_parts[0]
                                    fname_sim = 0
                                    if _HAS_RAPIDFUZZ and _token_set_ratio is not None:
                                        try:
                                            fname_sim = int(_token_set_ratio(first_p, first_n) or 0)
                                        except Exception:
                                            fname_sim = 0
                                    else:
                                        try:
                                            fname_sim = int(SequenceMatcher(None, first_p, first_n).ratio() * 100)
                                        except Exception:
                                            fname_sim = 0

                                    if not (
                                        fname_sim >= FIRSTNAME_REQUIRE
                                        or (
                                            int(best_sim * 100) >= LASTNAME_HIGH
                                            and fname_sim >= FIRSTNAME_SECONDARY
                                        )
                                    ):
                                        return None

                                    # Guard: require surname alignment (exact or phonetic)
                                    if not _last_names_align(player_norm, normalize_name(nn)):
                                        return None
                        except Exception:
                            pass

                        return {
                            "type": "suggest",
                            "report_id": int(best_rid),
                            "player_name": get_report(user_id, int(best_rid)).get("player"),
                            "score": int(best_sim * 100),
                        }
                except Exception:
                    return None
            return None

        tops = []
        try:
            if getattr(emb_utils, "_HAS_SBER", False):
                tops = find_nearest(conn, client, player, top_k=3)
        except Exception:
            tops = []

        if not tops:
            try:
                tops = find_nearest(conn, client, player, top_k=3)
            except Exception:
                tops = []

        if tops:
            best_rid, best_sim = tops[0]
            res = _handle_top(best_rid, best_sim)
            if res:
                return res
    except Exception:
        pass

    best = None
    best_score = 0

    for c in candidates:
        try:
            if time.monotonic() - started > max_secs:
                return None
        except Exception:
            pass
        name_raw = (c.get("player_name") or c.get("player") or "").strip()
        if not name_raw:
            continue
        name_norm = normalize_name(name_raw, transliterate=transliterate)

        score = 0
        if _HAS_RAPIDFUZZ and _token_sort_ratio is not None:
            try:
                s1 = int(_token_sort_ratio(player_norm, name_norm) or 0)
            except Exception:
                try:
                    s1 = int(
                        _token_sort_ratio(player_norm, name_norm, score_cutoff=0) or 0
                    )
                except Exception:
                    s1 = 0
            s2 = 0
            if _token_set_ratio is not None:
                try:
                    s2 = int(_token_set_ratio(player_norm, name_norm) or 0)
                except Exception:
                    s2 = 0
            score = max(s1, s2)
        else:
            score = int(SequenceMatcher(None, player_norm, name_norm).ratio() * 100)

        # If the caller provided a league and the candidate has one,
        # prefer same-league reports only. This prevents cross-league
        # surname collisions (e.g., two different players named "White").
        try:
            cand_league = (c.get("league") or c.get("league_norm") or "").strip().lower()
            if league_norm and cand_league and league_norm != cand_league:
                continue
        except Exception:
            pass

        # If the surname matches but first names are dissimilar, avoid
        # letting token-based scores (which can be high due to identical
        # last names) push this into an auto-match. Cap such surname-only
        # scores so they become suggestions instead.
        try:
            aln = _compute_alignment(player_norm, name_norm)
            if aln["last_sim"] >= 75 or aln["last_p"] == aln["last_n"]:
                first_p = aln["first_p"]
                first_n = aln["first_n"]
                fname_sim = aln["first_sim"]

                first_p_canon = NICKNAME_MAP.get(first_p, first_p)
                first_n_canon = NICKNAME_MAP.get(first_n, first_n)

                if (
                    first_p_canon != first_n_canon
                    and fname_sim < 80
                    and not (first_p.startswith(first_n) or first_n.startswith(first_p))
                ):
                    try:
                        score = min(score, int(suggest_threshold) - 1)
                    except Exception:
                        score = min(score, 84)
        except Exception:
            pass

        if score > best_score:
            best_score = score
            best = {"meta": c, "name_raw": name_raw}

        try:
            if len(pn_parts) >= 2 and len(nn_parts) >= 2:
                p_reduced = f"{pn_parts[0]} {pn_parts[-1]}"
                n_reduced = f"{nn_parts[0]} {nn_parts[-1]}"
                red_score = 0
                if _HAS_RAPIDFUZZ and _token_sort_ratio is not None:
                    try:
                        red_score = int(_token_sort_ratio(p_reduced, n_reduced) or 0)
                    except Exception:
                        red_score = 0
                else:
                    red_score = int(
                        SequenceMatcher(None, p_reduced, n_reduced).ratio() * 100
                    )

                # Compute first-name and last-name similarity separately
                lp = pn_parts[-1]
                ln = nn_parts[-1]
                first_p = pn_parts[0]
                first_n = nn_parts[0]
                try:
                    if _HAS_RAPIDFUZZ and _token_set_ratio is not None:
                        first_sim = int(_token_set_ratio(first_p, first_n) or 0)
                        last_sim = int(_token_set_ratio(lp, ln) or 0)
                    else:
                        first_sim = int(SequenceMatcher(None, first_p, first_n).ratio() * 100)
                        last_sim = int(SequenceMatcher(None, lp, ln).ratio() * 100)
                except Exception:
                    first_sim = 0
                    last_sim = 0

                cand_team = (c.get("team") or "").strip().lower()
                cand_league = (c.get("league") or c.get("league_norm") or "").strip().lower()
                # Only consider 'have_team_or_league' true when the caller
                # provided a team/league AND the candidate matches that
                # team/league. This prevents promoting surname-only matches
                # when the provided context doesn't actually align with the
                # candidate.
                have_team_or_league = bool(
                    (league_norm and cand_league and league_norm == cand_league)
                    or (team_norm and cand_team and team_norm == cand_team)
                )

                strong_last = last_sim >= 85 or lp == ln
                first_p_canon = NICKNAME_MAP.get(first_p, first_p)
                first_n_canon = NICKNAME_MAP.get(first_n, first_n)
                strong_first = first_sim >= 60 or first_p == first_n or first_p_canon == first_n_canon

                print(f"DEBUG FUZZY: Comparing '{player_norm}' with '{name_norm}'", file=sys.stderr)
                print(f"  - Scores: token={score}, reduced={red_score}, first={first_sim}, last={last_sim}", file=sys.stderr)
                print(f"  - Parts: player=({first_p}, {lp}), candidate=({first_n}, {ln})", file=sys.stderr)
                print(f"  - Canonical: player={first_p_canon}, candidate={first_n_canon}", file=sys.stderr)
                print(f"  - Strong: last={strong_last}, first={strong_first}, team/league={have_team_or_league}", file=sys.stderr)

                if red_score >= 80 and strong_last:
                    if strong_first or have_team_or_league:
                        boosted = max(score, 88)
                        print(f"  → BOOSTING to {boosted} (was {score})", file=sys.stderr)
                        if boosted > best_score:
                            best_score = boosted
                            best = {"meta": c, "name_raw": name_raw}
                    else:
                        print(f"  → CAPPING to {min(score, 84)} (not strong_first, no team/league)", file=sys.stderr)
                        try:
                            score = min(score, int(suggest_threshold) - 1)
                        except Exception:
                            score = min(score, 84)

                try:
                    try:
                        pk = phonetic_key(lp)
                        nk = phonetic_key(ln)
                    except Exception:
                        pk = nk = None

                    if pk and nk and pk == nk:
                        if strong_first or have_team_or_league:
                            boosted = max(score, 88)
                            if boosted > best_score:
                                best_score = boosted
                                best = {"meta": c, "name_raw": name_raw}
                        else:
                            try:
                                score = min(score, int(suggest_threshold) - 1)
                            except Exception:
                                score = min(score, 84)
                    else:
                        sk_p = re.sub(r"[aeiou]", "", lp)
                        sk_n = re.sub(r"[aeiou]", "", ln)
                        sk_score = int(SequenceMatcher(None, sk_p, sk_n).ratio() * 100)
                        if sk_score >= 55:
                            if strong_first or have_team_or_league:
                                boosted = max(score, 86)
                                if boosted > best_score:
                                    best_score = boosted
                                    best = {"meta": c, "name_raw": name_raw}
                            else:
                                try:
                                    score = min(score, int(suggest_threshold) - 1)
                                except Exception:
                                    score = min(score, 84)
                except Exception:
                    pass
        except Exception:
            pass

    if not best:
        return None

    if best_score >= int(auto_threshold):
        try:
            from db_pg import get_report, get_balance

            payload = get_report(user_id, int(best["meta"].get("id")))
            if payload:
                payload["cached"] = True
                payload["report_id"] = int(best["meta"].get("id"))
                payload["matched_player_name"] = best["name_raw"]
                payload["matched_score"] = int(best_score)
                payload["credits_remaining"] = get_balance(user_id)
                return {"type": "auto", "payload": payload, "score": int(best_score)}
        except Exception:
            return None

    # Extra safety: if the best candidate's first-name similarity is
    # below the required threshold, avoid returning a suggestion so the
    # server will fall back to LLM generation. Allow suggestion only if
    # the last-name similarity is extremely high and the first-name meets
    # the secondary threshold.
    try:
        from utils.normalize import normalize_name as _norm_name
        aln = _compute_alignment(player_norm, _norm_name(best["name_raw"]))
        fname_sim_best = aln.get("first_sim", 0)
        last_sim_best = aln.get("last_sim", 0)
        if not (
            fname_sim_best >= FIRSTNAME_REQUIRE
            or (int(best_score) >= LASTNAME_HIGH and fname_sim_best >= FIRSTNAME_SECONDARY)
        ):
            # Insufficient first-name signal for the best candidate: treat as no match
            # so the server falls back to LLM generation rather than returning
            # a potentially incorrect suggestion (e.g., Okaro vs Derrick).
            return None

        # Additional guard: surnames must align (exact or phonetic) to return a suggestion
        if not _last_names_align(player_norm, _norm_name(best["name_raw"])):
            return None
    except Exception as e:
        # Log but don't suppress safety check failures
        import logging
        logging.getLogger(__name__).exception("Safety check failed in _best_similar_report: %s", e)

    if best_score >= int(suggest_threshold):
        return {
            "type": "suggest",
            "report_id": int(best["meta"].get("id")),
            "player_name": best["name_raw"],
            "score": int(best_score),
        }

    return None


# --- Cost Estimation ---

def estimate_cost(usage: dict, prices: dict) -> float:
    """Calculate estimated cost based on token usage and pricing.
    
    Args:
        usage: Dict with 'input_tokens' and 'output_tokens' keys
        prices: Dict with 'input' and 'output' keys (price per 1M tokens)
    
    Returns:
        Estimated cost in dollars
    """
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    
    return (
        input_tokens / 1_000_000 * prices["input"]
        + output_tokens / 1_000_000 * prices["output"]
    )


# Default pricing for common models ($ per 1M tokens)
# Source: https://openai.com/api/pricing/ (as of January 2026)
MODEL_PRICES = {
    # GPT-5 Series (Flagship models)
    "gpt-5.2": {"input": 1.75, "output": 14.00},
    "gpt-5.2-pro": {"input": 21.00, "output": 168.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    
    # GPT-4.1 Series
    "gpt-4.1": {"input": 3.00, "output": 12.00},
    "gpt-4.1-mini": {"input": 0.80, "output": 3.20},
    "gpt-4.1-nano": {"input": 0.20, "output": 0.80},
    
    # Legacy GPT-4 Series (deprecated, using estimated pricing)
    "gpt-4": {"input": 30.0, "output": 60.0},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    "gpt-4o": {"input": 5.0, "output": 15.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    
    # GPT-3.5 Series (legacy)
    "gpt-3.5-turbo": {"input": 0.5, "output": 1.5},
    
    # Default fallback
    "default": {"input": 1.75, "output": 14.00},  # GPT-5.2 pricing
}


def get_model_prices(model: str) -> dict:
    """Get pricing for a specific model.
    
    Args:
        model: Model name
        
    Returns:
        Dict with 'input' and 'output' pricing per 1M tokens
    """
    # Check exact match first
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    
    # Check if model name contains known model as substring
    for model_key in MODEL_PRICES:
        if model_key in model.lower():
            return MODEL_PRICES[model_key]
    
    # Return default pricing
    return MODEL_PRICES["default"]
