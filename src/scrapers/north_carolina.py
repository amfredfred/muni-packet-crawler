"""
UNC's School of Government (sog.unc.edu) maintains a public "N.C.
Cities and Towns" reference page as a single three-column table: City/
Town (linking to the official site), Additional Information (chamber
of commerce, visitors bureau, YouTube channel — not the city itself),
and County (linking to the county's own site). Only the first
column's link is the municipality's official site; the other two
columns are dropped entirely rather than risk pulling in a chamber or
county link by mistake. Towns with no site on file simply have no link
in that first cell and are skipped — a real gap in UNC's page, not a
parsing shortfall.
"""

import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

LIST_URL = "https://www.sog.unc.edu/resources/microsites/knapp-library/cities-north-carolina"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def fetch_municipalities() -> dict[str, str]:
    resp = requests.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    out: dict[str, str] = {}
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        link = tds[0].find("a", href=True)
        if not link:
            continue
        name = link.get_text(strip=True)
        href = link["href"].strip()
        if name and href.startswith("http"):
            out[name] = href
    return out


def main() -> None:
    municipalities = fetch_municipalities()
    print(f"fetched {len(municipalities)} North Carolina municipalities")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in municipalities.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'North Carolina', 'city', ?, 'unc-sog-cities-and-towns')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} North Carolina municipalities indexed")


if __name__ == "__main__":
    main()
