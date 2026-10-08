"""Evidence guardrail and the fixed dot/index path resolver (pure, in-memory only)."""

import datetime as dt

import pytest

from invoice_pipeline.approval import (
    MalformedPath,
    PathError,
    UnresolvedPath,
    check_assessments,
    resolve_path,
)
from invoice_pipeline.model import (
    FindingCode,
    GuardrailCause,
    HistoryEntry,
    ToolCall,
    WarningAssessment,
)

PRICE = FindingCode.PRICE_DEVIATION
GOOD = ["invoice.items.0.unit_price", "references.reference_prices.WidgetA"]


def assessment(code=PRICE, line=0, explained=True, evidence=None, rationale="because"):
    return WarningAssessment(
        code=code,
        line=line,
        explained=explained,
        evidence=GOOD if evidence is None else evidence,
        rationale=rationale,
    )


def tool_call(index, name, arguments, result):
    return ToolCall(
        index=index,
        attempt=1,
        name=name,
        arguments=arguments,
        result=result,
        error=None,
        called_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        elapsed_ms=1,
    )


def causes(case_file, assessments, calls=()):
    return [f.cause for f in check_assessments(case_file, assessments, list(calls))]


@pytest.fixture
def case_file(make_case_file):
    return make_case_file(items=[("WidgetA", "300")])  # PRICE_DEVIATION line 0 only


def test_a_grounded_assessment_is_accepted(case_file):
    assert check_assessments(case_file, [assessment()], []) == []


def test_a_missing_assessment_is_correctable(case_file):
    (failure,) = check_assessments(case_file, [], [])
    assert failure.cause is GuardrailCause.MISSING_ASSESSMENT
    assert failure.correctable and "PRICE_DEVIATION line 0" in failure.where


@pytest.mark.parametrize(
    "wrong",
    [assessment(line=1), assessment(code=FindingCode.VENDOR_UNKNOWN, line=None)],
)
def test_an_assessment_for_a_different_warning_is_wrong_and_leaves_the_real_one_missing(
    case_file, wrong
):
    assert causes(case_file, [wrong]) == [
        GuardrailCause.WRONG_ASSESSMENT,
        GuardrailCause.MISSING_ASSESSMENT,
    ]


def test_a_duplicate_assessment_is_wrong(case_file):
    assert causes(case_file, [assessment(), assessment()]) == [GuardrailCause.WRONG_ASSESSMENT]


def test_unexplained_finality_is_not_correctable(case_file):
    (failure,) = check_assessments(case_file, [assessment(explained=False, evidence=[])], [])
    assert failure.cause is GuardrailCause.UNEXPLAINED and not failure.correctable


def test_empty_evidence_is_refused(case_file):
    assert causes(case_file, [assessment(evidence=[])]) == [GuardrailCause.EMPTY_EVIDENCE]


@pytest.mark.parametrize(
    "path",
    ["", "invoice..items", "invoice.items[0]", "open('/etc/passwd')", "../secret", "a b", "x;y"],
)
def test_malformed_paths_are_refused_and_never_evaluated(case_file, path):
    assert causes(case_file, [assessment(evidence=[path])]) == [GuardrailCause.MALFORMED_PATH]


@pytest.mark.parametrize(
    "path", ["invoice.items.9.sku", "invoice.vendor.name", "invoice.po_reference", "invoice", "x"]
)
def test_unresolved_paths_are_refused(case_file, path):
    assert causes(case_file, [assessment(evidence=[path])]) == [GuardrailCause.UNRESOLVED_EVIDENCE]


@pytest.mark.parametrize(
    "path", ["findings.0.detail", "checklist.PRICE_DEVIATION", "decision_context.0"]
)
def test_citing_only_the_finding_itself_is_self_evidence(case_file, path):
    assert causes(case_file, [assessment(evidence=[path])]) == [GuardrailCause.SELF_EVIDENCE]


def test_self_evidence_alongside_real_evidence_does_not_count_but_does_not_fail(case_file):
    assert causes(case_file, [assessment(evidence=[*GOOD, "findings.0.detail"])]) == []


def test_evidence_unrelated_to_the_warning_is_irrelevant(case_file):
    assert causes(case_file, [assessment(evidence=["invoice.invoice_number"])]) == [
        GuardrailCause.IRRELEVANT_EVIDENCE
    ]


def test_evidence_from_another_line_is_cross_line(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "300"), ("WidgetB", "500")])
    for bad in ("invoice.items.1.unit_price", "references.reference_prices.WidgetB"):
        assert causes(case_file, [assessment(evidence=[*GOOD, bad])]) == [
            GuardrailCause.CROSS_LINE_EVIDENCE
        ]


def test_tool_evidence_for_the_line_is_accepted(case_file):
    call = tool_call(0, "get_reference_price", {"sku": "WidgetA"}, {"unit_price": 250})
    ok = assessment(evidence=["tool.0.result.unit_price"])
    assert check_assessments(case_file, [ok], [call]) == []


def test_tool_evidence_about_another_vendor_is_cross_vendor(case_file):
    call = tool_call(
        0, "get_vendor_history", {"vendor_key": "other inc"}, {"entries": [], "total": 4}
    )
    bad = assessment(evidence=[*GOOD, "tool.0.result.total"])
    assert causes(case_file, [bad], [call]) == [GuardrailCause.CROSS_VENDOR_EVIDENCE]


def test_tool_evidence_about_another_sku_or_line_is_cross_line(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "300"), ("WidgetB", "500")])
    price = tool_call(0, "get_reference_price", {"sku": "WidgetB"}, {"unit_price": 500})
    line = tool_call(1, "get_invoice_line", {"n": 1}, {"found": True, "item": {"sku": "WidgetB"}})
    for path in ("tool.0.result.unit_price", "tool.1.result.found"):
        assert causes(case_file, [assessment(evidence=[*GOOD, path])], [price, line]) == [
            GuardrailCause.CROSS_LINE_EVIDENCE
        ]


def test_a_tool_path_to_a_missing_or_failed_call_is_unresolved(case_file):
    assert causes(case_file, [assessment(evidence=["tool.3.result.total"])]) == [
        GuardrailCause.UNRESOLVED_EVIDENCE
    ]


def test_history_in_another_currency_than_the_invoice_is_refused(make_case_file):
    history = [
        HistoryEntry(
            number="INV-1", total=None, currency="USD", state="paid", date=dt.date(2026, 1, 1)
        )
    ]
    case_file = make_case_file(items=[("WidgetA", "250")], currency="EUR", history=history)
    bad = assessment(
        code=FindingCode.CURRENCY_NON_USD,
        line=None,
        evidence=["invoice.currency", "vendor_history.0.currency"],
    )
    assert causes(case_file, [bad]) == [GuardrailCause.WRONG_HISTORY_CURRENCY]


def test_history_in_the_invoice_currency_is_accepted(make_case_file):
    history = [HistoryEntry(number="INV-1", total=None, currency="EUR", state="paid", date=None)]
    case_file = make_case_file(items=[("WidgetA", "250")], currency="EUR", history=history)
    ok = assessment(
        code=FindingCode.CURRENCY_NON_USD,
        line=None,
        evidence=["invoice.currency", "vendor_history.0.currency"],
    )
    assert causes(case_file, [ok]) == []


def test_failures_are_reported_in_a_deterministic_order(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "300")], vendor="Zeta Corp")
    first = causes(case_file, [])
    assert first == causes(case_file, [])
    assert first == [GuardrailCause.MISSING_ASSESSMENT] * 2


# --- resolver -------------------------------------------------------------------------------


def test_resolver_walks_dicts_lists_and_tool_results():
    root = {"a": {"b": [{"c": 5}]}, "empty": [], "none": None}
    calls = [tool_call(0, "get_stock_level", {"sku": "X"}, {"stock_level": "7"})]
    assert resolve_path(root, calls, "a.b.0.c") == 5
    assert resolve_path(root, calls, "tool.0.result.stock_level") == "7"
    assert resolve_path(root, calls, "tool.0.name") == "get_stock_level"


@pytest.mark.parametrize(
    "path", ["a.b.1.c", "a.x", "none", "empty", "a", "a.b.0.c.d", "tool.1.name"]
)
def test_resolver_rejects_values_that_name_nothing_concrete(path):
    root = {"a": {"b": [{"c": 5}]}, "empty": [], "none": None}
    with pytest.raises(UnresolvedPath):
        resolve_path(root, [], path)


@pytest.mark.parametrize("path", ["a.", ".a", "a[0]", "a b", "a.b()", "__import__('os')", "a/b"])
def test_resolver_rejects_malformed_paths(path):
    with pytest.raises(MalformedPath):
        resolve_path({"a": 1}, [], path)
    assert issubclass(MalformedPath, PathError)
