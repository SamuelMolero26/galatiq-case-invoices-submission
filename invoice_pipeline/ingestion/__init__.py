"""Deterministic Ingestion: one document in, one typed invoice (or Unreadable Document) out."""

from pathlib import Path

import pdfplumber

from invoice_pipeline.ingestion.structured import parse_json
from invoice_pipeline.ingestion.text import parse_text
from invoice_pipeline.model import FindingCode, Ingested, finding

NO_TEXT_LAYER = "PDF has no text layer; scanned invoices and OCR are shelved outside v1"
_MIN_CHARS = 50


def has_text_layer(text: str) -> bool:
    """Usable text: at least 50 stripped characters and at least one digit."""
    stripped = text.strip()
    return len(stripped) >= _MIN_CHARS and any(c.isdigit() for c in stripped)


def parse_pdf(path: Path) -> Ingested:
    """Text-layer PDF: pdfplumber text feeds the TXT parser; OCR is shelved outside v1."""
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if not has_text_layer(text):
        return Ingested(
            invoice=None,
            findings=[finding(FindingCode.UNREADABLE_DOCUMENT, NO_TEXT_LAYER)],
            unreadable_reason=f"pdf: {NO_TEXT_LAYER}",
        )
    return parse_text(text, path.name, "pdf")


def _unreadable(reason: str, raw_text: str | None = None) -> Ingested:
    return Ingested(
        invoice=None,
        findings=[finding(FindingCode.UNREADABLE_DOCUMENT, reason)],
        unreadable_reason=reason,
        raw_text=raw_text,
    )


def ingest(path: Path | str) -> Ingested:
    """Never raises: any failure becomes an Unreadable Document naming step, type and message."""
    path = Path(path)
    step = "route"
    try:
        suffix = path.suffix.lower()
        if suffix not in (".txt", ".json", ".pdf"):
            return _unreadable(f"route: unsupported file type {path.suffix!r}")
        step = "read"
        if not path.read_bytes().strip():
            return _unreadable("read: the file is empty")
        step = "parse"
        if suffix == ".txt":
            result = parse_text(path.read_text(encoding="utf-8"), path.name, "txt")
        elif suffix == ".json":
            result = parse_json(path.read_text(encoding="utf-8"), path.name)
        else:
            result = parse_pdf(path)
        if result.invoice is not None and not result.invoice.items:
            return _unreadable("parse: no line item could be recovered", result.raw_text)
        return result
    except Exception as exc:
        return _unreadable(f"{step}: {type(exc).__name__}: {exc}")
