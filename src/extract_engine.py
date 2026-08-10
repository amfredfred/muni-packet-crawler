"""
Batch-extracts applicant contact records from each saved packet's PDF (and
any PDFs it links to) — see applicant_extract.py for the parsing logic.

Same single-writer-thread architecture as crawl_sites.py/review_packets.py
and for the same reason: worker threads only do network/PDF-parsing work
and return a result; this main loop is the only thing that ever touches the
database, since SQLite allows just one writer at a time even in WAL mode.
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# See webapp.py's matching comment: redirect before anything riskier than
# the stdlib gets imported, so a crash in a later import (applicant_extract,
# which pulls in pdf_review -> pypdf/pytesseract/pdf2image) lands in the log
# file instead of vanishing silently under pythonw.exe.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import redirect_console_if_headless
redirect_console_if_headless("extract_engine.log")

from applicant_extract import extract_applicants_for_packet
from db import get_connection


def process_one(packet_id: int, url: str, recursive: bool) -> tuple[int, list[dict], list[str], str | None]:
    try:
        applicants, errors = extract_applicants_for_packet(url, recursive=recursive)
        return packet_id, applicants, errors, None
    except Exception as e:
        return packet_id, [], [], str(e)[:300]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", help="Only extract for municipalities in this state")
    parser.add_argument("--board-type", help="Only extract for packets matching this exact board_type")
    parser.add_argument("--upcoming-only", action="store_true", help="Skip packets with no known date")
    parser.add_argument("--limit", type=int, help="Max packets to process this run")
    parser.add_argument("--reextract", action="store_true", help="Re-extract even if already extracted")
    parser.add_argument("--recursive", action="store_true",
                         help="Follow links found inside linked documents too, not just the packet's own")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent worker threads (default 8)")
    args = parser.parse_args()

    conn = get_connection()
    cur = conn.cursor()

    query = """
        SELECT p.id, p.url FROM packets p
        JOIN municipalities m ON p.municipality_id = m.id
    """
    where = []
    params: list = []
    if not args.reextract:
        where.append("p.extraction_status IS NULL")
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
    print(f"extracting applicants from {len(rows)} packets with {args.workers} workers"
          f"{' (recursive)' if args.recursive else ''}")

    found_total = 0
    extracted_packets = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_one, pid, url, args.recursive): pid for pid, url in rows}
        for future in as_completed(futures):
            packet_id, applicants, errors, error = future.result()

            cur.execute("DELETE FROM packet_applicants WHERE packet_id = ?", (packet_id,))
            for a in applicants:
                cur.execute(
                    """INSERT INTO packet_applicants
                       (packet_id, name, email, address, phone, property_address, summary, source_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (packet_id, a.get("name", ""), a["email"], a.get("address", ""),
                     a.get("phone", ""), a.get("property_address", ""), a.get("summary", ""),
                     a.get("source_url", "")),
                )
            status = "error" if error else ("extracted" if applicants else "no_applicants")
            cur.execute(
                "UPDATE packets SET extraction_status = ?, extracted_at = datetime('now') WHERE id = ?",
                (status, packet_id),
            )
            conn.commit()

            done += 1
            found_total += len(applicants)
            if applicants:
                extracted_packets += 1

            if error:
                print(f"  [{done}/{len(rows)}] packet {packet_id}: ERROR — {error}")
            elif applicants:
                emails = ", ".join(a["email"] for a in applicants)
                print(f"  [{done}/{len(rows)}] packet {packet_id}: {len(applicants)} applicant(s) — {emails}")
            else:
                note = f" ({len(errors)} fetch error(s))" if errors else ""
                print(f"  [{done}/{len(rows)}] packet {packet_id}: no applicants found{note}")

    conn.close()
    print(f"done — {found_total} applicant record(s) across {extracted_packets} packet(s) "
          f"(of {done} processed)")


if __name__ == "__main__":
    main()
