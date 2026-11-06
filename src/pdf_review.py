"""
Best-effort review of a packet's PDF: extract its text and look for
applicant/owner details (e.g. on a liquor-license application), which are
treated as higher priority for follow-up — a document naming a specific
applicant/owner who has presumably paid a fee is worth a human's attention
sooner than a routine agenda.

Text comes from pypdf's native extraction only: most of these PDFs are
generated from Word/HTML by the municipality's own software, so the text is
already embedded. A scanned image PDF has no text layer and simply reports
review_method 'no_text_found'. This whole module is a best-effort pass, not
a verified extraction; findings are leads to review, not confirmed facts.
"""

import io
import re

import requests
import urllib3
from pypdf import PdfReader

MAX_PDF_BYTES = 25 * 1024 * 1024  # skip absurdly large files rather than hang on them
DOWNLOAD_TIMEOUT = 30
MIN_NATIVE_TEXT_CHARS = 40  # below this, treat native extraction as "no text layer"

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

def fetch_pdf_bytes(url: str) -> bytes | None:
    try:
        with requests.get(url, headers=HEADERS, timeout=DOWNLOAD_TIMEOUT, stream=True) as resp:
            if resp.status_code != 200:
                return None
            content = resp.raw.read(MAX_PDF_BYTES + 1, decode_content=True)
    except (requests.RequestException, urllib3.exceptions.HTTPError):
        # Reading the raw stream raises urllib3's own errors, not requests'.
        # A connection that drops mid-download is one bad PDF, not a bad town.
        return None
    return None if len(content) > MAX_PDF_BYTES else content


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
    review_method is one of: 'download_failed', 'native', 'no_text_found'
    (no usable text layer, e.g. a scanned image PDF)."""
    pdf_bytes = fetch_pdf_bytes(url)
    if pdf_bytes is None:
        return {"review_method": "download_failed", "has_applicant_info": False, "applicant_snippet": None}

    native_text = extract_native_text(pdf_bytes)
    if len(native_text.strip()) < MIN_NATIVE_TEXT_CHARS:
        return {"review_method": "no_text_found", "has_applicant_info": False, "applicant_snippet": None}

    matches = find_applicant_owner_info(native_text)
    return {
        "review_method": "native",
        "has_applicant_info": bool(matches),
        "applicant_snippet": " | ".join(matches) if matches else None,
    }
