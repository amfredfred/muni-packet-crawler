"""
The Wyoming Association of Municipalities' directory page itself has
no visible website field, but it links a "Download 2026 Municipal
Directory" PDF that does — each municipality's entry states a bare
domain (no scheme) right after its contact email, e.g. Afton's entry
reads "tmcgarvey@aftonwyoming.gov" then "aftonwyoming.gov" on the very
next line. Unlike Kentucky/Mississippi/Vermont, this is the actual
stated website, not an inferred one from the email's domain — no
verification step needed, since the source hands it over directly.

The PDF's text extraction has a layout quirk worth handling explicitly:
each entry's header line runs the municipality name straight into
"TOWN OF"/"CITY OF" with tab characters as the only separator, even
between words of a multi-word name ("BIG\\tPINEYTOWN\\tOF\\t") — so the
header regex has to accept tabs (not just spaces) inside the name, and
the captured name gets its internal whitespace collapsed back down to
single spaces afterward.

About a quarter of entries (mostly the larger cities, e.g. Casper) have
no email/website found via this scan — a real gap in that specific
entry's layout or content, not a parsing shortfall across the board.
"""

import re
import sys
from pathlib import Path

import pypdf
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

DIRECTORY_PAGE_URL = "https://wyomuni.org/about/membership-directory/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# "Municipality Information" runs from page 11 through 108 per the PDF's
# own table of contents; page 109 switches to "Statewide Elected Officials".
LISTINGS_FIRST_PAGE = 11
LISTINGS_LAST_PAGE = 108

HEADER_RE = re.compile(r"^\s*(?:\d+\s*)?([A-Z][A-Z .'\-\s]*?)(TOWN\s+OF|CITY\s+OF)\s*$")
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$")
WEBSITE_RE = re.compile(r"^(?:https?://)?(?:www\.)?[\w.-]+\.[A-Za-z]{2,}/?$")
NON_WEBSITE_LINE_STARTS = ("incorporation", "population", "county", "wam region", "form of gov")


def find_directory_pdf_url() -> str:
    resp = requests.get(DIRECTORY_PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(".pdf"):
            return a["href"]
    raise RuntimeError("could not find the WAM municipal directory PDF link on the membership-directory page")


def parse_municipalities(pdf_path: Path) -> dict[str, str]:
    reader = pypdf.PdfReader(str(pdf_path))
    full_text = "\n".join(
        reader.pages[i].extract_text() for i in range(LISTINGS_FIRST_PAGE, LISTINGS_LAST_PAGE + 1)
    )
    lines = full_text.split("\n")

    websites: dict[str, str] = {}
    current: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        header_match = HEADER_RE.match(line)
        if header_match:
            current = re.sub(r"\s+", " ", header_match.group(1)).strip().title()
            i += 1
            continue
        if current and EMAIL_RE.match(line) and current not in websites:
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                looks_like_website = WEBSITE_RE.match(next_line) and not any(
                    next_line.lower().startswith(p) for p in NON_WEBSITE_LINE_STARTS
                )
                if looks_like_website:
                    url = next_line if next_line.startswith("http") else f"https://{next_line}"
                    websites[current] = url
        i += 1
    return websites


def main() -> None:
    pdf_url = find_directory_pdf_url()
    pdf_path = Path(__file__).resolve().parent / "_wy_directory_tmp.pdf"
    resp = requests.get(pdf_url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    pdf_path.write_bytes(resp.content)

    websites = parse_municipalities(pdf_path)
    pdf_path.unlink(missing_ok=True)
    print(f"parsed {len(websites)} Wyoming municipality websites from the WAM directory PDF")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in websites.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Wyoming', 'city', ?, 'wam-directory-pdf')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Wyoming municipalities indexed")


if __name__ == "__main__":
    main()
