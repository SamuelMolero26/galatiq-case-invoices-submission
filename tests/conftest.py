"""Shared fixtures: a tiny catalog, constructed Case Files, and a network guard."""

import socket
from decimal import Decimal

import pytest

from invoice_pipeline.catalog import Catalog
from invoice_pipeline.critic import build_case_file
from invoice_pipeline.model import ArrivalSummary, HistoryEntry, Invoice, LineItem
from invoice_pipeline.validation import validate


@pytest.fixture
def no_network(monkeypatch):
    """Any socket connect during the test fails loudly."""

    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(
        stock={"WidgetA": Decimal(100), "WidgetB": Decimal(100), "GadgetX": Decimal(100)},
        prices={"WidgetA": Decimal(250), "WidgetB": Decimal(500), "GadgetX": Decimal(750)},
        vendors={"acme corp": ("Acme Corp", "trusted")},
    )


def line(sku: str, unit_price: str, quantity: str = "1") -> LineItem:
    price, qty = Decimal(unit_price), Decimal(quantity)
    return LineItem(
        raw_name=sku,
        sku=sku,
        raw_quantity=quantity,
        quantity=qty,
        unit_price=price,
        line_total=price * qty,
    )


def invoice(items: list[LineItem], vendor: str = "Acme Corp", currency: str = "USD") -> Invoice:
    total = sum((i.line_total for i in items), Decimal(0))
    return Invoice(
        invoice_number="INV-1001",
        vendor=vendor,
        invoice_date=None,
        due_date_text=None,
        payment_terms=None,
        currency=currency,
        items=items,
        subtotal=total,
        tax=None,
        shipping=None,
        total=total,
        notes=None,
        po_reference=None,
        source_path="x.txt",
        source_format="txt",
    )


@pytest.fixture
def make_case_file(catalog):
    """`make(items=[("WidgetA", "300")], vendor=..., history=[...], online=True)`."""

    def make(
        items=(("WidgetA", "300"),),
        vendor="Acme Corp",
        currency="USD",
        history: list[HistoryEntry] | None = None,
        history_total: int | None = None,
        online: bool = True,
    ):
        inv = invoice([line(sku, price) for sku, price in items], vendor, currency)
        history = history or []
        return build_case_file(
            inv,
            validate(inv, catalog),
            ArrivalSummary(kind="new"),
            None,
            catalog,
            history,
            len(history) if history_total is None else history_total,
            online=online,
        )

    return make
