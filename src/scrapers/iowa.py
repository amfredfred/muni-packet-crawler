"""
The Iowa League of Cities' "Cities in Iowa" page (/cities/) renders its
full member table server-side in the initial HTML — all 947 cities in
one plain `requests` GET, no AJAX/pagination step needed (unlike
Arkansas's wpDataTables setup, which required replaying an AJAX call).

The League's own website column has data-quality issues worth handling
explicitly rather than silently mangling: about 43% of rows have no
website on file at all (rendered as a bare "http://" link — a real gap
in the League's data, not ours), and ~3% have a doubled scheme
("http://https://actualsite.gov/") from what looks like a template bug
on their end. Blank ones are skipped; doubled ones are fixed by
stripping the outer "http://" rather than dropped.
"""

import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

LIST_URL = "https://iowaleague.org/cities/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
DOUBLED_SCHEME_RE = re.compile(r"^https?://(https?://)", re.IGNORECASE)


def fetch_cities() -> dict[str, str]:
    resp = requests.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    out: dict[str, str] = {}
    for tr in soup.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        name = tds[0].get_text(strip=True)
        website_a = tds[4].find("a", href=True)
        if not name or not website_a:
            continue
        href = DOUBLED_SCHEME_RE.sub(r"\1", website_a["href"].strip())
        if not re.sub(r"^https?://", "", href, flags=re.IGNORECASE):
            continue  # blank on file
        out[name] = href
    return out


def main() -> None:
    cities = fetch_cities()
    print(f"fetched {len(cities)} Iowa cities with a website on file")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in cities.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Iowa', 'city', ?, 'iowa-league-directory')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Iowa cities indexed")


if __name__ == "__main__":
    main()
