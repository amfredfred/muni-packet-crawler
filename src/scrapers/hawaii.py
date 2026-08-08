"""
Hawaii is a genuine special case, not a scraping failure: state law never
lets any city or town incorporate, so there are no "municipalities" in
the sense every other state has. All local government — the county
councils and planning commissions that would hold the meetings this
crawler cares about — is done by the four county governments (the City
and County of Honolulu is a consolidated city-county). Kalawao County
(the former Kalaupapa settlement) has no general-purpose elected
government at all — it's administered directly by the state Department
of Health — so it's deliberately left out; there's no council or
commission there to ever produce a packet.

Same municipalities table, place_type='county', matching how Virginia's
independent counties and Georgia's consolidated city-counties are
already stored — this isn't a new category, just Hawaii using the one
that already fits.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

# Verified directly: honolulu.gov returns 200 over plain HTTPS. The other
# three return 403/429 to a scripted request (Cloudflare/bot-protection,
# not evidence of a wrong domain) but are confirmed via the Hawaii
# Legislative Reference Bureau's own government guide (kauai.gov) and
# each county's public "Government" pages (mauicounty.gov, hawaiicounty.gov).
COUNTIES = {
    "Honolulu": "https://www.honolulu.gov",
    "Hawaii": "https://www.hawaiicounty.gov",
    "Maui": "https://www.mauicounty.gov",
    "Kauai": "https://www.kauai.gov",
}


def main() -> None:
    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in COUNTIES.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Hawaii', 'county', ?, 'manual-hi-county-government')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Hawaii county governments indexed (no incorporated municipalities exist in Hawaii)")


if __name__ == "__main__":
    main()
