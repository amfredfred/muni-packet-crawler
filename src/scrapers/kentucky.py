"""
Kentucky has no source that hands over city names paired with website
links directly — three were tried and ruled out first: the Kentucky
League of Cities has no public city-directory page at all (its site
nav was fully enumerated, nothing there); the state GIS "Incorporated
Cities" map (kygeonet.ky.gov/kycities) is an ArcGIS Experience Builder
app whose underlying feature layer (Ky_DGI_CityHalls_WM_gdb) only has
address/phone fields, no website; Wikipedia's list of Kentucky cities
has no external links either, just internal wiki links.

What DOES exist for every city is the Department for Local
Government's legacy per-city profile system (kydlgweb.ky.gov/Cities/
16_CityView.cfm?City_ID=N, N from 1 to 495 with some empty IDs and a
handful of non-city rows — unincorporated-county placeholders and
"Z_"-prefixed entries that duplicate the real city, both filtered out).
Each real city's profile shows exactly one primary contact email, and
in practice that email's domain is the city's own domain in the
overwhelming majority of cases (e.g. Louisville -> louisvilleky.gov,
Adairville -> adairvilleky.com) — so the site itself never states a
"website" field, but it hands you the domain implicitly. Rather than
trust that inference blindly, every candidate domain is verified live
AND checked for an actual connection to the city (see
_domain_verify.py) before being stored — domains on generic freemail
providers (gmail.com etc) are skipped outright, and a first pass
without the content check turned up ~44 cities' domains that resolved
fine but were really a clerk's ISP or phone co-op's own site
(windstream.net, scrtc.com and the like), a webmail login page, or
similar false positives — proof that liveness alone isn't a safe bar.
"""

import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection
from scrapers._domain_verify import verify_domain_belongs_to

MIN_CITY_ID = 1
MAX_CITY_ID = 495
VIEW_URL = "http://kydlgweb.ky.gov/Cities/16_CityView.cfm?City_ID={id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

NAME_RE = re.compile(r"<h1[^>]*>City of (.*?)</h1>", re.IGNORECASE | re.DOTALL)
EMAIL_RE = re.compile(
    r"e-Mail:.*?<td[^>]*>\s*([\w.+-]+@([\w.-]+\.[A-Za-z]{2,}))\s*</td>",
    re.IGNORECASE | re.DOTALL,
)
NON_CITY_NAME_RE = re.compile(r"\(UNINC\)|CDP$|^Z_", re.IGNORECASE)
FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "comcast.net", "att.net",
}


def get_with_retry(session: requests.Session, url: str, tries: int = 4):
    last_exc = None
    for attempt in range(tries):
        try:
            return session.get(url, timeout=20)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise last_exc


def fetch_candidates(session: requests.Session) -> dict[str, str]:
    """Returns {city_name: email_domain} for every real city with a
    plausible (non-freemail) domain — not yet verified as a live site."""
    out: dict[str, str] = {}
    for city_id in range(MIN_CITY_ID, MAX_CITY_ID + 1):
        resp = get_with_retry(session, VIEW_URL.format(id=city_id))
        name_match = NAME_RE.search(resp.text)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        if not name or NON_CITY_NAME_RE.search(name):
            continue
        email_match = EMAIL_RE.search(resp.text)
        if not email_match:
            continue
        domain = email_match.group(2).lower()
        if domain in FREEMAIL_DOMAINS:
            continue
        out[name] = domain
    return out


def main() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)

    candidates = fetch_candidates(session)
    print(f"found {len(candidates)} Kentucky cities with a plausible non-freemail contact domain")

    verified: dict[str, str] = {}
    for name, domain in candidates.items():
        url = verify_domain_belongs_to(session, name, domain)
        if url:
            verified[name] = url
    print(f"verified {len(verified)} of {len(candidates)} domains as the city's own live website")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in verified.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Kentucky', 'city', ?, 'ky-dlg-email-domain')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Kentucky cities indexed")


if __name__ == "__main__":
    main()
