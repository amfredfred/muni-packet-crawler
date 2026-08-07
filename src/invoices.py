"""
Business invoice generation: commission-type fee templates, invoice/line-item
storage, and PDF rendering.

Design principle: totals are always computed forward, never back-filled.
A commission type's line items are *rules* (percentage-of-base or a fixed
fee) applied to a base amount to produce a starting set of invoice_items;
after that, an invoice's subtotal/total is simply the sum of its own
invoice_items rows. Nothing here ever takes a target total and manufactures
line items to match it — if you want a different total, you edit the real
line items, and the total follows.
"""

import io
import re
import sqlite3
from datetime import date
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT

DEFAULT_ACCENT = "#2955a3"
MUTED = colors.HexColor("#666666")
LIGHT_BG = colors.HexColor("#f2f4f7")
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOGO_PATH = DATA_DIR / "logo.png"
LOGO_WATERMARK_PATH = DATA_DIR / "logo_watermark.png"
LOGO_MAX_DIMENSION = 800  # px, for the stored full-opacity copy
LOGO_BOX = (1.8 * inch, 0.9 * inch)  # fixed-size box the header logo is fitted into
WATERMARK_ALPHA = 0.09  # faint background mark, not meant to compete with real content
WATERMARK_WIDTH_FRACTION = 0.6  # of page width


def save_logo(file_stream) -> None:
    """Normalize an uploaded logo into two fixed files: a full-opacity PNG
    for the header, and a faded copy for the watermark. Re-encoding through
    Pillow (rather than saving the uploaded bytes as-is) both validates the
    upload is actually a real image and strips anything else the original
    file might carry (metadata, non-image payloads appended to the file)."""
    img = PILImage.open(file_stream)
    img.verify()
    file_stream.seek(0)
    img = PILImage.open(file_stream).convert("RGBA")
    img.thumbnail((LOGO_MAX_DIMENSION, LOGO_MAX_DIMENSION))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    img.save(LOGO_PATH, format="PNG")

    alpha = img.split()[3].point(lambda p: int(p * WATERMARK_ALPHA))
    watermark = img.copy()
    watermark.putalpha(alpha)
    watermark.save(LOGO_WATERMARK_PATH, format="PNG")


def remove_logo() -> None:
    for path in (LOGO_PATH, LOGO_WATERMARK_PATH):
        path.unlink(missing_ok=True)


def has_logo() -> bool:
    return LOGO_PATH.exists()


def resolve_accent_color(theme_color: str | None) -> "colors.Color":
    # theme_color comes from a user-editable field (invoice or profile), so
    # validate the format before trusting it as a hex color rather than
    # letting a malformed value raise inside reportlab mid-render.
    if theme_color and HEX_COLOR_RE.match(theme_color):
        return colors.HexColor(theme_color)
    return colors.HexColor(DEFAULT_ACCENT)


def get_business_profile(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM business_profile WHERE id = 1").fetchone()
    if not row:
        return {"name": "", "address": "", "phone": "", "email": "", "account_details": "", "theme_color": DEFAULT_ACCENT}
    cols = [c[0] for c in conn.execute("SELECT * FROM business_profile WHERE id = 1").description]
    return dict(zip(cols, row))


def save_business_profile(conn: sqlite3.Connection, name: str, address: str, phone: str, email: str, account_details: str = "", theme_color: str = "") -> None:
    conn.execute(
        """
        INSERT INTO business_profile (id, name, address, phone, email, account_details, theme_color)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, address=excluded.address,
            phone=excluded.phone, email=excluded.email,
            account_details=excluded.account_details,
            theme_color=excluded.theme_color,
            updated_at=CURRENT_TIMESTAMP
        """,
        (name, address, phone, email, account_details, theme_color or None),
    )
    conn.commit()


def next_invoice_number(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(id) FROM invoices").fetchone()
    next_id = (row[0] or 0) + 1
    return f"INV-{next_id:05d}"


def compute_items_from_commission_type(conn: sqlite3.Connection, commission_type_id: int, base_amount: float) -> list[dict]:
    """Evaluate a commission type's rules against base_amount. Percentage
    rules are value% of base_amount; fixed rules use value as-is. This is
    a starting point for the invoice's real, editable line items — not
    the invoice's final total (that's always the sum of invoice_items)."""
    rows = conn.execute(
        "SELECT description, calc_type, value FROM commission_type_items "
        "WHERE commission_type_id = ? ORDER BY sort_order, id",
        (commission_type_id,),
    ).fetchall()
    items = []
    for description, calc_type, value in rows:
        if calc_type == "percentage":
            amount = round(base_amount * (value / 100.0), 2)
        else:
            amount = round(value, 2)
        items.append({"description": description, "amount": amount})
    return items


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _header_logo_flowable():
    if not LOGO_PATH.exists():
        return None
    with PILImage.open(LOGO_PATH) as im:
        src_w, src_h = im.size
    max_w, max_h = LOGO_BOX
    scale = min(max_w / src_w, max_h / src_h)
    logo = RLImage(str(LOGO_PATH), width=src_w * scale, height=src_h * scale)
    logo.hAlign = "CENTER"
    return logo


def _draw_watermark(canvas, doc):
    if not LOGO_WATERMARK_PATH.exists():
        return
    with PILImage.open(LOGO_WATERMARK_PATH) as im:
        src_w, src_h = im.size
    page_w, page_h = letter
    target_w = page_w * WATERMARK_WIDTH_FRACTION
    target_h = src_h * (target_w / src_w)
    x = (page_w - target_w) / 2
    y = (page_h - target_h) / 2
    canvas.saveState()
    canvas.drawImage(str(LOGO_WATERMARK_PATH), x, y, width=target_w, height=target_h, mask="auto")
    canvas.restoreState()


def generate_pdf(invoice: dict, items: list[dict]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.65 * inch, rightMargin=0.65 * inch,
    )
    accent = resolve_accent_color(invoice.get("theme_color"))

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("InvoiceTitle", parent=styles["Title"], textColor=accent, fontSize=26, spaceAfter=0)
    label_style = ParagraphStyle("Label", parent=styles["Normal"], textColor=MUTED, fontSize=8, spaceAfter=2, leading=10)
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=10, leading=14)
    meta_label_style = ParagraphStyle("MetaLabel", parent=styles["Normal"], fontSize=9, textColor=MUTED, alignment=TA_RIGHT)
    meta_value_style = ParagraphStyle("MetaValue", parent=styles["Normal"], fontSize=9, alignment=TA_RIGHT, fontName="Helvetica-Bold")

    flow = []
    logo_flowable = _header_logo_flowable()
    if logo_flowable:
        flow.append(logo_flowable)
        flow.append(Spacer(1, 10))
    flow.append(Paragraph("INVOICE", title_style))
    flow.append(Spacer(1, 4))
    flow.append(HRFlowable(width="100%", thickness=2, color=accent, spaceAfter=14))

    def block(label, name, address, phone, email):
        parts = [Paragraph(label, label_style)]
        if name:
            parts.append(Paragraph(f"<b>{name}</b>", body_style))
        if address:
            parts.append(Paragraph(address.replace("\n", "<br/>"), body_style))
        if phone:
            parts.append(Paragraph(f"Phone: {phone}", body_style))
        if email:
            parts.append(Paragraph(f"Email: {email}", body_style))
        return parts

    from_block = block("FROM", invoice.get("from_name"), invoice.get("from_address"),
                        invoice.get("from_phone"), invoice.get("from_email"))
    to_block = block("TO", invoice.get("to_name"), invoice.get("to_address"),
                      invoice.get("to_phone"), invoice.get("to_email"))

    meta_rows = [
        ("Invoice No.", invoice.get("invoice_number", "")),
        ("Invoice Date", invoice.get("invoice_date", "")),
        ("Due Date", invoice.get("due_date") or "—"),
        ("Payment Method", invoice.get("payment_method") or "—"),
    ]
    meta_flow = []
    for lbl, val in meta_rows:
        meta_flow.append(Paragraph(lbl, meta_label_style))
        meta_flow.append(Paragraph(str(val), meta_value_style))
        meta_flow.append(Spacer(1, 3))

    header_table = Table(
        [[from_block, to_block, meta_flow]],
        colWidths=[2.3 * inch, 2.3 * inch, 2.6 * inch],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    flow.append(header_table)
    flow.append(Spacer(1, 22))

    table_data = [["Description", "Amount"]]
    for item in items:
        table_data.append([item["description"], _money(item["amount"])])

    subtotal = round(sum(i["amount"] for i in items), 2)
    table_data.append(["Subtotal", _money(subtotal)])
    table_data.append(["Total", _money(subtotal)])

    n_items = len(items)
    item_table = Table(table_data, colWidths=[4.7 * inch, 1.5 * inch])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.white),
        ("GRID", (0, 0), (-1, n_items), 0.5, colors.HexColor("#dddddd")),
        ("LINEABOVE", (0, n_items + 1), (-1, n_items + 1), 1, colors.HexColor("#333333")),
        ("FONTNAME", (0, n_items + 2), (-1, n_items + 2), "Helvetica-Bold"),
        ("FONTSIZE", (0, n_items + 2), (-1, n_items + 2), 12),
        ("TEXTCOLOR", (0, n_items + 2), (-1, n_items + 2), accent),
        ("LINEABOVE", (0, n_items + 2), (-1, n_items + 2), 0.5, colors.HexColor("#dddddd")),
    ]
    for row in range(1, n_items + 1):
        if row % 2 == 0:
            style.append(("BACKGROUND", (0, row), (-1, row), LIGHT_BG))
    item_table.setStyle(TableStyle(style))
    flow.append(item_table)

    if invoice.get("account_details"):
        flow.append(Spacer(1, 20))
        flow.append(Paragraph("Account Details for Payment", label_style))
        flow.append(Paragraph(invoice["account_details"].replace("\n", "<br/>"), body_style))

    if invoice.get("notes"):
        flow.append(Spacer(1, 20))
        flow.append(Paragraph("Notes", label_style))
        flow.append(Paragraph(invoice["notes"].replace("\n", "<br/>"), body_style))

    doc.build(flow, onFirstPage=_draw_watermark, onLaterPages=_draw_watermark)
    return buf.getvalue()
