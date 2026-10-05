import importlib.util

import pytest

from invoice_pipeline.model import FindingCode, Severity
from invoice_pipeline.validation import validate
from tests.factories import codes, make_catalog, make_invoice, make_item

CATALOG = make_catalog()


@pytest.mark.parametrize("currency", ["EUR", "GBP"])
def test_non_usd_adds_warning_and_unsupported_review_trigger(currency):
    invoice = make_invoice([make_item("WidgetA", qty="1", price="1.00")], currency=currency)
    findings = validate(invoice, CATALOG)
    assert set(codes(findings)) == {"CURRENCY_NON_USD", "CURRENCY_NO_RATE"}
    by_code = {f.code: f for f in findings}
    assert by_code[FindingCode.CURRENCY_NON_USD].severity is Severity.WARNING
    no_rate = by_code[FindingCode.CURRENCY_NO_RATE]
    assert no_rate.severity is Severity.REVIEW_TRIGGER
    assert "currency not supported yet" in no_rate.detail
    assert currency in no_rate.detail


def test_non_usd_skips_usd_price_comparison():
    invoice = make_invoice([make_item("WidgetA", qty="1", price="999.00")], currency="EUR")
    assert FindingCode.PRICE_DEVIATION not in {f.code for f in validate(invoice, CATALOG)}


def test_usd_invoice_is_unchanged():
    assert validate(make_invoice(), CATALOG) == []


def test_non_usd_reaches_review_by_severity():
    findings = validate(make_invoice(currency="EUR"), CATALOG)
    assert any(f.severity is Severity.REVIEW_TRIGGER for f in findings)


def test_no_rate_module_exists_before_slice_three():
    assert importlib.util.find_spec("invoice_pipeline.rates") is None
