"""
Scrapes the Virginia Municipal League's archived local-government-members
page — a static three-column list of cities/towns/counties with direct
links to official websites. Last updated by VML July 2022; counties are
included too since VML membership covers them, but place_type distinguishes
them so downstream crawling can skip non-municipal entries if desired.
"""

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

URL = "https://www.old.vml.org/organization/local-government-members/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

ENTRY_RE = re.compile(
    r'<a href="(https?://[^"]+)"[^>]*>(City of|Town of|County of)\s*([^<]+)</a>'
)

PLACE_TYPE_MAP = {"City of": "city", "Town of": "town", "County of": "county"}


def main() -> None:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for website, prefix, raw_name in ENTRY_RE.findall(resp.text):
        name = raw_name.strip()
        place_type = PLACE_TYPE_MAP[prefix]
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Virginia', ?, ?, 'va-league')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                place_type=excluded.place_type,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, place_type, website),
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"done — {total} Virginia local governments indexed")


if __name__ == "__main__":
    main()
