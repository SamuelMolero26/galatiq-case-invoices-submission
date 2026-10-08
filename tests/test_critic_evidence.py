"""Escalate-review evidence paths must name a concrete value, not a whole record or a null."""

from pydantic import BaseModel

from invoice_pipeline.critic import _is_case_file_path


class Line(BaseModel):
    sku: str


class Doc(BaseModel):
    total: int
    po_number: str | None = None
    lines: list[Line]


class Case(BaseModel):
    invoice: Doc
    findings: list[str]


CASE = Case(invoice=Doc(total=10, lines=[Line(sku="A")]), findings=[])


def test_scalar_and_list_values_are_evidence():
    for path in ("invoice.total", "invoice.lines.0.sku", "findings", "invoice.lines"):
        assert _is_case_file_path(CASE, path), path


def test_whole_records_nulls_and_unknown_paths_are_not_evidence():
    for path in ("invoice", "invoice.lines.0", "invoice.po_number", "invoice.po", "", "x.y"):
        assert not _is_case_file_path(CASE, path), path
