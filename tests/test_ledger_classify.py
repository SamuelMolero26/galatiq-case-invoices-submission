from decimal import Decimal

from invoice_pipeline.approval import decide
from invoice_pipeline.critic import offline_agents
from invoice_pipeline.ledger import ArrivalRow, classify
from invoice_pipeline.model import FindingCode, Outcome
from tests.factories import case_file_for, make_invoice, make_item

PAID = ArrivalRow(id=4, state="paid", currency="USD", amount_paid=Decimal("1000.00"))
PENDING = ArrivalRow(id=7, state="payment_pending", currency="USD", amount_due=Decimal("800.00"))


def row(id, state, **kw):
    return ArrivalRow(id=id, state=state, currency="USD", **kw)


def test_no_history_is_new_and_payable_for_its_total():
    ctx = classify([], make_invoice(total="250"))
    assert (ctx.arrival.kind, ctx.arrival.amount_due, ctx.findings) == ("new", Decimal("250"), [])


def test_paid_history_without_marker_is_a_duplicate_linked_to_it():
    ctx = classify([PAID], make_invoice())
    arrival = ctx.arrival
    assert (arrival.kind, arrival.duplicate_of, arrival.paid_to_date) == (
        "duplicate",
        4,
        Decimal("1000.00"),
    )
    assert arrival.claimed_state == "paid" and arrival.amount_due is None and ctx.findings == []


def test_active_pending_history_is_a_duplicate_of_the_pending_claim():
    arrival = classify([PENDING], make_invoice()).arrival
    assert (arrival.kind, arrival.duplicate_of) == ("duplicate", 7)
    assert (arrival.claimed_state, arrival.paid_to_date) == ("payment_pending", Decimal("800.00"))


def test_later_rejection_or_duplicate_rows_do_not_erase_claimed_history():
    rows = [PAID, row(5, "logged_rejection"), row(6, "duplicate", amount_due=None)]
    arrival = classify(rows, make_invoice()).arrival
    assert (arrival.kind, arrival.duplicate_of) == ("duplicate", 4)


def test_duplicate_links_to_the_latest_claimed_arrival():
    first = row(2, "paid", amount_paid=Decimal("10"))
    latest = row(9, "paid", amount_paid=Decimal("20"))
    arrival = classify([latest, first], make_invoice()).arrival
    assert (arrival.duplicate_of, arrival.paid_to_date) == (9, Decimal("20"))


def test_unfinished_or_terminal_unclaimed_history_is_a_new_arrival_in_slice_1():
    for state in ("needs_review", "logged_rejection", "superseded", "duplicate"):
        assert classify([row(3, state)], make_invoice()).arrival.kind == "new"


def test_findings_never_outrank_duplicate():
    invoice = make_invoice(items=[make_item(sku="WidgetC")], vendor="Fraudster LLC")
    ctx = classify([PAID], invoice)
    assert ctx.arrival.kind == "duplicate"
    decision = decide(case_file_for(invoice, arrival=ctx.arrival), offline_agents())
    assert (decision.outcome, decision.precedence_row) == (Outcome.DUPLICATE, 1)


def test_partial_identity_routes_separately_and_never_matches_history():
    ctx = classify([PAID], make_invoice(vendor=None))
    assert (ctx.arrival.kind, ctx.arrival.duplicate_of) == ("new", None)
    assert classify([PAID], make_invoice(number=None)).arrival.kind == "new"


def test_marked_arrival_on_claimed_history_raises_the_delta_trigger_without_payable_amount():
    ctx = classify([PAID, PENDING], make_invoice(total="1200", revision="R1"))
    assert ctx.arrival.kind == "revision" and ctx.arrival.amount_due is None
    assert ctx.arrival.paid_to_date == Decimal("1800.00")
    [finding] = ctx.findings
    assert finding.code is FindingCode.REVISION_PAYMENT_DELTA
    assert "1000.00" in finding.detail and "800.00" in finding.detail


def test_marked_arrival_with_nothing_to_revise_is_a_new_arrival():
    assert classify([], make_invoice(revision="R1")).arrival.kind == "new"
    assert classify([row(3, "needs_review")], make_invoice(revision="R1")).arrival.kind == "new"


def test_a_blank_revision_marker_is_no_marker():
    assert classify([PAID], make_invoice(revision="")).arrival.kind == "duplicate"
