"""Payment: pay an already-claimed arrival through an injected callable.

The claim (cap guard + Payment Pending + provisional Payment Issue) is committed by the caller's
write transaction (`ledger.claim`) before this module runs. The bank is called with no
transaction open, and there is no automatic retry and no settlement operation: an unconfirmed
payment stays Payment Pending for a human to check outside the system.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from invoice_pipeline import ledger
from invoice_pipeline.model import PaymentIssue

PayFn = Callable[[str, Decimal, str], dict]  # (vendor, amount, currency) -> {"status": ...}

log = logging.getLogger(__name__)


def mock_payment(vendor: str, amount: Decimal, currency: str) -> dict:
    """The default bank: simulated locally, always confirms."""
    log.info("mock payment: %s %s to %s", amount, currency, vendor)
    return {"status": "success"}


def _failure(what: str, now: datetime, response: dict | None = None) -> PaymentIssue:
    return PaymentIssue(what=what, when=now, bank_response=response)


def pay(
    conn: sqlite3.Connection, arrival_id: int, pay_fn: PayFn, now: Callable[[], datetime]
) -> PaymentIssue | None:
    """Call the bank once for the claimed amount: None when Paid, else the Payment Issue."""
    row = conn.execute(
        "SELECT vendor_name, currency, amount_due FROM arrivals"
        " WHERE id = ? AND state = 'payment_pending'",
        (arrival_id,),
    ).fetchone()
    amount = Decimal(row["amount_due"])
    try:
        result = pay_fn(row["vendor_name"], amount, row["currency"])
    except Exception as exc:  # refusal, connection error, timeout: all unconfirmed
        issue = _failure(f"payment call failed: {type(exc).__name__}: {exc}", now())
    else:
        status = result.get("status") if isinstance(result, dict) else None
        if status == "success":
            with ledger.write_txn(conn):
                ledger.update_arrival(
                    conn, arrival_id, state="paid", amount_paid=str(amount), payment_issue=None
                )
            return None
        issue = _failure(
            f"bank returned status {status!r}", now(), result if isinstance(result, dict) else None
        )
    with ledger.write_txn(conn):
        ledger.update_arrival(conn, arrival_id, payment_issue=issue.model_dump_json())
    return issue
