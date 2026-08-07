"""
Two-step scrape of Atlas Alabama (a state-government-adjacent municipality
directory): the main directory page links to each municipality's own
atlasalabama.gov profile page (not the official site directly), and each
profile page has a clearly labeled "City Website" button linking out to the
real official government site.
"""

import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

DIRECTORY_URL = "https://www.atlasalabama.gov/directory-municipality/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

PROFILE_URL_RE = re.compile(r'https://www\.atlasalabama\.gov/municipalities/([a-z0-9-]+)/')
# Alabama classifies municipalities as either a "city" or a "town" and the
# profile page's own wording follows suit ("city of X" / "town of X", and
# a "City Website" / "Town Website" button respectively) — matching only
# the city form silently skipped every town, including ones with a real
# website (e.g. Silverhill), which looked identical to "no website listed"
# in the earlier run's output.
NAME_RE = re.compile(r'<h1 class="display-1">(?:city|town) of ([^<]+)</h1>', re.I)
WEBSITE_RE = re.compile(
    r'<a href="(https?://[^"]+)"[^>]*>(?:City|Town) Website', re.I
)


def get_profile_slugs(session: requests.Session) -> list[str]:
    resp = session.get(DIRECTORY_URL, timeout=30)
    resp.raise_for_status()
    return sorted(set(PROFILE_URL_RE.findall(resp.text)))


def get_profile_data(session: requests.Session, slug: str) -> tuple[str, str] | None:
    resp = session.get(f"https://www.atlasalabama.gov/municipalities/{slug}/", timeout=30)
    if resp.status_code != 200:
        return None
    name_match = NAME_RE.search(resp.text)
    website_match = WEBSITE_RE.search(resp.text)
    if not name_match or not website_match:
        return None
    return name_match.group(1).strip(), website_match.group(1)


def main() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)

    slugs = get_profile_slugs(session)
    print(f"found {len(slugs)} municipality profiles")

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for slug in slugs:
        try:
            data = get_profile_data(session, slug)
        except requests.RequestException as e:
            print(f"  {slug}: request failed ({e}), skipping")
            continue
        if not data:
            print(f"  {slug}: no name/website found, skipping")
            continue
        name, website = data
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Alabama', 'city', ?, 'al-atlas')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
        conn.commit()
        time.sleep(0.2)

    conn.close()
    print(f"done — {total} Alabama municipalities indexed")


if __name__ == "__main__":
    main()
