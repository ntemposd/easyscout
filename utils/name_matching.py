"""Clean name matching for player deduplication."""

from difflib import SequenceMatcher

try:
    from rapidfuzz import fuzz
    _token_set_ratio = getattr(fuzz, "token_set_ratio", None)
    if _token_set_ratio is None:
        from rapidfuzz.fuzz import token_set_ratio as _token_set_ratio
    _HAS_RAPIDFUZZ = True
except Exception:
    _token_set_ratio = None
    _HAS_RAPIDFUZZ = False

from utils.normalize import normalize_name
from utils.phonetic import phonetic_key
from utils.name_variants import NICKNAME_MAP


def _sim_ratio(a: str, b: str) -> int:
    """Return similarity score 0-100."""
    try:
        if _HAS_RAPIDFUZZ and _token_set_ratio is not None:
            return int(_token_set_ratio(a, b) or 0)
        return int(SequenceMatcher(None, a, b).ratio() * 100)
    except Exception:
        return 0


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
