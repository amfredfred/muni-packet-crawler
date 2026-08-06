"""
Best-effort meeting-date extraction for a matched packet link.

Meeting listing pages vary wildly across the ~1,700 different municipal
sites/platforms this crawls: some (Municode/Legistar-style tables) expose a
machine-readable ISO datetime right next to the link; others only have the
date embedded in the link's own text or URL slug (e.g.
".../1-12-23-Planning-Commission-Minutes.pdf"); others need a nearby
row/list-item container. This is NOT a universal solution — it materially
improves coverage for common cases and returns None rather than guessing
wrong when it can't confidently parse a date. Other locales beyond English
and Spanish remain a known gap.

Extraction order matters and is deliberately narrow-to-wide:
  1. The anchor's own text, then its href/URL — these are almost always
     about the one specific document, so a date found here is trustworthy.
  2. A nearby structural row (<tr>/<li>/<article>) if one exists close by.
  3. Nothing wider than that. A flat WordPress-style block where dozens of
     unrelated links share one big container (no per-item wrapper at all)
     is common, and searching that whole container for "a date" previously
     picked up an unrelated date (e.g. a "last updated" stamp) and wrongly
     attached it to every link in the block — see the Fruitport, MI
     regression this was built to fix. Returning None is strictly better
     than a wrong shared date once the search has to go that wide.
"""

import re
from datetime import date, datetime

from bs4.element import Tag

ISO_ATTR_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ENGLISH_MONTHS = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
)
TEXT_DATE_RE = re.compile(
    rf"\b{ENGLISH_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.I
)
ORDINAL_SUFFIX_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)\b", re.I)
# Matches both slash and dash separated numeric dates, 2- or 4-digit year:
# "07/20/2026", "8-8-23", "1-12-23". Assumes US month-day-year order.
NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{2}|\d{4})\b")

SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
SPANISH_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+de\s+("
    + "|".join(SPANISH_MONTHS)
    + r")\s+de\s+(\d{4})\b",
    re.I,
)

TEXT_DATE_FORMATS = ["%B %d %Y", "%b %d %Y"]
ROW_TAGS = {"tr", "li", "article"}
MAX_ANCESTOR_HOPS = 4


def _try_parse_iso(text: str) -> date | None:
    m = ISO_ATTR_RE.search(text)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(0)).date()
    except ValueError:
        return None


def _try_parse_numeric(text: str) -> date | None:
    m = NUMERIC_DATE_RE.search(text)
    if not m:
        return None
    month, day, year = m.groups()
    year_num = int(year)
    if year_num < 100:
        year_num += 2000
    try:
        return date(year_num, int(month), int(day))
    except ValueError:
        return None


def _try_parse_spanish(text: str) -> date | None:
    m = SPANISH_DATE_RE.search(text)
    if not m:
        return None
    day, month_name, year = m.groups()
    try:
        return date(int(year), SPANISH_MONTHS[month_name.lower()], int(day))
    except ValueError:
        return None


def _try_parse_any(text: str) -> date | None:
    m = TEXT_DATE_RE.search(text)
    if m:
        cleaned = ORDINAL_SUFFIX_RE.sub(r"\1", m.group(0)).replace(",", "")
        # "Sept" is a common informal abbreviation strptime's %b doesn't
        # recognize (it expects the 3-letter "Sep"); trailing periods after
        # any abbreviated month ("Sept.", "Jan.") also aren't accepted.
        cleaned = re.sub(r"\bSept\b", "Sep", cleaned, flags=re.I)
        cleaned = re.sub(rf"\b({ENGLISH_MONTHS})\.", r"\1", cleaned, flags=re.I)
        for fmt in TEXT_DATE_FORMATS:
            try:
                return datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
    return _try_parse_numeric(text) or _try_parse_spanish(text)


def _find_nearby_row(anchor: Tag) -> Tag | None:
    """Nearest structural row ancestor, only within MAX_ANCESTOR_HOPS —
    returns None (rather than an arbitrary distant ancestor) if none is
    found that close, since a container that wide is usually shared by
    many unrelated links."""
    node = anchor
    for _ in range(MAX_ANCESTOR_HOPS):
        parent = node.find_parent()
        if parent is None:
            return None
        if parent.name in ROW_TAGS:
            return parent
        node = parent
    return None


def extract_meeting_date(anchor: Tag) -> date | None:
    own_text = anchor.get_text(" ", strip=True)
    parsed = _try_parse_any(own_text)
    if parsed:
        return parsed

    href = anchor.get("href", "")
    parsed = _try_parse_any(href.replace("-", " ").replace("/", " "))
    if parsed:
        return parsed
    # Re-check href with separators intact too — numeric regex needs them.
    parsed = _try_parse_numeric(href)
    if parsed:
        return parsed

    row = _find_nearby_row(anchor)
    if row is None:
        return None

    for tag in row.find_all(True):
        for attr in ("content", "datetime"):
            value = tag.get(attr)
            if value:
                parsed = _try_parse_iso(value)
                if parsed:
                    return parsed

    return _try_parse_any(row.get_text(" ", strip=True))


def is_upcoming(meeting_date: date | None, today: date | None = None) -> int | None:
    """1 = upcoming/today, 0 = past, None = date unknown (not discarded)."""
    if meeting_date is None:
        return None
    ref = today or date.today()
    return 1 if meeting_date >= ref else 0
