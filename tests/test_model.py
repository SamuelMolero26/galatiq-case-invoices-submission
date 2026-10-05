import pytest
from pydantic import ValidationError

from invoice_pipeline.model import SEVERITY, Finding, FindingCode, Severity, finding

REJECTION_RULES = {
    "VENDOR_BLOCKED",
    "ITEM_ZERO_STOCK",
    "ITEM_UNKNOWN",
    "QUANTITY_INVALID",
    "INCOMPLETE_IDENTITY",
}
REVIEW_TRIGGERS = {
    "STOCK_SHORTAGE",
    "RECONCILIATION_MISMATCH",
    "MISSING_REQUIRED_FIELD",
    "NONPOSITIVE_TOTAL",
    "VENDOR_LOOKALIKE",
    "CURRENCY_NO_RATE",
    "PARTIAL_IDENTITY",
    "REVISION_PAYMENT_DELTA",
    "UNREADABLE_DOCUMENT",
    "LLM_EXTRACTED",
}
WARNINGS = {"VENDOR_UNKNOWN", "PRICE_DEVIATION", "CURRENCY_NON_USD"}


def test_catalogue_is_exactly_the_specified_codes():
    assert {c.value for c in FindingCode} == REJECTION_RULES | REVIEW_TRIGGERS | WARNINGS


def test_every_code_maps_to_exactly_one_severity():
    assert set(SEVERITY) == set(FindingCode)
    for code in FindingCode:
        expected = (
            Severity.REJECTION_RULE
            if code in REJECTION_RULES
            else Severity.REVIEW_TRIGGER
            if code in REVIEW_TRIGGERS
            else Severity.WARNING
        )
        assert SEVERITY[code] is expected


def test_duplicate_payment_is_not_a_finding():
    assert "DUPLICATE_PAYMENT" not in {c.value for c in FindingCode}


def test_finding_constructor_derives_severity():
    f = finding(FindingCode.PRICE_DEVIATION, "unit price 300.00 vs 250.00", line=2)
    assert f == Finding(
        code=FindingCode.PRICE_DEVIATION,
        severity=Severity.WARNING,
        detail="unit price 300.00 vs 250.00",
        line=2,
    )
    assert finding(FindingCode.VENDOR_BLOCKED, "x").line is None


def test_callers_cannot_choose_severity():
    with pytest.raises(TypeError):
        finding(FindingCode.VENDOR_BLOCKED, "x", severity=Severity.WARNING)  # type: ignore[call-arg]


def test_finding_is_frozen():
    f = finding(FindingCode.ITEM_UNKNOWN, "WidgetC")
    with pytest.raises(ValidationError):
        f.detail = "changed"
