# utils/clean.py
from __future__ import annotations

import re

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", re.IGNORECASE)
_HTTP_RE = re.compile(r"\bhttps?://[^\s)]+", re.IGNORECASE)
_WWW_RE = re.compile(r"\bwww\.[^\s)]+", re.IGNORECASE)

# Bare domains (common TLDs) - keep conservative to avoid deleting normal text
_DOMAIN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9.-]*\.(com|org|net|io|co|eu|gov|edu|gr|uk|de|fr|it|es|pt|nl|be|ch|se|no|dk|fi|pl|cz|at|ie|tr|ro|bg|rs|hr|si|hu|sk|jp|kr|cn|in|au|nz)(/[^\s)]*)?\b",
    re.IGNORECASE,
)

# Empty parentheses/brackets after stripping URLs
_EMPTY_PARENS_RE = re.compile(r"\(\s*[,;:|/-]*\s*\)")
_EMPTY_BRACKETS_RE = re.compile(r"\[\s*[,;:|/-]*\s*\]")
_EMPTY_BRACES_RE = re.compile(r"\{\s*[,;:|/-]*\s*\}")

_MULTI_SPACE_RE = re.compile(r"\s{2,}")


def scrub_urls(text: str) -> str:
    """
    Remove URLs/domains from text + cleanup leftover punctuation.
    Intended for UI display, not for canonical storage.
    """
    t = str(text or "")
    t = t.replace("\u00a0", " ")

    # [label](url) -> label
    t = _MD_LINK_RE.sub(r"\1", t)

    # remove raw urls/domains
    t = _HTTP_RE.sub("", t)
    t = _WWW_RE.sub("", t)
    t = _DOMAIN_RE.sub("", t)

    # remove now-empty wrappers repeatedly
    for _ in range(6):
        before = t
        t = _EMPTY_PARENS_RE.sub(" ", t)
        t = _EMPTY_BRACKETS_RE.sub(" ", t)
        t = _EMPTY_BRACES_RE.sub(" ", t)
        if t == before:
            break

    # cleanup spacing + dangling punctuation spacing
    t = _MULTI_SPACE_RE.sub(" ", t).strip()
    t = re.sub(r"\s+([,.;:])", r"\1", t).strip()

    return t


def scrub_urls_preserve_newlines(text: str) -> str:
    """
    Similar to `scrub_urls` but preserves line breaks so markdown structure
    (headings, lists, blank lines) remains intact. Only collapses runs of
    spaces, not newlines.
    """
    t = str(text or "")
    t = t.replace("\u00a0", " ")

    # [label](url) -> label
    t = _MD_LINK_RE.sub(r"\1", t)

    # remove raw urls/domains
    t = _HTTP_RE.sub("", t)
    t = _WWW_RE.sub("", t)
    t = _DOMAIN_RE.sub("", t)

    # remove now-empty wrappers repeatedly
    for _ in range(6):
        before = t
        t = _EMPTY_PARENS_RE.sub(" ", t)
        t = _EMPTY_BRACKETS_RE.sub(" ", t)
        t = _EMPTY_BRACES_RE.sub(" ", t)
        if t == before:
            break

    # collapse multiple spaces but preserve newlines
    t = re.sub(r" {2,}", " ", t)
    # remove spaces before punctuation
    t = re.sub(r"[ \t]+([,.;:])", r"\1", t).strip()

    return t


def clean_value(v: str, fallback: str = "Unknown") -> str:
    """
    For table cells: strip urls, remove trailing empty (), normalize Unknown.
    """
    s = scrub_urls(str(v or "")).strip()
    s = re.sub(r"\s*\(\s*\)\s*$", "", s).strip()

    if not s:
        return fallback
    if s.lower() == "unknown":
        return fallback
    return s
