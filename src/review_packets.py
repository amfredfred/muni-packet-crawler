"""
Batch-reviews saved packets' PDFs (see pdf_review.py) and records whether
each names an applicant/owner — the priority signal for follow-up.

Same architecture as crawl_sites.py and for the same reason: worker threads
do only the network/PDF-parsing work and return a result; a single writer
thread (the main loop here) is the only one that ever touches the database,
since SQLite allows just one writer at a time even in WAL mode and letting
every worker write directly caused persistent 'database is locked' errors
under real concurrency.
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# See webapp.py's matching comment: redirect before anything riskier than
# the stdlib gets imported, so a crash in a later import (pdf_review, which
# pulls in pypdf) lands in the log file instead of
# vanishing silently under pythonw.exe (sys.stdout/stderr start out as
# None there).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import redirect_console_if_headless
redirect_console_if_headless("review_engine.log")

from db import get_connection
from pdf_review import review_pdf


def process_one(packet_id: int, url: str) -> tuple[int, dict]:
    try:
        return packet_id, review_pdf(url)
    except Exception as e:
        return packet_id, {"review_method": "error", "has_applicant_info": False, "applicant_snippet": str(e)[:200]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", help="Only review packets for municipalities in this state")
    parser.add_argument("--board-type", help="Only review packets matching this exact board_type")
    parser.add_argument("--upcoming-only", action="store_true", help="Skip packets with no known date")
    parser.add_argument("--limit", type=int, help="Max packets to review this run")
    parser.add_argument("--rereview", action="store_true", help="Re-review even if already reviewed")
    parser.add_argument("--workers", type=int, default=10, help="Concurrent worker threads (default 10)")
    args = parser.parse_args()

    conn = get_connection()
    cur = conn.cursor()

    query = """
        SELECT p.id, p.url FROM packets p
        JOIN municipalities m ON p.municipality_id = m.id
    """
    where = []
    params: list = []
    if not args.rereview:
        where.append("p.review_method IS NULL")
    if args.state:
        where.append("m.state = ?")
        params.append(args.state)
    if args.board_type:
        where.append("p.board_type = ?")
        params.append(args.board_type)
    if args.upcoming_only:
        where.append("p.is_upcoming = 1")
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY p.is_upcoming DESC, p.id"
    if args.limit:
        query += f" LIMIT {args.limit}"

    cur.execute(query, params)
    rows = cur.fetchall()
    print(f"reviewing {len(rows)} packets with {args.workers} workers")

    method_counts: dict[str, int] = {}
    priority_hits = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_one, pid, url): url for pid, url in rows}
        for future in as_completed(futures):
            packet_id, result = future.result()
            priority = 1 if result["has_applicant_info"] else 0
            cur.execute(
                "UPDATE packets SET review_method = ?, applicant_snippet = ?, priority = ? WHERE id = ?",
                (result["review_method"], result["applicant_snippet"], priority, packet_id),
            )
            conn.commit()
            done += 1
            priority_hits += priority
            method_counts[result["review_method"]] = method_counts.get(result["review_method"], 0) + 1
            if priority:
                print(f"  [{done}/{len(rows)}] packet {packet_id}: PRIORITY — {result['applicant_snippet']}")
            else:
                print(f"  [{done}/{len(rows)}] packet {packet_id}: {result['review_method']}")

    conn.close()
    print(f"done — {priority_hits} priority hit(s); methods: {method_counts}")


if __name__ == "__main__":
    main()
