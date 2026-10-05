import datetime as dt
from decimal import Decimal

from invoice_pipeline.model import (
    ArrivalSummary,
    CaseFile,
    FindingCode,
    HistoryEntry,
    Invoice,
    LineItem,
    References,
    UsdEquivalent,
    finding,
)


def minimal_case_file(**overrides) -> CaseFile:
    invoice = Invoice(
        invoice_number="INV-1001",
        vendor="Widgets Inc.",
        invoice_date=dt.date(2026, 1, 15),
        due_date_text=None,
        payment_terms="Net 30",
        currency="USD",
        items=[
            LineItem(
                raw_name="WidgetA",
                sku="WidgetA",
                raw_quantity="4",
                quantity=Decimal("4"),
                unit_price=Decimal("300.00"),
                line_total=Decimal("1200.00"),
                note="rush order",
            )
        ],
        subtotal=Decimal("1200.00"),
        tax=None,
        shipping=None,
        total=Decimal("1200.00"),
        notes=None,
        po_reference=None,
        source_path="x.txt",
        source_format="txt",
    )
    data = dict(
        invoice=invoice,
        findings=[
            finding(
                FindingCode.PRICE_DEVIATION,
                "unit price 300.00 vs reference 250.00: +20.00%, tolerance 15%",
                line=0,
            )
        ],
        arrival=ArrivalSummary(kind="new", amount_due=Decimal("1200.00")),
        references=References(
            reference_prices={"WidgetA": Decimal("250.00")},
            stock_levels={"WidgetA": Decimal("15")},
            aggregated_quantities={"WidgetA": Decimal("4")},
            price_tolerance=Decimal("0.15"),
            price_deviations={0: Decimal("0.20")},
            usd_equivalent=None,
            heightened_scrutiny_line=Decimal("10000"),
        ),
        vendor_history=[
            HistoryEntry(
                number="INV-0900",
                total=Decimal("99.99"),
                currency="USD",
                state="paid",
                date=dt.date(2025, 12, 1),
            )
        ],
        vendor_history_total=1,
    )
    data.update(overrides)
    return CaseFile(**data)


def test_slice_one_case_file_round_trips_with_exact_money():
    case_file = minimal_case_file()
    restored = CaseFile.model_validate_json(case_file.model_dump_json())
    assert restored == case_file
    assert restored.references.usd_equivalent is None
    assert restored.references.price_deviations == {0: Decimal("0.20")}
    assert restored.vendor_history[0].total == Decimal("99.99")
    assert restored.invoice.currency == "USD"
    assert restored.arrival.kind == "new"
    assert restored.decision_context == []
    assert restored.checklist == {}


def test_case_file_carries_usd_equivalent_when_present():
    usd = UsdEquivalent(
        amount=Decimal("1134.00"),
        rate=Decimal("1.08"),
        as_of=dt.date(2026, 1, 2),
        buffer=Decimal("0.05"),
    )
    case_file = minimal_case_file()
    case_file.references.usd_equivalent = usd
    restored = CaseFile.model_validate_json(case_file.model_dump_json())
    assert restored.references.usd_equivalent == usd


def test_checklist_keys_survive_json():
    case_file = minimal_case_file(checklist={FindingCode.PRICE_DEVIATION: ("Is there a note?",)})
    restored = CaseFile.model_validate_json(case_file.model_dump_json())
    assert restored.checklist == {FindingCode.PRICE_DEVIATION: ("Is there a note?",)}
