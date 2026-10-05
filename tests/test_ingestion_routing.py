from pathlib import Path

import pytest

from invoice_pipeline import ingestion
from invoice_pipeline.approval import decide_unreadable
from invoice_pipeline.ingestion import ingest
from invoice_pipeline.model import FindingCode, Outcome

CORPUS = Path(__file__).parent.parent / "data" / "invoices"


def write(tmp_path, name, content: bytes | str) -> Path:
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


def assert_unreadable(result, *needles):
    assert result.invoice is None
    [finding] = result.findings
    assert finding.code is FindingCode.UNREADABLE_DOCUMENT
    assert result.unreadable_reason and finding.detail == result.unreadable_reason
    for needle in needles:
        assert needle in result.unreadable_reason
    return result


@pytest.mark.parametrize(
    "name, fmt",
    [
        ("invoice_1001.txt", "txt"),
        ("invoice_1004.json", "json"),
        ("invoice_1011.pdf", "pdf"),
    ],
)
def test_routes_by_suffix(name, fmt):
    result = ingest(CORPUS / name)
    assert result.invoice.source_format == fmt
    assert result.invoice.source_path == name


def test_suffix_routing_is_case_insensitive(tmp_path):
    path = write(tmp_path, "UPPER.JSON", (CORPUS / "invoice_1004.json").read_bytes())
    assert ingest(path).invoice.source_format == "json"


def test_unsupported_extension_is_unreadable_and_needs_review(tmp_path):
    result = assert_unreadable(ingest(write(tmp_path, "a.docx", "x")), "route", ".docx")
    assert decide_unreadable(result.findings).outcome is Outcome.NEEDS_REVIEW


@pytest.mark.parametrize(
    "name, content, needles",
    [
        ("corrupt.json", "{ not json", ("parse", "JSONDecodeError")),
        ("array.json", "[1, 2]", ("parse", "ValueError")),
        ("empty.txt", "", ("read", "empty")),
        ("blank.json", "  \n ", ("read", "empty")),
        ("empty.pdf", "", ("read", "empty")),
        ("bad.txt", b"\xff\xfe\x00 \xc3\x28", ("parse", "UnicodeDecodeError")),
        ("truncated.pdf", b"%PDF-1.4\n1 0 obj<</Type/Catalog", ("parse",)),
        ("noitems.txt", "Vendor: A\nInvoice: 1\nTotal: $5\n", ("parse", "line item")),
        ("noitems.json", '{"invoice_number": "1", "line_items": []}', ("parse", "line item")),
    ],
)
def test_corrupt_empty_and_unrecoverable_documents_are_unreadable(tmp_path, name, content, needles):
    assert_unreadable(ingest(write(tmp_path, name, content)), *needles)


def test_missing_file_does_not_raise(tmp_path):
    assert_unreadable(ingest(tmp_path / "ghost.txt"), "read", "FileNotFoundError")


def test_unexpected_parser_failure_names_step_type_and_message(tmp_path, monkeypatch):
    def boom(path):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(ingestion._PARSERS, ".txt", boom)
    assert_unreadable(ingest(write(tmp_path, "a.txt", "x")), "parse", "RuntimeError", "kaboom")


def test_missing_identity_alone_stays_a_typed_invoice(tmp_path):
    result = ingest(write(tmp_path, "a.txt", "WidgetA  qty: 2  unit price: $5.00\nTotal: $10.00\n"))
    assert result.invoice is not None and result.invoice.identity() is None
    assert result.findings == [] and result.missing_required == ["vendor", "invoice_number"]


def test_results_are_deterministic():
    assert ingest(CORPUS / "invoice_1012.pdf") == ingest(CORPUS / "invoice_1012.pdf")
    assert ingest(str(CORPUS / "invoice_1002.txt")) == ingest(CORPUS / "invoice_1002.txt")
