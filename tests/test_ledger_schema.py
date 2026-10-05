import sqlite3

import pytest

from invoice_pipeline import ledger

ISSUE = '{"what": "attempt initiated; bank outcome unconfirmed", "when": "2026-01-01T00:00:00Z"}'


def put(conn, **cols):
    base = dict(
        arrived_at="2026-01-01T00:00:00+00:00",
        source="a.txt",
        vendor_key="acme",
        invoice_number="INV-1",
        decision="needs_review",
        state="needs_review",
        record="{}",
    )
    return ledger.insert_arrival(conn, **{**base, **cols})


@pytest.fixture
def conn(tmp_path):
    connection = ledger.connect(tmp_path / "ledger.db")
    yield connection
    connection.close()


def seq_of(conn, arrival_id):
    return conn.execute("SELECT seq FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()[0]


def test_new_ledger_is_wal_with_one_table_and_schema_version(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert tables == ["arrivals"]


def test_reopening_keeps_the_rows(tmp_path):
    first = ledger.connect(tmp_path / "l.db")
    put(first)
    first.close()
    second = ledger.connect(tmp_path / "l.db")
    assert second.execute("SELECT COUNT(*) FROM arrivals").fetchone()[0] == 1
    second.close()


def test_wrong_schema_version_fails(tmp_path):
    path = tmp_path / "l.db"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(ledger.LedgerError, match="user_version 99"):
        ledger.connect(path)


def test_every_insert_and_update_takes_the_next_global_seq(conn):
    first, second = put(conn), put(conn, vendor_key="other")
    assert (seq_of(conn, first), seq_of(conn, second)) == (1, 2)
    ledger.update_arrival(conn, first, state="logged_rejection", decision="rejected")
    assert seq_of(conn, first) == 3
    assert seq_of(conn, second) == 2
    assert put(conn, vendor_key="third") == 3 and seq_of(conn, 3) == 4


def test_identity_version_is_max_seq_of_its_rows_or_zero(conn):
    identity = ("acme", "INV-1")
    assert ledger.state_version(conn, identity) == 0
    a = put(conn)
    put(conn, vendor_key="other")  # another identity moves the global seq, not this version
    assert ledger.state_version(conn, identity) == seq_of(conn, a) == 1
    ledger.update_arrival(conn, a, resolution="reject", resolution_reason="no")
    assert ledger.state_version(conn, identity) == 3


def test_amounts_are_text_with_currency(conn):
    a = put(conn, total="1890.00", currency="USD")
    assert conn.execute("SELECT typeof(total), total FROM arrivals WHERE id = ?", (a,)).fetchone()[
        :
    ] == ("text", "1890.00")


@pytest.mark.parametrize(
    "cols",
    [
        dict(state="payment_pending", amount_due="5", currency="USD"),  # no issue
        dict(state="payment_pending", payment_issue=ISSUE, currency="USD"),  # no amount_due
        dict(state="payment_pending", payment_issue=ISSUE, amount_due="5"),  # no currency
        dict(state="paid", currency="USD"),  # no amount_paid
        dict(state="paid", amount_paid="5"),  # no currency
        dict(decision="duplicate", state="needs_review"),
        dict(decision="approved", state="duplicate", duplicate_of=1),
        dict(decision="duplicate", state="duplicate"),  # no link
        dict(state="bogus"),
        dict(decision="bogus"),
        dict(resolution="settle"),
    ],
)
def test_check_constraints_reject_invalid_rows(conn, cols):
    put(conn)  # arrival 1 exists so a duplicate link is not the failing part
    with pytest.raises(sqlite3.IntegrityError):
        put(conn, vendor_key="other", **cols)


def test_valid_payment_pending_paid_and_duplicate_rows_are_accepted(conn):
    paid = put(conn, state="paid", decision="approved", amount_paid="5", currency="USD")
    put(
        conn,
        vendor_key="b",
        state="payment_pending",
        decision="approved",
        payment_issue=ISSUE,
        amount_due="5",
        currency="USD",
    )
    put(conn, state="duplicate", decision="duplicate", duplicate_of=paid)


def test_only_one_pending_row_per_complete_identity(conn):
    pending = dict(
        state="payment_pending",
        decision="approved",
        payment_issue=ISSUE,
        amount_due="5",
        currency="USD",
    )
    put(conn, **pending)
    with pytest.raises(sqlite3.IntegrityError):
        put(conn, **pending)
    put(conn, vendor_key="other", **pending)
    put(conn, vendor_key=None, invoice_number=None, **{**pending, "state": "needs_review"})
