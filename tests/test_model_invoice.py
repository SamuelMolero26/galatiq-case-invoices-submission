from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from invoice_pipeline.model import (
    Ingested,
    Invoice,
    LineItem,
    Repair,
    normalize_invoice_number,
    vendor_key,
)


def make_invoice(**overrides) -> Invoice:
    data = dict(
        invoice_number="INV-1001",
        vendor="Widgets Inc.",
        invoice_date=date(2026, 1, 15),
        due_date_text="2026-02-14",
        payment_terms="Net 30",
        currency="USD",
        items=[
            LineItem(
                raw_name="WidgetA",
                sku="WidgetA",
                raw_quantity="10",
                quantity=Decimal("10"),
                unit_price=Decimal("250.00"),
                line_total=Decimal("2500.00"),
            )
        ],
        subtotal=Decimal("2500.00"),
        tax=None,
        shipping=None,
        total=Decimal("2500.00"),
        notes=None,
        po_reference=None,
        source_path="data/invoices/invoice_1001.txt",
        source_format="txt",
    )
    data.update(overrides)
    return Invoice(**data)


def test_floats_are_rejected_for_money():
    with pytest.raises(ValidationError):
        make_invoice(total=2500.5)
    with pytest.raises(ValidationError):
        LineItem(
            raw_name="x",
            sku="x",
            raw_quantity="1",
            quantity=Decimal(1),
            unit_price=1.5,
            line_total=None,
        )
    with pytest.raises(ValidationError):
        LineItem(
            raw_name="x", sku="x", raw_quantity="1", quantity=1.0, unit_price=None, line_total=None
        )


def test_decimal_round_trips_exactly_through_json():
    invoice = make_invoice(total=Decimal("0.10"), tax=Decimal("0.20"))
    restored = Invoice.model_validate_json(invoice.model_dump_json())
    assert restored == invoice
    assert restored.total == Decimal("0.10")
    assert isinstance(restored.items[0].unit_price, Decimal)


def test_identity_is_normalized_and_complete_only():
    assert make_invoice().identity() == ("widgets inc.", "INV-1001")
    assert make_invoice(vendor="  WIDGETS   inc. ", invoice_number="1001").identity() == (
        "widgets inc.",
        "INV-1001",
    )
    assert make_invoice(invoice_number="INV 1001").identity() == ("widgets inc.", "INV-1001")


@pytest.mark.parametrize(
    "vendor, number",
    [(None, "INV-1"), ("", "INV-1"), ("  ", "INV-1"), ("Acme", None), ("Acme", " "), (None, None)],
)
def test_missing_identity_component_yields_none(vendor, number):
    assert make_invoice(vendor=vendor, invoice_number=number).identity() is None


def test_normalizers():
    assert vendor_key("  ACME   Co. ") == "acme co."
    assert vendor_key(None) is None
    assert vendor_key("   ") is None
    assert normalize_invoice_number("inv-1002") == "INV-1002"
    assert normalize_invoice_number("1002") == "INV-1002"
    assert normalize_invoice_number("") is None


def test_ingested_retains_fallback_inputs_without_guessing():
    ingested = Ingested(
        invoice=make_invoice(vendor=None, total=None),
        findings=[],
        repairs=[Repair(field="invoice_date", raw="2O26", repaired="2026")],
        raw_text="Invoice text",
        missing_required=["vendor", "total"],
    )
    assert ingested.invoice.vendor is None
    assert ingested.invoice.total is None
    assert ingested.raw_text == "Invoice text"
    assert ingested.missing_required == ["vendor", "total"]
    assert ingested.repairs[0].raw == "2O26"
    assert Ingested(invoice=None, findings=[], unreadable_reason="read: OSError: x").invoice is None
    with pytest.raises(ValidationError):
        Ingested(invoice=None, findings=[], missing_required=["vat"])
