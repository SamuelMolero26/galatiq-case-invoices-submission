from datetime import date
from decimal import Decimal
from pathlib import Path

from invoice_pipeline.ingestion.text import parse_text
from invoice_pipeline.model import Repair

CORPUS = Path(__file__).parent.parent / "data" / "invoices"


def ingest_corpus(name: str):
    return parse_text((CORPUS / name).read_text(), name, "txt")


def ingest(text: str):
    return parse_text(text, "inline.txt", "txt")


def lines(inv):
    return [(i.sku, i.quantity, i.unit_price, i.line_total, i.note) for i in inv.items]


D = Decimal


def test_standard_labels_1001():
    result = ingest_corpus("invoice_1001.txt")
    inv = result.invoice
    assert (inv.vendor, inv.invoice_number) == ("Widgets Inc.", "INV-1001")
    assert inv.invoice_date == date(2026, 1, 15) and inv.due_date_text == "2026-02-01"
    assert lines(inv) == [
        ("WidgetA", D(10), D("250.00"), None, None),
        ("WidgetB", D(5), D("500.00"), None, None),
    ]
    assert (inv.subtotal, inv.tax, inv.total) == (D("5000.00"), D("0.00"), D("5000.00"))
    assert inv.payment_terms == "Net 15" and inv.currency == "USD"
    assert inv.source_format == "txt" and inv.source_path == "invoice_1001.txt"
    assert result.findings == [] and result.repairs == []
    assert result.missing_required == [] and result.raw_text is not None


def test_misspelled_labels_1002():
    inv = ingest_corpus("invoice_1002.txt").invoice
    assert (inv.vendor, inv.invoice_number) == ("Gadgets Co.", "INV-1002")
    assert inv.invoice_date == date(2026, 1, 30) and inv.total == D("15000.00")
    assert lines(inv) == [("GadgetX", D(20), D("750.00"), None, None)]
    assert inv.payment_terms == "Net 30"


def test_relative_due_date_terms_and_urgent_note_kept_verbatim_1003():
    inv = ingest_corpus("invoice_1003.txt").invoice
    assert inv.due_date_text == "yesterday" and inv.payment_terms == "Immediate"
    assert inv.notes == "URGENT - Pay immediately to avoid penalties!!! Wire transfer preferred."


def test_email_wrapper_1008():
    result = ingest_corpus("invoice_1008.txt")
    inv = result.invoice
    assert (inv.vendor, inv.invoice_number) == ("NoProd Industries", "INV-1008")
    assert lines(inv) == [
        ("SuperGizmo", D(12), D("400.00"), None, None),
        ("MegaSprocket", D(6), D("850.00"), None, None),
    ]
    assert inv.total == D("9900.00") and inv.due_date_text == "2026-01-20"
    assert result.missing_required == []


def test_table_with_rate_amount_and_annotated_sku_1010():
    inv = ingest_corpus("invoice_1010.txt").invoice
    assert (inv.vendor, inv.invoice_number) == ("Consolidated Materials Group", "INV-1010")
    assert inv.invoice_date == date(2026, 1, 27)
    assert lines(inv)[3] == ("WidgetA", D(4), D("300.00"), D("1200.00"), "rush order")
    assert inv.items[3].raw_name == "WidgetA (rush order)"
    assert (inv.subtotal, inv.tax, inv.shipping, inv.total) == (
        D("6700.00"),
        D("335.00"),
        D("150.00"),
        D("7185.00"),
    )


def test_repairs_spaced_skus_po_and_multiline_notes_1012():
    result = ingest_corpus("invoice_1012.txt")
    inv = result.invoice
    assert (inv.vendor, inv.invoice_number) == ("QuickShip Distributers", "INV-1012")
    assert inv.invoice_date == date(2026, 1, 26)
    assert [i.sku for i in inv.items] == ["WidgetA", "WidgetB", "GadgetX"]
    assert inv.items[1].line_total == D("3500.00") and inv.items[0].unit_price == D("250.00")
    assert (inv.tax, inv.total, inv.po_reference) == (D("475.00"), D("9975.00"), "PO-20260115")
    assert inv.notes == (
        "Ref PO-20260115. Deliver to warehouse dock B. Contact Jim at ext 4421 with questions."
    )
    assert result.repairs == [
        Repair(field="invoice_date", raw="26-Jan-2O26", repaired="26-Jan-2026"),
        Repair(field="items[1].line_total", raw="$3,500.O0", repaired="3500.00"),
    ]
    assert result.findings == []  # a repair never raises a Finding


def test_annotated_and_spaced_sku_lines():
    inv = ingest(
        "Vendor: A Co\nInvoice: 7\nItems:\n"
        "WidgetA (rush order)  4  $300.00\nwidget a  2  $10.00\nTotal: $1,220.00\n"
    ).invoice
    assert lines(inv) == [
        ("WidgetA", D(4), D("300.00"), None, "rush order"),
        ("WidgetA", D(2), D("10.00"), None, None),
    ]


def test_price_notations_equivalent():
    inv = ingest(
        "Vendor: A Co\nInvoice: 7\n"
        "A  qty: 1  unit price: $5.00\nB  qty 2  @ $6 ea\nC  x3  $7.00 each\n"
        "D  4  $8.00  $32.00\nTotal: $1.00\n"
    ).invoice
    assert [(i.sku, i.quantity, i.unit_price) for i in inv.items] == [
        ("A", D(1), D("5.00")),
        ("B", D(2), D("6.00")),
        ("C", D(3), D("7.00")),
        ("D", D(4), D("8.00")),
    ]
    assert inv.items[3].line_total == D("32.00")


def test_invalid_raw_quantity_kept_without_erasing_the_invoice():
    result = ingest("Vendor: A Co\nInvoice: 7\nWidgetA  qty oneO  @ $10 ea\nTotal: $10.00\n")
    item = result.invoice.items[0]
    assert (item.raw_quantity, item.quantity, item.unit_price) == ("oneO", None, D("10.00"))


def test_missing_total_stays_missing_and_is_listed():
    result = ingest("Vendor: A Co\nInvoice: 7\nWidgetA  qty: 2  unit price: $5.00\n")
    assert result.invoice.total is None
    assert result.missing_required == ["total"]
    assert result.findings == []


def test_missing_identity_is_absent_and_listed_not_raised():
    result = ingest("WidgetA  qty: 2  unit price: $5.00\nTotal: $10.00\n")
    assert result.invoice.vendor is None and result.invoice.invoice_number is None
    assert result.missing_required == ["vendor", "invoice_number"]
    assert result.invoice.identity() is None


def test_no_line_items_listed_as_missing():
    assert ingest("Vendor: A Co\nInvoice: 7\nTotal: $10.00\n").missing_required == ["items"]


def test_terms_without_due_date():
    inv = ingest(
        "Vendor: A Co\nInvoice: 7\nWidgetA qty 1 @ $5 ea\nTotal: $5\nPayment Terms: Net 30\n"
    ).invoice
    assert inv.due_date_text is None and inv.payment_terms == "Net 30"


def test_explicit_revision_label_creates_a_marker_but_free_text_does_not():
    base = "Vendor: A Co\nInvoice: 7\nWidgetA qty 1 @ $5 ea\nTotal: $5\n"
    assert ingest(base + "Revision: R2\n").invoice.revision == "R2"
    assert ingest(base + "Notes: Revised invoice\n").invoice.revision is None
    assert ingest(base + "Notes: Revised invoice\n").invoice.notes == "Revised invoice"


def test_stated_currency_overrides_the_usd_default():
    base = "Vendor: A Co\nInvoice: 7\nWidgetA qty 1 @ $5 ea\nTotal: $5\n"
    assert ingest(base + "Currency: eur\n").invoice.currency == "EUR"
    assert ingest(base).invoice.currency == "USD"


def test_email_from_header_is_not_a_vendor_but_a_from_line_is_the_fallback():
    email = "From: billing@x.biz\nInvoice: 7\nA qty 1 @ $5 ea\nTotal: $5\n"
    assert ingest(email).invoice.vendor is None
    assert ingest(email.replace("billing@x.biz", "Shipco Ltd")).invoice.vendor == "Shipco Ltd"


def test_two_labels_on_one_line_are_split():
    inv = ingest("Invoice: INV-9 Date: 2026-01-24\nVendor: Atlas Co Due: 2026-03-24\n").invoice
    assert (inv.invoice_number, inv.vendor) == ("INV-9", "Atlas Co")
    assert inv.invoice_date == date(2026, 1, 24) and inv.due_date_text == "2026-03-24"


def test_parsing_is_deterministic_and_offline(no_network):
    text = (CORPUS / "invoice_1012.txt").read_text()
    assert parse_text(text, "x", "txt") == parse_text(text, "x", "txt")


def test_pdf_format_is_carried_through():
    assert parse_text("Vendor: A\nInvoice: 1\n", "x.pdf", "pdf").invoice.source_format == "pdf"
