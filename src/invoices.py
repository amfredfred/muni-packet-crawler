"""
Business invoice generation: parses a single JSON blob (fields + line
items) into a PDF. Nothing here is persisted anywhere — an invoice exists
only as the PDF it renders to; there's no database table, no history, no
edit-an-existing-invoice flow. Re-generate it from the same JSON (kept
wherever the user wants — their own notes, a saved file) if it's needed
again.

Design principle: totals are always computed forward, never back-filled.
An invoice's subtotal/total is simply the sum of its own line items —
nothing here ever takes a target total and manufactures line items to
match it. If you want a different total, you edit the real line items,
and the total follows.
"""

import io
import json
import re
import secrets
from datetime import date

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT

DEFAULT_ACCENT = "#2955a3"
MUTED = colors.HexColor("#666666")
LIGHT_BG = colors.HexColor("#f2f4f7")
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

LOGO_MAX_DIMENSION = 800  # px, for the stored full-opacity copy
LOGO_BOX = (2.8 * inch, 1.4 * inch)  # fixed-size box the header logo is fitted into
WATERMARK_ALPHA = 0.09  # faint background mark, not meant to compete with real content
WATERMARK_WIDTH_FRACTION = 0.6  # of page width


def process_logo_image(file_stream) -> tuple[bytes, bytes]:
    """Normalize an uploaded image into (full-opacity PNG bytes, faded
    watermark PNG bytes), stored as BLOBs on the invoice row. Re-encoding
    through Pillow (rather than keeping the uploaded bytes as-is) both
    validates the upload is actually a real image and strips anything else
    the original file might carry (metadata, non-image payloads appended
    to the file)."""
    img = PILImage.open(file_stream)
    img.verify()
    file_stream.seek(0)
    img = PILImage.open(file_stream).convert("RGBA")
    img.thumbnail((LOGO_MAX_DIMENSION, LOGO_MAX_DIMENSION))

    full_buf = io.BytesIO()
    img.save(full_buf, format="PNG")

    alpha = img.split()[3].point(lambda p: int(p * WATERMARK_ALPHA))
    watermark = img.copy()
    watermark.putalpha(alpha)
    watermark_buf = io.BytesIO()
    watermark.save(watermark_buf, format="PNG")

    return full_buf.getvalue(), watermark_buf.getvalue()


# The invoice page is a single JSON textarea rather than one input per
# field — these are the text fields that shape (see parse_invoice_json).
# invoice_number is included and overridable: leave the suggested value
# as-is or type your own; either way it's just letterhead text now, not
# a key into any stored record.
INVOICE_JSON_FIELDS = [
    "invoice_number",
    "from_name", "from_address", "from_phone", "from_email",
    "to_name", "to_address", "to_phone", "to_email",
    "invoice_date", "due_date", "payment_method",
    "theme_color", "account_details", "notes",
]


def default_invoice_json(suggested_invoice_number: str) -> dict:
    """Pre-fills the new-invoice textarea: the auto-generated invoice
    number (editable — leave it as-is to accept it, or type your own),
    empty strings for every other field, today's date, no items yet."""
    return {
        "invoice_number": suggested_invoice_number,
        "from_name": "",
        "from_address": "",
        "from_phone": "",
        "from_email": "",
        "to_name": "",
        "to_address": "",
        "to_phone": "",
        "to_email": "",
        "invoice_date": date.today().isoformat(),
        "due_date": "",
        "payment_method": "",
        "theme_color": DEFAULT_ACCENT,
        "account_details": "",
        "notes": "",
        "items": [],
    }


# A fully filled-in reference example for the "How to use it" tab — every
# field populated so pasting it as-is and downloading produces a real,
# complete invoice, not just a schema skeleton.
EXAMPLE_INVOICE_JSON = {
    "invoice_number": "INV-00042",
    "from_name": "Fred Amdev Realty Group",
    "from_address": "123 Main St, Suite 400\nSpringfield, ST 00000",
    "from_phone": "555-010-0100",
    "from_email": "billing@fredamdevrealty.com",
    "to_name": "Jordan Client",
    "to_address": "456 Oak Ave\nRiverside, ST 00001",
    "to_phone": "555-010-0200",
    "to_email": "jordan@example.com",
    "invoice_date": "2026-08-01",
    "due_date": "2026-08-15",
    "payment_method": "Bank transfer",
    "theme_color": "#2955a3",
    "account_details": "Bank: First State Bank\nRouting: 000000000\nAccount: 111111111",
    "notes": "Thank you for your business — please reference the invoice number with payment.",
    "items": [
        {"description": "Buyer's agent commission (2.5% of $350,000)", "amount": 8750.00},
        {"description": "Transaction coordination fee", "amount": 250.00},
        {"description": "Document processing", "amount": 75.00},
    ],
}


def parse_invoice_json(raw: str) -> dict:
    """Parses the posted JSON blob into {"fields": {...}, "items": [...]},
    raising ValueError with a human-readable message on anything malformed
    — invalid JSON, items that isn't a list, or fields of the wrong shape
    — rather than letting a raw JSONDecodeError/KeyError bubble up as a
    500. Individual malformed item rows (missing description/amount) are
    silently dropped rather than rejecting the whole invoice, matching how
    the old per-row form fields behaved."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON ({e})")
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")

    fields = {key: (str(data[key]).strip() or None) if data.get(key) not in (None, "") else None
              for key in INVOICE_JSON_FIELDS}

    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        raise ValueError('"items" must be a list')
    items = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        desc = str(entry.get("description", "")).strip()
        amount = entry.get("amount")
        if not desc or amount in (None, ""):
            continue
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        items.append({"description": desc, "amount": amount})

    return {"fields": fields, "items": items}


def resolve_accent_color(theme_color: str | None) -> "colors.Color":
    # theme_color comes from a user-editable field, so validate the format
    # before trusting it as a hex color rather than letting a malformed
    # value raise inside reportlab mid-render.
    if theme_color and HEX_COLOR_RE.match(theme_color):
        return colors.HexColor(theme_color)
    return colors.HexColor(DEFAULT_ACCENT)


def suggest_invoice_number() -> str:
    """A friendly starting point for the invoice_number field — invoices
    aren't stored anywhere, so there's no counter to increment; this just
    needs to look reasonable and be easy to edit, not be unique."""
    return f"INV-{date.today().strftime('%Y%m%d')}-{secrets.randbelow(90000) + 10000}"


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _header_logo_flowable(invoice: dict):
    source = invoice.get("logo_image")
    if source is None:
        return None
    with PILImage.open(io.BytesIO(source)) as im:
        src_w, src_h = im.size
    max_w, max_h = LOGO_BOX
    scale = min(max_w / src_w, max_h / src_h)
    # Unlike canvas.drawImage (used for the watermark below), the Platypus
    # Image flowable wants a raw file-like object, not an ImageReader —
    # wrapping it here raises a TypeError deep in reportlab's internals
    # (it tries os.path.splitext() on it, assuming a path/ImageReader-less
    # object).
    logo = RLImage(io.BytesIO(source), width=src_w * scale, height=src_h * scale)
    logo.hAlign = "CENTER"
    return logo


def _make_watermark_drawer(invoice: dict):
    """Returns a (canvas, doc) -> None callback for doc.build()'s
    onFirstPage/onLaterPages — those don't get a way to pass the invoice
    through directly, so this closes over it instead."""
    def _draw(canvas, doc):
        source = invoice.get("logo_watermark")
        if source is None:
            return
        with PILImage.open(io.BytesIO(source)) as im:
            src_w, src_h = im.size
        page_w, page_h = letter
        target_w = page_w * WATERMARK_WIDTH_FRACTION
        target_h = src_h * (target_w / src_w)
        x = (page_w - target_w) / 2
        y = (page_h - target_h) / 2
        canvas.saveState()
        canvas.drawImage(ImageReader(io.BytesIO(source)), x, y, width=target_w, height=target_h, mask="auto")
        canvas.restoreState()
    return _draw


def generate_pdf(invoice: dict, items: list[dict]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.65 * inch, rightMargin=0.65 * inch,
        # Embeds real PDF /Title metadata — browsers' built-in PDF viewers
        # (the live preview iframe included, since that's shown inline via
        # a blob: URL with no filename of its own) use this as the
        # suggested "Save As" filename, independent of the real download's
        # Content-Disposition header.
        title="INVOICE",
    )
    accent = resolve_accent_color(invoice.get("theme_color"))

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("InvoiceTitle", parent=styles["Title"], textColor=accent, fontSize=26, spaceAfter=0)
    label_style = ParagraphStyle("Label", parent=styles["Normal"], textColor=MUTED, fontSize=8, spaceAfter=2, leading=10)
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=10, leading=14)
    meta_label_style = ParagraphStyle("MetaLabel", parent=styles["Normal"], fontSize=9, textColor=MUTED, alignment=TA_RIGHT)
    meta_value_style = ParagraphStyle("MetaValue", parent=styles["Normal"], fontSize=9, alignment=TA_RIGHT, fontName="Helvetica-Bold")

    flow = []
    logo_flowable = _header_logo_flowable(invoice)
    if logo_flowable:
        flow.append(logo_flowable)
        flow.append(Spacer(1, 26))
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

    watermark_drawer = _make_watermark_drawer(invoice)
    doc.build(flow, onFirstPage=watermark_drawer, onLaterPages=watermark_drawer)
    return buf.getvalue()
