from pathlib import Path

import pytest

from invoice_pipeline.ingestion import pdf as pdf_module
from invoice_pipeline.ingestion.pdf import has_text_layer, parse_pdf
from invoice_pipeline.ingestion.structured import parse_json
from invoice_pipeline.ingestion.text import parse_text
from invoice_pipeline.model import FindingCode

CORPUS = Path(__file__).parent.parent / "data" / "invoices"

# A one-page PDF with no content stream: pdfplumber finds no text, like a scan with no text layer.
BLANK_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R/Size 4>>\n%%EOF\n"
)


def shape(inv):
    return (
        inv.identity(),
        [(i.sku, i.quantity, i.unit_price, i.line_total, i.note) for i in inv.items],
        inv.total,
    )


@pytest.mark.parametrize("name", ["invoice_1011", "invoice_1012"])
def test_corpus_pdf_matches_its_text_mirror(name, no_network):
    from_pdf = parse_pdf(CORPUS / f"{name}.pdf")
    from_txt = parse_text((CORPUS / f"{name}.txt").read_text(), f"{name}.txt", "txt")
    assert from_pdf.invoice.source_format == "pdf"
    assert from_pdf.invoice.source_path == f"{name}.pdf"
    assert shape(from_pdf.invoice) == shape(from_txt.invoice)
    assert from_pdf.findings == [] and from_pdf.raw_text


def test_pdf_repairs_survive_the_text_layer_1012():
    fields = {r.field for r in parse_pdf(CORPUS / "invoice_1012.pdf").repairs}
    assert fields == {"invoice_date", "items[1].line_total"}


def test_pdf_mirrors_the_json_with_notes_and_inline_labels_1013():
    from_pdf = parse_pdf(CORPUS / "invoice_1013.pdf").invoice
    from_json = parse_json((CORPUS / "invoice_1013.json").read_text(), "invoice_1013.json").invoice
    assert shape(from_pdf) == shape(from_json)
    assert from_pdf.identity() == from_json.identity()


def test_pdf_without_a_text_layer_is_unreadable_with_no_raw_text(tmp_path, no_network):
    path = tmp_path / "scan.pdf"
    path.write_bytes(BLANK_PDF)
    result = parse_pdf(path)
    assert result.invoice is None and result.raw_text is None
    [finding] = result.findings
    assert finding.code is FindingCode.UNREADABLE_DOCUMENT
    for text in (finding.detail, result.unreadable_reason):
        assert "no text layer" in text and "OCR" in text


@pytest.mark.parametrize(
    "text, usable",
    [
        ("", False),
        ("   \n ", False),
        ("Invoice 1 " * 3, False),
        ("a" * 80, False),
        ("a1" * 30, True),
    ],
)
def test_usability_heuristic_is_fifty_chars_and_a_digit(text, usable):
    assert has_text_layer(text) is usable


def test_no_ocr_implementation_exists():
    modules = {p.stem for p in Path(pdf_module.__file__).parent.glob("*.py")}
    assert not {m for m in modules if "ocr" in m.lower()}
