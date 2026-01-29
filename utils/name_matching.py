"""Clean name matching for player deduplication."""

import logging
import os
from difflib import SequenceMatcher

try:
    from rapidfuzz import fuzz
    _token_sort_ratio = getattr(fuzz, "token_sort_ratio", None)
    _token_set_ratio = getattr(fuzz, "token_set_ratio", None)
    if _token_sort_ratio is None:
        from rapidfuzz.fuzz import token_sort_ratio as _token_sort_ratio
    if _token_set_ratio is None:
        from rapidfuzz.fuzz import token_set_ratio as _token_set_ratio
    _HAS_RAPIDFUZZ = True
except Exception:
    _token_sort_ratio = None
    _token_set_ratio = None
    _HAS_RAPIDFUZZ = False

from utils.normalize import normalize_name
from utils.phonetic import phonetic_key
from utils.name_variants import NICKNAME_MAP

logger = logging.getLogger(__name__)

# Matching thresholds (configurable via env)
FIRSTNAME_REQUIRE = int(os.getenv("FIRSTNAME_REQUIRE", "90"))
FIRSTNAME_SECONDARY = int(os.getenv("FIRSTNAME_SECONDARY", "70"))
LASTNAME_HIGH = int(os.getenv("LASTNAME_HIGH", "95"))


def _sim_ratio(a: str, b: str) -> int:
    """Return similarity score 0-100."""
    try:
        if _HAS_RAPIDFUZZ and _token_set_ratio is not None:
            return int(_token_set_ratio(a, b) or 0)
        return int(SequenceMatcher(None, a, b).ratio() * 100)
    except Exception:
        return 0


def _compute_name_similarity(name_a: str, name_b: str) -> int:
    """Compute fuzzy similarity between two names.
    
    Uses rapidfuzz if available, falls back to difflib's SequenceMatcher.
    
    Args:
        name_a: First name to compare
        name_b: Second name to compare
        
    Returns:
        Similarity score from 0-100
    """
    return _sim_ratio(name_a, name_b)


def _check_first_name_alignment(
    player_norm: str,
    candidate_name: str,
    primary_score: int,
    primary_threshold: int = LASTNAME_HIGH,
) -> bool:
    """Check if first names align sufficiently for matching.
    
    Validates that first names are similar enough to accept a match,
    accounting for nicknames and fuzzy matching. Used as a safety check
    to prevent surname-only false matches (e.g., two different players
    with the same last name).
    
    Args:
        player_norm: Normalized query player name
        candidate_name: Candidate player name from database
        primary_score: Main similarity score (embedding or fuzzy)
        primary_threshold: Threshold above which secondary fname requirement applies
        
    Returns:
        True if first names align sufficiently, False otherwise
    """
    try:
        pn_parts = player_norm.split()
        nn_parts = normalize_name(candidate_name, transliterate=True).split()
        
        if not pn_parts or not nn_parts:
            return False
        
        first_p = pn_parts[0]
        first_n = nn_parts[0]
        
        # Check nickname canonicalization first (fast path)
        first_p_canon = NICKNAME_MAP.get(first_p, first_p)
        first_n_canon = NICKNAME_MAP.get(first_n, first_n)
        
        if first_p_canon == first_n_canon:
            return True  # Exact match or nickname equivalence
        
        # Compute fuzzy similarity
        fname_sim = _compute_name_similarity(first_p, first_n)
        
        # Allow if first name meets primary threshold OR
        # if primary score is very high and fname meets secondary threshold
        return (
            fname_sim >= FIRSTNAME_REQUIRE
            or (primary_score >= primary_threshold and fname_sim >= FIRSTNAME_SECONDARY)
        )
    except Exception:
        return False


def _last_names_align(a_norm: str, b_norm: str) -> bool:
    """Require last names to agree (exact, phonetic, or fuzzy) for suggestions.

    Tolerates 1-2 char typos (e.g., Papanikolaoy vs Papanikolaou).
    Prevents cross-player reuse when surnames are genuinely different.
    
    Args:
        a_norm: First normalized name
        b_norm: Second normalized name
        
    Returns:
        True if last names align, False otherwise
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
        # Fuzzy match for typos: allow if similarity >= 80% and length difference <= 2
        # Lowered from 85% to catch common single-char typos (e.g., "donic" vs "doncic" = 83%)
        try:
            if _HAS_RAPIDFUZZ and _token_set_ratio is not None:
                sim = int(_token_set_ratio(a_last.lower(), b_last.lower()) or 0)
            else:
                sim = int(SequenceMatcher(None, a_last.lower(), b_last.lower()).ratio() * 100)
            
            len_diff = abs(len(a_last) - len(b_last))
            if sim >= 80 and len_diff <= 2:
                return True
        except Exception:
            pass
    except Exception:
        return False
    return False


def names_match(name1: str, name2: str, threshold: int = 85) -> bool:
    """Check if two player names match via fuzzy + phonetic + nickname matching.
    
    Handles typos, nicknames, and transliteration by:
    1. Normalizing both names
    2. Applying nickname canonicalization (Kostas → Konstantinos)
    3. Requiring strong last-name match (>=85)
    4. Allowing looser first-name match if last-name very strong (>=95)
    
    Args:
        name1: First name to compare
        name2: Second name to compare
        threshold: Min similarity for first name (default 85)
    
    Returns:
        True if names are a match
    """
    if not name1 or not name2:
        return False

    def _canon(n: str) -> str:
        norm = normalize_name(n, transliterate=True)
        parts = [p for p in norm.split() if p]
        # Apply nickname canonicalization to first name
        if parts:
            parts[0] = NICKNAME_MAP.get(parts[0], parts[0])
        return " ".join(parts)

    n1 = _canon(name1)
    n2 = _canon(name2)

    if n1 == n2:
        return True

    parts1 = n1.split()
    parts2 = n2.split()
    if not parts1 or not parts2:
        return False

    first1, last1 = parts1[0], parts1[-1]
    first2, last2 = parts2[0], parts2[-1]

    # Last name similarity (strict)
    last_score = _sim_ratio(last1, last2)
    if last_score < 85:
        # Phonetic fallback
        try:
            if phonetic_key(last1) == phonetic_key(last2):
                last_score = 90
            else:
                return False
        except Exception:
            return False

    # First name similarity (looser if last name very strong)
    first_score = _sim_ratio(first1, first2)
    if last_score >= 95:
        return first_score >= 70 or first_score >= threshold
    return first_score >= threshold
