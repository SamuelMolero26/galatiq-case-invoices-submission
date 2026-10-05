import json
from decimal import Decimal

import pytest

from invoice_pipeline import ledger
from invoice_pipeline.model import Decision, Finding, FindingCode, Ingested, Outcome, finding
from tests.factories import NOW, make_arrival, make_invoice

IDENTITY = ("widgets inc.", "INV-1001")


@pytest.fixture
def conn(tmp_path):
    connection = ledger.connect(tmp_path / "ledger.db")
    yield connection
    connection.close()


arrival = make_arrival


def row_of(conn, arrival_id):
    return conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM arrivals").fetchone()[0]


def test_read_identity_returns_rows_and_version_without_holding_a_lock(conn, tmp_path):
    first = ledger.record_if_unchanged(conn, IDENTITY, 0, arrival(Outcome.NEEDS_REVIEW))
    rows, version = ledger.read_identity(conn, IDENTITY)
    assert [(r.id, r.state) for r in rows] == [(first, "needs_review")]
    assert version == ledger.state_version(conn, IDENTITY) > 0
    other = ledger.connect(tmp_path / "ledger.db")  # a second writer commits right away
    with ledger.write_txn(other):
        ledger.insert_arrival(
            other,
            arrived_at="t",
            source="x",
            decision="rejected",
            state="logged_rejection",
            record="{}",
            vendor_key=IDENTITY[0],
            invoice_number=IDENTITY[1],
        )
    other.close()
    assert ledger.state_version(conn, IDENTITY) > version  # the earlier read is now stale


def test_unchanged_version_records_arrival_decision_and_findings_in_one_commit(conn):
    f = finding(FindingCode.STOCK_SHORTAGE, "short", line=0)
    new_id = ledger.record_if_unchanged(
        conn, IDENTITY, 0, arrival(Outcome.NEEDS_REVIEW, findings=[f])
    )
    row = row_of(conn, new_id)
    assert (row["state"], row["decision"], row["vendor_key"]) == (
        "needs_review",
        "needs_review",
        "widgets inc.",
    )
    assert (row["invoice_number"], row["vendor_name"], row["currency"]) == (
        "INV-1001",
        "Widgets Inc.",
        "USD",
    )
    assert (row["total"], row["amount_due"], row["source"]) == (
        "250.00",
        "250.00",
        "constructed.txt",
    )
    record = json.loads(row["record"])
    assert Finding.model_validate(record["findings"][0]) == f
    assert (
        record["decision"]["outcome"] == "needs_review"
        and record["invoice"]["vendor"] == "Widgets Inc."
    )


def test_approved_arrival_is_claimed_atomically_as_payment_pending_with_provisional_issue(conn):
    new_id = ledger.record_if_unchanged(conn, IDENTITY, 0, arrival(Outcome.APPROVED))
    row = row_of(conn, new_id)
    assert (row["state"], row["decision"], row["amount_due"]) == (
        "payment_pending",
        "approved",
        "250.00",
    )
    issue = json.loads(row["payment_issue"])
    assert "unconfirmed" in issue["what"] and issue["when"].startswith("2026-01-02T03:04:05")
    assert issue["bank_response"] is None


def test_rejected_and_duplicate_arrivals_map_to_their_terminal_states(conn):
    rejected = ledger.record_if_unchanged(conn, IDENTITY, 0, arrival(Outcome.REJECTED))
    assert row_of(conn, rejected)["state"] == "logged_rejection"
    version = ledger.state_version(conn, IDENTITY)
    dup = ledger.record_if_unchanged(
        conn, IDENTITY, version, arrival(Outcome.DUPLICATE, amount_due=None, duplicate_of=rejected)
    )
    row = row_of(conn, dup)
    assert (row["state"], row["duplicate_of"]) == ("duplicate", rejected)


@pytest.mark.parametrize("change", ["arrival", "resolution", "payment"])
def test_changed_version_writes_nothing_and_returns_none(conn, change):
    existing = ledger.record_if_unchanged(conn, IDENTITY, 0, arrival(Outcome.NEEDS_REVIEW))
    _, version = ledger.read_identity(conn, IDENTITY)
    with ledger.write_txn(conn):  # the competing write, on the same identity
        if change == "arrival":
            ledger.insert_arrival(
                conn,
                arrived_at="t",
                source="y",
                vendor_key=IDENTITY[0],
                invoice_number=IDENTITY[1],
                decision="rejected",
                state="logged_rejection",
                record="{}",
            )
        elif change == "resolution":
            ledger.update_arrival(conn, existing, resolution="reject", resolution_reason="no")
        else:
            ledger.update_arrival(conn, existing, state="paid", amount_paid="1", currency="USD")
    before = count(conn)
    assert ledger.record_if_unchanged(conn, IDENTITY, version, arrival(Outcome.APPROVED)) is None
    assert count(conn) == before
    assert ledger.state_version(conn, IDENTITY) > version


def test_exception_during_the_write_rolls_everything_back(conn):
    with pytest.raises(ValueError, match="payable"):
        ledger.record_if_unchanged(conn, IDENTITY, 0, arrival(Outcome.APPROVED, amount_due=None))
    assert count(conn) == 0
    assert not conn.in_transaction


def test_claim_over_the_approved_total_raises_and_records_nothing(conn):
    paid = arrival(Outcome.APPROVED)
    ledger.record_if_unchanged(conn, IDENTITY, 0, paid)
    with ledger.write_txn(conn):
        ledger.update_arrival(conn, 1, state="paid", amount_paid="250.00")
    version = ledger.state_version(conn, IDENTITY)
    with pytest.raises(ledger.CapExceeded):
        ledger.record_if_unchanged(conn, IDENTITY, version, arrival(Outcome.APPROVED))
    assert count(conn) == 1


def test_arrival_without_a_complete_identity_is_recorded_with_missing_columns_null(conn):
    partial = arrival(Outcome.NEEDS_REVIEW, invoice=make_invoice(vendor=None))
    new_id = ledger.record(conn, partial)
    row = row_of(conn, new_id)
    assert (row["vendor_key"], row["invoice_number"], row["vendor_name"]) == (
        None,
        "INV-1001",
        None,
    )


def test_unreadable_document_is_recorded_without_an_invoice(conn):
    reason = "parse: ValueError: boom"
    unreadable = Ingested(
        invoice=None,
        findings=[finding(FindingCode.UNREADABLE_DOCUMENT, reason)],
        unreadable_reason=reason,
    )
    item = ledger.Arrival(
        source="bad.json",
        arrived_at=NOW,
        ingested=unreadable,
        findings=unreadable.findings,
        decision=Decision(
            outcome=Outcome.NEEDS_REVIEW,
            reasons=[reason],
            precedence_row=3,
            decided_by="rule_engine",
        ),
        amount_due=None,
    )
    row = row_of(conn, ledger.record(conn, item))
    assert (row["vendor_key"], row["total"], row["state"]) == (None, None, "needs_review")
    assert json.loads(row["record"])["unreadable_reason"] == reason


def test_amounts_round_trip_as_exact_decimal_text(conn):
    invoice = make_invoice(total="1890.00", subtotal="1890.00")
    new_id = ledger.record_if_unchanged(conn, IDENTITY, 0, arrival(Outcome.NEEDS_REVIEW, invoice))
    assert Decimal(row_of(conn, new_id)["total"]) == Decimal("1890.00")
