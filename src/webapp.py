"""
Local control panel for the muni-packet-crawler pipeline: a dashboard to
start/stop the crawl engine, and a browser for the municipalities/meetings/
packets tables with the ability to mark packets reviewed, force a
re-crawl, or manually add/delete a municipality.

Gated by single-admin TOTP (Google Authenticator-style) — see auth.py for
the registration/login model. First visit with no admin registered shows
the setup page (QR code); every visit after that requires a 6-digit code.

Run with: python src/webapp.py, then open http://localhost:5000
"""

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

# Redirect stdout/stderr (if headless — see db.redirect_console_if_headless)
# before anything riskier than the stdlib gets imported. Under pythonw.exe,
# sys.stdout/stderr start out as None; if a later import (flask, reportlab,
# playwright, ...) throws, Python's default unhandled-exception handler
# tries to print the traceback to stderr and, finding it None, fails
# silently too — the process just vanishes with no trace anywhere. Doing
# this first means any such crash lands in data/webapp.log instead.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import redirect_console_if_headless
redirect_console_if_headless("webapp.log")

import requests
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

import auth
import invoices as invoicing
from crawl_sites import BOARD_KEYWORDS
from db import get_connection

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY_PATH = BASE_DIR / "data" / ".flask_secret_key"
PAGE_SIZE = 50

RETRY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# Both background scripts this webapp can start/stop, keyed by the name
# used in URLs (/engine/<name>/start). Each gets its own subprocess slot and
# log file so the crawl and the OCR/applicant reviewer can run (or not)
# independently — starting one never touches the other's tracked state.
ENGINES = {
    "crawl": {
        "script": "crawl_sites.py",
        "log": BASE_DIR / "data" / "crawl_engine.log",
        "label": "Crawl Engine",
    },
    "review": {
        "script": "review_packets.py",
        "log": BASE_DIR / "data" / "review_engine.log",
        "label": "Review Engine (OCR / applicant check)",
    },
}
_engine_state: dict[str, dict] = {name: {"proc": None, "desc": ""} for name in ENGINES}


def _load_or_create_secret_key() -> bytes:
    """A hardcoded Flask secret_key would let anyone who's read this file
    forge the session cookie that marks a visitor as TOTP-authenticated —
    defeating the whole point of gating the app. Persisted here (not
    regenerated per run) so logged-in sessions survive a restart."""
    SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_bytes()
    key = os.urandom(32)
    SECRET_KEY_PATH.write_bytes(key)
    return key


app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = _load_or_create_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # logo uploads only; everything else here is small forms


@app.errorhandler(413)
def _upload_too_large(e):
    flash("That file is too large (max 8MB).")
    return redirect(url_for("invoices"))

_engine_proc: subprocess.Popen | None = None
_engine_desc: str = ""

_UNAUTHED_ENDPOINTS = {"setup", "login", "static"}


@app.before_request
def _require_auth():
    if request.endpoint in _UNAUTHED_ENDPOINTS:
        return None
    conn = get_connection()
    try:
        if not auth.is_registered(conn):
            return redirect(url_for("setup"))
    finally:
        conn.close()
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    return None


@app.route("/setup", methods=["GET", "POST"])
def setup():
    conn = get_connection()
    try:
        if auth.is_registered(conn):
            # Setup already completed — the only way back here is deleting
            # the auth_totp row directly in the database.
            return redirect(url_for("login"))

        if "pending_totp_secret" not in session:
            session["pending_totp_secret"] = auth.generate_secret()
        secret = session["pending_totp_secret"]

        if request.method == "POST":
            code = request.form.get("code", "")
            if auth.verify_code(secret, code):
                if auth.register_secret(conn, secret):
                    session.pop("pending_totp_secret", None)
                    session["authenticated"] = True
                    flash("Authenticator registered. You're in.")
                    return redirect(url_for("dashboard"))
                flash("Setup already completed by another request — log in instead.")
                return redirect(url_for("login"))
            flash("That code didn't match. Check your authenticator app and try again.")

        return render_template("setup.html", qr_data_uri=auth.qr_code_data_uri(secret), secret=secret)
    finally:
        conn.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = get_connection()
    try:
        if not auth.is_registered(conn):
            return redirect(url_for("setup"))
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            code = request.form.get("code", "")
            secret = auth.get_secret(conn)
            if auth.verify_code(secret, code):
                session["authenticated"] = True
                return redirect(url_for("dashboard"))
            flash("Invalid or expired code.")

        return render_template("login.html")
    finally:
        conn.close()


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))


def dict_rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_all_states() -> list[str]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT state FROM municipalities ORDER BY state")
    states = [r[0] for r in cur.fetchall()]
    conn.close()
    return states


def is_engine_running(name: str) -> bool:
    proc = _engine_state[name]["proc"]
    return proc is not None and proc.poll() is None


@app.route("/")
def dashboard():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT site_status, COUNT(*) FROM municipalities GROUP BY site_status ORDER BY 2 DESC")
    site_status_counts = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM packets")
    total_packets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM packets WHERE is_upcoming = 1")
    upcoming_packets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM packets WHERE is_upcoming IS NULL")
    unknown_date_packets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM packets WHERE reviewed = 1")
    reviewed_packets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM packets WHERE review_method IS NULL")
    unreviewed_ocr_packets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM packets WHERE priority = 1")
    priority_packets = cur.fetchone()[0]

    cur.execute(
        """
        SELECT state,
               COUNT(*) AS total,
               SUM(CASE WHEN site_status = 'ok' THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN site_status = 'no_packets_found' THEN 1 ELSE 0 END) AS no_packets_found,
               SUM(CASE WHEN site_status = 'no_agenda_found' THEN 1 ELSE 0 END) AS no_agenda_found,
               SUM(CASE WHEN site_status = 'error' THEN 1 ELSE 0 END) AS error,
               SUM(CASE WHEN site_status = 'unchecked' THEN 1 ELSE 0 END) AS unchecked
        FROM municipalities GROUP BY state ORDER BY total DESC
        """
    )
    state_table = dict_rows(cur)

    cur.execute("SELECT * FROM blocked_sources ORDER BY state, category")
    blocked_sources = dict_rows(cur)
    conn.close()

    engines = {}
    for name, cfg in ENGINES.items():
        log_tail = ""
        if cfg["log"].exists():
            lines = cfg["log"].read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(lines[-40:])
        proc = _engine_state[name]["proc"]
        engines[name] = {
            "label": cfg["label"],
            "running": is_engine_running(name),
            "pid": proc.pid if proc else None,
            "desc": _engine_state[name]["desc"],
            "log_tail": log_tail,
        }

    return render_template(
        "dashboard.html",
        site_status_counts=site_status_counts,
        total_packets=total_packets,
        upcoming_packets=upcoming_packets,
        unknown_date_packets=unknown_date_packets,
        reviewed_packets=reviewed_packets,
        unreviewed_ocr_packets=unreviewed_ocr_packets,
        priority_packets=priority_packets,
        state_table=state_table,
        all_states=get_all_states(),
        all_board_types=BOARD_KEYWORDS,
        engines=engines,
        blocked_sources=blocked_sources,
    )


@app.route("/engine/<name>/start", methods=["POST"])
def engine_start(name: str):
    if name not in ENGINES:
        flash("Unknown engine.")
        return redirect(url_for("dashboard"))
    if is_engine_running(name):
        flash(f"{ENGINES[name]['label']} is already running.")
        return redirect(url_for("dashboard"))

    script_path = Path(__file__).resolve().parent / ENGINES[name]["script"]
    args = [sys.executable, str(script_path)]
    desc_parts = []

    if name == "crawl":
        state = request.form.get("state", "").strip()
        limit = request.form.get("limit", "").strip()
        recheck = request.form.get("recheck") == "1"
        if state:
            args += ["--state", state]
            desc_parts.append(state)
        if limit:
            args += ["--limit", limit]
            desc_parts.append(f"limit {limit}")
        if recheck:
            args += ["--recheck"]
            desc_parts.append("recheck")
    elif name == "review":
        board_type = request.form.get("board_type", "").strip()
        limit = request.form.get("limit", "").strip()
        upcoming_only = request.form.get("upcoming_only") == "1"
        rereview = request.form.get("rereview") == "1"
        if board_type:
            args += ["--board-type", board_type]
            desc_parts.append(board_type)
        if limit:
            args += ["--limit", limit]
            desc_parts.append(f"limit {limit}")
        if upcoming_only:
            args += ["--upcoming-only"]
            desc_parts.append("upcoming only")
        if rereview:
            args += ["--rereview"]
            desc_parts.append("re-review")

    log_path = ENGINES[name]["log"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(args, stdout=log_file, stderr=subprocess.STDOUT, cwd=str(BASE_DIR))
    desc = ", ".join(desc_parts) or "default options"
    _engine_state[name] = {"proc": proc, "desc": desc}
    flash(f"Started {ENGINES[name]['label']} ({desc}).")
    return redirect(url_for("dashboard"))


@app.route("/engine/<name>/stop", methods=["POST"])
def engine_stop(name: str):
    if name not in ENGINES:
        flash("Unknown engine.")
        return redirect(url_for("dashboard"))
    if is_engine_running(name):
        _engine_state[name]["proc"].terminate()
        flash(f"{ENGINES[name]['label']} stopped.")
    else:
        flash(f"{ENGINES[name]['label']} was not running.")
    return redirect(url_for("dashboard"))


@app.route("/blocked-sources/<int:source_id>/retry", methods=["POST"])
def retry_blocked_source(source_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT url FROM blocked_sources WHERE id = ?", (source_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        flash("Source not found.")
        return redirect(url_for("dashboard"))
    url = row[0]

    try:
        resp = requests.get(url, headers=RETRY_HEADERS, timeout=15)
        if resp.status_code == 200 and len(resp.text) > 500:
            status = f"200 OK, {len(resp.text)} bytes — worth re-checking the parser by hand"
        elif resp.status_code in (403, 429):
            status = f"still blocked (HTTP {resp.status_code})"
        else:
            status = f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        status = f"still unreachable ({type(e).__name__})"

    cur.execute(
        "UPDATE blocked_sources SET last_status = ?, last_checked_at = datetime('now') WHERE id = ?",
        (status, source_id),
    )
    conn.commit()
    conn.close()
    flash(f"Retried {url} — {status}")
    return redirect(url_for("dashboard"))


@app.route("/municipalities")
def municipalities():
    state = request.args.get("state", "")
    status = request.args.get("status", "")
    q = request.args.get("q", "")
    page = max(1, request.args.get("page", 1, type=int))

    conn = get_connection()
    cur = conn.cursor()

    where = []
    params: list = []
    if state:
        where.append("state = ?")
        params.append(state)
    if status:
        where.append("site_status = ?")
        params.append(status)
    if q:
        where.append("name LIKE ?")
        params.append(f"%{q}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    cur.execute(f"SELECT COUNT(*) FROM municipalities {where_sql}", params)
    total = cur.fetchone()[0]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    cur.execute(
        f"SELECT * FROM municipalities {where_sql} ORDER BY state, name LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    rows = dict_rows(cur)
    conn.close()

    return render_template(
        "municipalities.html",
        rows=rows,
        all_states=get_all_states(),
        all_statuses=["unchecked", "ok", "no_packets_found", "no_agenda_found", "error"],
        state=state,
        status=status,
        q=q,
        page=page,
        total=total,
        total_pages=total_pages,
    )


@app.route("/municipalities/new", methods=["GET", "POST"])
def new_municipality():
    if request.method == "POST":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, ?, ?, ?, 'manual')
            ON CONFLICT(state, name) DO UPDATE SET official_url = excluded.official_url
            """,
            (request.form["name"].strip(), request.form["state"].strip(),
             request.form["place_type"], request.form["official_url"].strip()),
        )
        conn.commit()
        conn.close()
        flash(f"Added {request.form['name']}, {request.form['state']}.")
        return redirect(url_for("municipalities"))
    return render_template("new_municipality.html")


@app.route("/municipalities/<int:muni_id>")
def municipality_detail(muni_id: int):
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM municipalities WHERE id = ?", (muni_id,))
    m = dict_rows(cur)
    if not m:
        conn.close()
        flash("Municipality not found.")
        return redirect(url_for("municipalities"))
    m = m[0]

    where = ["municipality_id = ?"]
    params: list = [muni_id]
    if date_from:
        where.append("meeting_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("meeting_date <= ?")
        params.append(date_to)

    cur.execute(
        f"SELECT * FROM packets WHERE {' AND '.join(where)} ORDER BY meeting_date DESC",
        params,
    )
    packet_rows = dict_rows(cur)
    conn.close()

    return render_template(
        "municipality_detail.html", m=m, packets=packet_rows, date_from=date_from, date_to=date_to
    )


@app.route("/municipalities/<int:muni_id>/recheck", methods=["POST"])
def municipality_recheck(muni_id: int):
    conn = get_connection()
    conn.execute(
        "UPDATE municipalities SET site_status = 'unchecked', agenda_url = NULL, platform = NULL WHERE id = ?",
        (muni_id,),
    )
    conn.commit()
    conn.close()
    flash("Marked for re-check on next crawl.")
    return redirect(url_for("municipality_detail", muni_id=muni_id))


@app.route("/municipalities/<int:muni_id>/delete", methods=["POST"])
def municipality_delete(muni_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM meetings WHERE municipality_id = ?", (muni_id,))
    meeting_ids = [r[0] for r in cur.fetchall()]
    for mid in meeting_ids:
        cur.execute("DELETE FROM packets WHERE meeting_id = ?", (mid,))
    cur.execute("DELETE FROM meetings WHERE municipality_id = ?", (muni_id,))
    cur.execute("DELETE FROM municipalities WHERE id = ?", (muni_id,))
    conn.commit()
    conn.close()
    flash("Deleted.")
    return redirect(url_for("municipalities"))


@app.route("/packets")
def packets():
    state = request.args.get("state", "")
    which = request.args.get("which", "upcoming")
    reviewed = request.args.get("reviewed", "")
    board_type = request.args.get("board_type", "")
    priority_only = request.args.get("priority_only", "")
    within_days = request.args.get("within_days", "10")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    page = max(1, request.args.get("page", 1, type=int))

    where = []
    params: list = []
    if which == "upcoming":
        where.append("p.is_upcoming = 1")
        # Caps the view to a near-term window so a real but distant agenda
        # doesn't bury this week's — doesn't touch what's stored, so
        # widening/clearing this later needs no re-crawl. Only meaningful
        # for is_upcoming=1 rows (unknown-date rows have no meeting_date to
        # compare, and NULL <= X is never true in SQL, so this must not be
        # applied to the "unknown" branch — see list_upcoming.py's docstring
        # for the same gotcha).
        if within_days.strip().isdigit() and int(within_days) > 0:
            where.append("p.meeting_date <= date('now', ?)")
            params.append(f"+{int(within_days)} days")
    elif which == "unknown":
        where.append("p.is_upcoming IS NULL")
    # "all" -> no extra filter beyond the save-time exclusion of confirmed-past packets
    if state:
        where.append("m.state = ?")
        params.append(state)
    if reviewed in ("0", "1"):
        where.append("p.reviewed = ?")
        params.append(int(reviewed))
    if board_type:
        where.append("p.board_type = ?")
        params.append(board_type)
    if priority_only == "1":
        where.append("p.priority = 1")
    # Explicit date range — independent of the "which"/within_days quick
    # filters above, so e.g. "show me everything in September" is possible
    # regardless of upcoming/unknown/all. Same NULL caveat as within_days:
    # a packet with no parseable meeting_date can never match either bound,
    # so combining a date range with which=unknown always returns nothing —
    # that's correct SQL behavior, not a bug, just worth knowing.
    if date_from:
        where.append("p.meeting_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("p.meeting_date <= ?")
        params.append(date_to)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT COUNT(*) FROM packets p
        JOIN municipalities m ON p.municipality_id = m.id
        {where_sql}
        """,
        params,
    )
    total = cur.fetchone()[0]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    cur.execute(
        f"""
        SELECT p.*, m.name, m.state FROM packets p
        JOIN municipalities m ON p.municipality_id = m.id
        {where_sql}
        ORDER BY p.meeting_date IS NULL, p.meeting_date
        LIMIT ? OFFSET ?
        """,
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    rows = dict_rows(cur)
    conn.close()

    return render_template(
        "packets.html",
        rows=rows,
        all_states=get_all_states(),
        all_board_types=BOARD_KEYWORDS,
        state=state,
        which=which,
        reviewed=reviewed,
        board_type=board_type,
        priority_only=priority_only,
        within_days=within_days,
        date_from=date_from,
        date_to=date_to,
        page=page,
        total=total,
        total_pages=total_pages,
    )


@app.route("/packets/<int:packet_id>/toggle_reviewed", methods=["POST"])
def toggle_reviewed(packet_id: int):
    conn = get_connection()
    conn.execute("UPDATE packets SET reviewed = 1 - reviewed WHERE id = ?", (packet_id,))
    conn.commit()
    conn.close()
    next_url = request.form.get("next") or url_for("packets")
    return redirect(next_url)


@app.route("/packets/<int:packet_id>/mark_reviewed", methods=["POST"])
def mark_reviewed(packet_id: int):
    """Fired by JS when a packet's link is clicked (see packets.html/
    municipality_detail.html) — sets reviewed=1 outright rather than
    toggling, so opening the same link twice can't accidentally flip it
    back to unreviewed. Returns JSON (not a redirect) since this is called
    via fetch(), not a form submit — the click itself still navigates
    normally to the PDF in a new tab."""
    conn = get_connection()
    conn.execute("UPDATE packets SET reviewed = 1 WHERE id = ?", (packet_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.route("/invoices/preview", methods=["POST"])
def preview_new_invoice():
    """Backs the live preview pane on the invoice page: renders the
    current (possibly invalid) form state as a real PDF — same
    generate_pdf() the download uses, so the preview can never drift from
    the actual output. Nothing here touches a database; there isn't one
    for invoices."""
    try:
        parsed = invoicing.parse_invoice_json(request.form.get("invoice_json", ""))
    except ValueError as e:
        return {"error": str(e)}, 400
    f = parsed["fields"]

    logo_image = logo_watermark = None
    logo_file = request.files.get("logo")
    if logo_file and logo_file.filename:
        try:
            logo_image, logo_watermark = invoicing.process_logo_image(logo_file.stream)
        except Exception:
            pass

    fake_invoice = {
        "invoice_number": f["invoice_number"] or "PREVIEW",
        "invoice_date": f["invoice_date"] or date.today().isoformat(),
        "due_date": f["due_date"],
        "payment_method": f["payment_method"],
        "from_name": f["from_name"],
        "from_address": f["from_address"],
        "from_phone": f["from_phone"],
        "from_email": f["from_email"],
        "to_name": f["to_name"],
        "to_address": f["to_address"],
        "to_phone": f["to_phone"],
        "to_email": f["to_email"],
        "notes": f["notes"],
        "account_details": f["account_details"],
        "theme_color": f["theme_color"],
        "logo_image": logo_image,
        "logo_watermark": logo_watermark,
    }
    pdf_bytes = invoicing.generate_pdf(fake_invoice, parsed["items"])
    return Response(pdf_bytes, mimetype="application/pdf")


@app.route("/invoices", methods=["GET", "POST"])
def invoices():
    """The whole invoice feature is one page: fill in the JSON (see the
    "How to use it" tab), preview it live, and download the PDF — nothing
    is ever saved server-side, so there's no list of past invoices and no
    edit-an-existing-invoice flow. Re-download by re-pasting the same JSON
    whenever it's needed again.

    The POST here is called via fetch() (see invoice_form.html's
    downloadPdf()), not a plain form submission — the browser's own "Save
    As" picker (File System Access API) needs the PDF bytes in hand
    before it can offer one, which a native form POST's browser-driven
    download doesn't allow. Errors come back as JSON, same shape as the
    preview endpoint, rather than a flash+redirect."""
    if request.method == "POST":
        try:
            parsed = invoicing.parse_invoice_json(request.form.get("invoice_json", ""))
        except ValueError as e:
            return {"error": str(e)}, 400
        f = parsed["fields"]
        invoice_number = f["invoice_number"] or invoicing.suggest_invoice_number()

        logo_image = logo_watermark = None
        logo_file = request.files.get("logo")
        if logo_file and logo_file.filename:
            try:
                logo_image, logo_watermark = invoicing.process_logo_image(logo_file.stream)
            except Exception:
                pass  # not a valid image — generated without it, same as the preview endpoint

        invoice = {
            "invoice_number": invoice_number,
            "invoice_date": f["invoice_date"] or date.today().isoformat(),
            "due_date": f["due_date"],
            "payment_method": f["payment_method"],
            "from_name": f["from_name"],
            "from_address": f["from_address"],
            "from_phone": f["from_phone"],
            "from_email": f["from_email"],
            "to_name": f["to_name"],
            "to_address": f["to_address"],
            "to_phone": f["to_phone"],
            "to_email": f["to_email"],
            "notes": f["notes"],
            "account_details": f["account_details"],
            "theme_color": f["theme_color"],
            "logo_image": logo_image,
            "logo_watermark": logo_watermark,
        }
        pdf_bytes = invoicing.generate_pdf(invoice, parsed["items"])
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            # A literal, fixed name rather than "{invoice_number}.pdf" — the
            # Content-Disposition filename here always wins over the PDF's
            # own /Title metadata for a real (attachment) download, so it
            # has to be set explicitly rather than relying on that.
            headers={"Content-Disposition": "attachment; filename=INVOICE.pdf"},
        )

    default_json = json.dumps(invoicing.default_invoice_json(invoicing.suggest_invoice_number()), indent=2)
    example_json = json.dumps(invoicing.EXAMPLE_INVOICE_JSON, indent=2)
    return render_template("invoice_form.html", default_json=default_json, example_json=example_json)


def main() -> None:
    app.run(debug=False, port=5000)


if __name__ == "__main__":
    main()
