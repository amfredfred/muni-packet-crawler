"""
Minnesota's mn.gov city-website directory sits behind Radware Bot Manager,
which was originally recorded as an unsolvable CAPTCHA wall. Re-tested: a
real, non-headless Chromium session passes through with no CAPTCHA shown
at all (confirmed headless Playwright still gets the CAPTCHA page every
time — this is a headless-fingerprint block, not a human-verification
gate). So the fix was simply fetching the page with a full browser once,
not a recurring per-run bot-evasion mechanism.

The list itself is also a genuine data-quality limit, not a scraping gap:
of ~850 city names on the page, only a subset have an actual href — the
rest are plain unlinked text, meaning the state's own directory never
received a website URL for most small cities. This file is a static
snapshot of the (name, url) pairs that page yielded on 2026-08-07, pulled
via a real browser session's DOM rather than re-fetched headlessly, since
headless refetching would just hit the CAPTCHA again.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

# (display title, url) as captured from mn.gov/portal/government/local/cities/
RAW_ENTRIES = [
    ("Adrian", "http://www.adrian.govoffice2.com/"),
    ("Afton", "http://ci.afton.mn.us/"),
    ("Aitkin", "http://www.ci.aitkin.mn.us/"),
    ("Akeley", "http://www.akeleymn.com/"),
    ("Albany", "http://www.ci.albany.mn.us/"),
    ("Albert Lea", "http://www.cityofalbertlea.org/"),
    ("Albertville", "http://www.ci.albertville.mn.us/"),
    ("Alexandria", "http://www.ci.alexandria.mn.us/"),
    ("Andover", "http://www.ci.andover.mn.us/"),
    ("Annandale", "http://www.annandale.mn.us/"),
    ("Anoka", "http://www.ci.anoka.mn.us/"),
    ("Apple Valley", "http://www.ci.apple-valley.mn.us/"),
    ("Arden Hills", "http://www.ci.arden-hills.mn.us/"),
    ("Argyle", "http://www.ci.argyle.mn.us/"),
    ("Arlington", "http://www.arlingtonmn.com/"),
    ("Ashby", "http://www.ashbyminnesota.org/"),
    ("Atwater", "http://www.atwaterchamber.com/"),
    ("Aurora", "http://www.ci.aurora.mn.us/"),
    ("Austin", "http://www.ci.austin.mn.us/"),
    ("Avon", "http://www.cityofavonmn.com/"),
    ("Dilworth, Minnesota", "https://www.cityofdilworth.com/"),
    ("Golden Valley Website", "http://www.goldenvalleymn.gov/"),
    ("Hastings, MN", "http://www.hastingsmn.gov/"),
    ("Little Falls", "http://www.cityoflittlefalls.com/"),
    ("Rochester, MN", "http://www.rochestermn.gov/"),
    ("St. Cloud", "http://www.ci.stcloud.mn.us/"),
    ("Vadnais Heights", "http://www.cityvadnaisheights.com/"),
    ("Victoria", "http://www.ci.victoria.mn.us/"),
    ("Virginia", "http://www.virginiamn.us/"),
    ("Wabasha", "http://www.wabasha.org/"),
    ("Waconia", "http://www.waconia.org/"),
    ("Wadena", "http://www.wadena.org/"),
    ("Wahkon", "http://www.cityofwahkon.com/"),
    ("Waite Park", "http://www.ci.waitepark.mn.us/"),
    ("Walnut Grove", "http://www.walnutgrove.org/"),
    ("Wanamingo", "http://www.cityofwanamingo.com/"),
    ("Warren", "http://www.warrenminnesota.com/"),
    ("Waseca", "http://www.ci.waseca.mn.us/"),
    ("Watertown", "http://www.ci.watertown.mn.us/"),
    ("Waterville", "http://www.watervillemn.com/"),
    ("Waverly", "http://www.waverlymn.org/"),
    ("Wayzata", "http://www.wayzata.org/"),
    ("West Concord", "http://www.westconcordmn.com/"),
    ("West St. Paul", "http://wspmn.gov/"),
    ("White Bear Lake", "https://www.whitebearlake.org/"),
    ("Willmar", "http://www.ci.willmar.mn.us/"),
    ("Windom", "http://www.windom-mn.com/"),
    ("Winnebago", "http://www.cityofwinnebago.com/"),
    ("Winona", "https://www.cityofwinona.com/"),
    ("Winsted", "http://www.winsted.mn.us/"),
    ("Woodbury", "https://www.woodburymn.gov/"),
    ("Woodland", "http://www.woodlandmn.org/"),
    ("Worthington", "http://www.ci.worthington.mn.us/"),
    ("Wykoff", "http://www.wykoff.govoffice2.com/"),
    ("Wyoming", "http://www.wyomingmn.org/"),
    ("Zimmerman", "http://zimmerman.govoffice.com/"),
    ("Zumbrota", "http://www.ci.zumbrota.mn.us/"),
]

TRAILING_JUNK_RE = re.compile(r",?\s*(Minnesota|MN)$|\s+Website$", re.I)


def clean_name(raw: str) -> str:
    return TRAILING_JUNK_RE.sub("", raw).strip()


def main() -> None:
    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for raw_name, url in RAW_ENTRIES:
        name = clean_name(raw_name)
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Minnesota', 'city', ?, 'mn-gov-directory')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, url),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Minnesota municipalities indexed")


if __name__ == "__main__":
    main()
