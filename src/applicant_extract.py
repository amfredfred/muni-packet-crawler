"""
Extracts structured applicant contact records (name / email / address /
phone / property address / summary) from a packet's PDF and any PDFs it
links to.

Built directly on pdf_review.py's fetch/native-text/OCR/link-following
primitives (reused, not duplicated) — this module adds the field-parsing
layer on top: turning raw extracted text into structured records, and
applying the standing rule that an entry with no email is not a valid
applicant record and gets dropped before it's ever stored.

Field parsing uses three fallback heuristics, same as the standalone
civic-agenda-extractor prototype this was ported from:
  1. Plain labeled forms ("Name: ... Email: ... Phone: ...")
  2. OpenGov-style permit/license record layouts ("Applicant" heading
     followed by a name/phone/email block)
  3. A generic fallback: for every email found anywhere in the text, grab
     a few lines of surrounding context and take a best-effort guess at
     the name/phone. This is the noisiest tier — free-text correspondence
     (e.g. a forwarded email chain) can produce a name-guess that's really
     a sentence fragment, not an actual name. Treat low-confidence hits as
     leads to verify, not confirmed facts, same framing as OCR results
     elsewhere in this project.
"""

import io
import re

from pypdf import PdfReader

from pdf_review import (
    MIN_NATIVE_TEXT_CHARS,
    extract_links_from_pdf,
    extract_ocr_text,
    fetch_pdf_bytes,
)

MAX_LINKED_DOCS = 25  # hard cap so a busy packet can't trigger a runaway crawl

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})")
PROPERTY_ADDRESS_RE = re.compile(r"Property\s+Address:\s*(?P<val>.+?)\s*\n", re.I)
SUMMARY_RE = re.compile(r"Summary(?:/Description)?:\s*(?P<val>.+?)\s*\n", re.I)

# Same keyword set pdf_review.py's APPLICANT_OWNER_RE already gates on for
# the priority-flag check — reused here for the fallback tier specifically:
# an email with no labeled form and no OpenGov "Applicant" heading is only
# worth keeping if the surrounding text actually calls this person an
# applicant/owner/licensee, not just anyone whose email happens to be near
# some text (a memo's staff sign-off, a CC'd notary, a prior correspondent
# in a forwarded thread — all real false positives seen in testing).
APPLICANT_KEYWORD_RE = re.compile(
    r"applicant(?:'s)?|property\s+owner|business\s+owner|licensee|owner|"
    r"dba|doing\s+business\s+as|permit|license\b",
    re.I,
)


# ---- field parsing heuristics -------------------------------------------

def _parse_labeled_form(text: str, source_url: str) -> list[dict]:
    out = []
    for m in re.finditer(
        r"Name:\s*(?P<name>.+?)\s*\n"
        r"(?:.*\n)?Email:\s*(?P<email>[^\s\n]*)\s*\n?"
        r"(?:.*Address:\s*(?P<address>.+?)\s*\n)?"
        r"(?:.*Phone:\s*(?P<phone>.+?)\s*\n)?",
        text,
    ):
        d = m.groupdict()
        if not d.get("email"):
            continue
        # Grab the nearest Property Address:/Summary: labels within a
        # generous window after this match — these forms don't always put
        # them immediately adjacent to Name/Email, so search the whole
        # document rather than just this regex's own match span.
        tail = text[m.end():m.end() + 2000]
        prop_m = PROPERTY_ADDRESS_RE.search(tail)
        summary_m = SUMMARY_RE.search(tail)
        out.append({
            "name": (d.get("name") or "").strip(),
            "email": (d.get("email") or "").strip(),
            "address": (d.get("address") or "").strip(),
            "phone": (d.get("phone") or "").strip(),
            "property_address": (prop_m.group("val").strip() if prop_m else ""),
            "summary": (summary_m.group("val").strip() if summary_m else ""),
            "source_url": source_url,
        })
    return out


def _parse_opengov_style(text: str, source_url: str) -> list[dict]:
    out = []
    if "Applicant" not in text:
        return out
    for m in re.finditer(r"Applicant\s*\n(.{0,400})", text, re.DOTALL):
        window = m.group(1)
        email_m = EMAIL_RE.search(window)
        if not email_m:
            continue
        lines = [l.strip() for l in window.splitlines() if l.strip()]
        name = lines[0] if lines else ""
        phone_m = PHONE_RE.search(window)
        # OpenGov records commonly print the event/request description right
        # after the contact block under a "Describe..."/"Summary" label, and
        # the property/site address under its own label further down —
        # widen the search past the fixed 400-char contact window for these.
        wide_window = text[m.start():m.start() + 3000]
        prop_m = PROPERTY_ADDRESS_RE.search(wide_window)
        summary_m = SUMMARY_RE.search(wide_window)
        out.append({
            "name": name,
            "email": email_m.group(0),
            "address": "",
            "phone": phone_m.group(0) if phone_m else "",
            "property_address": (prop_m.group("val").strip() if prop_m else ""),
            "summary": (summary_m.group("val").strip() if summary_m else ""),
            "source_url": source_url,
        })
    return out


def _parse_fallback_by_email(text: str, source_url: str) -> list[dict]:
    """Last-resort tier: only fires when there's no labeled form and no
    OpenGov "Applicant" heading. Gated on APPLICANT_KEYWORD_RE within a
    wider window (not just the immediate context lines) so a bare email
    with no applicant/owner/licensee/permit language nearby — a staff
    sign-off, a CC'd notary, a name in a forwarded correspondence chain —
    is not treated as an applicant just because it's the closest email in
    the document. This is the single biggest source of false positives
    seen in testing (e.g. town staff emails inside status memos getting
    reported as "applicants")."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for email in EMAIL_RE.findall(line):
            wide_context = lines[max(0, i - 8):i + 8]
            if not APPLICANT_KEYWORD_RE.search("\n".join(wide_context)):
                continue
            context = lines[max(0, i - 3):i + 3]
            name_guess = next((l.strip() for l in context
                                if l.strip() and "@" not in l
                                and not PHONE_RE.search(l)), "")
            phone_m = next((PHONE_RE.search(l) for l in context
                             if PHONE_RE.search(l)), None)
            prop_m = PROPERTY_ADDRESS_RE.search("\n".join(wide_context) + "\n")
            summary_m = SUMMARY_RE.search("\n".join(wide_context) + "\n")
            out.append({
                "name": name_guess,
                "email": email,
                "address": "",
                "phone": phone_m.group(0) if phone_m else "",
                "property_address": (prop_m.group("val").strip() if prop_m else ""),
                "summary": (summary_m.group("val").strip() if summary_m else ""),
                "source_url": source_url,
            })
    return out


def parse_applicants(text: str, source_url: str) -> list[dict]:
    return (_parse_labeled_form(text, source_url)
            or _parse_opengov_style(text, source_url)
            or _parse_fallback_by_email(text, source_url))


# ---- driver ---------------------------------------------------------------

def _page_texts(pdf_bytes: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return [page.extract_text() or "" for page in reader.pages]
    except Exception:
        return []


def _get_text(pdf_bytes: bytes) -> tuple[str, str]:
    """Returns (text, method) where method is 'native', 'ocr', 'mixed', or
    'none'.

    Checked per-page, not on the whole-document total: a PDF that mixes a
    real text page (e.g. a plain activity log) with pages rendered as
    vector paths/images (e.g. a styled application form with no actual text
    layer) can clear a whole-document character threshold on the strength
    of the one text-bearing page alone, silently hiding the fact that the
    page that actually names the applicant has zero extractable text. Any
    page below MIN_NATIVE_TEXT_CHARS triggers OCR for the whole document,
    merged with whatever native text was already found."""
    pages = _page_texts(pdf_bytes)
    native = "\n".join(pages)
    all_pages_have_text = bool(pages) and all(
        len(p.strip()) >= MIN_NATIVE_TEXT_CHARS for p in pages
    )
    if all_pages_have_text:
        return native, "native"

    ocr = extract_ocr_text(pdf_bytes)
    if ocr and len(ocr.strip()) >= MIN_NATIVE_TEXT_CHARS:
        if len(native.strip()) >= MIN_NATIVE_TEXT_CHARS:
            return (native + "\n" + ocr).strip(), "mixed"
        return ocr, "ocr"

    if len(native.strip()) >= MIN_NATIVE_TEXT_CHARS:
        return native, "native"
    return "", "none"


def extract_applicants_for_packet(
    url: str, recursive: bool = False, max_docs: int = MAX_LINKED_DOCS
) -> tuple[list[dict], list[str]]:
    """Returns (applicant_records, errors).

    Follows the packet's own linked PDFs one level deep by default (its
    direct attachments/exhibits). recursive=True additionally follows links
    found inside those too — still capped at max_docs total documents
    visited, same safety net as the standalone prototype this was ported
    from.

    Standing rule applied here (not left to callers): records with no email
    are dropped, and what's left is deduped by email and sorted by it.
    """
    seen = {url}
    queue: list[tuple[str, int]] = [(url, 0)]
    raw_results: list[dict] = []
    errors: list[str] = []

    while queue:
        if len(seen) > max_docs:
            errors.append(f"hit max_docs ({max_docs}), stopped following links")
            break
        cur_url, depth = queue.pop(0)
        pdf_bytes = fetch_pdf_bytes(cur_url)
        if pdf_bytes is None:
            errors.append(f"failed to fetch {cur_url}")
            continue

        text, method = _get_text(pdf_bytes)
        if text:
            found = parse_applicants(text, cur_url)
            if method in ("ocr", "mixed"):
                for a in found:
                    a["summary"] = (a.get("summary", "") + " [OCR-extracted, verify]").strip()
            raw_results.extend(found)

        if depth == 0 or recursive:
            for link in extract_links_from_pdf(pdf_bytes, cur_url):
                if link in seen:
                    continue
                seen.add(link)
                queue.append((link, depth + 1))

    valid = [a for a in raw_results if a.get("email")]
    by_email: dict[str, dict] = {}
    for a in valid:
        by_email.setdefault(a["email"], a)
    deduped = sorted(by_email.values(), key=lambda a: a["email"].lower())
    return deduped, errors
