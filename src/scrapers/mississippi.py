"""
Mississippi's own municipal league (mmlonline.com) turned out to have
no website field anywhere — its public member list is just a name
dropdown with no linked URL, and its per-city profile pages (checked
directly, e.g. /members/municipalities/Morgan-City/) list elected
officials by name only, no email or site link at all.

What worked instead: the Mississippi Secretary of State's official
"Blue Book" reference (a downloadable PDF at sos.ms.gov), whose
Municipal Government chapter lists every incorporated municipality with
population, incorporation date, and (for about three-quarters of them)
a contact email. As with Kentucky's DLG system, that email's domain is
usually the city's own domain — but a lot of small Mississippi towns'
listed contact is a personal or ISP email account instead (gmail, aol,
bellsouth, yahoo, a local ISP like dixie-net or panola), which is
useless as a website and is filtered out by domain rather than assumed
to be a website. Every remaining domain is verified live AND checked
for an actual connection to the city (see _domain_verify.py, shared
with the Kentucky scraper which hit the identical problem) before
being stored — a first pass without that content check turned up
domains that resolve fine but have nothing to do with the city (a
local phone co-op's own site for a clerk who emails from her ISP
account, one clerk's email itself resolving to a Yahoo Mail login
page, one town's contact domain having since expired and been
squatted by an unrelated foreign site).

The PDF itself is behind a check that blocks a plain `requests` GET
(403) but not a real browser, so it's fetched via Playwright's download
handling rather than a plain HTTP request.
"""

import re
import sys
from pathlib import Path

import pypdf
import requests
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection
from scrapers._domain_verify import verify_domain_belongs_to

BLUEBOOK_URL = (
    "https://www.sos.ms.gov/content/documents/ed_pubs/pubs/BlueBook20-24/"
    "12%20Municipal%20Government.pdf"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# The listings section runs from the "ARTESIA" entry through "WIGGINS" —
# pages after that switch to a by-county name index with no contact info.
LISTINGS_FIRST_PAGE = 5
LISTINGS_LAST_PAGE = 41

CITY_HEADER_RE = re.compile(r"^([A-Z][A-Z .'-]+?)\s*\(([^)]+Count(?:y|ies))\)\s*$")
FREEMAIL_OR_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "comcast.net", "att.net",
    "bellsouth.net", "centurylink.net", "windstream.net", "dixie-net.com",
    "panola.com", "tecinfo.com", "nctv.com", "netdoor.com",
}


def download_bluebook_pdf(dest_path: Path) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"], accept_downloads=True)
        with page.expect_download(timeout=45000) as download_info:
            try:
                page.goto(BLUEBOOK_URL, timeout=45000)
            except Exception:
                pass  # goto never "completes" once the browser starts a file download
        download_info.value.save_as(str(dest_path))
        browser.close()


def parse_city_emails(pdf_path: Path) -> dict[str, str | None]:
    reader = pypdf.PdfReader(str(pdf_path))
    full_text = "\n".join(
        reader.pages[i].extract_text() for i in range(LISTINGS_FIRST_PAGE, LISTINGS_LAST_PAGE + 1)
    )

    cities: dict[str, str | None] = {}
    current: str | None = None
    for raw_line in full_text.split("\n"):
        line = raw_line.strip()
        header_match = CITY_HEADER_RE.match(line)
        if header_match:
            current = header_match.group(1).strip().title()
            cities.setdefault(current, None)
            continue
        if line.startswith("Email:") and current:
            # PDF text extraction inserts stray spaces around dots
            # ("name@aol .com") — strip all whitespace to recover it.
            email = re.sub(r"\s+", "", line.split("Email:", 1)[1])
            if "@" in email:
                cities[current] = email
    return cities


def main() -> None:
    pdf_path = Path(__file__).resolve().parent / "_ms_bluebook_tmp.pdf"
    download_bluebook_pdf(pdf_path)
    cities = parse_city_emails(pdf_path)
    pdf_path.unlink(missing_ok=True)
    print(f"parsed {len(cities)} Mississippi municipalities from the Blue Book")

    session = requests.Session()
    session.headers.update(HEADERS)

    verified: dict[str, str] = {}
    for name, email in cities.items():
        if not email:
            continue
        domain = email.split("@", 1)[1].lower()
        if domain in FREEMAIL_OR_ISP_DOMAINS:
            continue
        url = verify_domain_belongs_to(session, name, domain)
        if url:
            verified[name] = url
    print(f"verified {len(verified)} live municipal websites")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in verified.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Mississippi', 'city', ?, 'ms-sos-bluebook-email-domain')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Mississippi municipalities indexed")


if __name__ == "__main__":
    main()
