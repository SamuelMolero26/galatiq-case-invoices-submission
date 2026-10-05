"""Constructed invoices and catalogs for unit tests (no corpus files, no database)."""

from datetime import date
from decimal import Decimal

from invoice_pipeline.catalog import Catalog, KnownVendor
from invoice_pipeline.model import Invoice, LineItem, vendor_key

_DEFAULT = object()


def D(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def make_item(
    sku="WidgetA",
    qty="1",
    price="250.00",
    total=_DEFAULT,
    note=None,
    raw_quantity=_DEFAULT,
) -> LineItem:
    quantity = D(qty) if qty is not None and _is_number(qty) else None
    line_total = (
        (quantity * D(price) if quantity is not None and price is not None else None)
        if total is _DEFAULT
        else D(total)
    )
    return LineItem(
        raw_name=sku or "",
        sku=sku,
        raw_quantity=(str(qty) if qty is not None else None)
        if raw_quantity is _DEFAULT
        else raw_quantity,
        quantity=quantity,
        unit_price=D(price),
        line_total=line_total,
        note=note,
    )


def _is_number(value) -> bool:
    try:
        Decimal(str(value))
    except ArithmeticError:
        return False
    return True


def make_invoice(
    items=_DEFAULT,
    vendor="Widgets Inc.",
    number="INV-1001",
    currency="USD",
    subtotal=_DEFAULT,
    tax=None,
    shipping=None,
    total=_DEFAULT,
    **extra,
) -> Invoice:
    items = [make_item()] if items is _DEFAULT else items
    line_sum = sum((i.line_total for i in items if i.line_total is not None), Decimal(0))
    if subtotal is _DEFAULT:
        subtotal = line_sum
    if total is _DEFAULT:
        total = (subtotal or line_sum) + (D(tax) or 0) + (D(shipping) or 0)
    data = dict(
        invoice_number=number,
        vendor=vendor,
        invoice_date=date(2026, 1, 15),
        due_date_text=None,
        payment_terms=None,
        currency=currency,
        items=items,
        subtotal=D(subtotal),
        tax=D(tax),
        shipping=D(shipping),
        total=D(total),
        notes=None,
        po_reference=None,
        source_path="constructed.txt",
        source_format="txt",
    )
    data.update(extra)
    return Invoice(**data)


def make_catalog(**overrides) -> Catalog:
    data = dict(
        stock={
            "WidgetA": Decimal("15"),
            "WidgetB": Decimal("10"),
            "GadgetX": Decimal("5"),
            "FakeItem": Decimal("0"),
        },
        prices={
            "WidgetA": Decimal("250"),
            "WidgetB": Decimal("500"),
            "GadgetX": Decimal("750"),
        },
        vendors={
            vendor_key(name): KnownVendor(name, status)
            for name, status in [
                ("Widgets Inc.", "trusted"),
                ("Acme Supplies", "trusted"),
                ("Gadgets Co.", "trusted"),
                ("Fraudster LLC", "blocked"),
                ("Shadow Traders", "unknown"),
            ]
        },
    )
    data.update(overrides)
    return Catalog(**data)


def codes(findings) -> list[str]:
    return [f.code.value for f in findings]
