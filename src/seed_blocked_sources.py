"""
One-time (re-runnable) seed of `blocked_sources` with the specific URLs
that were tested and found blocked/dead/unusable during state-scraper
research — see README's "Checked and ruled out" section for the narrative.
Re-run after fixing/removing an entry's underlying cause to keep this list
in sync with reality.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_connection

# (state, url, reason, category)
# category values: bot_block, captcha, dead_domain, js_gated, no_data_field,
# login_gated, timeout
ENTRIES = [
    ("Florida", "https://www.flcities.com/directory/",
     "403 Forbidden (nginx-level) even with realistic headers and a real headless browser", "bot_block"),
    ("Wisconsin", "https://www.lwm-info.org/1236/Directory-of-Cities-Villages",
     "403 Forbidden", "bot_block"),
    ("Indiana", "https://aimindiana.org/directory/municipal/",
     "Cloudflare 'Sorry, you have been blocked' challenge page, confirmed with headless browser too", "bot_block"),
    ("Kentucky", "https://dlg.ky.gov/cities/Pages/default.aspx",
     "403 Forbidden", "bot_block"),
    ("Missouri", "https://mocities.site-ym.com/page/memberwebsites",
     "403 on first check, 'Site Maintenance' page on retry — worth trying again later", "timeout"),
    ("Ohio", "https://www.omlohio.org/QuickLinks.aspx?CID=35",
     "Re-checked: the site has been reorganized and this category no longer exists (\"There are no Quick Links "
     "for this selection\"). Found the closest replacement via full-text site search — a members-only PDF roster "
     "(DocumentCenter/View/3360) — but it's a flat list of city names with no website URLs or link annotations, "
     "so it can't populate official_url the way a real directory would.", "no_data_field"),
    ("Mississippi", "https://www.mmlonline.com/members/",
     "Directory loads fine but has no official-website field at all, only meeting/contact info", "no_data_field"),
    ("Wyoming", "https://wyomuni.org/about/membership-directory/",
     "Directory loads fine but only exposes phone/fax/address, no website field", "no_data_field"),
    ("Montana", "https://legacy.mtleague.org/basic/",
     "DNS resolution failure (domain does not resolve)", "dead_domain"),
    ("North Dakota", "https://www.nd.gov/government/local-government/city-website-list",
     "Connection timeout with both plain requests and headless Chromium — the best-named lead of this batch, genuinely unreachable", "timeout"),
    ("Delaware", "https://dllg.delaware.gov/del-govt-directory/",
     "DNS resolution failure", "dead_domain"),
    ("Delaware", "https://www.ipa.udel.edu/localgovt/dllg/websites.asp",
     "Loads fine but nested-accordion structure with inconsistent heading levels, not worth the parser effort for a small state", "js_gated"),
    ("Idaho", "https://idahocities.org/store/ListProducts.aspx?catid=440197",
     "Member directory requires login", "login_gated"),
    ("Alaska", "https://www.akml.org/about/municipalities/",
     "Loads fine, but data is one PDF profile sheet per municipality (~165) — needs a PDF-parsing scraper, not yet built", "no_data_field"),
]


def main() -> None:
    conn = get_connection()
    cur = conn.cursor()
    for state, url, reason, category in ENTRIES:
        cur.execute(
            """
            INSERT INTO blocked_sources (state, url, reason, category)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(state, url) DO UPDATE SET reason=excluded.reason, category=excluded.category
            """,
            (state, url, reason, category),
        )
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM blocked_sources")
    print(f"seeded — {cur.fetchone()[0]} blocked sources on file")
    conn.close()


if __name__ == "__main__":
    main()
