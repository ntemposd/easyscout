# utils/app_helpers.py
"""Helper utilities extracted from app.py to keep the Flask app lean.

This module contains non-request-facing helpers only; imports are local
where needed to avoid circular imports with `app.py`.
"""
from difflib import SequenceMatcher
import os
import re

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
    from posthog import Posthog

    _POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
    _POSTHOG_HOST = os.getenv("POSTHOG_HOST") or "https://app.posthog.com"
    if _POSTHOG_API_KEY:
        # Prefer explicit keyword `project_api_key` for compatibility
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
                from posthog import Posthog as PH

                ph = PH(project_api_key=os.getenv("POSTHOG_API_KEY"), host=os.getenv("POSTHOG_HOST") or "https://app.posthog.com")
                try:
                    ph.capture(event, properties=properties or {}, distinct_id=distinct_id or "anonymous")
                except TypeError:
                    import posthog as ph_mod

                    ph_mod.capture(distinct_id or "anonymous", event, properties=properties or {})
                try:
                    ph.shutdown()
                except Exception:
                    pass
                logger.info("event flushed immediately: %s", event)
                return
            except Exception as e:
                logger.exception("Immediate flush failed, falling back to pooled client: %s", e)

        # Normal path: use pooled client
        logger.info("tracking event %s for %s: %s", event, distinct_id or "anonymous", properties or {})
        try:
            _analytics_client.capture(event, properties=properties or {}, distinct_id=distinct_id or "anonymous")
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

# Small nickname/alias map to normalize common first-name variants
NICKNAME_MAP = {
    "kostas": "konstantinos",
    "kostaras": "konstantinos",
    "kostis": "konstantinos",
    "konsta": "konstantinos",
    "gianis": "giannis",
    "yianis": "giannis",
    "gannis": "giannis",
}

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
    """Scan recent reports for this user and return either an `auto` payload,
    a `suggest` dict with `report_id`, or None.
    """
    if not player or not player.strip():
        return None
    player_norm = normalize_name(player, transliterate=transliterate)

    candidates = []
    try:
        from db import list_local_reports

        candidates = list_local_reports(limit=max_scan)
    except Exception:
        candidates = []

    if not candidates:
        try:
            from db_pg import list_reports

            candidates = list_reports(user_id, q="", limit=max_scan)
        except Exception:
            return None

    # Normalize provided league/team for quick checks
    league_norm = (league or "").strip().lower()
    team_norm = (team or "").strip().lower()

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
                strong_first = first_sim >= 60 or first_p == first_n or NICKNAME_MAP.get(first_p) == NICKNAME_MAP.get(first_n)

                if red_score >= 80 and strong_last:
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
