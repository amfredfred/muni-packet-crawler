"""
Single-page scrape of the Delaware League of Local Governments'
membership directory. Two earlier Delaware sources were dead ends
(dllg.delaware.gov no longer resolves; ipa.udel.edu/localgovt/dllg's own
websites.asp page mostly just links back to udel.edu profile pages) —
this is a different domain (dllg.us) found by crawling the udel.edu
page's external links, and it turned out to be a clean, static,
already-built list: every municipality name is a plain <a> tag pointing
straight at its official website, no JS rendering or pagination involved.

A handful of the ~61 external links on the page aren't municipalities at
all (the League's own LinkedIn/YouTube, a link to a udel.edu directory-
of-officials page, and two "Delmarva Digital" web-vendor credit links) —
those are dropped by name via NON_MUNICIPALITY_NAMES rather than by
guessing at URL patterns, since the vendor links use delaware-sounding
copy but an unrelated domain.
"""

import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

LIST_URL = "https://www.dllg.us/membership-directory.htm"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

NON_MUNICIPALITY_NAMES = {
    "",
    "Directory of Delaware Local Governments",
    "CityPORT.Pro: Delmarva Digital eGovernment Web Tools",
    "Delmarva Digital",
}
# Kent/New Castle/Sussex County are counties, not municipalities — the
# muni-packet-crawler's municipalities table is city/town scoped.
NON_MUNICIPALITY_SUFFIXES = (" County", " Levy Court")


def fetch_municipalities() -> dict[str, str]:
    resp = requests.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        name = a.get_text(strip=True)
        href = a["href"].strip()
        if not href.startswith("http") or "dllg.us" in href:
            continue
        if name in NON_MUNICIPALITY_NAMES:
            continue
        if any(name.endswith(suffix) for suffix in NON_MUNICIPALITY_SUFFIXES):
            continue
        out[name] = href
    return out


def main() -> None:
    municipalities = fetch_municipalities()
    print(f"fetched {len(municipalities)} Delaware municipalities")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in municipalities.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Delaware', 'city', ?, 'dllg-directory')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Delaware municipalities indexed")


if __name__ == "__main__":
    main()
