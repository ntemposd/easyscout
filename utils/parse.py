# utils/parse.py
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from utils.clean import clean_value, scrub_urls

# ----------------------------
# Regex helpers
# ----------------------------

_BOLD_FIELD_RE = re.compile(r"^\s*\*\*(.+?):\*\*\s*(.*)\s*$")

# Match section headings we want to remove from display markdown
_SECTION_START_RE = re.compile(
    r"^\s*#{2,6}\s*(season snapshot|last 3 games|grades|final verdict|sources)\b",
    re.IGNORECASE,
)

# Model sometimes collapses tables into one line like:
# "Grades (1-5) | Category | Grade | Shooting | 4/5 | ..."
_COLLAPSED_BLOCK_RE = re.compile(
    r"^\s*(season snapshot|last 3 games|grades)\b.*\|.*\|",
    re.IGNORECASE,
)

# Simple URL/domain detection for display cleanup
_URL_LIKE_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

# Markdown table separator row like: |---|---|
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*[-:\s|]{5,}\s*\|?\s*$")

# Headings for locating sections
_HEAD_SEASON_RE = re.compile(r"^\s*#{2,6}\s*season snapshot\b", re.IGNORECASE)
_HEAD_LAST3_RE = re.compile(r"^\s*#{2,6}\s*last\s*3\s*games\b", re.IGNORECASE)
_HEAD_GRADES_RE = re.compile(r"^\s*#{2,6}\s*grades\b", re.IGNORECASE)
_HEAD_VERDICT_RE = re.compile(r"^\s*#{2,6}\s*final verdict\b", re.IGNORECASE)

# Support "Final verdict: ..." inline anywhere
_INLINE_VERDICT_RE = re.compile(r"(?im)^\s*[-*•]?\s*final verdict\s*[:—\-]\s*(.+?)\s*$")

# Pipe row matcher for "Skill | 3/5" rows anywhere
_PIPE_ROW_RE = re.compile(
    r"\|\s*([^|\n]+?)\s*\|\s*([0-5](?:\.[0-9]+)?)\s*(?:\s*/\s*5)?\s*\|",
    re.IGNORECASE,
)

# "Skill: 3/5" lines
_SKILL_LINE_RE = re.compile(
    r"^(.+?)\s*[:—\-]\s*([0-5](?:\.[0-9]+)?)\s*(?:\s*/\s*5)?\s*$",
    re.IGNORECASE,
)


# ----------------------------
# Info fields
# ----------------------------


def _split_team_league(text: str) -> Tuple[str, str]:
    s = clean_value(text or "", "Unknown")
    if s == "Unknown":
        return "Unknown", "Unknown"

    # "Milwaukee Bucks (NBA)"
    m = re.match(r"^(.+?)\s*\((.+?)\)\s*$", s)
    if m:
        return clean_value(m.group(1), "Unknown"), clean_value(m.group(2), "Unknown")

    # separators
    for sep in [" / ", " — ", " – ", " - ", " | ", "•", "·"]:
        if sep in s:
            parts = [p.strip() for p in s.split(sep) if p.strip()]
            if len(parts) >= 2:
                return clean_value(parts[0], "Unknown"), clean_value(
                    " ".join(parts[1:]), "Unknown"
                )

    return clean_value(s, "Unknown"), "Unknown"


def _split_height_weight(fields: Dict[str, str]) -> None:
    """Post-process info_fields dict to split combined Height/Weight values.
    
    Modifies the dict in-place. Handles two cases:
    1. Separate "Height / Weight" key with combined value
    2. "Height" key containing both values (e.g., "6'8\"/214 lb")
    """
    try:
        # Case 1: Look for combined key like "Height / Weight"
        hw_key = None
        for k in list(fields.keys()):
            lk = (k or "").strip().lower()
            if "height" in lk and "weight" in lk and "/" in k:
                hw_key = k
                break

        if hw_key:
            combined = fields.get(hw_key, "") or ""
            parts = [p.strip() for p in re.split(r"[/\\|–—-]", combined) if p.strip()]
            if parts:
                h = parts[0]
                w = parts[1] if len(parts) > 1 else ""
                if "Height" not in fields or not fields.get("Height"):
                    fields["Height"] = clean_value(h, "Unknown")
                if "Weight" not in fields or not fields.get("Weight"):
                    fields["Weight"] = clean_value(w, "Unknown")
                try:
                    del fields[hw_key]
                except Exception:
                    pass
        else:
            # Case 2: "Height" key contains both values (e.g., "6'8\"/214 lb")
            if "Height" in fields:
                hv = (fields.get("Height") or "").strip()
                if hv and "/" in hv and (
                    not fields.get("Weight") or fields.get("Weight") == "Unknown"
                ):
                    parts = [p.strip() for p in re.split(r"[/\\|–—-]", hv) if p.strip()]
                    if parts:
                        h = parts[0]
                        w = parts[1] if len(parts) > 1 else ""
                        fields["Height"] = clean_value(h, "Unknown")
                        fields["Weight"] = clean_value(w, "Unknown")
    except Exception:
        pass


def extract_info_fields(report_md: str) -> Dict[str, str]:
    """
    Extract header fields:
      **Team:** ...
      **League:** ...
      etc.
    Scans only the beginning until the first "###" section.
    Also derives Team + League from Team / League if needed.
    """
    fields: Dict[str, str] = {}
    if not report_md:
        return fields

    lines = report_md.replace("\r\n", "\n").splitlines()[:120]

    for line in lines:
        s = line.strip()

        # stop once we hit the first real section
        if s.startswith("### "):
            break

        m = _BOLD_FIELD_RE.match(s)
        if not m:
            continue

        key = (m.group(1) or "").strip()
        val = clean_value(m.group(2) or "", "Unknown")
        fields[key] = val

        if len(fields) >= 24:
            break

    # Normalize key variants
    if "Dominant Hand" in fields and "Dominant hand" not in fields:
        fields["Dominant hand"] = fields["Dominant Hand"]
    if "Team/League" in fields and "Team / League" not in fields:
        fields["Team / League"] = fields["Team/League"]

    # Derive Team + League from combined if missing
    combined = fields.get("Team / League", "")
    team = fields.get("Team", "")
    league = fields.get("League", "")

    if combined and (
        (not team or team == "Unknown") or (not league or league == "Unknown")
    ):
        t, league = _split_team_league(combined)
        fields.setdefault("Team", t)
        fields.setdefault("League", league)

    # If Team exists but League missing and Team looks like "Team (League)" or "Team / League"
    if (not fields.get("League") or fields.get("League") == "Unknown") and fields.get(
        "Team"
    ) not in (None, "", "Unknown"):
        t, league = _split_team_league(fields["Team"])
        if league != "Unknown":
            fields["Team"] = t
            fields["League"] = league

    # If Height / Weight provided as a single combined field, split into two
    # separate fields so UI can render them on separate lines.
    try:
        # Look for combined key variants
        hw_key = None
        for k in list(fields.keys()):
            lk = (k or "").strip().lower()
            if "height" in lk and "weight" in lk and "/" in k:
                hw_key = k
                break

        if hw_key:
            combined = fields.get(hw_key, "") or ""
            # Split on slash or other separators
            parts = [p.strip() for p in re.split(r"[/\\|–—-]", combined) if p.strip()]
            if parts:
                h = parts[0]
                w = parts[1] if len(parts) > 1 else ""
                # Don't overwrite if explicit separate fields already exist
                if "Height" not in fields or not fields.get("Height"):
                    fields["Height"] = clean_value(h, "Unknown")
                if "Weight" not in fields or not fields.get("Weight"):
                    fields["Weight"] = clean_value(w, "Unknown")
                # Remove combined key to avoid duplicate display
                try:
                    del fields[hw_key]
                except Exception:
                    pass
        else:
            # Fallback: sometimes the markdown provides a `**Height:** 6'8"/214 lb`
            # (combined value under the `Height` key) while `Weight` is missing
            # or marked `Unknown`. Detect that and split the value across both
            # `Height` and `Weight` fields so the UI renders them on separate lines.
            try:
                if "Height" in fields:
                    hv = (fields.get("Height") or "").strip()
                    if hv and "/" in hv and (
                        not fields.get("Weight") or fields.get("Weight") == "Unknown"
                    ):
                        parts = [p.strip() for p in re.split(r"[/\\|–—-]", hv) if p.strip()]
                        if parts:
                            h = parts[0]
                            w = parts[1] if len(parts) > 1 else ""
                            fields["Height"] = clean_value(h, "Unknown")
                            fields.setdefault("Weight", clean_value(w, "Unknown"))
            except Exception:
                pass
    except Exception:
        pass

    return fields


def extract_canonical_player(report_md: str) -> str:
    """Try to heuristically extract a canonical player name from the report markdown.

    Looks for:
    - Bold header fields (handled elsewhere) via 'Player' or 'Name'
    - Title lines like 'Scouting Report — Name (Team)'
    - First heading or leading line that looks like 'Name (Team)'
    Returns empty string when not found.
    """
    if not report_md:
        return ""

    # 1) Try existing info fields first
    try:
        fields = extract_info_fields(report_md)
        p = (fields.get("Player") or fields.get("Name") or "").strip()
        if p:
            return p
    except Exception:
        pass

    # Look at the first 10 lines for title-like patterns
    lines = report_md.replace("\r\n", "\n").splitlines()[:10]
    for ln in lines:
        s = ln.strip()
        if not s:
            continue

        # Example: "Scouting Report — Giannis Antetokounmpo (Milwaukee Bucks)"
        m = re.search(
            r"scouting report\s*[—:-]\s*(?P<name>[^\(\n\r]+)", s, re.IGNORECASE
        )
        if m:
            name = m.group("name").strip()
            # remove trailing team in parentheses if present
            name = re.sub(r"\s*\([^\)]*\)\s*$", "", name).strip()
            if name:
                return name

        # Example: "Giannis Antetokounmpo (Milwaukee Bucks)"
        m2 = re.match(r"^(?P<name>[^\(\n\r]+)\s*\([^\)]+\)\s*$", s)
        if m2:
            name = m2.group("name").strip()
            if name:
                return name

        # If the first non-empty line is short and looks like a name (has a space), take it
        if len(s) < 60 and " " in s and not s.endswith(":"):
            # avoid lines like 'Team: Milwaukee Bucks' (they contain ':')
            if ":" not in s and "|" not in s:
                return s

    return ""


def _extract_from_verified_note(report_md: str) -> str:
    try:
        m = re.search(
            r"Verified stats correspond to\s*(?P<name>[^\(\n\r]+)",
            report_md,
            re.IGNORECASE,
        )
        if m:
            name = m.group("name").strip()
            name = re.sub(r"\s*\([^\)]*\)\s*$", "", name).strip()
            if name:
                return name
    except Exception:
        pass
    return ""


def _extract_from_urls(report_md: str) -> str:
    # find URLs and try to extract name-like last path component
    try:
        urls = re.findall(r"https?://[^\s)]+", report_md)
        for u in urls:
            # take last path segment
            parts = re.split(r"[/?#]", u)
            if not parts:
                continue
            last = parts[-1] or parts[-2] if len(parts) > 1 else ""
            if not last:
                continue
            # common pattern: giannis-antetokounmpo
            if re.search(r"[a-z]+-[a-z]+", last, re.IGNORECASE):
                nm = last.replace("-", " ")
                nm = re.sub(r"[^A-Za-z \-]", "", nm).strip()
                if nm and " " in nm:
                    # Title-case the extracted slug
                    return " ".join([p.capitalize() for p in nm.split()])
    except Exception:
        pass
    return ""


# ----------------------------
# Markdown table parsing
# ----------------------------


def _is_table_line(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _parse_first_md_table(
    section_lines: List[str],
) -> Tuple[List[str], List[List[str]], str]:
    """
    Finds the first markdown table in the given section lines.
    Returns (headers, rows, note).
    Note is the first non-table non-empty line after the table, if any.
    """
    idx = None
    for i, line in enumerate(section_lines):
        if _is_table_line(line.strip()):
            idx = i
            break

    if idx is None:
        return [], [], ""

    headers = [h.strip() for h in section_lines[idx].strip().strip("|").split("|")]
    rows: List[List[str]] = []
    note = ""

    for line in section_lines[idx + 1 :]:
        s = (line or "").strip()
        if not s:
            continue

        if not _is_table_line(s):
            note = s
            break

        cells = [c.strip() for c in s.strip("|").split("|")]
        if _MD_TABLE_SEP_RE.match(s):
            continue
        rows.append(cells)

    return headers, rows, note


def _section_lines(report_md: str, head_re: re.Pattern) -> Optional[List[str]]:
    lines = report_md.replace("\r\n", "\n").splitlines()
    start = None

    for i, line in enumerate(lines):
        if head_re.match(line):
            start = i + 1
            break

    if start is None:
        return None

    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r"^\s*#{2,6}\s+", lines[j]):
            end = j
            break

    return lines[start:end]


def _header_map(headers: List[str], row: List[str]) -> Dict[str, str]:
    hu = [h.strip().upper() for h in headers]
    out: Dict[str, str] = {}
    for i, h in enumerate(hu):
        out[h] = (row[i].strip() if i < len(row) else "").strip()
    return out


# ----------------------------
# Season snapshot + last 3 games
# ----------------------------


def extract_season_snapshot(report_md: str) -> Dict[str, str]:
    """
    Expects section:
      ### Season snapshot
      | GAMES | PTS | REB | AST | FG |
      ...
      Note: ...
    Returns dict with games/pts/reb/ast/fg/note (defaults to "—" for cells).
    """
    if not report_md:
        return {}

    sec = _section_lines(report_md, _HEAD_SEASON_RE)
    if not sec:
        return {}

    headers, rows, note = _parse_first_md_table(sec)
    if not headers or not rows:
        # Still return note if present (cleaned)
        return {"note": clean_value(note, "")} if note else {}

    hmap = _header_map(headers, rows[0])

    return {
        "games": clean_value(hmap.get("GAMES", ""), "—"),
        "pts": clean_value(hmap.get("PTS", ""), "—"),
        "reb": clean_value(hmap.get("REB", ""), "—"),
        "ast": clean_value(hmap.get("AST", ""), "—"),
        "fg": clean_value(hmap.get("FG", ""), "—"),
        "note": clean_value(note, "") if note else "",
    }


def extract_last3_games(report_md: str) -> List[Dict[str, str]]:
    """
    Expects section:
      ### Last 3 games
      | OPP | PTS | REB | AST | FG |
      (3 rows)
      Note: ...
    Returns up to 3 parsed game rows.
    Filters out accidental repeated header rows.
    """
    if not report_md:
        return []

    sec = _section_lines(report_md, _HEAD_LAST3_RE)
    if not sec:
        return []

    headers, rows, _note = _parse_first_md_table(sec)
    if not headers or not rows:
        return []

    out: List[Dict[str, str]] = []
    for r in rows:
        hmap = _header_map(headers, r)

        opp = (hmap.get("OPP", "") or "").strip()
        pts = (hmap.get("PTS", "") or "").strip()

        # Skip repeated header row inside body
        if opp.upper() == "OPP" or pts.upper() == "PTS":
            continue

        out.append(
            {
                "opp": clean_value(opp, "—"),
                "pts": clean_value(hmap.get("PTS", ""), "—"),
                "reb": clean_value(hmap.get("REB", ""), "—"),
                "ast": clean_value(hmap.get("AST", ""), "—"),
                "fg": clean_value(hmap.get("FG", ""), "—"),
            }
        )

        if len(out) >= 3:
            break

    return out


# ----------------------------
# Grades + Final verdict
# ----------------------------


def _extract_final_verdict(report_md: str) -> str:
    if not report_md:
        return ""

    # Inline "Final verdict: ..."
    m = _INLINE_VERDICT_RE.search(report_md)
    if m:
        return clean_value(m.group(1), "")

    # Heading + next non-empty line
    lines = report_md.replace("\r\n", "\n").splitlines()
    for i, line in enumerate(lines):
        if _HEAD_VERDICT_RE.match(line):
            for j in range(i + 1, min(i + 8, len(lines))):
                s = lines[j].strip()
                if not s:
                    continue
                if re.match(r"^\s*#{2,6}\s+", s):
                    return ""
                return clean_value(s, "")
            return ""

    return ""


def extract_grades(report_md: str) -> Tuple[List[dict], str]:
    """
    Extract grades + final verdict.
    Handles:
      - Proper "### Grades" section with markdown table rows
      - Headings like "### Grades (1–5)"
      - Collapsed one-line junk like "Grades (1-5) | Category | Grade | ..."
      - Fallback to last ~350 lines if section missing
    Returns (grades, final_verdict)
    """
    if not report_md:
        return [], ""

    final_verdict = _extract_final_verdict(report_md)

    lines = report_md.replace("\r\n", "\n").splitlines()

    # Find grades heading
    start = None
    for i, line in enumerate(lines):
        if _HEAD_GRADES_RE.match(line):
            start = i + 1
            break

    if start is not None:
        end = len(lines)
        for j in range(start, len(lines)):
            if re.match(r"^\s*#{2,6}\s+", lines[j]):
                end = j
                break
        section_lines = lines[start:end] if start < end else lines[-350:]
    else:
        section_lines = lines[-350:]

    # Build candidate text:
    # - grades section (if found)
    # - any lines containing "grades" + pipes (collapsed tables)
    candidates: List[str] = []
    if start is not None:
        candidates.extend(section_lines)

    for ln in lines:
        s = ln.strip()
        if "grade" in s.lower() and "|" in s:
            candidates.append(s)

    section_text = "\n".join(candidates)
    section_text = section_text.replace("**", "").replace("__", "")

    grades_map: Dict[str, dict] = {}

    # 1) Parse pipe rows like "| Shooting | 3/5 |"
    for skill, score in _PIPE_ROW_RE.findall(section_text):
        s = re.sub(r"\s+", " ", (skill or "")).strip()
        if s.lower() in {"category", "grade"}:
            continue
        sc = max(0.0, min(5.0, float(score)))
        grades_map[s.lower()] = {"skill": clean_value(s, s), "score": sc}

    # 2) Parse "Skill: 3/5" lines
    for raw in candidates:
        line = (raw or "").strip()
        if not line:
            continue
        line = re.sub(r"^\s*[-*•]\s*", "", line)
        line = line.replace("**", "").replace("__", "").strip()

        m = _SKILL_LINE_RE.match(line)
        if not m:
            continue

        skill = re.sub(r"\s+", " ", m.group(1)).strip()
        if skill.lower().startswith("final verdict"):
            continue

        sc = max(0.0, min(5.0, float(m.group(2))))
        grades_map[skill.lower()] = {"skill": clean_value(skill, skill), "score": sc}

    # 3) Extra robust parse for collapsed inline tables:
    # e.g. "Grades (1-5) | Category | Grade | Shooting | 4/5 | Finishing | 5/5 | ..."
    # We'll scan for pairs after "Grades" line.
    for raw in candidates:
        s = (raw or "").strip()
        if not s.lower().startswith("grades"):
            continue
        if "|" not in s:
            continue

        parts = [p.strip() for p in s.split("|") if p.strip()]
        # remove leading "Grades..." if present
        if parts and parts[0].lower().startswith("grades"):
            parts = parts[1:]

        # remove header tokens if present
        parts = [p for p in parts if p.lower() not in {"category", "grade"}]

        # Now attempt pair parsing: skill, score, skill, score...
        for i in range(0, len(parts) - 1, 2):
            skill = parts[i]
            score_txt = parts[i + 1]
            m = re.search(r"([0-5](?:\.[0-9]+)?)", score_txt)
            if not m:
                continue
            sc = max(0.0, min(5.0, float(m.group(1))))
            sk = clean_value(skill, skill)
            if sk.lower() in {"grades", "grade"}:
                continue
            grades_map[sk.lower()] = {"skill": sk, "score": sc}

    return list(grades_map.values()), final_verdict


# ----------------------------
# Display markdown builder (server-side)
# ----------------------------


def _strip_sections_by_heading(md: str) -> str:
    """
    Remove whole sections if they appear under headings:
      ### Season snapshot
      ### Last 3 games
      ### Grades
      ### Final verdict
      ### Sources
    Skip until next heading.
    """
    lines = md.replace("\r\n", "\n").splitlines()
    out: List[str] = []
    skipping = False

    for line in lines:
        if _SECTION_START_RE.match(line):
            skipping = True
            continue

        if skipping and re.match(r"^\s*#{2,6}\s+", line):
            skipping = False

        if skipping:
            continue

        out.append(line)

    return "\n".join(out).strip()


def extract_display_md(report_md: str) -> str:
    """
    Returns display-only markdown that won't duplicate your UI tables:
    - removes the report title heading
    - removes header info lines (**Team:** etc)
    - removes sections (even if model collapses them into one line)
    - removes sources + url lines
    """
    if not report_md:
        return ""

    md = report_md

    # Normalize inline heading markers that the model sometimes emits directly
    # after a paragraph (e.g. "... selection. ### Physical ..."). Convert
    # occurrences of a heading token that are not on their own line into a
    # proper newline-prefixed heading so downstream section-stripping and the
    # markdown renderer treat them as headings.
    try:
        md = re.sub(r"(?<!\n)(\s*)(#{1,6}\s+)", r"\n\n\2", md)
    except Exception:
        pass

    # 1) Remove proper heading-based sections
    md = _strip_sections_by_heading(md)

    lines = md.replace("\r\n", "\n").splitlines()

    # 2) Remove the report title if present
    if lines and re.match(r"^\s*#{1,6}\s*scouting report\b", lines[0], re.IGNORECASE):
        lines = lines[1:]

    # 3) Remove bold header info fields until first ### section
    out: List[str] = []
    in_header = True

    for line in lines:
        s = (line or "").strip()

        if in_header:
            if s.startswith("### "):
                in_header = False
                out.append(line)
                continue

            if _BOLD_FIELD_RE.match(s) or s == "":
                continue

            # keep any intro line before sections if exists
            out.append(line)
            continue

        out.append(line)

    # 4) Remove collapsed one-line blocks + url lines + leftover table separators
    cleaned: List[str] = []
    for line in out:
        s = (line or "").strip()

        # "Grades ... | ... | ..." or "Season snapshot ... | ... | ..."
        if _COLLAPSED_BLOCK_RE.match(s):
            continue

        # inline sources leaks
        if s.lower().startswith("sources"):
            continue

        # drop url-like lines (we also scrub during render, but this keeps output cleaner)
        if _URL_LIKE_RE.search(s):
            continue

        # drop stray markdown separator rows that can leak
        if _MD_TABLE_SEP_RE.match(s):
            continue

        cleaned.append(line)

    # 5) Final pass: scrub urls/domains from remaining text
    final_lines = []
    for line in cleaned:
        final_lines.append(scrub_urls(line))

    return "\n".join(final_lines).strip()
