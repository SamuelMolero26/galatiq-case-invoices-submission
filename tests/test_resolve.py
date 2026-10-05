import inspect
from datetime import timedelta
from decimal import Decimal

import pytest

from invoice_pipeline import ledger, service
from invoice_pipeline.model import FindingCode, Ingested, Outcome, finding
from tests.factories import NOW, make_arrival, make_catalog, make_invoice

TRIGGER = FindingCode.STOCK_SHORTAGE


class Env:
    def __init__(self, tmp_path):
        self.path = tmp_path / "ledger.db"
        self.conn = ledger.connect(self.path)
        self.calls, self.events = [], []
        self.reply = {"status": "success"}
        self.rt = service.Runtime(
            catalog=make_catalog(),
            ledger_path=self.path,
            pay_fn=self.bank,
            on_event=self.events.append,
            now=lambda: NOW + timedelta(hours=1),
        )

    def bank(self, vendor, amount, currency):
        self.calls.append((vendor, amount, currency))
        return self.reply

    def review(self, findings=(TRIGGER,), minutes=0, invoice=None, **kw):
        found = [finding(code, "x") for code in findings]
        return ledger.record(
            self.conn,
            make_arrival(
                Outcome.NEEDS_REVIEW,
                invoice or make_invoice(),
                found,
                arrived_at=NOW + timedelta(minutes=minutes),
                **kw,
            ),
        )

    def snapshot(self):
        return [tuple(r) for r in self.conn.execute("SELECT * FROM arrivals ORDER BY id")]

    def row(self, arrival_id):
        return self.conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.conn.close()


def refused(env, arrival_id, action="approve", reason="because", match=None):
    before = env.snapshot()
    with pytest.raises(service.ResolutionRefused, match=match):
        service.resolve(env.rt, arrival_id, action, reason)
    assert env.snapshot() == before and env.calls == []


def test_approve_claims_pays_and_records_one_resolution(env):
    arrival_id = env.review()
    result = service.resolve(env.rt, arrival_id, "approve", "stock confirmed by warehouse")
    assert (result.arrival_id, result.state) == (arrival_id, "paid")
    assert env.calls == [("Widgets Inc.", Decimal("250.00"), "USD")]
    row = env.row(arrival_id)
    assert (row["resolution"], row["resolution_reason"]) == (
        "approve",
        "stock confirmed by warehouse",
    )
    assert row["resolved_at"].startswith("2026-01-02T04:04:05") and row["state"] == "paid"
    assert [e.name for e in env.events] == ["payment_sent"]


def test_approve_with_a_failing_bank_stays_payment_pending(env):
    env.reply = {"status": "declined"}
    arrival_id = env.review()
    result = service.resolve(env.rt, arrival_id, "approve", "ok")
    assert result.state == "payment_pending" and len(env.calls) == 1
    assert [e.name for e in env.events] == ["payment_failed"]
    assert env.row(arrival_id)["resolution"] == "approve"


def test_reject_logs_the_rejection_without_a_payment_call(env):
    arrival_id = env.review()
    result = service.resolve(env.rt, arrival_id, "reject", "not ordered")
    assert result.state == "logged_rejection" and env.calls == []
    row = env.row(arrival_id)
    assert (row["resolution"], row["resolution_reason"]) == ("reject", "not ordered")


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_reason_is_mandatory(env, reason):
    arrival_id = env.review()
    refused(env, arrival_id, "approve", reason, match="reason")
    refused(env, arrival_id, "reject", reason, match="reason")


def test_unknown_action_and_missing_row_are_refused(env):
    arrival_id = env.review()
    refused(env, arrival_id, "settle", match="approve or reject")
    refused(env, 999, match="no arrival")


def test_a_second_resolution_is_refused_and_the_first_stands(env):
    arrival_id = env.review()
    service.resolve(env.rt, arrival_id, "reject", "first")
    before = env.snapshot()
    for action in ("approve", "reject"):
        with pytest.raises(service.ResolutionRefused):
            service.resolve(env.rt, arrival_id, action, "second")
    assert env.snapshot() == before and env.row(arrival_id)["resolution_reason"] == "first"


def test_payment_pending_accepts_no_action(env):
    env.reply = {"status": "declined"}
    arrival_id = env.review()
    service.resolve(env.rt, arrival_id, "approve", "ok")
    env.calls.clear()
    for action in ("approve", "reject"):
        refused(env, arrival_id, action, match="settlement")


def test_paid_and_logged_rejection_rows_refuse_with_their_reason(env):
    paid = env.review()
    service.resolve(env.rt, paid, "approve", "ok")
    env.calls.clear()
    refused(env, paid, match="already paid")
    rejected = ledger.record(env.conn, make_arrival(Outcome.REJECTED, make_invoice(number="INV-2")))
    refused(env, rejected, match="new arrival of a corrected invoice")


def test_duplicate_refusal_names_the_original_arrival(env):
    original = env.review()
    with ledger.write_txn(env.conn):
        ledger.update_arrival(env.conn, original, state="paid", amount_paid="250.00")
    dup = ledger.record(
        env.conn,
        make_arrival(Outcome.DUPLICATE, make_invoice(), amount_due=None, duplicate_of=original),
    )
    refused(env, dup, match=f"arrival #{original}")


def test_superseded_refusal_says_it_was_replaced(env):
    old, new = env.review(), env.review(minutes=1)
    with ledger.write_txn(env.conn):
        ledger.update_arrival(env.conn, old, state="superseded", superseded_by=new)
    refused(env, old, match="replaced by its later arrival")


def test_stale_view_of_a_row_paid_meanwhile_is_refused(env):
    arrival_id = env.review()
    other = ledger.connect(env.path)
    with ledger.write_txn(other):
        ledger.update_arrival(other, arrival_id, state="paid", amount_paid="250.00")
    other.close()
    refused(env, arrival_id, match="already paid")


def test_unreadable_document_can_only_be_rejected(env):
    from invoice_pipeline.model import Decision

    reason = "parse: boom"
    bad = Ingested(
        invoice=None,
        findings=[finding(FindingCode.UNREADABLE_DOCUMENT, reason)],
        unreadable_reason=reason,
    )
    decision = Decision(
        outcome=Outcome.NEEDS_REVIEW, reasons=[reason], precedence_row=3, decided_by="rule_engine"
    )
    arrival_id = ledger.record(
        env.conn, ledger.Arrival("bad.json", NOW, bad, bad.findings, decision, None)
    )
    refused(env, arrival_id, match="unreadable")
    assert (
        service.resolve(env.rt, arrival_id, "reject", "send it again").state == "logged_rejection"
    )


@pytest.mark.parametrize(
    "findings, invoice, kw, match",
    [
        ([FindingCode.PARTIAL_IDENTITY], dict(vendor=None), {}, "incomplete"),
        ([FindingCode.MISSING_REQUIRED_FIELD], dict(), dict(amount_due=None), "required amount"),
        ([FindingCode.NONPOSITIVE_TOTAL], dict(), dict(amount_due=Decimal("0")), "not positive"),
        (
            [FindingCode.REVISION_PAYMENT_DELTA],
            dict(revision="R1"),
            dict(amount_due=None),
            "payable",
        ),
        ([TRIGGER], dict(), dict(amount_due=Decimal("-5")), "payable"),
        ([TRIGGER], dict(vendor=None), {}, "incomplete"),
        ([TRIGGER], dict(number=None), {}, "incomplete"),
    ],
)
def test_reject_only_entries_refuse_approval_but_allow_rejection(env, findings, invoice, kw, match):
    arrival_id = env.review(findings, invoice=make_invoice(**invoice), **kw)
    refused(env, arrival_id, "approve", match=match)
    assert service.resolve(env.rt, arrival_id, "reject", "send a corrected invoice").state == (
        "logged_rejection"
    )


def test_approval_blocked_by_the_payment_cap_changes_nothing(env):
    paid = env.review()
    with ledger.write_txn(env.conn):
        ledger.update_arrival(env.conn, paid, state="paid", amount_paid="250.00")
    stale = env.review(minutes=1)
    refused(env, stale, match="payment cap")
    assert env.row(stale)["state"] == "needs_review" and env.row(stale)["resolution"] is None


def test_approval_refused_when_the_identity_has_a_payment_pending_row(env):
    first, second = env.review(), env.review(minutes=1)
    env.reply = {"status": "failed"}
    service.resolve(env.rt, first, "approve", "ok")
    assert env.row(first)["state"] == "payment_pending"
    env.calls.clear()
    with ledger.write_txn(env.conn):  # a revision-like payable that fits the cap
        ledger.update_arrival(env.conn, second, total="500.00", amount_due="100.00")
    refused(env, second, match=f"payment pending on arrival #{first}")
    assert env.row(second)["state"] == "needs_review"


def test_resolutions_never_touch_other_rows_or_future_decisions(env):
    first, other = env.review(), env.review(invoice=make_invoice(number="INV-7"))
    service.resolve(env.rt, first, "reject", "no")
    assert env.row(other)["state"] == "needs_review" and env.row(other)["resolution"] is None


def test_the_operation_exposes_no_field_editing():
    assert list(inspect.signature(service.resolve).parameters) == [
        "rt",
        "arrival_id",
        "action",
        "reason",
    ]
    assert not [n for n in dir(service) if n.startswith(("edit", "update", "set_", "supply"))]
