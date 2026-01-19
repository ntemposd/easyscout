import re
import unicodedata

try:
    from unidecode import unidecode

    _HAS_UNIDECODE = True
except Exception:
    unidecode = None
    _HAS_UNIDECODE = False


def normalize_name(s: str, transliterate: bool = True) -> str:
    """Normalize a player/team/league string for comparisons and indexing.

    Behaviors:
    - Trim and coerce to str
    - Unicode NFKC normalization
    - Optional transliteration via `unidecode` when available
    - Remove diacritics
    - Lowercase
    - Replace punctuation with spaces and collapse whitespace
    """
    if not s:
        return ""
    s = str(s).strip()
    s = unicodedata.normalize("NFKC", s)
    if transliterate and _HAS_UNIDECODE:
        try:
            s = unidecode(s)
        except Exception:
            pass
    s = "".join(
        ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn"
    )
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.U)
    s = re.sub(r"\s+", " ", s).strip()
    return s
