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
import sqlite3
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT

ACCENT = colors.HexColor("#2955a3")
MUTED = colors.HexColor("#666666")
LIGHT_BG = colors.HexColor("#f2f4f7")


def get_business_profile(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM business_profile WHERE id = 1").fetchone()
    if not row:
        return {"name": "", "address": "", "phone": "", "email": ""}
    cols = [c[0] for c in conn.execute("SELECT * FROM business_profile WHERE id = 1").description]
    return dict(zip(cols, row))


def save_business_profile(conn: sqlite3.Connection, name: str, address: str, phone: str, email: str) -> None:
    conn.execute(
        """
        INSERT INTO business_profile (id, name, address, phone, email)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, address=excluded.address,
            phone=excluded.phone, email=excluded.email,
            updated_at=CURRENT_TIMESTAMP
        """,
        (name, address, phone, email),
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


def generate_pdf(invoice: dict, items: list[dict]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.65 * inch, rightMargin=0.65 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("InvoiceTitle", parent=styles["Title"], textColor=ACCENT, fontSize=26, spaceAfter=0)
    label_style = ParagraphStyle("Label", parent=styles["Normal"], textColor=MUTED, fontSize=8, spaceAfter=2, leading=10)
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=10, leading=14)
    meta_label_style = ParagraphStyle("MetaLabel", parent=styles["Normal"], fontSize=9, textColor=MUTED, alignment=TA_RIGHT)
    meta_value_style = ParagraphStyle("MetaValue", parent=styles["Normal"], fontSize=9, alignment=TA_RIGHT, fontName="Helvetica-Bold")

    flow = []
    flow.append(Paragraph("INVOICE", title_style))
    flow.append(Spacer(1, 4))
    flow.append(HRFlowable(width="100%", thickness=2, color=ACCENT, spaceAfter=14))

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
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
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
        ("TEXTCOLOR", (0, n_items + 2), (-1, n_items + 2), ACCENT),
        ("LINEABOVE", (0, n_items + 2), (-1, n_items + 2), 0.5, colors.HexColor("#dddddd")),
    ]
    for row in range(1, n_items + 1):
        if row % 2 == 0:
            style.append(("BACKGROUND", (0, row), (-1, row), LIGHT_BG))
    item_table.setStyle(TableStyle(style))
    flow.append(item_table)

    if invoice.get("notes"):
        flow.append(Spacer(1, 20))
        flow.append(Paragraph("Notes", label_style))
        flow.append(Paragraph(invoice["notes"].replace("\n", "<br/>"), body_style))

    doc.build(flow)
    return buf.getvalue()
