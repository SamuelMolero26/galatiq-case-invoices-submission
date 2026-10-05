import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_pipeline.ingestion.structured import parse_json

CORPUS = Path(__file__).parent.parent / "data" / "invoices"
D = Decimal


def corpus(name: str):
    return parse_json((CORPUS / name).read_text(), name)


def doc(**overrides) -> str:
    base = {
        "invoice_number": "INV-1",
        "vendor": {"name": "Acme"},
        "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 10.10}],
        "total": 10.10,
    }
    return json.dumps({**base, **overrides})


def test_nested_vendor_and_exact_money_1004():
    result = corpus("invoice_1004.json")
    inv = result.invoice
    assert (inv.vendor, inv.invoice_number) == ("Precision Parts Ltd.", "INV-1004")
    assert inv.invoice_date == date(2026, 1, 22) and inv.due_date_text == "2026-02-22"
    assert [(i.sku, i.quantity, i.unit_price) for i in inv.items] == [
        ("WidgetA", D(3), D("250.00")),
        ("WidgetB", D(2), D("500.00")),
    ]
    assert (inv.subtotal, inv.tax, inv.total) == (D("1750.00"), D("140.00"), D("1890.00"))
    assert (inv.currency, inv.payment_terms, inv.revision) == ("USD", "Net 30", None)
    assert inv.source_format == "json" and inv.source_path == "invoice_1004.json"
    assert result.findings == [] and result.repairs == []
    assert result.raw_text is None and result.missing_required == []


def test_explicit_revision_field_is_captured_and_identity_is_unchanged():
    original, revised = corpus("invoice_1004.json").invoice, corpus("invoice_1004_revised.json")
    assert revised.invoice.revision == "R1"
    assert revised.invoice.identity() == original.identity()
    assert revised.invoice.notes == "Revised invoice - additional items added per PO amendment"


def test_note_alone_is_not_a_revision_marker():
    assert parse_json(doc(notes="Revised invoice"), "x.json").invoice.revision is None


def test_blank_and_null_identity_are_absent_not_errors_1009():
    inv = corpus("invoice_1009.json").invoice
    assert inv.vendor is None and inv.invoice_number == "INV-1009"
    assert inv.due_date_text is None and inv.payment_terms is None
    assert inv.identity() is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_invoice_number_is_absent(value):
    assert parse_json(doc(invoice_number=value), "x.json").invoice.invoice_number is None


def test_negative_quantity_is_kept_for_validation_1009():
    item = corpus("invoice_1009.json").invoice.items[0]
    assert (item.raw_quantity, item.quantity) == ("-5", D(-5))
    assert corpus("invoice_1009.json").invoice.total == D("-250.00")


def test_line_notes_and_amounts_1013():
    inv = corpus("invoice_1013.json").invoice
    assert [i.note for i in inv.items[3:]] == [
        "Volume discount",
        "Volume discount",
        "Expedited",
        "Replacement",
        "Sample",
    ]
    assert inv.items[3].line_total == D("1200.00") and inv.tax == D("1472.80")


def test_money_is_exact_decimal_never_float():
    inv = parse_json(doc(), "x.json").invoice
    assert inv.items[0].unit_price == D("10.10") and inv.total == D("10.10")
    assert all(isinstance(v, Decimal) for v in (inv.items[0].unit_price, inv.total))


def test_key_absence_stays_absence_and_nothing_is_guessed():
    data = json.loads(doc())
    for key in ("total", "vendor"):
        del data[key]
    inv = parse_json(json.dumps(data), "x.json").invoice
    assert inv.total is None and inv.vendor is None
    assert inv.subtotal is inv.tax is inv.shipping is inv.invoice_date is None
    assert inv.currency == "USD"


def test_string_vendor_and_stated_currency():
    inv = parse_json(doc(vendor="Plain Co", currency="eur"), "x.json").invoice
    assert (inv.vendor, inv.currency) == ("Plain Co", "EUR")


def test_sku_is_normalized_and_annotation_moves_to_the_note():
    items = [{"item": "widget a (rush)", "quantity": 2, "unit_price": 5, "note": "ctx"}]
    item = parse_json(doc(line_items=items), "x.json").invoice.items[0]
    assert (item.raw_name, item.sku, item.note) == ("widget a (rush)", "WidgetA", "rush; ctx")


def test_syntactically_invalid_json_raises_for_the_ingestion_boundary():
    with pytest.raises(ValueError):
        parse_json("{ not json", "x.json")
    with pytest.raises(ValueError):
        parse_json("[1, 2]", "x.json")
