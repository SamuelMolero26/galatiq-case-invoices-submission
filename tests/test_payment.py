import json
from datetime import timedelta
from decimal import Decimal

import pytest

from invoice_pipeline import ledger, payment
from invoice_pipeline.model import Outcome
from tests.factories import NOW, make_arrival, make_invoice, make_item


@pytest.fixture
def path(tmp_path):
    return tmp_path / "ledger.db"


@pytest.fixture
def conn(path):
    connection = ledger.connect(path)
    yield connection
    connection.close()


def claimed(conn, number="INV-1", **invoice_kw):
    """An Approved arrival, recorded and claimed (Payment Pending) like the write phase does."""
    return ledger.record(
        conn, make_arrival(Outcome.APPROVED, make_invoice(number=number, **invoice_kw))
    )


def state_of(conn, arrival_id):
    return conn.execute(
        "SELECT state, amount_paid, payment_issue FROM arrivals WHERE id = ?", (arrival_id,)
    ).fetchone()


def later():
    return NOW + timedelta(minutes=5)


def test_callable_sees_committed_pending_with_provisional_issue_then_paid(conn, path):
    arrival_id = claimed(conn)
    seen = []

    def bank(vendor, amount, currency):
        other = ledger.connect(path)  # a separate connection sees only committed state
        row = state_of(other, arrival_id)
        seen.append((vendor, amount, currency, row["state"], json.loads(row["payment_issue"])))
        other.close()
        assert not conn.in_transaction  # no transaction is open across the bank call
        return {"status": "success"}

    assert payment.pay(conn, arrival_id, bank, later) is None
    [(vendor, amount, currency, state, issue)] = seen
    assert (vendor, amount, currency) == ("Widgets Inc.", Decimal("250.00"), "USD")
    assert state == "payment_pending" and "unconfirmed" in issue["what"]
    row = state_of(conn, arrival_id)
    assert (row["state"], Decimal(row["amount_paid"])) == ("paid", Decimal("250.00"))


def test_no_lock_is_held_during_the_bank_call(conn, path):
    arrival_id = claimed(conn)

    def bank(*args):
        other = ledger.connect(path)
        with ledger.write_txn(other):  # would block (busy timeout) if the bank call held a lock
            ledger.insert_arrival(
                other,
                arrived_at="t",
                source="u",
                decision="rejected",
                state="logged_rejection",
                record="{}",
            )
        other.close()
        return {"status": "success"}

    assert payment.pay(conn, arrival_id, bank, later) is None


def test_currency_is_passed_unchanged(conn):
    invoice = dict(currency="EUR", items=[make_item(price="4125.00")])
    arrival_id = claimed(conn, **invoice)
    calls = []
    payment.pay(conn, arrival_id, lambda *args: calls.append(args) or {"status": "success"}, later)
    assert calls == [("Widgets Inc.", Decimal("4125.00"), "EUR")]


def test_refusal_keeps_pending_with_the_bank_response_and_one_call(conn):
    arrival_id = claimed(conn)
    calls = []

    def bank(*args):
        calls.append(args)
        return {"status": "declined", "code": "E42"}

    issue = payment.pay(conn, arrival_id, bank, later)
    assert len(calls) == 1
    assert (
        "declined" in issue.what and issue.when == later() and issue.bank_response["code"] == "E42"
    )
    row = state_of(conn, arrival_id)
    assert row["state"] == "payment_pending" and row["amount_paid"] is None
    assert json.loads(row["payment_issue"])["bank_response"] == {
        "status": "declined",
        "code": "E42",
    }


@pytest.mark.parametrize("error", [ConnectionError("boom"), TimeoutError("slow")])
def test_exception_or_timeout_keeps_pending_with_the_error_and_no_retry(conn, error):
    arrival_id = claimed(conn)
    calls = []

    def bank(*args):
        calls.append(args)
        raise error

    issue = payment.pay(conn, arrival_id, bank, later)
    assert len(calls) == 1
    assert type(error).__name__ in issue.what and str(error) in issue.what
    assert issue.when == later() and issue.bank_response is None
    assert state_of(conn, arrival_id)["state"] == "payment_pending"


def test_unusable_bank_reply_is_not_a_success(conn):
    arrival_id = claimed(conn)
    assert payment.pay(conn, arrival_id, lambda *args: None, later) is not None
    assert state_of(conn, arrival_id)["state"] == "payment_pending"


def test_mock_payment_logs_and_confirms():
    assert payment.mock_payment("Acme", Decimal("1.00"), "USD") == {"status": "success"}


def test_the_module_offers_no_settlement_or_retry_operation():
    public = {name for name in dir(payment) if not name.startswith("_")}
    assert not {n for n in public if "settle" in n or "retry" in n or "mark_paid" in n}


def pair(conn, due="100", total="100"):
    """Two Needs Review arrivals of one identity, each claimable for `due` against `total`."""
    ids = []
    for _ in range(2):
        invoice = make_invoice(items=[make_item(price=total)], total=total)
        ids.append(ledger.record(conn, make_arrival(Outcome.NEEDS_REVIEW, invoice, amount_due=due)))
    return ids


def claim(connection, arrival_id):
    with ledger.write_txn(connection):
        ledger.claim(connection, arrival_id, NOW)


def test_two_connections_cannot_reserve_the_same_money(conn, path):
    first, second = pair(conn)
    other = ledger.connect(path)
    claim(conn, first)
    with pytest.raises(ledger.CapExceeded, match="payment cap"):
        claim(other, second)
    assert state_of(other, second)["state"] == "needs_review"
    pending = other.execute("SELECT COUNT(*) FROM arrivals WHERE state = 'payment_pending'")
    assert pending.fetchone()[0] == 1
    other.close()


def test_cap_counts_paid_plus_pending_against_the_approved_total(conn):
    paid, over, fits = pair(conn, due="60", total="100") + pair(conn, due="40", total="100")[:1]
    with ledger.write_txn(conn):
        ledger.update_arrival(conn, paid, state="paid", amount_paid="60", currency="USD")
        ledger.update_arrival(conn, fits, amount_due="40")
    with pytest.raises(ledger.CapExceeded):
        claim(conn, over)  # 60 paid + 60 > 100
    claim(conn, fits)  # 60 paid + 40 == 100
    assert state_of(conn, fits)["state"] == "payment_pending"


def test_claim_refuses_when_the_identity_has_history_in_another_currency(conn):
    paid, other = pair(conn, due="40", total="100")
    with ledger.write_txn(conn):
        ledger.update_arrival(conn, paid, state="paid", amount_paid="40", currency="EUR")
    with pytest.raises(ledger.CapExceeded, match="another currency"):
        claim(conn, other)  # no reference rates: the cap cannot be proven across currencies
    assert state_of(conn, other)["state"] == "needs_review"
