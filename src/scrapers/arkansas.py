"""
Two-step scrape of the Arkansas Municipal League's member directory.
Looked like a hard block at first — the page only renders 10 rows
initially (a wpDataTables WordPress plugin doing server-side pagination
via /wp-admin/admin-ajax.php), and a plain POST to that endpoint with just
draw/start/length/nonce returns an empty body. Turned out the server
validates the *entire* DataTables column-definition payload, not just
those four fields — replaying a real browser's exact request (captured
via Playwright) worked immediately, so this rebuilds that same payload
shape programmatically rather than depending on a hand-copied one.

One row per elected official (Mayor, Council Member, Fire Chief, ...),
not one per city — 6,396 rows collapse to 500 unique cities. Only 199 of
those 500 have a website listed at all, a genuine gap in the League's own
data, not a scraping shortfall.
"""

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

LIST_URL = "https://www.armunileague.org/member-directory/"
AJAX_URL = "https://www.armunileague.org/wp-admin/admin-ajax.php?action=get_wdtable&table_id=10"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# Column order/names as embedded in the page's DataTables config — index 0
# is the city name, index 16 is the website link (rendered as a full <a>
# tag in the response, not a bare URL).
COLUMN_NAMES = [
    "CITYNAME", "NAME", "POSITION", "COUNTY", "POPULATION", "SENATE", "HOUSE",
    "CONG", "CLASSIFICATION", "ADDRESS", "CITY", "STATE", "ZIP", "PHONE",
    "FAX", "MEETINGS", "WEBSITE",
]
WEBSITE_ANCHOR_RE = re.compile(r'href="(https?://[^"]+)"')


def fetch_nonce(session: requests.Session) -> str:
    resp = session.get(LIST_URL, timeout=30)
    resp.raise_for_status()
    match = re.search(r'wdtNonceFrontendServerSide_10"\s+name="wdtNonceFrontendServerSide_10"\s+value="([^"]+)"', resp.text)
    if not match:
        raise RuntimeError("could not find the wpDataTables nonce on the member-directory page")
    return match.group(1)


def build_payload(nonce: str, length: int = 7000) -> dict:
    payload = {"draw": "1", "start": "0", "length": str(length), "wdtNonce": nonce}
    for i, name in enumerate(COLUMN_NAMES):
        payload[f"columns[{i}][data]"] = str(i)
        payload[f"columns[{i}][name]"] = name
        payload[f"columns[{i}][searchable]"] = "true"
        payload[f"columns[{i}][orderable]"] = "true"
        payload[f"columns[{i}][search][value]"] = ""
        payload[f"columns[{i}][search][regex]"] = "false"
    return payload


def main() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)

    nonce = fetch_nonce(session)
    resp = session.post(AJAX_URL, data=build_payload(nonce), timeout=60)
    resp.raise_for_status()
    result = resp.json()
    print(f"fetched {len(result['data'])} official-level rows ({result['recordsTotal']} total in table)")

    cities: dict[str, str | None] = {}
    for row in result["data"]:
        name = row[0].strip().title()
        website_cell = row[16].strip() if len(row) > 16 else ""
        website_match = WEBSITE_ANCHOR_RE.search(website_cell)
        website = website_match.group(1) if website_match else None
        if name not in cities or (not cities[name] and website):
            cities[name] = website

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in cities.items():
        if not website:
            continue
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Arkansas', 'city', ?, 'arml-directory')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} of {len(cities)} Arkansas municipalities indexed (rest have no website listed)")


if __name__ == "__main__":
    main()
