import argparse
import dataclasses
from decimal import Decimal
from pathlib import Path

from invoice_pipeline import service
from invoice_pipeline.ledger import ArrivalRow, classify
from invoice_pipeline.model import FindingCode, Invoice

CORPUS = Path(__file__).resolve().parent.parent / "data" / "invoices"


def _inv(**kw):
    base = dict(invoice_number="INV-1004", vendor="V", invoice_date=None, due_date_text=None,
                payment_terms=None, currency="USD", items=[], subtotal=None, tax=None,
                shipping=None, total=Decimal("5940.00"), notes=None, po_reference=None,
                source_path="x", source_format="json")
    return Invoice(**{**base, **kw})


PAID = [ArrivalRow(1, "paid", "USD", Decimal("1890.00"), Decimal("1890.00"))]


def test_explicit_revision_triggers_delta_with_evidence():
    ctx = classify(PAID, _inv(revision="R1", notes="Additional items added per PO amendment"))
    assert ctx.arrival.kind == "revision"
    d = ctx.findings[0].detail
    assert ctx.findings[0].code == FindingCode.REVISION_PAYMENT_DELTA
    assert "remaining 4050.00 USD" in d and "explicit revision marker: R1" in d


def test_note_without_revision_marker_is_duplicate():
    ctx = classify(PAID, _inv(notes="This invoice is not revised"))

    assert ctx.arrival.kind == "duplicate"
    assert ctx.arrival.amount_due is None


def test_revision_payable_is_remaining_delta():
    assert classify(PAID, _inv(revision="R1")).arrival.amount_due == Decimal("4050.00")


def test_revision_not_above_paid_has_no_payable():
    ctx = classify(PAID, _inv(revision="R1", total=Decimal("1890.00")))
    assert ctx.arrival.amount_due is None


def test_reviewer_approval_pays_only_the_revision_delta(tmp_path):
    args = argparse.Namespace(
        llm="offline", ledger=tmp_path / "ledger.db", inventory=tmp_path / "inventory.db"
    )
    paid = []
    rt = dataclasses.replace(
        service.bootstrap(args),
        pay_fn=lambda vendor, amount, currency: paid.append((vendor, amount, currency))
        or {"status": "success"},
    )

    original = service.process_path(CORPUS / "invoice_1004.json", rt).results[0]
    revision = service.process_path(CORPUS / "invoice_1004_revised.json", rt).results[0]
    resolved = service.resolve(rt, revision.arrival_id, "approve", "revision verified against PO")

    assert (original.state, revision.state, resolved.state) == ("paid", "needs_review", "paid")
    assert paid == [
        ("Precision Parts Ltd.", Decimal("1890.00"), "USD"),
        ("Precision Parts Ltd.", Decimal("4050.00"), "USD"),
    ]
