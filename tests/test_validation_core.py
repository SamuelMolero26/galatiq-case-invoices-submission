import pytest

from invoice_pipeline.model import FindingCode, Severity
from invoice_pipeline.validation import validate
from tests.factories import codes, make_catalog, make_invoice, make_item

CATALOG = make_catalog()


def run(invoice):
    return validate(invoice, CATALOG)


def by_code(findings, code):
    return [f for f in findings if f.code == code]


def test_clean_invoice_has_no_findings():
    assert run(make_invoice()) == []


# --- identity -----------------------------------------------------------------


def test_both_identity_components_missing_is_a_rejection_rule():
    findings = run(make_invoice(vendor=None, number=None))
    assert codes(findings) == ["INCOMPLETE_IDENTITY"]
    assert findings[0].severity is Severity.REJECTION_RULE


def test_vendor_missing_only_is_partial_identity_without_vendor_findings():
    findings = run(make_invoice(vendor="  ", number="INV-2001"))
    assert codes(findings) == ["PARTIAL_IDENTITY"]
    assert "vendor missing" in findings[0].detail


def test_number_missing_only_is_partial_identity():
    findings = run(make_invoice(number=None))
    assert codes(findings) == ["PARTIAL_IDENTITY"]
    assert "invoice number missing" in findings[0].detail


def test_complete_identity_raises_no_identity_finding():
    assert not {"INCOMPLETE_IDENTITY", "PARTIAL_IDENTITY"} & set(codes(run(make_invoice())))


# --- quantity -----------------------------------------------------------------


@pytest.mark.parametrize("qty", ["0", "-5", "2.5"])
def test_zero_negative_or_fractional_quantity_is_invalid(qty):
    findings = by_code(run(make_invoice([make_item(qty=qty)])), FindingCode.QUANTITY_INVALID)
    assert len(findings) == 1
    assert findings[0].line == 0
    assert findings[0].severity is Severity.REJECTION_RULE
    assert qty in findings[0].detail


def test_non_numeric_quantity_keeps_raw_token_and_is_not_aggregated():
    item = make_item(qty="oneO", price="250.00", total=None)
    findings = run(make_invoice([item], total="250.00"))
    invalid = by_code(findings, FindingCode.QUANTITY_INVALID)
    assert len(invalid) == 1 and "oneO" in invalid[0].detail
    assert FindingCode.STOCK_SHORTAGE not in {f.code for f in findings}


def test_absent_quantity_is_invalid():
    item = make_item(qty=None, price="250.00", total=None)
    findings = run(make_invoice([item], total="250.00"))
    assert FindingCode.QUANTITY_INVALID in {f.code for f in findings}


# --- stock, items --------------------------------------------------------------


def test_shortage_on_one_line():
    findings = run(make_invoice([make_item("GadgetX", qty="20", price="750")]))
    assert codes(findings) == ["STOCK_SHORTAGE"]
    assert (
        "GadgetX" in findings[0].detail and "20" in findings[0].detail and "5" in findings[0].detail
    )


def test_shortage_only_visible_after_aggregation():
    items = [make_item(qty="15"), make_item(qty="5"), make_item(qty="2")]
    findings = by_code(run(make_invoice(items)), FindingCode.STOCK_SHORTAGE)
    assert len(findings) == 1 and "22" in findings[0].detail


def test_aggregate_within_stock_and_exact_stock_do_not_fire():
    assert run(make_invoice([make_item(qty="8"), make_item(qty="4")])) == []
    assert run(make_invoice([make_item("GadgetX", qty="5", price="750")])) == []


def test_zero_stock_item_is_a_rejection_rule_not_a_shortage():
    findings = run(make_invoice([make_item("FakeItem", qty="100", price="10")]))
    assert codes(findings) == ["ITEM_ZERO_STOCK"]
    assert findings[0].severity is Severity.REJECTION_RULE


def test_unknown_item_fires_once_per_item():
    items = [make_item("WidgetC", qty="1"), make_item("WidgetC", qty="2")]
    findings = by_code(run(make_invoice(items)), FindingCode.ITEM_UNKNOWN)
    assert len(findings) == 1 and "WidgetC" in findings[0].detail


def test_sku_matching_is_case_insensitive():
    assert run(make_invoice([make_item("widgeta", qty="2")])) == []


def test_stock_is_not_consumed_by_earlier_invoices():
    for _ in range(3):
        assert run(make_invoice([make_item("WidgetA", qty="10")])) == []
    assert CATALOG.stock["WidgetA"] == 15


# --- vendor -------------------------------------------------------------------


def test_blocked_vendor_is_a_rejection_rule_alongside_other_findings():
    findings = run(make_invoice([make_item("WidgetC")], vendor="Fraudster LLC"))
    assert {"VENDOR_BLOCKED", "ITEM_UNKNOWN"} <= set(codes(findings))
    assert by_code(findings, FindingCode.VENDOR_BLOCKED)[0].severity is Severity.REJECTION_RULE


@pytest.mark.parametrize("vendor", ["Shadow Traders", "Northwind Traders"])
def test_unknown_or_unlisted_vendor_is_a_warning(vendor):
    findings = run(make_invoice(vendor=vendor))
    assert codes(findings) == ["VENDOR_UNKNOWN"]
    assert findings[0].severity is Severity.WARNING


def test_trusted_vendor_with_formatting_differences_raises_nothing():
    assert run(make_invoice(vendor="  WIDGETS   inc. ")) == []
