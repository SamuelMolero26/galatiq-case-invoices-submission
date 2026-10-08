"""Focused evidence and structured Critic response boundary tests."""

import datetime as dt
import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from invoice_pipeline.approval import check_assessments
from invoice_pipeline.critic import _is_case_file_path, parse_assessor, parse_verifier
from invoice_pipeline.llm import CorrectableError
from invoice_pipeline.model import (
    ArrivalSummary,
    CaseFile,
    FindingCode,
    GuardrailCause,
    HistoryEntry,
    Invoice,
    LineItem,
    References,
    ToolCall,
    VerifyCheck,
    WarningAssessment,
    finding,
)


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


def test_dict_records_are_not_evidence_but_their_leaves_are():
    view = {"references": {"price_deviations": {}, "reference_prices": {"WidgetA": 250}}}
    assert not _is_case_file_path(view, "references.price_deviations")
    assert not _is_case_file_path(view, "references.reference_prices")
    assert _is_case_file_path(view, "references.reference_prices.WidgetA")


def _price_case() -> CaseFile:
    invoice = Invoice(
        invoice_number="INV-1",
        vendor="Widgets Inc.",
        invoice_date=date(2026, 1, 1),
        due_date_text=None,
        payment_terms=None,
        currency="USD",
        items=[
            LineItem(
                raw_name="Widget A",
                sku="WidgetA",
                raw_quantity="2",
                quantity=Decimal("2"),
                unit_price=Decimal("300"),
                line_total=Decimal("600"),
                note="rush order",
            ),
            LineItem(
                raw_name="Widget B",
                sku="WidgetB",
                raw_quantity="1",
                quantity=Decimal("1"),
                unit_price=Decimal("500"),
                line_total=Decimal("500"),
                note="standard order",
            ),
        ],
        subtotal=Decimal("1100"),
        tax=Decimal("0"),
        shipping=Decimal("0"),
        total=Decimal("1100"),
        notes="deliver together",
        po_reference="PO-1",
        source_path="invoice.json",
        source_format="json",
    )
    return CaseFile(
        invoice=invoice,
        findings=[finding(FindingCode.PRICE_DEVIATION, "20% above reference", line=0)],
        arrival=ArrivalSummary(kind="new"),
        references=References(
            reference_prices={"WidgetA": Decimal("250"), "WidgetB": Decimal("500")},
            stock_levels={"WidgetA": Decimal("15"), "WidgetB": Decimal("10")},
            aggregated_quantities={"WidgetA": Decimal("2"), "WidgetB": Decimal("1")},
            price_tolerance=Decimal("0.10"),
            price_deviations={0: Decimal("0.20"), 1: Decimal("0")},
            usd_equivalent=None,
            heightened_scrutiny_line=Decimal("10000"),
        ),
        vendor_history=[
            HistoryEntry(
                number="INV-OLD",
                total=Decimal("900"),
                currency="USD",
                state="paid",
                date=date(2025, 12, 1),
            )
        ],
        vendor_history_total=1,
    )


def _reference_price_call(
    *, sku: str = "WidgetA", found: bool = True, error: str | None = None
) -> ToolCall:
    return ToolCall(
        index=0,
        attempt=1,
        name="get_reference_price",
        arguments={"sku": sku},
        result={
            "sku": sku,
            "found": found,
            "unit_price": "250" if found else None,
        }
        if error is None
        else None,
        error=error,
        called_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        elapsed_ms=1,
    )


def _price_deviation_failures(path: str, tool_calls: list[ToolCall] | None = None):
    assessment = WarningAssessment(
        code=FindingCode.PRICE_DEVIATION,
        line=0,
        explained=True,
        evidence=[path],
        rationale="The invoice price differs from the reference price.",
    )
    return check_assessments(_price_case(), [assessment], tool_calls or [])


@pytest.mark.parametrize(
    "path",
    [
        "invoice.items.0.quantity",
        "invoice.items.0.line_total",
        "invoice.items.0.sku",
        "invoice.items.0.note",
        "invoice.notes",
        "vendor_history.0.total",
        "vendor_history_total",
        "references.stock_levels.WidgetA",
        "references.aggregated_quantities.WidgetA",
        "references.price_tolerance",
    ],
)
def test_price_deviation_rejects_unrelated_case_file_evidence(path):
    failures = _price_deviation_failures(path)

    assert [failure.cause for failure in failures] == [GuardrailCause.IRRELEVANT_EVIDENCE]


@pytest.mark.parametrize(
    "path",
    [
        "tool.0.name",
        "tool.0.arguments.sku",
        "tool.0.result.sku",
        "tool.0.result.found",
    ],
)
def test_price_deviation_rejects_tool_metadata_arguments_and_nonprice_results(path):
    failures = _price_deviation_failures(path, [_reference_price_call()])

    assert [failure.cause for failure in failures] == [GuardrailCause.IRRELEVANT_EVIDENCE]


def test_price_deviation_rejects_an_unsuccessful_reference_price_lookup():
    failures = _price_deviation_failures(
        "tool.0.result.found", [_reference_price_call(found=False)]
    )

    assert [failure.cause for failure in failures] == [GuardrailCause.IRRELEVANT_EVIDENCE]


def test_price_deviation_rejects_other_same_sku_tool_results():
    call = _reference_price_call().model_copy(
        update={
            "name": "get_stock_level",
            "result": {"sku": "WidgetA", "found": True, "stock_level": "15"},
        }
    )

    failures = _price_deviation_failures("tool.0.result.stock_level", [call])

    assert [failure.cause for failure in failures] == [GuardrailCause.IRRELEVANT_EVIDENCE]


@pytest.mark.parametrize(
    "path",
    [
        "invoice.items.0.unit_price",
        "references.reference_prices.WidgetA",
        "references.price_deviations.0",
    ],
)
def test_price_deviation_accepts_exact_authoritative_case_file_evidence(path):
    assert _price_deviation_failures(path) == []


def test_price_deviation_accepts_successful_matching_reference_price_result():
    failures = _price_deviation_failures(
        "tool.0.result.unit_price", [_reference_price_call()]
    )

    assert failures == []


@pytest.mark.parametrize(
    "path",
    [
        "invoice.items.1.unit_price",
        "references.reference_prices.WidgetB",
        "references.price_deviations.1",
    ],
)
def test_price_deviation_preserves_cross_line_evidence(path):
    failures = _price_deviation_failures(path)

    assert [failure.cause for failure in failures] == [GuardrailCause.CROSS_LINE_EVIDENCE]


def test_price_deviation_preserves_cross_line_tool_evidence():
    failures = _price_deviation_failures(
        "tool.0.result.unit_price", [_reference_price_call(sku="WidgetB")]
    )

    assert [failure.cause for failure in failures] == [GuardrailCause.CROSS_LINE_EVIDENCE]


def test_price_deviation_preserves_failed_tool_path_as_unresolved():
    failures = _price_deviation_failures(
        "tool.0.arguments.sku", [_reference_price_call(error="lookup failed")]
    )

    assert [failure.cause for failure in failures] == [GuardrailCause.UNRESOLVED_EVIDENCE]


@pytest.mark.parametrize(
    ("path", "cause"),
    [
        ("invoice.items[0].unit_price", GuardrailCause.MALFORMED_PATH),
        ("invoice.items.0.discount", GuardrailCause.UNRESOLVED_EVIDENCE),
    ],
)
def test_price_deviation_preserves_invalid_path_failures(path, cause):
    failures = _price_deviation_failures(path)

    assert [failure.cause for failure in failures] == [cause]


_BLANK_RATIONALES = ["", "   ", "\t\n"]


def _assessor_answer(rationale: str) -> str:
    return json.dumps(
        {
            "assessments": [
                {
                    "code": FindingCode.PRICE_DEVIATION,
                    "line": 0,
                    "explained": True,
                    "evidence": ["invoice.items.0.unit_price"],
                    "rationale": rationale,
                }
            ]
        }
    )


def _verifier_answer(rationale: str) -> str:
    return json.dumps(
        {
            "checks": [
                {
                    "code": FindingCode.PRICE_DEVIATION,
                    "line": 0,
                    "holds": True,
                    "rationale": rationale,
                }
            ]
        }
    )


@pytest.mark.parametrize("rationale", _BLANK_RATIONALES)
def test_assessor_blank_rationale_is_a_correctable_parsing_failure(rationale):
    parsed = parse_assessor(_assessor_answer(rationale))

    assert isinstance(parsed, CorrectableError)
    assert "field 'rationale'" in parsed.message


@pytest.mark.parametrize("rationale", _BLANK_RATIONALES)
def test_verifier_blank_rationale_is_a_correctable_parsing_failure(rationale):
    parsed = parse_verifier(
        _verifier_answer(rationale), [(FindingCode.PRICE_DEVIATION, 0)]
    )

    assert isinstance(parsed, CorrectableError)
    assert "field 'rationale'" in parsed.message


@pytest.mark.parametrize(
    ("model", "role_fields"),
    [
        (WarningAssessment, {"explained": True, "evidence": []}),
        (VerifyCheck, {"holds": True}),
    ],
    ids=["assessor", "verifier"],
)
@pytest.mark.parametrize("rationale", _BLANK_RATIONALES)
def test_role_models_reject_blank_rationales(model, role_fields, rationale):
    with pytest.raises(ValidationError, match="rationale"):
        model(
            code=FindingCode.PRICE_DEVIATION,
            line=0,
            rationale=rationale,
            **role_fields,
        )


def test_role_parsers_accept_nonblank_rationales():
    assessor = parse_assessor(_assessor_answer("The cited prices differ."))
    verifier = parse_verifier(
        _verifier_answer("The cited prices support the assessment."),
        [(FindingCode.PRICE_DEVIATION, 0)],
    )

    assert isinstance(assessor, list)
    assert assessor[0].rationale == "The cited prices differ."
    assert isinstance(verifier, list)
    assert verifier[0].rationale == "The cited prices support the assessment."
