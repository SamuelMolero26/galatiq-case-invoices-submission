from decimal import Decimal

import pytest

from invoice_pipeline.model import FindingCode, Severity
from invoice_pipeline.validation import PRICE_TOLERANCE, price_deviations, validate
from tests.factories import make_catalog, make_invoice, make_item

CATALOG = make_catalog()


def price_findings(invoice):
    return [f for f in validate(invoice, CATALOG) if f.code == FindingCode.PRICE_DEVIATION]


def one_line(price, **kw):
    return make_invoice([make_item("WidgetA", qty="1", price=price, **kw)])


def test_tolerance_is_one_named_constant():
    assert PRICE_TOLERANCE == Decimal("0.15")


@pytest.mark.parametrize(
    "price, fires",
    [
        ("287.47", False),  # 14.99%
        ("287.50", False),  # exactly 15%
        ("287.53", True),  # 15.01%
        ("212.50", False),  # exactly 15% under
        ("212.47", True),  # 15.01% under
        ("240.00", False),
    ],
)
def test_boundaries(price, fires):
    assert bool(price_findings(one_line(price))) is fires


def test_over_tolerance_states_deviation_price_and_reference_and_keeps_note():
    findings = price_findings(
        make_invoice([make_item("WidgetA", qty="4", price="300.00", note="rush order")])
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.WARNING and finding.line == 0
    assert "20.00%" in finding.detail
    assert "300.00" in finding.detail and "250.00" in finding.detail


def test_under_reference_states_sign():
    findings = price_findings(one_line("175.00"))
    assert len(findings) == 1 and "-30.00%" in findings[0].detail


@pytest.mark.parametrize("note", ["(rush)", "approved by VP", "Volume discount"])
def test_notes_never_suppress_the_finding(note):
    assert len(price_findings(one_line("300.00", note=note))) == 1
    invoice = one_line("300.00")
    invoice.notes = "approved by VP"
    assert len(price_findings(invoice)) == 1


def test_non_usd_lines_are_not_price_compared():
    invoice = make_invoice([make_item("WidgetA", qty="1", price="1.00")], currency="EUR")
    assert price_findings(invoice) == []


def test_sku_without_reference_price_is_not_compared():
    invoice = make_invoice([make_item("FakeItem", qty="1", price="999.00")])
    assert price_findings(invoice) == []


def test_finding_and_case_file_share_one_computed_deviation():
    invoice = make_invoice(
        [
            make_item("WidgetA", qty="1", price="300.00"),
            make_item("WidgetB", qty="1", price="500.00"),
        ]
    )
    deviations = price_deviations(invoice, CATALOG)
    assert deviations == {0: Decimal("0.2")}
    finding = price_findings(invoice)[0]
    assert f"{deviations[finding.line] * 100:.2f}%" in finding.detail
