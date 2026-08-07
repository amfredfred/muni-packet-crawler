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

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth
import invoices as invoicing
from crawl_sites import BOARD_KEYWORDS
from db import get_connection, redirect_console_if_headless

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
    return redirect(url_for("business_profile"))

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


@app.route("/commission-types")
def commission_types():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM commission_types ORDER BY name")
    types = dict_rows(cur)
    for t in types:
        cur.execute(
            "SELECT * FROM commission_type_items WHERE commission_type_id = ? ORDER BY sort_order, id",
            (t["id"],),
        )
        t["rules"] = dict_rows(cur)
    conn.close()
    return render_template("commission_types.html", types=types)


@app.route("/commission-types/new", methods=["GET", "POST"])
def new_commission_type():
    if request.method == "POST":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO commission_types (name, description) VALUES (?, ?)",
            (request.form["name"].strip(), request.form.get("description", "").strip()),
        )
        type_id = cur.lastrowid
        descriptions = request.form.getlist("item_description")
        calc_types = request.form.getlist("item_calc_type")
        values = request.form.getlist("item_value")
        for i, (desc, calc_type, value) in enumerate(zip(descriptions, calc_types, values)):
            desc = desc.strip()
            if not desc or not value:
                continue
            cur.execute(
                "INSERT INTO commission_type_items (commission_type_id, description, calc_type, value, sort_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (type_id, desc, calc_type, float(value), i),
            )
        conn.commit()
        conn.close()
        flash(f"Created commission type \"{request.form['name']}\".")
        return redirect(url_for("commission_types"))
    return render_template("commission_type_form.html", ct=None)


@app.route("/commission-types/<int:type_id>/edit", methods=["GET", "POST"])
def edit_commission_type(type_id: int):
    conn = get_connection()
    cur = conn.cursor()
    if request.method == "POST":
        cur.execute(
            "UPDATE commission_types SET name = ?, description = ? WHERE id = ?",
            (request.form["name"].strip(), request.form.get("description", "").strip(), type_id),
        )
        cur.execute("DELETE FROM commission_type_items WHERE commission_type_id = ?", (type_id,))
        descriptions = request.form.getlist("item_description")
        calc_types = request.form.getlist("item_calc_type")
        values = request.form.getlist("item_value")
        for i, (desc, calc_type, value) in enumerate(zip(descriptions, calc_types, values)):
            desc = desc.strip()
            if not desc or not value:
                continue
            cur.execute(
                "INSERT INTO commission_type_items (commission_type_id, description, calc_type, value, sort_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (type_id, desc, calc_type, float(value), i),
            )
        conn.commit()
        conn.close()
        flash(f"Updated \"{request.form['name']}\".")
        return redirect(url_for("commission_types"))

    cur.execute("SELECT * FROM commission_types WHERE id = ?", (type_id,))
    ct = dict_rows(cur)
    if not ct:
        conn.close()
        flash("Commission type not found.")
        return redirect(url_for("commission_types"))
    ct = ct[0]
    cur.execute(
        "SELECT * FROM commission_type_items WHERE commission_type_id = ? ORDER BY sort_order, id",
        (type_id,),
    )
    ct["rules"] = dict_rows(cur)
    conn.close()
    return render_template("commission_type_form.html", ct=ct)


@app.route("/commission-types/<int:type_id>/delete", methods=["POST"])
def delete_commission_type(type_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM commission_type_items WHERE commission_type_id = ?", (type_id,))
    conn.execute("DELETE FROM commission_types WHERE id = ?", (type_id,))
    conn.commit()
    conn.close()
    flash("Commission type deleted.")
    return redirect(url_for("commission_types"))


@app.route("/invoices")
def invoices():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT i.*, (SELECT COALESCE(SUM(amount), 0) FROM invoice_items WHERE invoice_id = i.id) AS total
        FROM invoices i ORDER BY i.created_at DESC
        """
    )
    rows = dict_rows(cur)
    conn.close()
    return render_template("invoices.html", invoices=rows)


@app.route("/commission-types/<int:type_id>/compute")
def compute_commission_type(type_id: int):
    """JSON endpoint backing the invoice form's "Generate from commission
    type" button — returns the rule-computed items for a base amount so the
    page can drop them straight into its items table via JS, without a
    round-trip through invoice creation itself."""
    base_amount = float(request.args.get("base_amount") or 0)
    conn = get_connection()
    items = invoicing.compute_items_from_commission_type(conn, type_id, base_amount)
    conn.close()
    return {"items": items}


@app.route("/invoices/new", methods=["GET", "POST"])
def new_invoice():
    conn = get_connection()
    cur = conn.cursor()

    if request.method == "POST":
        profile = invoicing.get_business_profile(conn)
        invoice_number = invoicing.next_invoice_number(conn)
        commission_type_id = request.form.get("commission_type_id") or None
        base_amount = float(request.form.get("base_amount") or 0)

        cur.execute(
            """
            INSERT INTO invoices (
                invoice_number, invoice_date, due_date, payment_method,
                from_name, from_address, from_phone, from_email,
                to_name, to_address, to_phone, to_email,
                commission_type_id, base_amount, notes, account_details, theme_color
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_number,
                request.form.get("invoice_date") or date.today().isoformat(),
                request.form.get("due_date") or None,
                request.form.get("payment_method") or None,
                request.form.get("from_name") or profile.get("name"),
                request.form.get("from_address") or profile.get("address"),
                request.form.get("from_phone") or profile.get("phone"),
                request.form.get("from_email") or profile.get("email"),
                request.form.get("to_name") or None,
                request.form.get("to_address") or None,
                request.form.get("to_phone") or None,
                request.form.get("to_email") or None,
                commission_type_id,
                base_amount or None,
                request.form.get("notes") or None,
                request.form.get("account_details") or profile.get("account_details") or None,
                request.form.get("theme_color") or profile.get("theme_color") or None,
            ),
        )
        invoice_id = cur.lastrowid

        # Line items are submitted directly from the form's own items table
        # (populated manually, via the JSON paste box, or via the "Generate
        # from commission type" button — all client-side), not recomputed
        # here. This keeps invoice creation and editing on the same footing:
        # the total is always the sum of whatever rows were actually on the
        # page at submit time.
        descriptions = request.form.getlist("item_description")
        amounts = request.form.getlist("item_amount")
        for i, (desc, amount) in enumerate(zip(descriptions, amounts)):
            desc = desc.strip()
            if not desc or not amount:
                continue
            cur.execute(
                "INSERT INTO invoice_items (invoice_id, description, amount, sort_order) VALUES (?, ?, ?, ?)",
                (invoice_id, desc, float(amount), i),
            )
        conn.commit()
        conn.close()
        flash(f"Created {invoice_number}.")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    cur.execute("SELECT id, name FROM commission_types ORDER BY name")
    commission_type_options = dict_rows(cur)
    profile = invoicing.get_business_profile(conn)
    conn.close()
    return render_template(
        "invoice_form.html",
        commission_types=commission_type_options,
        profile=profile,
        today=date.today().isoformat(),
    )


@app.route("/invoices/<int:invoice_id>")
def invoice_detail(invoice_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
    inv = dict_rows(cur)
    if not inv:
        conn.close()
        flash("Invoice not found.")
        return redirect(url_for("invoices"))
    inv = inv[0]
    cur.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY sort_order, id", (invoice_id,))
    items = dict_rows(cur)
    conn.close()
    total = round(sum(i["amount"] for i in items), 2)
    return render_template("invoice_detail.html", inv=inv, items=items, total=total)


@app.route("/invoices/<int:invoice_id>/update", methods=["POST"])
def update_invoice(invoice_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE invoices SET
            invoice_date = ?, due_date = ?, payment_method = ?,
            from_name = ?, from_address = ?, from_phone = ?, from_email = ?,
            to_name = ?, to_address = ?, to_phone = ?, to_email = ?,
            notes = ?, account_details = ?, theme_color = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            request.form.get("invoice_date"),
            request.form.get("due_date") or None,
            request.form.get("payment_method") or None,
            request.form.get("from_name") or None,
            request.form.get("from_address") or None,
            request.form.get("from_phone") or None,
            request.form.get("from_email") or None,
            request.form.get("to_name") or None,
            request.form.get("to_address") or None,
            request.form.get("to_phone") or None,
            request.form.get("to_email") or None,
            request.form.get("notes") or None,
            request.form.get("account_details") or None,
            request.form.get("theme_color") or None,
            invoice_id,
        ),
    )

    cur.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    descriptions = request.form.getlist("item_description")
    amounts = request.form.getlist("item_amount")
    for i, (desc, amount) in enumerate(zip(descriptions, amounts)):
        desc = desc.strip()
        if not desc or not amount:
            continue
        cur.execute(
            "INSERT INTO invoice_items (invoice_id, description, amount, sort_order) VALUES (?, ?, ?, ?)",
            (invoice_id, desc, float(amount), i),
        )
    conn.commit()
    conn.close()
    flash("Invoice updated.")
    return redirect(url_for("invoice_detail", invoice_id=invoice_id))


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
def delete_invoice(invoice_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    conn.commit()
    conn.close()
    flash("Invoice deleted.")
    return redirect(url_for("invoices"))


@app.route("/invoices/<int:invoice_id>/pdf")
def invoice_pdf(invoice_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
    inv = dict_rows(cur)
    if not inv:
        conn.close()
        flash("Invoice not found.")
        return redirect(url_for("invoices"))
    inv = inv[0]
    cur.execute("SELECT description, amount FROM invoice_items WHERE invoice_id = ? ORDER BY sort_order, id", (invoice_id,))
    items = dict_rows(cur)
    conn.close()

    pdf_bytes = invoicing.generate_pdf(inv, items)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"inline; filename={inv['invoice_number']}.pdf"},
    )


@app.route("/business-profile", methods=["GET", "POST"])
def business_profile():
    conn = get_connection()
    if request.method == "POST":
        invoicing.save_business_profile(
            conn,
            request.form.get("name", "").strip(),
            request.form.get("address", "").strip(),
            request.form.get("phone", "").strip(),
            request.form.get("email", "").strip(),
            request.form.get("account_details", "").strip(),
            request.form.get("theme_color", "").strip(),
        )
        conn.close()

        logo_file = request.files.get("logo")
        if logo_file and logo_file.filename:
            try:
                invoicing.save_logo(logo_file.stream)
                flash("Business profile and logo saved.")
            except Exception:
                flash("Business profile saved, but that file doesn't look like a valid image — logo not updated.")
        else:
            flash("Business profile saved.")
        return redirect(url_for("business_profile"))
    profile = invoicing.get_business_profile(conn)
    conn.close()
    return render_template("business_profile.html", profile=profile, has_logo=invoicing.has_logo())


@app.route("/business-profile/logo")
def business_profile_logo():
    if not invoicing.has_logo():
        return "", 404
    return Response(invoicing.LOGO_PATH.read_bytes(), mimetype="image/png")


@app.route("/business-profile/logo/remove", methods=["POST"])
def remove_business_profile_logo():
    invoicing.remove_logo()
    flash("Logo removed.")
    return redirect(url_for("business_profile"))


def main() -> None:
    redirect_console_if_headless("webapp.log")
    app.run(debug=False, port=5000)


if __name__ == "__main__":
    main()
