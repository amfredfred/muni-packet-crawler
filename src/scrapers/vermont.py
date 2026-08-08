"""
Vermont's own league (vlct.org) gates its member directory behind
login, and the Secretary of State's own site (sos.vermont.gov) sits
behind an F5 WAF that rejects the request outright — not something to
work around. What worked instead: the Secretary of State's Elections
Division still publishes its "Guide to Vermont's Town Clerks,
Treasurers & County Clerks" PDF from a *different* subdomain
(outside.vermont.gov), which isn't behind that same block.

Same email-domain-inference approach as Kentucky and Mississippi (see
_domain_verify.py, shared across all three) — the guide states a
clerk's contact email, not a website, so the domain is inferred and
then verified live + content-checked before being trusted. One PDF-
specific quirk: several entries wrap the address across a line break
("Email:" on one line, the address on the next) rather than keeping it
on the same line, so the parser peeks at the following line whenever
"Email:" is followed by nothing on its own line.
"""

import re
import sys
from pathlib import Path

import pypdf
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection
from scrapers._domain_verify import verify_domain_belongs_to

GUIDE_URL = (
    "https://outside.vermont.gov/dept/sos/Elections_Division/voters/"
    "guide_vermonts_town_clerks_treasurers_county.pdf"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# Town/city clerk entries run from page 6 (0-indexed) through 129;
# page 130 switches to a separate "Vermont County Clerks" section.
LISTINGS_FIRST_PAGE = 6
LISTINGS_LAST_PAGE = 129

TOWN_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z .'-]+?)\s*\(([^)]+County)\)\s*$")
FREEMAIL_OR_ISP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "comcast.net", "att.net",
    "tds.net", "gmavt.net", "vermontel.net", "myfairpoint.net",
}


def parse_town_emails(pdf_path: Path) -> dict[str, str]:
    reader = pypdf.PdfReader(str(pdf_path))
    full_text = "\n".join(
        reader.pages[i].extract_text() for i in range(LISTINGS_FIRST_PAGE, LISTINGS_LAST_PAGE + 1)
    )
    lines = full_text.split("\n")

    towns: dict[str, str] = {}
    current: str | None = None
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        header_match = TOWN_HEADER_RE.match(line)
        if header_match:
            current = header_match.group(1).strip()
            continue
        if line.startswith("Email:") and current and current not in towns:
            rest = line.split("Email:", 1)[1].strip()
            if not rest and i + 1 < len(lines):
                rest = lines[i + 1].strip()
            email = re.sub(r"\s+", "", rest)
            if "@" in email:
                towns[current] = email
    return towns


def main() -> None:
    pdf_path = Path(__file__).resolve().parent / "_vt_clerks_tmp.pdf"
    resp = requests.get(GUIDE_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    pdf_path.write_bytes(resp.content)
    towns = parse_town_emails(pdf_path)
    pdf_path.unlink(missing_ok=True)
    print(f"parsed {len(towns)} Vermont towns from the Secretary of State's clerk guide")

    session = requests.Session()
    session.headers.update(HEADERS)

    verified: dict[str, str] = {}
    for name, email in towns.items():
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
            VALUES (?, 'Vermont', 'town', ?, 'vt-sos-clerk-guide-email-domain')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Vermont towns indexed")


if __name__ == "__main__":
    main()
