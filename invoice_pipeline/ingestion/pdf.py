"""Text-layer PDF ingestion: pdfplumber text feeds the TXT parser; OCR is shelved outside v1."""

from pathlib import Path

import pdfplumber

from invoice_pipeline.ingestion.text import parse_text
from invoice_pipeline.model import FindingCode, Ingested, finding

NO_TEXT_LAYER = "PDF has no text layer; scanned invoices and OCR are shelved outside v1"


def parse_pdf(path: Path) -> Ingested:
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    stripped = text.strip()
    if len(stripped) < 50 or not any(c.isdigit() for c in stripped):
        return Ingested(
            invoice=None,
            findings=[finding(FindingCode.UNREADABLE_DOCUMENT, NO_TEXT_LAYER)],
            unreadable_reason=f"pdf: {NO_TEXT_LAYER}",
        )
    return parse_text(text, path.name, "pdf")
