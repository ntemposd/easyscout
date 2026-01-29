"""Report similarity matching using embeddings and fuzzy name matching.

Provides functions to find similar player reports via:
1. Embedding vectors (fast, semantic similarity)
2. Fuzzy name matching (comprehensive, handles typos)

Used by the scout service to find existing reports before generating new ones.
"""

import logging
import os
import re
import sys
import time

from utils.normalize import normalize_name
from utils.name_matching import (
    _compute_name_similarity,
    _check_first_name_alignment,
    _last_names_align,
    FIRSTNAME_REQUIRE,
    FIRSTNAME_SECONDARY,
    LASTNAME_HIGH,
)
from utils.name_variants import NICKNAME_MAP
from utils.phonetic import phonetic_key

logger = logging.getLogger(__name__)

# Fuzzy matching library setup
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
    
    try:
        from utils.embeddings import find_nearest
        
        player_norm = normalize_name(player, transliterate=True)
        league_norm = (league or "").strip().lower()
        team_norm = (team or "").strip().lower()
        
        # Find nearest neighbors by embedding
        tops = find_nearest(client, player, top_k=5)
        
        if not tops:
            return None
        
        best_rid, best_sim = tops[0]
        
        # Check league/team constraints
        try:
            # If user_id is "*", fetch from any user (global search)
            if user_id == "*":
                from db import get_report_by_id
                payload = get_report_by_id(int(best_rid))
            else:
                from db import get_report
                payload = get_report(user_id, int(best_rid))
            
            if not payload:
                return None
            
            cand_league = (payload.get("league") or "").strip().lower()
            cand_team = (payload.get("team") or payload.get("team_name") or "").strip().lower()
            
            if league_norm and cand_league and league_norm != cand_league:
                # Try next best
                if len(tops) > 1:
                    best_rid, best_sim = tops[1]
                    if user_id == "*":
                        from db import get_report_by_id
                        payload = get_report_by_id(int(best_rid))
                    else:
                        from db import get_report
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
        nn = payload.get("player") or ""
        if not _last_names_align(player_norm, normalize_name(nn)):
            return None
        
        if not _check_first_name_alignment(player_norm, nn, int(best_sim * 100), 95):
            return None  # First name too different
        
        # Return based on similarity threshold
        if best_sim >= auto_threshold:
            payload["cached"] = True
            payload["report_id"] = int(best_rid)
            payload["matched_player_name"] = payload.get("player")
            payload["matched_score"] = int(best_sim * 100)
            from db import get_balance
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
        from db import list_reports
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

        # Direct alignment (p_first vs n_first, p_last vs n_last)
        first_sim_direct = _compute_name_similarity(first_p, first_n)
        last_sim_direct = _compute_name_similarity(last_p, last_n)
        score_direct = last_sim_direct * 2 + first_sim_direct

        # Cross alignment (p_first vs n_last, p_last vs n_first)
        first_sim_cross = _compute_name_similarity(first_p, last_n)
        last_sim_cross = _compute_name_similarity(last_p, first_n)
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
        from utils import embeddings as emb_utils
        from utils.embeddings import find_nearest

        def _handle_top(best_rid, best_sim):
            if best_sim >= float(os.getenv("EMBED_AUTO_THRESHOLD", "0.86")):
                try:
                    from db import get_report

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
                        # Apply surname-firstname safety check
                        nn = payload.get("player") or ""
                        if not _last_names_align(player_norm, normalize_name(nn)):
                            return None
                        
                        if not _check_first_name_alignment(player_norm, nn, int(best_sim * 100)):
                            return None

                        payload["cached"] = True
                        payload["report_id"] = int(best_rid)
                        payload["matched_player_name"] = payload.get("player")
                        payload["matched_score"] = int(best_sim * 100)
                        from db import get_balance
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
                        from db import get_report
                        # Always check first-name similarity for embedding suggestions
                        # to avoid surname-only false matches (e.g., Okaro → Derrick).
                        try:
                            payload = get_report(user_id, int(best_rid))
                            if payload:
                                pn_parts = player_norm.split()
                                nn = payload.get("player") or ""
                                nn = payload.get("player") or ""
                                if not _last_names_align(player_norm, normalize_name(nn)):
                                    return None
                                
                                if not _check_first_name_alignment(player_norm, nn, int(best_sim * 100)):
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
                tops = find_nearest(client, player, top_k=3)
        except Exception:
            tops = []

        if not tops:
            try:
                tops = find_nearest(client, player, top_k=3)
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
            from difflib import SequenceMatcher
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

        # Extract name parts for reduced matching
        pn_parts = player_norm.split()
        nn_parts = name_norm.split()
        
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
                    from difflib import SequenceMatcher
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
                        from difflib import SequenceMatcher
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
            from db import get_report, get_balance

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
