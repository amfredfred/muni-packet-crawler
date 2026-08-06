"""
Prints packets for upcoming meetings — the filtered view over `packets`
that answers "just give me the PDFs for meetings that haven't happened
yet." Packets with an unparseable date (is_upcoming IS NULL) are shown
separately rather than silently dropped, since "unknown date" is not the
same as "not upcoming" — many platforms don't expose a parseable date at
all (see date_extract.py's docstring for which ones).

--within-days caps the upcoming list to a near-term window (default 10) so
a real September 2026 agenda posted early doesn't get buried under one from
next year — this only narrows the *view*, it doesn't touch what's stored,
so widening the window later doesn't need a re-crawl. It only applies to
the dated bucket: a NULL meeting_date can never satisfy a "<=" comparison
in SQL, so the unknown-dates query is built separately rather than by
string-editing the dated one — reusing the same WHERE clause there would
silently zero out every unknown-date row.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_connection

BASE_QUERY = """
    SELECT m.name, m.state, mt.source_url, p.url, p.title, p.meeting_date
    FROM packets p
    JOIN meetings mt ON p.meeting_id = mt.id
    JOIN municipalities m ON mt.municipality_id = m.id
    WHERE {date_condition}
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state")
    parser.add_argument("--include-unknown-dates", action="store_true")
    parser.add_argument(
        "--within-days", type=int, default=10,
        help="Only show meetings within this many days from today (default 10). Pass 0 to disable the cap.",
    )
    args = parser.parse_args()

    conn = get_connection()
    cur = conn.cursor()

    def run(date_condition: str, extra_params: list) -> list:
        query = BASE_QUERY.format(date_condition=date_condition)
        params = list(extra_params)
        if args.state:
            query += " AND m.state = ?"
            params.append(args.state)
        query += " ORDER BY p.meeting_date, m.name"
        cur.execute(query, params)
        return cur.fetchall()

    dated_condition = "p.is_upcoming = 1"
    dated_params: list = []
    if args.within_days > 0:
        dated_condition += " AND p.meeting_date <= date('now', ?)"
        dated_params.append(f"+{args.within_days} days")

    upcoming = run(dated_condition, dated_params)

    print(f"{len(upcoming)} packet(s) for upcoming meetings:\n")
    for name, state, source_url, url, title, meeting_date in upcoming:
        print(f"[{meeting_date or '?'}] {name}, {state} — {title}")
        print(f"    {url}")

    if args.include_unknown_dates:
        unknown = run("p.is_upcoming IS NULL", [])
        print(f"\n{len(unknown)} packet(s) with no parseable date (not shown above, review manually):\n")
        for name, state, source_url, url, title, meeting_date in unknown:
            print(f"[unknown date] {name}, {state} — {title}")
            print(f"    {url}")
            print(f"    (listing page: {source_url})")

    conn.close()


if __name__ == "__main__":
    main()
