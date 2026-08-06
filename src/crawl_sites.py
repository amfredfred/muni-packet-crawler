"""
Site crawler: for each municipality in `municipalities`, finds its
meetings/agendas page (on-site or a linked civic platform) and extracts
packet/agenda links from it, recording results into `meetings` and
`packets`.

This is the headless, no-Google successor to the Chrome extension's
Auto-scan + Find Packets flow: instead of a Google search turning up a
`.gov` link, we already have the municipality's real official_url as the
starting point, so we go straight to crawling it.

Per-municipality steps:
  1. Fetch the official homepage.
  2. Rank candidate "meetings page" links found on it: a link naming an
     exact qualifying board type scores highest, then links to a known
     civic-platform host, then links whose text/href matches generic
     agenda/meeting keywords.
  3. Visit the top candidate page(s) and extract packet links: direct PDF
     links or link text matching "packet"/"agenda" (matchReason), plus a
     shallow follow (max FOLLOW_LIMIT) of sub-pages that look like an
     individual meeting/detail row on a known civic platform, mirroring the
     heuristic in the Chrome extension's extractPacketLinks. A packet is
     only kept if it, the page-level link that led to it, or its nearest
     heading names a qualifying board type (see matches_board_filter) — a
     municipality's site usually mixes Planning Board content in with City
     Council, Library Board, Parks Board, etc., and this filter is what
     keeps only the board types this project actually cares about.
  4. Record one `meetings` row per (municipality, candidate page) and one
     `packets` row per packet found on it (direct or via a sub-page).
  5. Update the municipality's site_status ('ok' / 'no_agenda_found' /
     'error'), agenda_url, and platform (host of the winning candidate, if
     it's a known civic platform).

Concurrency: municipalities are processed by a thread pool (--workers,
default 20) — this is I/O-bound work (waiting on network responses across
thousands of *distinct* third-party hosts), so threads are effective
despite the GIL, and hitting many different domains concurrently isn't the
same politeness concern as hammering one server repeatedly. Each worker
opens its own SQLite connection (required — connections aren't safely
shared across threads); WAL mode + a busy_timeout (see db.get_connection)
absorb the brief write conflicts from workers finishing at nearly the same
moment instead of raising "database is locked". Generous per-request
timeout, one retry on transient failure, no retries beyond that — across
thousands of arbitrary third-party sites, timeouts/errors are expected and
just get recorded as site_status='error' rather than crashing the run.
"""

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from date_extract import extract_meeting_date, is_upcoming
from db import get_connection

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
REQUEST_TIMEOUT = 15
CANDIDATE_PAGES_TO_VISIT = 2
FOLLOW_LIMIT = 3

CIVIC_HOSTS = [
    "civicplus.com",
    "municode.com",
    "granicus.com",
    "iqm2.com",
    "legistar.com",
    "escribemeetings.com",
    "novusagenda.com",
]

AGENDA_KEYWORDS = re.compile(
    r"\b(agenda|minutes|meeting|calendar|board|commission|planning|zoning|council)\b",
    re.I,
)
PDF_RE = re.compile(r"\.pdf($|[?#])", re.I)
TEXT_MATCH_RE = re.compile(r"\b(packet|agenda)\b", re.I)
# Excludes email/RSS signup links whose text happens to contain "agenda"
# (e.g. "Receive Agenda Alerts") — these aren't packets, just noise that
# passed the keyword check.
NOISE_RE = re.compile(
    r"\b(alerts?|subscribe|subscription|notify|notifications?|sign up|newsletters?|rss feed)\b",
    re.I,
)
PLATFORM_DETAIL_RE = re.compile(
    r"(MeetingDetail|ViewPublisher|AgendaViewer|MeetingView|Meeting\.aspx|Detail\.aspx)", re.I
)
MEETING_ROW_RE = re.compile(r"\b(minutes|meeting details|view agenda)\b", re.I)
DATE_RE = re.compile(r"\b[A-Z][a-z]{2}\s\d{1,2},\s\d{4}\b")

# The board-name filter: land-use/historic-preservation board types,
# matching the keyword list the sibling Chrome extension
# (packet-finder-chrome-extension/data.json) searches Google for —
# "Planning Board", "Historical Commission", "Planning and Zoning
# Commission", "Board of Adjustments and Appeals", "Planning Commission and
# Board of Zoning Appeals", "County Zoning Commission", "City Board of
# Adjustment", "City Zoning Commission", "County Board of Planning", "County
# Board of Adjustment", "Zoning Board of Adjustment", "Historic Zoning
# Commission" — PLUS a second category the original 12 didn't cover at all:
# licensing bodies (liquor/alcohol/business license boards — e.g. many
# Massachusetts towns have a "Board of License Commissioners" that approves
# liquor licenses, often for a fee). Rather than match phrases verbatim
# (real sites word things too many different ways — "Zoning Hearing Board",
# "Licensing Board for the City of X"), this matches on the underlying
# concept: a body whose name combines a topic word with a board/commission-
# type word. Verified this covers all 12 original phrases plus the new
# licensing ones.
BOARD_TOPIC_RE = re.compile(
    r"\b(planning|zoning|historic|historical|adjustments?|appeals"
    r"|licens\w*|liquor|alcohol\w*)\b",
    re.I,
)
BOARD_TYPE_RE = re.compile(r"\b(board|commission(?:ers)?)\b", re.I)


def matches_board_filter(text: str) -> bool:
    return bool(BOARD_TOPIC_RE.search(text) and BOARD_TYPE_RE.search(text))


# The exact 12 keywords from packet-finder-chrome-extension/data.json,
# exposed so the UI can offer them as a filter dropdown. classify_board_type
# picks the closest of these for a given packet by word overlap — it's an
# approximation (real site wording rarely matches one of the 12 verbatim,
# which is why matches_board_filter above is concept-based rather than
# exact-match), not a guarantee the packet's board is literally named that.
BOARD_KEYWORDS = [
    "Planning Board",
    "Historical Commission",
    "Planning and Zoning Commission",
    "Board of Adjustments and Appeals",
    "Planning Commission and Board of Zoning Appeals",
    "County Zoning Commission",
    "City Board of Adjustment",
    "City Zoning Commission",
    "County Board of Planning",
    "County Board of Adjustment",
    "Zoning Board of Adjustment",
    "Historic Zoning Commission",
    "Liquor License Board",
    "Alcoholic Beverage Control Board",
    "Board of License Commissioners",
    "License Commission",
]
_STOPWORDS = {"of", "and", "the"}
_KEYWORD_WORD_SETS = [
    (kw, {w.lower() for w in kw.split() if w.lower() not in _STOPWORDS}) for kw in BOARD_KEYWORDS
]


_STEM_LEN = 5  # "licens"[:5] matches license/licensing/licensed alike


def _stem(word: str) -> str:
    return word[:_STEM_LEN] if len(word) > _STEM_LEN else word


def classify_board_type(text: str) -> str | None:
    text_stems = {_stem(w) for w in re.findall(r"[a-z]+", text.lower())}
    best_kw, best_score = None, 0
    for kw, words in _KEYWORD_WORD_SETS:
        score = len({_stem(w) for w in words} & text_stems)
        if score > best_score:
            best_kw, best_score = kw, score
    return best_kw


HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
MAX_HEADING_HOPS = 6


def find_nearby_heading_text(anchor) -> str:
    """Walks up from the anchor looking for the nearest preceding heading —
    covers the common accordion/toggle pattern where a board name like
    "2023 Planning Commission Meeting Minutes" is a sibling heading above a
    shared content block, not part of the link itself (see Fruitport, MI:
    the same layout that caused the date-extraction regression this filter
    is deliberately more tolerant about than date_extract's row search,
    since misattributing a board *name* over a wider scope is low-risk —
    board names rarely change within a page section, unlike dates)."""
    node = anchor
    for _ in range(MAX_HEADING_HOPS):
        parent = node.find_parent()
        if parent is None:
            break
        for sibling in parent.find_previous_siblings():
            classes = sibling.get("class") or []
            if sibling.name in HEADING_TAGS or any("title" in c for c in classes):
                return sibling.get_text(strip=True)
            heading = sibling.find(list(HEADING_TAGS))
            if heading:
                return heading.get_text(strip=True)
        node = parent
    return ""


def is_civic_host(hostname: str) -> bool:
    """True only for an actual subdomain of a civic platform (e.g.
    "cityname.civicplus.com"), never the bare vendor domain itself — a link
    to e.g. "civicplus.com" is always just a "Powered by CivicPlus"
    attribution badge pointing at the vendor's own marketing site, not real
    agenda content."""
    return any(hostname != h and hostname.endswith("." + h) for h in CIVIC_HOSTS)


def is_vendor_marketing_domain(hostname: str) -> bool:
    """True for the bare vendor domain itself (civicplus.com, granicus.com,
    etc., with or without "www."), which is always the vendor's own
    marketing site — never a municipality's actual agenda content — and
    must be excluded outright rather than just de-scored, since vendor
    footer badges sometimes link straight to a specific marketing subpage
    (e.g. civicplus.com/agenda-meeting-management/) whose URL and text both
    contain "agenda", which would otherwise pass the keyword-match check."""
    bare = hostname[4:] if hostname.startswith("www.") else hostname
    return bare in CIVIC_HOSTS


def fetch(url: str, retries: int = 1) -> requests.Response | None:
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if resp.status_code != 200:
                return None  # not transient (403/404/etc.) — retrying won't help
            return resp
        except requests.RequestException:
            if attempt < retries:
                time.sleep(1.5)
                continue
            return None
    return None


def find_candidate_pages(html: str, base_url: str) -> list[tuple[str, str]]:
    """Returns (url, link_text) pairs, best-scored first. link_text is kept
    and passed downstream as page context: a link literally reading
    "Planning Board" makes everything found on that page presumptively
    relevant even if individual item titles don't repeat the board name."""
    soup = BeautifulSoup(html, "html.parser")
    scored: dict[str, tuple[int, str]] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(base_url, href)
        try:
            hostname = urlparse(url).hostname or ""
        except ValueError:
            continue
        if is_vendor_marketing_domain(hostname):
            continue

        text = a.get_text(strip=True)
        score = 0
        if matches_board_filter(text):
            score = 1000  # a link that names the exact board type we want
        elif is_civic_host(hostname):
            score = 100
        elif AGENDA_KEYWORDS.search(text) or AGENDA_KEYWORDS.search(href):
            score = 10

        if score and score > scored.get(url, (0, ""))[0]:
            scored[url] = (score, text)

    ranked = sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(url, text) for url, (_, text) in ranked]


def extract_packets(html: str, page_url: str, page_context: str = "") -> tuple[list[dict], list[dict]]:
    """Returns (direct_packets, candidate_subpages). Only direct packets
    whose own text/URL, OR the page_context they were found under, name a
    qualifying board type are kept — see matches_board_filter."""
    soup = BeautifulSoup(html, "html.parser")
    page_host = urlparse(page_url).hostname or ""

    direct: list[dict] = []
    candidates: list[dict] = []
    seen_direct: set[str] = set()
    seen_candidate: set[str] = set()

    for a in soup.find_all("a", href=True):
        raw_href = a["href"].strip()
        if not raw_href or raw_href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(page_url, raw_href)
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if not parsed.hostname:
            continue
        if is_vendor_marketing_domain(parsed.hostname):
            continue

        text = a.get_text(strip=True)
        if NOISE_RE.search(text):
            continue
        is_pdf = bool(PDF_RE.search(parsed.path))
        is_text_match = bool(TEXT_MATCH_RE.search(text) or TEXT_MATCH_RE.search(url))

        if is_pdf or is_text_match:
            if url not in seen_direct:
                heading_text = find_nearby_heading_text(a)
                combined_context = " ".join(filter(None, [text, page_context, heading_text]))
                if not matches_board_filter(combined_context):
                    continue
                seen_direct.add(url)
                meeting_date = extract_meeting_date(a)
                direct.append(
                    {
                        "url": url,
                        "title": text or url,
                        "match_reason": "pdf" if is_pdf else "text",
                        "meeting_date": meeting_date.isoformat() if meeting_date else None,
                        "is_upcoming": is_upcoming(meeting_date),
                        "board_type": classify_board_type(combined_context),
                    }
                )
            continue

        if len(candidates) >= 6 or parsed.hostname != page_host or url in seen_candidate:
            continue

        looks_like_platform_detail = is_civic_host(parsed.hostname) and PLATFORM_DETAIL_RE.search(url)
        looks_like_meeting_row = DATE_RE.search(text) or MEETING_ROW_RE.search(text)

        if looks_like_platform_detail or looks_like_meeting_row:
            seen_candidate.add(url)
            candidates.append({"url": url, "title": text or url})

    return direct, candidates


def save_meeting_and_packets(
    cur, municipality_id: int, source_url: str, direct: list[dict], subpage_results: list[tuple[dict, list[dict]]]
) -> int:
    cur.execute(
        """
        INSERT INTO meetings (municipality_id, source_url)
        VALUES (?, ?)
        ON CONFLICT(municipality_id, source_url) DO UPDATE SET discovered_at=discovered_at
        """,
        (municipality_id, source_url),
    )
    cur.execute(
        "SELECT id FROM meetings WHERE municipality_id = ? AND source_url = ?",
        (municipality_id, source_url),
    )
    meeting_id = cur.fetchone()[0]

    packet_count = 0
    for p in direct:
        if p["is_upcoming"] == 0:  # confirmed past — skip; unknown (None) still saved
            continue
        cur.execute(
            """
            INSERT INTO packets (meeting_id, municipality_id, url, title, match_reason, found_via, meeting_date, is_upcoming, board_type)
            VALUES (?, ?, ?, ?, ?, 'direct', ?, ?, ?)
            ON CONFLICT(municipality_id, url) DO UPDATE SET
                meeting_date=excluded.meeting_date, is_upcoming=excluded.is_upcoming, board_type=excluded.board_type
            """,
            (meeting_id, municipality_id, p["url"], p["title"], p["match_reason"], p["meeting_date"], p["is_upcoming"], p["board_type"]),
        )
        packet_count += 1

    for subpage, sub_direct in subpage_results:
        for p in sub_direct:
            if p["is_upcoming"] == 0:
                continue
            cur.execute(
                """
                INSERT INTO packets (meeting_id, municipality_id, url, title, match_reason, found_via, meeting_date, is_upcoming, board_type)
                VALUES (?, ?, ?, ?, ?, 'subpage', ?, ?, ?)
                ON CONFLICT(municipality_id, url) DO UPDATE SET
                    meeting_date=excluded.meeting_date, is_upcoming=excluded.is_upcoming, board_type=excluded.board_type
                """,
                (meeting_id, municipality_id, p["url"], p["title"], p["match_reason"], p["meeting_date"], p["is_upcoming"], p["board_type"]),
            )
            packet_count += 1

    return packet_count


def crawl_municipality(cur, muni_id: int, official_url: str) -> str:
    home_resp = fetch(official_url)
    if home_resp is None:
        return "error"

    candidates = find_candidate_pages(home_resp.text, home_resp.url)
    if not candidates:
        return "no_agenda_found"

    total_packets = 0
    winning_platform = None

    for page_url, page_context in candidates[:CANDIDATE_PAGES_TO_VISIT]:
        page_resp = fetch(page_url)
        if page_resp is None:
            continue

        direct, subpage_candidates = extract_packets(page_resp.text, page_resp.url, page_context)

        subpage_results = []
        for sub in subpage_candidates[:FOLLOW_LIMIT]:
            time.sleep(0.3)
            sub_resp = fetch(sub["url"])
            if sub_resp is None:
                continue
            sub_context = page_context or sub["title"]
            sub_direct, _ = extract_packets(sub_resp.text, sub_resp.url, sub_context)
            subpage_results.append((sub, sub_direct))

        count = save_meeting_and_packets(cur, muni_id, page_resp.url, direct, subpage_results)
        total_packets += count

        hostname = urlparse(page_resp.url).hostname or ""
        if is_civic_host(hostname) and winning_platform is None:
            winning_platform = next(h for h in CIVIC_HOSTS if hostname.endswith(h))

        cur.execute(
            "UPDATE municipalities SET agenda_url = ?, platform = ? WHERE id = ? AND agenda_url IS NULL",
            (page_resp.url, winning_platform, muni_id),
        )

    return "ok" if total_packets > 0 else "no_packets_found"


def process_one(muni_id: int, name: str, state: str, official_url: str) -> str:
    """Runs one municipality end-to-end on its own SQLite connection —
    required since connections can't be shared across threads. WAL mode +
    busy_timeout (see db.get_connection) absorb the brief write conflicts
    from multiple workers finishing at once."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        status = crawl_municipality(cur, muni_id, official_url)
    except Exception as e:
        print(f"  {name}, {state}: unexpected error ({e})")
        status = "error"
    cur.execute("UPDATE municipalities SET site_status = ? WHERE id = ?", (status, muni_id))
    conn.commit()
    conn.close()
    return status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", help="Only crawl municipalities in this state")
    parser.add_argument("--limit", type=int, help="Max number of municipalities to crawl this run")
    parser.add_argument("--recheck", action="store_true", help="Re-crawl even if already checked")
    parser.add_argument("--workers", type=int, default=20, help="Concurrent worker threads (default 20)")
    args = parser.parse_args()

    conn = get_connection()
    cur = conn.cursor()

    query = "SELECT id, name, state, official_url FROM municipalities"
    conditions = []
    params: list = []
    if not args.recheck:
        conditions.append("site_status = 'unchecked'")
    if args.state:
        conditions.append("state = ?")
        params.append(args.state)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id"
    if args.limit:
        query += f" LIMIT {args.limit}"

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    print(f"crawling {len(rows)} municipalities with {args.workers} workers")

    status_counts: dict[str, int] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_one, muni_id, name, state, official_url): (name, state)
            for muni_id, name, state, official_url in rows
        }
        for future in as_completed(futures):
            name, state = futures[future]
            status = future.result()
            done += 1
            status_counts[status] = status_counts.get(status, 0) + 1
            print(f"  [{done}/{len(rows)}] {name}, {state}: {status}")

    print("done —", status_counts)


if __name__ == "__main__":
    main()
