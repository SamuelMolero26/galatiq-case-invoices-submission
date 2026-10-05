from datetime import date, timedelta
from decimal import Decimal

import pytest

from invoice_pipeline import ledger
from invoice_pipeline.model import Outcome, QueueItem
from tests.factories import NOW, make_arrival, make_invoice


@pytest.fixture
def conn(tmp_path):
    connection = ledger.connect(tmp_path / "ledger.db")
    yield connection
    connection.close()


def put(conn, minutes, outcome=Outcome.NEEDS_REVIEW, number="INV-1", vendor="Acme Co.", **kw):
    invoice = make_invoice(vendor=vendor, number=number, **kw.pop("invoice", {}))
    when = NOW + timedelta(minutes=minutes)
    return ledger.record(conn, make_arrival(outcome, invoice, arrived_at=when, **kw))


def test_queue_holds_exactly_needs_review_and_payment_pending_in_arrival_order(conn):
    pending = put(conn, 3, Outcome.APPROVED, number="INV-3")  # claimed -> payment_pending
    review = put(conn, 1, number="INV-1", reasons=("STOCK_SHORTAGE: short",))
    put(conn, 2, Outcome.REJECTED, number="INV-2")
    put(conn, 4, Outcome.DUPLICATE, number="INV-4", duplicate_of=pending, amount_due=None)
    paid = put(conn, 5, Outcome.APPROVED, number="INV-5")
    superseded = put(conn, 6, number="INV-6")
    with ledger.write_txn(conn):
        ledger.update_arrival(conn, paid, state="paid", amount_paid="250.00")
        ledger.update_arrival(conn, superseded, state="superseded", superseded_by=review)
    items = ledger.review_queue(conn)
    assert [(i.arrival_id, i.state) for i in items] == [
        (review, "needs_review"),
        (pending, "payment_pending"),
    ]


def test_queue_item_carries_what_presenters_show(conn):
    put(conn, 1, number="INV-1", reasons=("STOCK_SHORTAGE: short", "VENDOR_UNKNOWN: x"))
    [item] = ledger.review_queue(conn)
    assert isinstance(item, QueueItem)
    assert (item.vendor, item.invoice_number, item.source) == (
        "Acme Co.",
        "INV-1",
        "constructed.txt",
    )
    assert (item.total, item.currency) == (Decimal("250.00"), "USD")
    assert item.reasons == ["STOCK_SHORTAGE: short", "VENDOR_UNKNOWN: x"]
    assert item.payment_issue is None


def test_payment_pending_item_exposes_its_payment_issue(conn):
    put(conn, 1, Outcome.APPROVED)
    [item] = ledger.review_queue(conn)
    assert item.state == "payment_pending"
    assert "unconfirmed" in item.payment_issue.what and item.payment_issue.when == NOW + timedelta(
        minutes=1
    )


def test_queue_listing_writes_nothing(conn):
    put(conn, 1)
    before = conn.execute("SELECT seq FROM arrivals").fetchall()
    ledger.review_queue(conn)
    assert conn.execute("SELECT seq FROM arrivals").fetchall() == before
    assert not conn.in_transaction


def test_unreadable_documents_are_queued_with_missing_identity(conn):
    from invoice_pipeline.model import Decision, FindingCode, Ingested, finding

    reason = "parse: boom"
    bad = Ingested(
        invoice=None,
        findings=[finding(FindingCode.UNREADABLE_DOCUMENT, reason)],
        unreadable_reason=reason,
    )
    decision = Decision(
        outcome=Outcome.NEEDS_REVIEW, reasons=[reason], precedence_row=3, decided_by="rule_engine"
    )
    ledger.record(
        conn,
        ledger.Arrival("bad.json", NOW, bad, bad.findings, decision, None),
    )
    [item] = ledger.review_queue(conn)
    assert (item.vendor, item.invoice_number, item.total, item.currency) == (None, None, None, None)
    assert item.source == "bad.json" and item.reasons == [reason]


def test_vendor_history_returns_ten_newest_entries_and_the_total(conn):
    for n in range(12):
        put(conn, n, number=f"INV-{n}", invoice=dict(invoice_date=date(2026, 1, 1 + n)))
    put(conn, 99, vendor="Other Co.", number="INV-X")
    entries, total = ledger.vendor_history(conn, "acme co.")
    assert total == 12 and len(entries) == ledger.VENDOR_HISTORY_LIMIT == 10
    assert [e.number for e in entries] == [f"INV-{n}" for n in range(11, 1, -1)]  # newest first
    newest = entries[0]
    assert (newest.total, newest.currency, newest.state) == (
        Decimal("250.00"),
        "USD",
        "needs_review",
    )
    assert newest.date == date(2026, 1, 12)


def test_vendor_history_is_empty_for_an_unknown_vendor(conn):
    assert ledger.vendor_history(conn, "nobody") == ([], 0)
