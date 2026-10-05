"""Slice 1a corpus check: every TXT, JSON and text-layer PDF in data/invoices/ (read-only)."""

from pathlib import Path

import pytest

from invoice_pipeline import ingestion
from invoice_pipeline.ingestion import ingest

CORPUS = Path(__file__).parent.parent / "data" / "invoices"
FILES = sorted(p for p in CORPUS.iterdir() if p.suffix in {".txt", ".json", ".pdf"})


def test_corpus_has_the_slice_1a_files():
    assert len(FILES) == 16  # 20 files minus 3 CSV and 1 XML (slice 1b)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_file_ingests_to_a_typed_invoice_offline(path, no_network):
    result = ingest(path)
    assert result.invoice is not None, result.unreadable_reason
    assert result.findings == [] and result.invoice.items
    assert result.missing_required == []  # no corpus document needs the Extraction Fallback
    assert result.invoice.source_path == path.name


def test_two_runs_produce_equal_typed_results(no_network):
    assert [ingest(p) for p in FILES] == [ingest(p) for p in FILES]


def test_documented_repairs_are_traced_and_only_there():
    repaired = {p.name: {r.field for r in ingest(p).repairs} for p in FILES}
    expected = {"invoice_date", "items[1].line_total"}
    assert repaired.pop("invoice_1012.txt") == repaired.pop("invoice_1012.pdf") == expected
    assert not any(repaired.values())


def test_only_the_explicit_field_creates_a_revision_marker():
    markers = {p.name: ingest(p).invoice.revision for p in FILES}
    assert markers.pop("invoice_1004_revised.json") == "R1"
    assert set(markers.values()) == {None}  # incl. the note "Revised invoice" and EUR-free text


@pytest.mark.parametrize(
    "a, b",
    [
        ("invoice_1011.pdf", "invoice_1011.txt"),
        ("invoice_1012.pdf", "invoice_1012.txt"),
        ("invoice_1013.json", "invoice_1013.pdf"),
        ("invoice_1004.json", "invoice_1004_revised.json"),
    ],
)
def test_mirrors_and_revisions_share_an_identity(a, b):
    assert ingest(CORPUS / a).invoice.identity() == ingest(CORPUS / b).invoice.identity()
    assert ingest(CORPUS / a).invoice.identity() is not None


def test_only_the_missing_vendor_corpus_file_has_no_identity():
    absent = [p.name for p in FILES if ingest(p).invoice.identity() is None]
    assert absent == ["invoice_1009.json"]


@pytest.mark.skipif(".xml" not in ingestion._PARSERS, reason="XML ingestion lands in slice 1b")
def test_eur_metadata_is_preserved():
    assert ingest(CORPUS / "invoice_1014.xml").invoice.currency == "EUR"


def test_non_eur_files_default_to_usd():
    assert {ingest(p).invoice.currency for p in FILES} == {"USD"}
