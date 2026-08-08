"""
The Maryland Municipal League's "Member Cities and Towns" page
(mdmunicipal.org/member-cities-and-towns) is a clean, public, static
list — no login wall, no AJAX pagination — with every member
municipality's name linking straight to its official website. 156 of
MML's stated 157 member municipalities came back with a usable link;
the shortfall is a real gap in MML's own page, not a scraping miss.
"""

import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

LIST_URL = "https://www.mdmunicipal.org/member-cities-and-towns"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

NON_MUNICIPALITY_NAMES = {"Schedule By Day", "Sponsorships", "MML Digital Library"}
NON_MUNICIPALITY_HOST_FRAGMENTS = ("mobileup.io", "memberclicks.net", "ebridge.com")


def fetch_municipalities() -> dict[str, str]:
    resp = requests.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        name = a.get_text(strip=True)
        if not href.startswith("http") or "mdmunicipal.org" in href:
            continue
        if name in NON_MUNICIPALITY_NAMES or any(f in href for f in NON_MUNICIPALITY_HOST_FRAGMENTS):
            continue
        if not name:
            continue
        out[name] = href
    return out


def main() -> None:
    municipalities = fetch_municipalities()
    print(f"fetched {len(municipalities)} Maryland municipalities")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in municipalities.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Maryland', 'city', ?, 'mml-member-directory')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Maryland municipalities indexed")


if __name__ == "__main__":
    main()
