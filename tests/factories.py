"""Constructed invoices and catalogs for unit tests (no corpus files, no database)."""

from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal

from invoice_pipeline import ledger
from invoice_pipeline.catalog import Catalog, KnownVendor
from invoice_pipeline.critic import build_case_file, offline_role
from invoice_pipeline.model import (
    Agents,
    ArrivalSummary,
    CaseFile,
    Decision,
    Ingested,
    Invoice,
    LineItem,
    Outcome,
    RoleCall,
    vendor_key,
)
from invoice_pipeline.validation import validate

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


def case_file_for(invoice: Invoice, catalog: Catalog | None = None, arrival=None, history=()):
    """Validate a constructed invoice and assemble its (slice-1) Case File."""
    catalog = catalog or make_catalog()
    arrival = arrival or ArrivalSummary(kind="new", amount_due=invoice.total)
    return build_case_file(
        invoice,
        validate(invoice, catalog),
        arrival,
        None,
        catalog,
        list(history),
        len(history),
    )


def online_role(role: str, answer: dict | None, error: str | None = None) -> RoleCall:
    return RoleCall(role=role, tier="stub", model="stub", tries=[], answer=answer, error=error)


class CountingAgents:
    """Stub model roles that count their calls; offline by default."""

    def __init__(self, escalate=None, advise=None):
        self.calls: Counter[str] = Counter()
        self._escalate, self._advise = escalate, advise

    def _assess(self, *args, **kwargs):
        self.calls["assess"] += 1

    def _verify(self, *args, **kwargs):
        self.calls["verify"] += 1

    def _escalate_review(self, case_file: CaseFile) -> RoleCall:
        self.calls["escalate_review"] += 1
        return self._escalate or offline_role("escalate_review")

    def _advise_role(self, case_file: CaseFile, decision) -> RoleCall:
        self.calls["advise"] += 1
        return self._advise or offline_role("advisory")

    @property
    def agents(self) -> Agents:
        return Agents(self._assess, self._verify, self._escalate_review, self._advise_role)


NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_arrival(
    outcome=Outcome.APPROVED,
    invoice=None,
    findings=(),
    amount_due="auto",
    arrived_at=NOW,
    reasons=("why",),
    **decision_kw,
) -> ledger.Arrival:
    """A Ledger write-phase record for a constructed invoice and a hand-built Decision."""
    invoice = invoice or make_invoice()
    decision = Decision(
        outcome=outcome,
        reasons=list(reasons),
        precedence_row=6,
        decided_by="rule_engine",
        **decision_kw,
    )
    return ledger.Arrival(
        source=invoice.source_path,
        arrived_at=arrived_at,
        ingested=Ingested(invoice=invoice, findings=[]),
        findings=list(findings),
        decision=decision,
        amount_due=invoice.total if amount_due == "auto" else amount_due,
    )
