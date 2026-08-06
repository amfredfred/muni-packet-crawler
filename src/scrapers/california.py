"""
Scrapes Cal Cities' (League of California Cities) static member-cities page —
a single page with all member cities and direct links to their official
websites, grouped alphabetically. The page renders the list twice (desktop +
mobile accordion blocks); duplicates are harmless since inserts upsert on
(state, name).
"""

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

URL = "https://www.calcities.org/home/about-us/member-cities"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

ENTRY_RE = re.compile(r'<a data-sf-ec-immutable="" href="(https?://[^"]+)">([^<]+)</a>')


def main() -> None:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    entries = ENTRY_RE.findall(resp.text)
    seen = set()
    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for website, raw_name in entries:
        name = raw_name.strip()
        if name in seen:
            continue
        seen.add(name)
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'California', 'city', ?, 'ca-league')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"done — {total} California cities indexed")


if __name__ == "__main__":
    main()
