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
from pathlib import Path

import requests
from pypdf import PdfReader

MAX_PDF_BYTES = 25 * 1024 * 1024  # skip absurdly large files rather than hang on them
DOWNLOAD_TIMEOUT = 30
MIN_NATIVE_TEXT_CHARS = 40  # below this, treat native extraction as "no text layer"
MAX_OCR_PAGES = 10  # cap OCR cost on long scanned documents

# Tesseract/Poppler are looked for on PATH first (shutil.which), but the
# installer doesn't necessarily add itself to PATH (it may install with
# UAC-elevated admin rights and never touch this user's PATH, or land in
# a per-user location that was never added to PATH in the first place).
# Both plausible install locations are checked as a fallback rather than
# requiring a PATH/environment-variable change, which would need
# re-verifying on every process launch anyway.
_TESSERACT_FALLBACK_EXES = [
    Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
    Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe",
]
# poppler-windows releases unzip into a version-numbered subfolder
# (poppler-XX.YY.Z/Library/bin/...) — globbed rather than hardcoded so a
# future reinstall/upgrade to a newer release doesn't silently break this.
_POPPLER_FALLBACK_BASE = Path.home() / "AppData" / "Local" / "Programs" / "poppler"


def _resolve_poppler_fallback_bin_dir() -> Path | None:
    if not _POPPLER_FALLBACK_BASE.is_dir():
        return None
    matches = sorted(_POPPLER_FALLBACK_BASE.glob("poppler-*/Library/bin/pdftoppm.exe"))
    return matches[-1].parent if matches else None

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

# A reasonably strict RFC-5322-ish match — good enough to tell "a real
# email address is present" from "no contact info at all" without trying
# to be a full validator (this is a save-gate heuristic, not a mail
# server's input validation).
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def find_emails(text: str) -> list[str]:
    seen = []
    for m in EMAIL_RE.finditer(text):
        addr = m.group(0)
        if addr not in seen:
            seen.append(addr)
    return seen


def has_applicant_and_contact(text: str) -> tuple[bool, list[str], list[str]]:
    """The stricter save-gate check: real applicant/owner info AND a real
    contact email, both present in the same document — not just one or the
    other. Returns (passes, applicant_matches, emails) so callers can log
    exactly what was found either way."""
    applicant_matches = find_applicant_owner_info(text)
    emails = find_emails(text)
    return bool(applicant_matches and emails), applicant_matches, emails


def _find_tesseract_exe() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _TESSERACT_FALLBACK_EXES:
        if candidate.exists():
            return str(candidate)
    return None


def _find_poppler_bin_dir() -> str | None:
    """None means "on PATH already, pdf2image doesn't need an explicit
    poppler_path"; otherwise the specific bin directory to pass it."""
    if shutil.which("pdftoppm"):
        return None
    fallback_dir = _resolve_poppler_fallback_bin_dir()
    return str(fallback_dir) if fallback_dir else None


def _tesseract_available() -> bool:
    return _find_tesseract_exe() is not None


def _poppler_available() -> bool:
    return shutil.which("pdftoppm") is not None or _resolve_poppler_fallback_bin_dir() is not None


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


EMBEDDED_PDF_LINK_RE = re.compile(r"https?://[^\s\"'<>)]+\.pdf\b", re.I)


def extract_links_from_pdf(pdf_bytes: bytes, source_url: str) -> list[str]:
    """Finds links to OTHER pdfs embedded inside this one — either real
    link annotations (clickable in a PDF viewer) or plain URLs printed as
    visible text (a lot of municipal agenda templates just print the
    packet's URL rather than hyperlinking it). Some municipalities'
    agendas point to a packet hosted at a separate URL that's never
    linked from any HTML page a normal crawl would visit — the only
    place it exists is inside the agenda PDF itself."""
    found: list[str] = []
    seen = {source_url}
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return found

    for page in reader.pages:
        try:
            annotations = page.get("/Annots") or []
            for annot_ref in annotations:
                annot = annot_ref.get_object()
                action = annot.get("/A")
                uri = action.get("/URI") if action else None
                if uri and re.search(r"\.pdf\b", uri, re.I) and uri not in seen:
                    seen.add(uri)
                    found.append(uri)
        except Exception:
            pass

        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        for m in EMBEDDED_PDF_LINK_RE.finditer(text):
            uri = m.group(0)
            if uri not in seen:
                seen.add(uri)
                found.append(uri)

    return found


def extract_ocr_text(pdf_bytes: bytes) -> str | None:
    tesseract_exe = _find_tesseract_exe()
    if not tesseract_exe or not _poppler_available():
        return None
    try:
        import pytesseract
        from pdf2image import convert_from_bytes

        pytesseract.pytesseract.tesseract_cmd = tesseract_exe
        convert_kwargs = {"first_page": 1, "last_page": MAX_OCR_PAGES}
        poppler_dir = _find_poppler_bin_dir()
        if poppler_dir:
            convert_kwargs["poppler_path"] = poppler_dir
        images = convert_from_bytes(pdf_bytes, **convert_kwargs)
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
