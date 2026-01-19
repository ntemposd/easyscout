"""Name-matching and similarity helpers (fuzzy and embedding-based).

Provides `_best_similar_report` and related constants used by the app.
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
                        try:
                            nn = payload.get("player") or ""
                            aln = _compute_alignment(player_norm, normalize_name(nn))
                            # Only apply surname-firstname guard when last-names
                            # align (strong last similarity or exact match).
                            if aln["last_sim"] >= 75 or aln["last_p"] == aln["last_n"]:
                                fname_sim = aln["first_sim"]
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
                    from db_pg import get_report, get_balance

                    payload = get_report(user_id, int(best_rid))
                    if payload:
                        # If the caller specified a league and the candidate
                        # payload has a league that doesn't match, skip this
                        # candidate (avoid cross-league suggestions).
                        try:
                            cand_league = ((payload.get("league") or "").strip().lower())
                            cand_team = ((payload.get("team") or payload.get("team_name") or "").strip().lower())
                            # If caller specified a league, require same-league candidate
                            if league_norm and cand_league and league_norm != cand_league:
                                return None
                            # If caller did not provide league but did provide a team,
                            # require same-team candidate.
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
                                # Enforce first-name requirement: if the first-name
                                # similarity is below the required threshold and
                                # the last-name similarity isn't very high, skip
                                # this embedding match so the LLM will be called.
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
                    # If the caller didn't provide league/team, avoid
                    # returning embedding-only surname matches as suggestions.
                    try:
                        payload = get_report(user_id, int(best_rid))
                        if payload and not (league_norm or team_norm):
                            # compute lightweight first-name similarity
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
                                        fname_sim = int(
                                            SequenceMatcher(None, first_p, first_n).ratio() * 100
                                        )
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
        try:
            aln = _compute_alignment(player_norm, name_norm)
            if aln["last_sim"] >= 75 or aln["last_p"] == aln["last_n"]:
                first_p = aln["first_p"]
                first_n = aln["first_n"]
                fname_sim = aln["first_sim"]

                first_p_canon = NICKNAME_MAP.get(first_p, first_p)
                first_n_canon = NICKNAME_MAP.get(first_n, first_n)

                # If first names are not canonical matches and similarity is
                # below threshold, cap the score to avoid auto-matching.
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
        except Exception:
            pass

        # Compute first-name and last-name similarity for this candidate
        try:
            pn_parts = player_norm.split()
            nn_parts = name_norm.split()
                try:
                    aln = _compute_alignment(player_norm, name_norm)
                    if aln["last_sim"] >= 75 or aln["last_p"] == aln["last_n"]:
                        first_p = aln["first_p"]
                        first_n = aln["first_n"]
                        fname_sim = aln["first_sim"]

                        first_p_canon = NICKNAME_MAP.get(first_p, first_p)
                        first_n_canon = NICKNAME_MAP.get(first_n, first_n)

                        # Only boost strongly when first-name variants or high first-name
                        # similarity are present. Avoid promoting surname-only matches
                        # to an auto-match.
                        if (
                            first_p_canon == first_n_canon
                            or fname_sim >= 80
                            or first_p.startswith(first_n)
                            or first_n.startswith(first_p)
                        ):
                            boosted = max(score, 85)
                            if boosted > best_score:
                                best_score = boosted
                                best = {"meta": c, "name_raw": name_raw}
        except Exception:
            pass
            best = {"meta": c, "name_raw": name_raw}

        try:
            pn_parts = player_norm.split()
            nn_parts = name_norm.split()
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
                        fname_sim = int(
                            SequenceMatcher(None, first_p, first_n).ratio() * 100
                        )
                    except Exception:
                        fname_sim = 0

                first_p_canon = NICKNAME_MAP.get(first_p, first_p)
                first_n_canon = NICKNAME_MAP.get(first_n, first_n)

                # Only boost strongly when first-name variants or high first-name
                # similarity are present. Avoid promoting surname-only matches
                # (e.g. Thanasis vs Giannis Antetokounmpo) to an auto-match.
                if (
                    first_p_canon == first_n_canon
                    or fname_sim >= 80
                    or first_p.startswith(first_n)
                    or first_n.startswith(first_p)
                ):
                    # Lower the boost so this becomes a suggestion (below
                    # the default auto_threshold of 95) unless the first
                    # names strongly match.
                    boosted = max(score, 85)
                    if boosted > best_score:
                        best_score = boosted
                        best = {"meta": c, "name_raw": name_raw}
        except Exception:
            pass

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
                    # Use alignment helper to compute first/last similarities
                    aln = _compute_alignment(player_norm, name_norm)
                    first_sim = aln.get("first_sim", 0)
                    last_sim = aln.get("last_sim", 0)
                    first_p = aln.get("first_p", "")
                    first_n = aln.get("first_n", "")
                    lp = aln.get("last_p", lp)
                    ln = aln.get("last_n", ln)
                except Exception:
                    first_sim = 0
                    last_sim = 0

                # Decide boosts only when there's reasonable first-name signal
                # or an explicit team/league match. Otherwise avoid boosting
                # surname-only collisions.
                cand_team = (c.get("team") or "").strip().lower()
                cand_league = (c.get("league") or c.get("league_norm") or "").strip().lower()
                # Only consider 'have_team_or_league' true when the caller
                # actually provided a team or league. This prevents promoting
                # surname-only matches when the client didn't supply context.
                have_team_or_league = bool(league_norm or team_norm)

                strong_last = last_sim >= 85 or lp == ln
                strong_first = first_sim >= 60 or first_p == first_n or NICKNAME_MAP.get(first_p) == NICKNAME_MAP.get(first_n)

                if red_score >= 80 and strong_last:
                    if strong_first or have_team_or_league:
                        boosted = max(score, 90)
                        if boosted > best_score:
                            best_score = boosted
                            best = {"meta": c, "name_raw": name_raw}
                    else:
                        # surname-only without first-name signal -> cap
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
                            boosted = max(score, 90)
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
                                boosted = max(score, 88)
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

    if best_score >= int(suggest_threshold):
        return {
            "type": "suggest",
            "report_id": int(best["meta"].get("id")),
            "player_name": best["name_raw"],
            "score": int(best_score),
        }

    return None
