try:
    import jellyfish

    _HAS_JELLYFISH = True
except Exception:
    jellyfish = None
    _HAS_JELLYFISH = False

import re


def phonetic_key(s: str) -> str:
    """Return a phonetic key for `s`.

    Priority:
    - If `jellyfish` available, use `jellyfish.metaphone`.
    - Otherwise, fallback to a consonant skeleton (drop vowels + collapse repeats).
    """
    if not s:
        return ""
    s = str(s).strip().lower()
    if _HAS_JELLYFISH:
        try:
            # metaphone is tolerant; use it for phonetic comparison
            return jellyfish.metaphone(s) or ""
        except Exception:
            pass

    # fallback: consonant skeleton + collapse repeated chars
    core = re.sub(r"[aeiou\s]+", "", s)
    # collapse repeated letters
    core = re.sub(r"(.)\1+", r"\1", core)
    return core
