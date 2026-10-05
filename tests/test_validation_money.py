from decimal import Decimal

import pytest

from invoice_pipeline.model import FindingCode, Severity
from invoice_pipeline.validation import reconcile, validate
from tests.factories import codes, make_catalog, make_invoice, make_item

CATALOG = make_catalog()


def run(invoice):
    return validate(invoice, CATALOG)


def mismatches(invoice):
    return [f for f in run(invoice) if f.code == FindingCode.RECONCILIATION_MISMATCH]


def test_ties_out_with_subtotal_tax_and_shipping():
    items = [
        make_item("WidgetA", qty="10", price="250.00"),
        make_item("WidgetB", qty="8", price="500.00"),
    ]
    invoice = make_invoice(
        items, subtotal="6500.00", tax="325.00", shipping="150.00", total="6975.00"
    )
    assert run(invoice) == []


def test_no_subtotal_no_tax():
    items = [make_item(qty="6", price="250.00"), make_item(qty="3", price="500.00")]
    assert run(make_invoice(items, subtotal=None, total="3000.00")) == []


def test_missing_line_amount_is_derived_and_noted():
    items = [
        make_item("WidgetB", qty="2", price="500.00"),
        make_item("WidgetA", qty="3", price="100.00", total=None),
    ]
    invoice = make_invoice(items, subtotal=None, total="1300.00")
    findings, notes = reconcile(invoice)
    assert findings == []
    assert any("line 1" in n and "derived" in n and "300.00" in n for n in notes)


def test_derived_amount_is_named_in_a_failing_tie_out():
    items = [
        make_item("WidgetB", qty="2", price="500.00"),
        make_item("WidgetA", qty="3", price="100.00", total=None),
    ]
    findings = mismatches(make_invoice(items, subtotal=None, total="1301.00"))
    assert len(findings) == 1 and "derived" in findings[0].detail


def test_unknowable_line_skips_later_tie_outs_without_a_mismatch():
    item = make_item(qty="oneO", price="250.00", total=None)
    invoice = make_invoice([item], subtotal="999.00", total="1.00")
    findings, notes = reconcile(invoice)
    assert findings == []
    assert any("not verifiable" in n for n in notes)
    assert FindingCode.RECONCILIATION_MISMATCH not in {f.code for f in run(invoice)}


def test_negative_quantity_still_reconciles():
    item = make_item(qty="-5", price="100.00", total="-500.00")
    assert mismatches(make_invoice([item], subtotal="-500.00", total="-500.00")) == []


def test_subtotal_does_not_match_lines():
    items = [make_item(qty="3", price="250.00")]
    findings = mismatches(make_invoice(items, subtotal="1000.00", total="1000.00"))
    assert len(findings) == 1
    assert findings[0].severity is Severity.REVIEW_TRIGGER
    assert "subtotal" in findings[0].detail and "750.00" in findings[0].detail


def test_line_amount_does_not_match_quantity_times_price():
    item = make_item("WidgetB", qty="4", price="100.00", total="410.00")
    findings = mismatches(make_invoice([item], subtotal="410.00", total="410.00"))
    assert len(findings) == 1
    assert "line 0" in findings[0].detail and "400.00" in findings[0].detail


def test_off_by_one_cent_on_total():
    items = [make_item(qty="4", price="250.00")]
    findings = mismatches(make_invoice(items, subtotal="1000.00", total="1000.01"))
    assert len(findings) == 1 and "total" in findings[0].detail


def test_each_failed_tie_out_is_named_separately():
    item = make_item(qty="4", price="100.00", total="410.00")
    findings = mismatches(make_invoice([item], subtotal="500.00", total="1.00"))
    assert len(findings) == 3
    assert {f.code for f in findings} == {FindingCode.RECONCILIATION_MISMATCH}


def test_exact_decimal_arithmetic_has_no_tolerance_or_float_error():
    items = [make_item(qty="3", price="0.10", total="0.30")]
    assert mismatches(make_invoice(items, subtotal="0.30", total="0.30")) == []


# --- payable fields ------------------------------------------------------------


def test_missing_total_fails_closed_and_is_not_derived():
    invoice = make_invoice(total=None, subtotal=None)
    assert invoice.total is None
    findings = run(invoice)
    assert "MISSING_REQUIRED_FIELD" in codes(findings)
    assert FindingCode.RECONCILIATION_MISMATCH not in {f.code for f in findings}
    assert findings[0].severity is Severity.REVIEW_TRIGGER


def test_missing_unit_price_fires_and_is_not_reconciled():
    item = make_item(qty="2", price=None, total=None)
    findings = run(make_invoice([item], subtotal=None, total="500.00"))
    missing = [f for f in findings if f.code == FindingCode.MISSING_REQUIRED_FIELD]
    assert len(missing) == 1 and missing[0].line == 0
    assert FindingCode.RECONCILIATION_MISMATCH not in {f.code for f in findings}


@pytest.mark.parametrize("total", ["0.00", "-10.00"])
def test_nonpositive_total(total):
    item = make_item(qty="1", price=total, total=total)
    findings = run(make_invoice([item], subtotal=total, total=total))
    assert "NONPOSITIVE_TOTAL" in codes(findings)
    assert FindingCode.RECONCILIATION_MISMATCH not in {f.code for f in findings}


def test_amounts_are_never_invented_on_the_invoice():
    invoice = make_invoice(total=None, subtotal=None)
    run(invoice)
    assert invoice.total is None
    assert invoice.items[0].line_total == Decimal("250.00")
