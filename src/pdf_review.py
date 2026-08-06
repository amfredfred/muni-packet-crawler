"""
Best-effort review of a packet's PDF: extract its text and look for
applicant/owner details (e.g. on a liquor-license application), which are
treated as higher priority for follow-up — a document naming a specific
applicant/owner who has presumably paid a fee is worth a human's attention
sooner than a routine agenda.

Two-tier extraction, in order:
  1. Native text extraction (pypdf) — the majority of these PDFs are
     generated from Word/HTML by the municipality's own software, so the
     text is already embedded and this is both faster and more accurate
     than OCR. No extra system dependency beyond the pypdf package.
  2. OCR (pytesseract + pdf2image), only as a fallback when native
     extraction yields near-nothing — i.e. the PDF is actually a scanned
     image. This requires two external system binaries this project does
     NOT install for you: the Tesseract OCR engine, and Poppler (for
     pdf2image's PDF->image rendering). If either is missing, OCR is
     skipped and review_method reports 'ocr_unavailable' rather than
     failing silently or crashing — per the "may not be trusted fully"
     framing, this whole module is explicitly a best-effort pass, not a
     verified extraction; findings should be treated as leads to review,
     not confirmed facts.
"""

import io
import re
import shutil

import requests
from pypdf import PdfReader

MAX_PDF_BYTES = 25 * 1024 * 1024  # skip absurdly large files rather than hang on them
DOWNLOAD_TIMEOUT = 30
MIN_NATIVE_TEXT_CHARS = 40  # below this, treat native extraction as "no text layer"
MAX_OCR_PAGES = 10  # cap OCR cost on long scanned documents

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

APPLICANT_OWNER_RE = re.compile(
    r"(applicant(?:'s)?\s*name|property\s+owner|business\s+owner|licensee|"
    r"applicant|owner|dba|doing\s+business\s+as)\s*[:\-]\s*([^\n\r]{2,80})",
    re.I,
)
MAX_SNIPPET_MATCHES = 5


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _poppler_available() -> bool:
    return shutil.which("pdftoppm") is not None


def fetch_pdf_bytes(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=DOWNLOAD_TIMEOUT, stream=True)
        if resp.status_code != 200:
            return None
        content = resp.raw.read(MAX_PDF_BYTES + 1, decode_content=True)
        if len(content) > MAX_PDF_BYTES:
            return None
        return content
    except requests.RequestException:
        return None


def extract_native_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def extract_ocr_text(pdf_bytes: bytes) -> str | None:
    if not (_tesseract_available() and _poppler_available()):
        return None
    try:
        import pytesseract
        from pdf2image import convert_from_bytes

        images = convert_from_bytes(pdf_bytes, first_page=1, last_page=MAX_OCR_PAGES)
        return "\n".join(pytesseract.image_to_string(img) for img in images)
    except Exception:
        return None


def find_applicant_owner_info(text: str) -> list[str]:
    matches = []
    seen = set()
    for label, value in APPLICANT_OWNER_RE.findall(text):
        snippet = f"{label.strip()}: {value.strip()}"
        if snippet not in seen:
            seen.add(snippet)
            matches.append(snippet)
        if len(matches) >= MAX_SNIPPET_MATCHES:
            break
    return matches


def review_pdf(url: str) -> dict:
    """Returns {review_method, has_applicant_info, applicant_snippet}.
    review_method is one of: 'download_failed', 'native', 'ocr',
    'ocr_unavailable' (native found nothing and OCR isn't set up on this
    machine), 'no_text_found' (both ran, neither found usable text)."""
    pdf_bytes = fetch_pdf_bytes(url)
    if pdf_bytes is None:
        return {"review_method": "download_failed", "has_applicant_info": False, "applicant_snippet": None}

    native_text = extract_native_text(pdf_bytes)
    if len(native_text.strip()) >= MIN_NATIVE_TEXT_CHARS:
        matches = find_applicant_owner_info(native_text)
        return {
            "review_method": "native",
            "has_applicant_info": bool(matches),
            "applicant_snippet": " | ".join(matches) if matches else None,
        }

    ocr_text = extract_ocr_text(pdf_bytes)
    if ocr_text is None:
        return {"review_method": "ocr_unavailable", "has_applicant_info": False, "applicant_snippet": None}

    matches = find_applicant_owner_info(ocr_text)
    if not matches and len(ocr_text.strip()) < MIN_NATIVE_TEXT_CHARS:
        return {"review_method": "no_text_found", "has_applicant_info": False, "applicant_snippet": None}
    return {
        "review_method": "ocr",
        "has_applicant_info": bool(matches),
        "applicant_snippet": " | ".join(matches) if matches else None,
    }
