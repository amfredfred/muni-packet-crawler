"""
Prints packets for upcoming meetings — the filtered view over `packets`
that answers "just give me the PDFs for meetings that haven't happened
yet." Packets with an unparseable date (is_upcoming IS NULL) are shown
separately rather than silently dropped, since "unknown date" is not the
same as "not upcoming" — many platforms don't expose a parseable date at
all (see date_extract.py's docstring for which ones).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_connection


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state")
    parser.add_argument("--include-unknown-dates", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    cur = conn.cursor()

    query = """
        SELECT m.name, m.state, mt.source_url, p.url, p.title, p.meeting_date
        FROM packets p
        JOIN meetings mt ON p.meeting_id = mt.id
        JOIN municipalities m ON mt.municipality_id = m.id
        WHERE p.is_upcoming = 1
    """
    params: list = []
    if args.state:
        query += " AND m.state = ?"
        params.append(args.state)
    query += " ORDER BY p.meeting_date, m.name"

    cur.execute(query, params)
    upcoming = cur.fetchall()

    print(f"{len(upcoming)} packet(s) for upcoming meetings:\n")
    for name, state, source_url, url, title, meeting_date in upcoming:
        print(f"[{meeting_date or '?'}] {name}, {state} — {title}")
        print(f"    {url}")

    if args.include_unknown_dates:
        unknown_query = query.replace("p.is_upcoming = 1", "p.is_upcoming IS NULL")
        cur.execute(unknown_query, params)
        unknown = cur.fetchall()
        print(f"\n{len(unknown)} packet(s) with no parseable date (not shown above, review manually):\n")
        for name, state, source_url, url, title, meeting_date in unknown:
            print(f"[unknown date] {name}, {state} — {title}")
            print(f"    {url}")
            print(f"    (listing page: {source_url})")

    conn.close()


if __name__ == "__main__":
    main()
