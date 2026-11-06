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
  5. Update the municipality's site_status ('ok' / 'no_packets_found' /
     'no_agenda_found' / 'blocked' / 'error'), agenda_url, and platform
     (host of the winning candidate, if it's a known civic platform).
     'blocked' means the site answered but refused automated access, so it
     is alive; 'error' means nothing usable came back at all. A town set
     to 'excluded' is never crawled until it's marked for re-check.

Concurrency: municipalities are processed by a thread pool (--workers,
default 20) — this is I/O-bound work (waiting on network responses across
thousands of *distinct* third-party hosts), so threads are effective
despite the GIL, and hitting many different domains concurrently isn't the
same politeness concern as hammering one server repeatedly. The exception
is CivicPlus, which hosts hundreds of these towns on a few networks: those
requests are spaced out across all workers (see wait_for_civicplus_slot).
Each worker
opens its own SQLite connection (required — connections aren't safely
shared across threads); WAL mode + a busy_timeout (see db.get_connection)
absorb the brief write conflicts from workers finishing at nearly the same
moment instead of raising "database is locked". Generous per-request
timeout, one retry on transient failure, no retries beyond that — across
thousands of arbitrary third-party sites, timeouts/errors are expected and
just get recorded as site_status='error' rather than crashing the run.
"""

import argparse
import ipaddress
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

# See webapp.py's matching comment: redirect before anything riskier than
# the stdlib gets imported, so a crash in a later import (requests, bs4,
# playwright via pdf_review) lands in the log file instead of vanishing
# silently under pythonw.exe (sys.stdout/stderr start out as None there).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import redirect_console_if_headless
redirect_console_if_headless("crawl_engine.log")

import requests
from bs4 import BeautifulSoup

from date_extract import extract_meeting_date, find_nearby_row, is_upcoming
from db import get_connection
from pdf_review import extract_links_from_pdf, fetch_pdf_bytes

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
# Retried once on a refusal. Some firewalls reject a "Chrome" whose connection
# doesn't look like one but allow a client that says what it is. Measured on
# live sites: this let 9% of refused sites through, while using it for every
# request would lose a few (.colorado.gov) that only accept the browser string.
IDENTIFIED_HEADERS = {
    "User-Agent": "muni-packet-crawler/0.1 (public meeting agenda indexer)",
    "Accept": "text/html,*/*;q=0.8",
}
BLOCKED_STATUSES = {401, 403, 406, 429}
# Requests that look spread across hundreds of different towns can all land on
# this one provider, and when it was hit too hard it stopped accepting
# connections from this machine. Everything bound for these networks (ARIN
# lists them under CivicPlus, LLC) shares one limit, however many workers run.
CIVICPLUS_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("207.38.72.0/21", "208.90.188.0/22", "192.196.0.0/24")
)
CIVICPLUS_SECONDS_BETWEEN_REQUESTS = 1.0
_civicplus_lock = threading.Lock()
_civicplus_next_slot = 0.0
REQUEST_TIMEOUT = 15
CANDIDATE_PAGES_TO_VISIT = 4
# Board pages are often just a member list with the documents kept on a
# shared "Agendas & Minutes" page, so they can't be allowed to fill every slot.
MAX_BOARD_PAGES = 2
FOLLOW_LIMIT = 3
MAX_EMBEDDED_PDF_LINKS = 3  # per source PDF — bounds cost, these are rare

CIVIC_HOSTS = [
    "civicplus.com",
    "municodemeetings.com",
    "granicus.com",
    "iqm2.com",
    "legistar.com",
    "escribemeetings.com",
    "novusagenda.com",
    "primegov.com",
    "civicclerk.com",
    "civicweb.net",
    "towncloud.io",
    "documents-on-demand.com",
]
# CivicPlus is a whole-site CMS, not just a meetings host: only its pages about
# meetings count, unlike the platforms above that hold nothing else.
CMS_HOSTS = ("civicplus.com",)
# Exact hosts that never hold agendas. library.municode.com is the code of
# ordinances, linked from nearly every city homepage; connect.civicplus.com is
# the "Powered by CivicPlus" badge.
NON_AGENDA_HOSTS = {"municode.com", "library.municode.com", "connect.civicplus.com", "calendar.google.com"}
# Video and social links are often labelled "Board Meetings" but hold no documents.
NON_AGENDA_DOMAINS = (
    "youtube.com", "youtu.be", "vimeo.com", "zoom.us", "facebook.com", "twitter.com", "x.com", "instagram.com",
)

AGENDA_KEYWORDS = re.compile(
    r"\b(agenda|minutes|meeting|calendar|board|commission|planning|zoning|council)\b",
    re.I,
)
PDF_RE = re.compile(r"\.pdf($|[?#])", re.I)
MEETING_DOC_RE = re.compile(r"\b(agenda|minutes|meeting|packet)\b", re.I)
TEXT_MATCH_RE = re.compile(r"\b(packet|agenda)\b", re.I)
# Excludes email/RSS signup links whose text happens to contain "agenda"
# (e.g. "Receive Agenda Alerts") — these aren't packets, just noise that
# passed the keyword check.
NOISE_RE = re.compile(
    r"\b(alerts?|subscribe|subscription|notify|notifications?|sign up|newsletters?|rss feed"
    r"|send to|add to calendar|outlook|google calendar|icalendar|ical)\b",
    re.I,
)
PLATFORM_DETAIL_RE = re.compile(
    r"(MeetingDetail|ViewPublisher|AgendaViewer|MeetingView|Meeting\.aspx|Detail\.aspx)", re.I
)
MEETING_ROW_RE = re.compile(r"\b(minutes|meeting details|view agenda)\b", re.I)
# The same AgendaCenter meeting linked again as an HTML view or a listing
# anchor. The plain PDF and its ?packet=true version are the ones worth keeping.
AGENDACENTER_DUPLICATE_RE = re.compile(r"/AgendaCenter/(?:ViewFile/Agenda/_\d+-\d+\?html=true|[^/]+/#_\d)", re.I)
# A link naming the shared listing page, which small towns use for every board.
AGENDA_PAGE_RE = re.compile(r"\b(agendas?|agenda\s*center|public\s+meetings?)\b", re.I)
# "Zoning" also names plenty of things that aren't the board's meeting records.
ZONING_DOC_RE = re.compile(r"\b(ordinances?|maps?|codes?|permits?|applications?|forms?|fees?|regulations?)\b", re.I)
MAX_ROW_TEXT = 200  # a longer "row" is a layout container, not one item
DATE_RE = re.compile(r"\b[A-Z][a-z]{2}\s\d{1,2},\s\d{4}\b")

# The boards this project tracks: Planning Commission, Planning Board, Board
# of Adjustment, anything Zoning, and license/liquor boards. This matches the concept rather
# than exact phrases, since real sites word things too many ways ("Zoning
# Hearing Board", "Planning & Zoning", "Board of License Commissioners").
# "Planning" or "license" alone isn't specific enough (a Planning Department,
# a "Dog License" form) so those need a board/commission word alongside them;
# "zoning" is specific enough on its own, which also covers the many sites
# that label the body "Planning & Zoning" with no such word at all.
PLANNING_RE = re.compile(r"\bplanning\b", re.I)
ZONING_RE = re.compile(r"\bzoning\b", re.I)
ADJUSTMENT_RE = re.compile(r"\bboard\s+of\s+(?:zoning\s+)?adjustments?\b", re.I)
LIQUOR_RE = re.compile(r"\b(liquor|alcohol\w*)\b", re.I)
LICENSE_RE = re.compile(r"\blicens\w*\b", re.I)
NEEDS_BOARD_WORD_RE = re.compile(r"\b(planning|licens\w*|liquor|alcohol\w*)\b", re.I)
BOARD_TOPIC_RE = re.compile(r"\b(planning|zoning|adjustments?|licens\w*|liquor|alcohol\w*)\b", re.I)
BOARD_TYPE_RE = re.compile(r"\b(board|commission(?:ers)?)\b", re.I)

BOARD_ABBREVIATIONS = [
    (re.compile(r"\b(?:zba|bza|zboa|zhb)\b", re.I), "Zoning Board"),
    (re.compile(r"\bp\s*&\s*z\b|\bpzc\b", re.I), "Planning and Zoning Commission"),
]
_CAMEL_CASE_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def normalize_title(text: str) -> str:
    """Spells out what the word-boundary regexes here can't otherwise see:
    a URL slug ("bryant_planning_commission", since "_" counts as a word
    character, and "Planning%20Board"), CamelCase ("PlanningBoard"), and the
    usual short forms ("ZBA Hearing", "P&Z Agenda")."""
    normalized = _CAMEL_CASE_RE.sub(" ", unquote(text).replace("_", " "))
    for pattern, full_name in BOARD_ABBREVIATIONS:
        normalized = pattern.sub(full_name, normalized)
    return normalized


def matches_board_filter(text: str) -> bool:
    return bool(
        ZONING_RE.search(text)
        or ADJUSTMENT_RE.search(text)
        or (NEEDS_BOARD_WORD_RE.search(text) and BOARD_TYPE_RE.search(text))
    )


# Small-town sites very often list every body's minutes on ONE shared page
# (Council, Planning Commission, Historical Commission, Library Board, all
# together) — so a page-level link like "Planning Commission" that got us
# onto the page does NOT mean every packet found there is actually about
# planning. Confirmed at scale: City/Village Council minutes were being
# tagged "Planning and Zoning Commission" or "Historical Commission" across
# many Michigan villages (Elberta, Gaylord, Bancroft, Essexville, Harbor
# Beach, Grand Beach) purely because that's the page they happened to sit
# on. A packet's OWN title/heading can name a body explicitly ("Council",
# "Library Board", "Parks Board", "Budget Committee") without using any of
# our topic words — when it does, that is a stronger, more specific signal
# than the page-level context, and must override it rather than being OR'd
# together with it.
OTHER_BODY_TYPE_RE = re.compile(r"\b(board|commission(?:ers)?|council|committee|authority|trustees)\b", re.I)


def names_other_body(text: str) -> bool:
    """True when text explicitly names SOME governing body but not one of
    ours — e.g. "Village Council", "Library Board" both have a type word
    with no qualifying topic word alongside it."""
    return bool(OTHER_BODY_TYPE_RE.search(text) and not BOARD_TOPIC_RE.search(text))


# The UI's keyword filter. classify_board_type picks one for a packet from its
# wording; matches_board_filter above decides whether it qualifies at all.
BOARD_KEYWORDS = ["Planning Commission", "Planning Board", "Board of Adjustment", "Zoning", "License", "Liquor"]
_PLANNING_COMMISSION_RE = re.compile(r"\bplanning\W+(?:(?:and\W+)?zoning\W+)?commission", re.I)
_PLANNING_BOARD_RE = re.compile(r"\bplanning\W+(?:(?:and\W+)?zoning\W+)?board", re.I)


def classify_board_type(text: str) -> str | None:
    if _PLANNING_COMMISSION_RE.search(text):
        return "Planning Commission"
    if _PLANNING_BOARD_RE.search(text):
        return "Planning Board"
    if ADJUSTMENT_RE.search(text):
        return "Board of Adjustment"
    if ZONING_RE.search(text):
        return "Zoning"
    if LIQUOR_RE.search(text):
        return "Liquor"
    if LICENSE_RE.search(text):
        return "License"
    if PLANNING_RE.search(text):
        return "Planning Commission" if re.search(r"\bcommission", text, re.I) else "Planning Board"
    return None


HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
MAX_HEADING_HOPS = 6
# AgendaCenter gives every row a date as its own heading ("Nov13, 2026"), which
# would otherwise hide the board name heading the whole list.
DATE_HEADING_RE = re.compile(r"^[A-Z][a-z]{2,8}\.?\s*\d{1,2},\s*\d{4}")


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
                heading = sibling
            else:
                heading = sibling.find(list(HEADING_TAGS))
            if heading:
                heading_text = heading.get_text(strip=True)
                if not DATE_HEADING_RE.match(heading_text):
                    return heading_text
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
    return (
        bare in CIVIC_HOSTS
        or bare in NON_AGENDA_HOSTS
        or any(bare == d or bare.endswith("." + d) for d in NON_AGENDA_DOMAINS)
    )


@lru_cache(maxsize=None)
def is_civicplus_hosted(hostname: str) -> bool:
    if hostname == "civicplus.com" or hostname.endswith(".civicplus.com"):
        return True
    try:
        address = ipaddress.ip_address(socket.gethostbyname(hostname))
    except (OSError, ValueError):
        return False
    return any(address in network for network in CIVICPLUS_NETWORKS)


def wait_for_civicplus_slot(url: str) -> None:
    """Blocks until it's this request's turn if url is hosted by CivicPlus;
    returns at once for any other site."""
    global _civicplus_next_slot
    try:
        hostname = urlparse(url).hostname
    except ValueError:
        return
    if not hostname or not is_civicplus_hosted(hostname):
        return
    with _civicplus_lock:
        now = time.monotonic()
        slot = max(now, _civicplus_next_slot)
        _civicplus_next_slot = slot + CIVICPLUS_SECONDS_BETWEEN_REQUESTS
    time.sleep(slot - now)


def _get(url: str, headers: dict = HEADERS) -> requests.Response:
    wait_for_civicplus_slot(url)
    return requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)


def fetch_with_status(url: str, retries: int = 1) -> tuple[requests.Response | None, int | None]:
    """Returns (response, http_status); response is None unless the status
    was 200, and http_status is None if no server answered at all. A site
    that refuses us is alive, which callers need to tell apart from one
    that isn't there, even if the retry after a refusal then times out."""
    refused_with = None
    for attempt in range(retries + 1):
        try:
            resp = _get(url)
            if resp.status_code in BLOCKED_STATUSES:
                refused_with = resp.status_code
                resp = _get(url, headers=IDENTIFIED_HEADERS)
            return (resp if resp.status_code == 200 else None), resp.status_code
        except requests.RequestException:
            if attempt < retries:
                time.sleep(1.5)
    return None, refused_with


def fetch(url: str, retries: int = 1) -> requests.Response | None:
    return fetch_with_status(url, retries)[0]


NAV_SECTION_RE = re.compile(
    r"\b(government|departments?|boards?(?:\s*(?:and|&)\s*commissions?)?|commissions?|"
    r"committees?|administration|elected\s+officials|city\s+hall|town\s+hall|municipal)\b",
    re.I,
)
MAX_NAV_PAGES_TO_FOLLOW = 3


def find_nav_section_pages(html: str, base_url: str) -> list[str]:
    """Fallback for sites where the real board/agenda link is nested one
    level deeper than the homepage — behind a generic "Government" or
    "Boards & Commissions" nav item rather than named directly (a multi-
    level government dropdown menu). Only used when find_candidate_pages
    finds nothing at all on the homepage itself, to avoid extra requests
    on sites that already work fine without it."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    pages: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        text = a.get_text(strip=True)
        if not NAV_SECTION_RE.search(text):
            continue
        url = urljoin(base_url, href)
        try:
            hostname = urlparse(url).hostname or ""
        except ValueError:
            continue
        if not hostname or is_vendor_marketing_domain(hostname) or url in seen:
            continue
        seen.add(url)
        pages.append(url)
        if len(pages) >= MAX_NAV_PAGES_TO_FOLLOW:
            break
    return pages


BOARD_LINK_SCORE = 1000


def find_candidate_pages(html: str, base_url: str) -> list[tuple[str, str]]:
    """Returns (url, link_text) pairs, best-scored first. link_text is kept
    and passed downstream as page context: a link literally reading
    "Planning Board" makes everything found on that page presumptively
    relevant even if individual item titles don't repeat the board name.
    At most MAX_BOARD_PAGES board links lead the list, so that the shared
    listing page still gets a visit.

    The civic-host score only applies to a link leaving the site: for a town
    hosted on one of these platforms, every internal link is on that host,
    and "Job Opportunities" would otherwise outrank "Agendas & Minutes"."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).hostname
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
        board_name = normalize_title(text)
        has_keyword = bool(AGENDA_KEYWORDS.search(text) or AGENDA_KEYWORDS.search(href))
        score = 0
        if matches_board_filter(board_name) and not ZONING_DOC_RE.search(board_name):
            score = BOARD_LINK_SCORE  # a link that names a board we want
        elif AGENDA_PAGE_RE.search(text) or AGENDA_PAGE_RE.search(normalize_title(href)):
            score = 500
        elif is_civic_host(hostname) and hostname != base_host and (has_keyword or not hostname.endswith(CMS_HOSTS)):
            score = 100
        elif has_keyword:
            score = 10

        if score and score > scored.get(url, (0, ""))[0]:
            scored[url] = (score, text)

    ranked = sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True)
    board_pages = [(url, text) for url, (score, text) in ranked if score == BOARD_LINK_SCORE]
    other_pages = [(url, text) for url, (score, text) in ranked if score != BOARD_LINK_SCORE]
    return board_pages[:MAX_BOARD_PAGES] + other_pages + board_pages[MAX_BOARD_PAGES:]


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
        if is_vendor_marketing_domain(parsed.hostname) or AGENDACENTER_DUPLICATE_RE.search(url):
            continue

        text = a.get_text(strip=True)
        if NOISE_RE.search(text):
            continue
        is_pdf = bool(PDF_RE.search(parsed.path))
        is_text_match = bool(TEXT_MATCH_RE.search(text) or TEXT_MATCH_RE.search(url))

        if is_pdf or is_text_match:
            if url not in seen_direct:
                # own_title: the item's OWN specific identity — its link
                # text, URL and table/list row (a listing often names the
                # body in the row's first column, not the link, which reads
                # just "Agenda"). Deliberately excludes heading_text and
                # page_context, both of which are shared by every other
                # item in the same section/page: if a Council item's own
                # title were merged with a surrounding "Planning Board"
                # heading before checking names_other_body, the merged
                # string would contain both a topic and a type word and
                # wrongly look like it qualifies on its own — exactly how
                # the first version of this fix still let Council items
                # through when they sat under a qualifying heading rather
                # than being reached via page_context. The disqualification
                # check must run on the item's own text alone.
                row = find_nearby_row(a)
                row_text = row.get_text(" ", strip=True) if row else ""
                if len(row_text) > MAX_ROW_TEXT:
                    row_text = ""
                own_title = normalize_title(" ".join(filter(None, [text, url, row_text])))
                heading_text = find_nearby_heading_text(a)
                external_context = normalize_title(" ".join(filter(None, [heading_text, page_context])))
                meeting_date = extract_meeting_date(a)

                if names_other_body(own_title):
                    # The item's own title names a specific body that
                    # isn't ours (Council, Library Board, ...) — overrides
                    # any qualifying heading/page context around it.
                    continue
                elif matches_board_filter(own_title):
                    classify_context = own_title
                elif matches_board_filter(external_context) and (
                    MEETING_DOC_RE.search(own_title) or meeting_date is not None
                ):
                    # own_title is generic (bare "Agenda"/"PDF", no body
                    # named at all) — fall back to the surrounding heading/
                    # page context, but only when this item also looks
                    # like an actual meeting document (has a meeting-ish
                    # word or a real date), not unrelated archive ephemera.
                    # Confirmed live: Historical Commission pages often
                    # double as a town history archive (press clippings,
                    # "directions to X"), which otherwise rode along on
                    # the surrounding qualifying context with nothing to
                    # flag them as non-meeting content.
                    classify_context = external_context
                else:
                    continue

                seen_direct.add(url)
                direct.append(
                    {
                        "url": url,
                        "title": text or url,
                        "match_reason": "pdf" if is_pdf else "text",
                        "meeting_date": meeting_date.isoformat() if meeting_date else None,
                        "is_upcoming": is_upcoming(meeting_date),
                        "board_type": classify_board_type(classify_context),
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


def follow_pdf_embedded_links(packets: list[dict]) -> list[dict]:
    """For each already-confirmed PDF packet, checks inside that PDF for
    links to OTHER packet PDFs (see pdf_review.extract_links_from_pdf) —
    some municipalities host the actual packet at a separate URL that's
    only ever linked from within the agenda PDF itself, never from any
    HTML page a normal crawl would visit. Best-effort and capped: a fetch
    failure or unparseable PDF just yields nothing extra from that one
    packet, not a crawl failure. Inherits the parent packet's date/board
    type — a linked packet found this way is presumptively part of the
    same meeting/board. That inheritance is why only upcoming packets get
    opened: anything linked from a past or undated one would inherit that
    date and be dropped, and a listing page can hold years of old PDFs."""
    extra: list[dict] = []
    seen_urls = {p["url"] for p in packets}
    for p in packets:
        if p["match_reason"] != "pdf" or p["is_upcoming"] != 1:
            continue
        wait_for_civicplus_slot(p["url"])
        pdf_bytes = fetch_pdf_bytes(p["url"])
        if pdf_bytes is None:
            continue
        linked_urls = extract_links_from_pdf(pdf_bytes, p["url"])
        for link_url in linked_urls[:MAX_EMBEDDED_PDF_LINKS]:
            if link_url in seen_urls:
                continue
            seen_urls.add(link_url)
            extra.append(
                {
                    "url": link_url,
                    "title": link_url.rsplit("/", 1)[-1] or link_url,
                    "match_reason": "pdf",
                    "meeting_date": p["meeting_date"],
                    "is_upcoming": p["is_upcoming"],
                    "board_type": p["board_type"],
                    "found_via_pdf_link": True,
                }
            )
    return extra


def save_packet(cur, meeting_id: int, municipality_id: int, p: dict, found_via: str) -> int:
    """Returns 1 if this is a real upcoming packet, else 0. Only a confirmed
    *upcoming* date is worth keeping: past (0) and undeterminable (None) are
    both skipped, since an undated packet is indistinguishable from an
    old/archived one and gets re-evaluated on the next crawl of this same
    page anyway. A link already opened in the app still counts as found
    (the town does have an upcoming packet) but isn't added back."""
    if p["is_upcoming"] != 1:
        return 0
    cur.execute("SELECT 1 FROM opened_links WHERE url = ?", (p["url"],))
    if cur.fetchone():
        return 1
    if p.get("found_via_pdf_link"):
        found_via = "pdf_link"
    cur.execute(
        """
        INSERT INTO packets (meeting_id, municipality_id, url, title, match_reason, found_via, meeting_date, is_upcoming, board_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(municipality_id, url) DO UPDATE SET
            meeting_date=excluded.meeting_date, is_upcoming=excluded.is_upcoming, board_type=excluded.board_type
        """,
        (meeting_id, municipality_id, p["url"], p["title"], p["match_reason"], found_via, p["meeting_date"], p["is_upcoming"], p["board_type"]),
    )
    return 1


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
        packet_count += save_packet(cur, meeting_id, municipality_id, p, "direct")
    for _subpage, sub_direct in subpage_results:
        for p in sub_direct:
            packet_count += save_packet(cur, meeting_id, municipality_id, p, "subpage")
    return packet_count


def prune_stale_packets(conn) -> dict:
    """Deletes packets no longer worth keeping. Two categories, both a
    deliberate policy call (not a bug fix): an undated packet is
    indistinguishable from an old/archived one, so it's treated the same
    way — save_meeting_and_packets already refuses to save new ones like
    this, but this retroactively clears out any that were saved before
    that rule existed. A dated packet naturally goes from upcoming to
    past as time moves forward; nothing else re-checks that after the
    initial save, so is_upcoming is recomputed against *today* for every
    dated packet first (a row's stored value is only as fresh as its last
    crawl), then anything now-past is deleted too. Links already opened in
    the app go last, with their URL kept in opened_links so a later crawl
    doesn't add them straight back."""
    cur = conn.cursor()
    today = date.today()
    rows = cur.execute("SELECT id, meeting_date FROM packets WHERE meeting_date IS NOT NULL").fetchall()
    for packet_id, meeting_date_str in rows:
        try:
            meeting_date = date.fromisoformat(meeting_date_str)
        except (ValueError, TypeError):
            continue
        cur.execute("UPDATE packets SET is_upcoming = ? WHERE id = ?", (is_upcoming(meeting_date, today), packet_id))

    cur.execute("DELETE FROM packets WHERE meeting_date IS NULL")
    undated_deleted = cur.rowcount
    cur.execute("DELETE FROM packets WHERE is_upcoming = 0")
    past_deleted = cur.rowcount
    cur.execute("INSERT OR IGNORE INTO opened_links (url) SELECT url FROM packets WHERE reviewed = 1")
    cur.execute("DELETE FROM packets WHERE reviewed = 1")
    opened_deleted = cur.rowcount
    conn.commit()
    return {"undated_deleted": undated_deleted, "past_deleted": past_deleted, "opened_deleted": opened_deleted}


def crawl_municipality(official_url: str) -> dict:
    """Pure network/parsing work — no database access. Deliberately kept
    that way so this can run inside a thread pool: SQLite only allows one
    writer at a time even in WAL mode, so having many worker threads each
    write directly caused persistent 'database is locked' errors under
    real concurrency once workers were added. All DB writes now happen in
    a single caller thread (see process_one/main) after this returns.

    status is only decided here for the terminal cases that don't depend
    on what actually gets saved ('error', 'blocked', 'no_agenda_found'); the
    'ok' vs 'no_packets_found' call depends on how many packets survive the
    past-meeting filter in save_meeting_and_packets, which only runs in the
    writer thread — so that distinction is left as 'has_candidates' here
    and finalized by the caller after the real save."""
    result: dict = {"status": "error", "agenda_url": None, "platform": None, "meetings": []}

    home_resp, http_status = fetch_with_status(official_url)
    if home_resp is None:
        if http_status in BLOCKED_STATUSES:
            result["status"] = "blocked"
        return result

    candidates = find_candidate_pages(home_resp.text, home_resp.url)
    if not candidates:
        # Homepage itself names nothing directly — some sites bury the
        # real link one level deeper behind a generic "Government" or
        # "Boards & Commissions" nav item (a multi-level government
        # dropdown menu) instead of linking it from the homepage at all.
        for nav_url in find_nav_section_pages(home_resp.text, home_resp.url):
            nav_resp = fetch(nav_url)
            if nav_resp is None:
                continue
            candidates = find_candidate_pages(nav_resp.text, nav_resp.url)
            if candidates:
                break
    if not candidates:
        result["status"] = "no_agenda_found"
        return result

    for page_url, page_context in candidates[:CANDIDATE_PAGES_TO_VISIT]:
        page_resp = fetch(page_url)
        if page_resp is None:
            continue

        direct, subpage_candidates = extract_packets(page_resp.text, page_resp.url, page_context)
        direct.extend(follow_pdf_embedded_links(direct))

        subpage_results = []
        for sub in subpage_candidates[:FOLLOW_LIMIT]:
            time.sleep(0.3)
            sub_resp = fetch(sub["url"])
            if sub_resp is None:
                continue
            sub_context = page_context or sub["title"]
            sub_direct, _ = extract_packets(sub_resp.text, sub_resp.url, sub_context)
            sub_direct.extend(follow_pdf_embedded_links(sub_direct))
            subpage_results.append((sub, sub_direct))

        result["meetings"].append(
            {"source_url": page_resp.url, "direct": direct, "subpage_results": subpage_results}
        )

        if result["agenda_url"] is None:
            result["agenda_url"] = page_resp.url
            hostname = urlparse(page_resp.url).hostname or ""
            if is_civic_host(hostname):
                result["platform"] = next(h for h in CIVIC_HOSTS if hostname.endswith(h))

    result["status"] = "has_candidates"
    return result


def process_one(muni_id: int, name: str, state: str, official_url: str) -> tuple[int, dict]:
    """Worker-side function: network/parsing only, no database access —
    see crawl_municipality's docstring for why."""
    try:
        result = crawl_municipality(official_url)
    except Exception as e:
        print(f"  {name}, {state}: unexpected error ({e})")
        result = {"status": "error", "agenda_url": None, "platform": None, "meetings": []}
    return muni_id, result


def write_result(cur, muni_id: int, result: dict) -> str:
    """Writer-side: the only place that touches the database for crawl
    results, called from a single thread (see main) so there is never more
    than one writer, regardless of --workers."""
    total_saved = 0
    for m in result["meetings"]:
        total_saved += save_meeting_and_packets(
            cur, muni_id, m["source_url"], m["direct"], m["subpage_results"]
        )

    if result["status"] == "has_candidates":
        status = "ok" if total_saved > 0 else "no_packets_found"
    else:
        status = result["status"]

    cur.execute(
        "UPDATE municipalities SET site_status = ?, agenda_url = ?, platform = ? WHERE id = ?",
        (status, result["agenda_url"], result["platform"], muni_id),
    )
    return status


def run_once(conn, args) -> None:
    cur = conn.cursor()

    query = "SELECT id, name, state, official_url FROM municipalities"
    conditions = ["site_status != 'excluded'"]  # switched off; even --recheck leaves these alone
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
            muni_id, result = future.result()
            status = write_result(cur, muni_id, result)  # main thread only — sole writer
            conn.commit()
            done += 1
            status_counts[status] = status_counts.get(status, 0) + 1
            print(f"  [{done}/{len(rows)}] {name}, {state}: {status}")

    prune_result = prune_stale_packets(conn)

    print("done —", status_counts)
    print(
        f"pruned {prune_result['undated_deleted']} undated, "
        f"{prune_result['past_deleted']} now-past and "
        f"{prune_result['opened_deleted']} opened packet(s)"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", help="Only crawl municipalities in this state")
    parser.add_argument("--limit", type=int, help="Max number of municipalities to crawl this run")
    parser.add_argument("--recheck", action="store_true", help="Re-crawl even if already checked")
    parser.add_argument("--workers", type=int, default=20, help="Concurrent worker threads (default 20)")
    parser.add_argument(
        "--forever",
        action="store_true",
        help="Keep crawling in a loop until manually stopped (Ctrl+C). Each pass "
        "after the first behaves like --recheck, since 'unchecked' rows would "
        "otherwise run out after the first pass.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds to sleep between passes when --forever is set (default 60)",
    )
    args = parser.parse_args()

    conn = get_connection()
    try:
        if not args.forever:
            run_once(conn, args)
            return

        pass_num = 0
        while True:
            pass_num += 1
            print(f"=== pass {pass_num} ===")
            # after the first pass, unchecked rows are exhausted — force
            # re-crawling so subsequent passes keep doing real work.
            if pass_num > 1:
                args.recheck = True
            try:
                run_once(conn, args)
            except Exception as e:
                print(f"pass {pass_num} failed: {e}")
            print(f"sleeping {args.interval}s before next pass (Ctrl+C to stop)...")
            try:
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("stopped.")
                break
    except KeyboardInterrupt:
        print("stopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
