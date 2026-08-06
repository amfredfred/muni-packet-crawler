"""
Scrapes the Michigan Municipal League's alphabetical member directory
(mml.org/league-member-communities-<letter>/) for Full Member cities and
villages with a linked official website. Limited Associate entries
(townships, fire boards, libraries, etc.) have no link and are skipped.
"""

import re
import string
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

ENTRY_RE = re.compile(
    r'<a href="(https?://[^"]+)"[^>]*>([^<]+)</a>\s*(?:&#8211;|&ndash;|-|–)\s*Full Member',
    re.IGNORECASE,
)


def scrape_letter(letter: str) -> list[tuple[str, str]]:
    url = f"https://mml.org/league-member-communities-{letter}/"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return ENTRY_RE.findall(resp.text)


def classify(name: str) -> str:
    return "village" if "village" in name.lower() else "city"


def main() -> None:
    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for letter in string.ascii_lowercase:
        entries = scrape_letter(letter)
        for website, raw_name in entries:
            name = raw_name.strip()
            place_type = classify(name)
            cur.execute(
                """
                INSERT INTO municipalities (name, state, place_type, official_url, source)
                VALUES (?, 'Michigan', ?, ?, 'mi-league')
                ON CONFLICT(state, name) DO UPDATE SET
                    official_url=excluded.official_url,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (name, place_type, website),
            )
        conn.commit()
        total += len(entries)
        print(f"[{letter}] +{len(entries)} (total {total})")
        time.sleep(0.5)

    conn.close()
    print(f"done — {total} Michigan municipalities indexed")


if __name__ == "__main__":
    main()
