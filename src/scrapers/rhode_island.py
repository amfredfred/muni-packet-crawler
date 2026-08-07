"""
Two-step scrape of the RI Department of State's government directory. Looked
JS-gated at first glance (each row is an <a onclick="..."> with no href),
but the onclick handler — GOVDirectoryMeetingInfo(id, type) — turned out to
just navigate to a plain, parameterized URL:
GOVDirectoryMeetingDetails?EntityID=<id>. So no browser automation is
needed at all: the entity IDs are already sitting in the main page's
static HTML, and each detail page has a clearly labeled "Web:" field.
"""

import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

LIST_URL = "https://opengov.sos.ri.gov/GOVDirectoryPublic/OfficialSearch?topmenuId=104&page=IdxMuni"
DETAIL_URL = "https://opengov.sos.ri.gov/GOVDirectoryPublic/GOVDirectoryMeetingDetails?EntityID={id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

ENTITY_RE = re.compile(r'onclick="return GOVDirectoryMeetingInfo\((\d+),\d+\);"><span>([^<]+)</span>')
WEB_FIELD_RE = re.compile(r'<b>Web:</b></span><a href="(https?://[^"]+)"')

TYPE_SUFFIXES = {
    ", Town of": "town",
    ", City of": "city",
}

# Some entries on RI's directory (e.g. Cranston, East Providence) have their
# "Web:" field double-prefixed — the underlying data has the full https://
# URL typed into a field that already prepends http://, yielding
# "http://https://www.example.gov". Strip the bogus leading scheme.
DOUBLE_SCHEME_RE = re.compile(r"^https?://(https?://.*)$")


def normalize_url(url: str) -> str:
    match = DOUBLE_SCHEME_RE.match(url)
    return match.group(1) if match else url


def parse_name(raw: str) -> tuple[str, str]:
    for suffix, place_type in TYPE_SUFFIXES.items():
        if raw.endswith(suffix):
            return raw[: -len(suffix)].strip(), place_type
    return raw.strip(), "city"


def main() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)

    resp = session.get(LIST_URL, timeout=30)
    resp.raise_for_status()
    entries = ENTITY_RE.findall(resp.text)
    print(f"found {len(entries)} entities in directory")

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for entity_id, raw_name in entries:
        name, place_type = parse_name(raw_name)
        try:
            detail_resp = session.get(DETAIL_URL.format(id=entity_id), timeout=30)
        except requests.RequestException as e:
            print(f"  {name}: request failed ({e}), skipping")
            continue
        if detail_resp.status_code != 200:
            print(f"  {name}: HTTP {detail_resp.status_code}, skipping")
            continue

        match = WEB_FIELD_RE.search(detail_resp.text)
        if not match:
            print(f"  {name}: no 'Web:' field found, skipping")
            continue
        website = normalize_url(match.group(1))

        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Rhode Island', ?, ?, 'ri-sos')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, place_type, website),
        )
        total += 1
        conn.commit()
        time.sleep(0.3)

    conn.close()
    print(f"done — {total} Rhode Island municipalities indexed")


if __name__ == "__main__":
    main()
