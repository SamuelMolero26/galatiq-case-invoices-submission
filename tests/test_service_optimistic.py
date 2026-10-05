import json

import pytest

from invoice_pipeline import ledger, service
from invoice_pipeline.critic import offline_role
from invoice_pipeline.model import Agents, Ingested, Outcome
from tests.factories import NOW, make_arrival, make_catalog, make_invoice

IDENTITY = ("widgets inc.", "INV-1001")


class Harness:
    """A Runtime whose clean-invoice review hook can commit competing writes mid-decide."""

    def __init__(self, tmp_path, competing=None, review=None):
        self.path = tmp_path / "ledger.db"
        self.events, self.calls = [], 0
        self.competing = competing or (lambda attempt, other: None)
        self.in_txn_while_deciding = []
        self.rt = service.Runtime(
            catalog=make_catalog(),
            ledger_path=self.path,
            tier="offline",
            agents=Agents(
                assess=lambda *a, **k: None,
                verify=lambda *a, **k: None,
                escalate_review=review or self._escalate,
                advise=lambda case_file, decision: offline_role("advisory"),
            ),
            on_event=self.events.append,
            now=lambda: NOW,
        )
        self.conn = ledger.connect(self.path)

    def _escalate(self, case_file):
        self.calls += 1
        self.in_txn_while_deciding.append(self.conn.in_transaction)
        other = ledger.connect(self.path)
        self.competing(self.calls, other)
        other.close()
        call = offline_role("escalate_review")
        return call.model_copy(update={"model": f"call-{self.calls}"})

    def record(self, invoice=None):
        invoice = invoice or make_invoice()
        return service.record_arrival(
            self.conn, Ingested(invoice=invoice, findings=[]), invoice.source_path, self.rt
        )

    def rows(self):
        return self.conn.execute("SELECT * FROM arrivals ORDER BY id").fetchall()

    def names(self):
        return [e.name for e in self.events]


def seed_review(conn):
    """An earlier arrival of the identity, waiting in Needs Review."""
    return ledger.record(conn, make_arrival(Outcome.NEEDS_REVIEW, make_invoice()))


def reject_competitor(other):
    with ledger.write_txn(other):
        ledger.insert_arrival(
            other,
            arrived_at="t",
            source="x",
            vendor_key=IDENTITY[0],
            invoice_number=IDENTITY[1],
            currency="USD",
            decision="rejected",
            state="logged_rejection",
            record="{}",
        )


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.conn.close()


def test_unchanged_identity_is_recorded_and_claimed_in_one_round(h):
    arrival_id = h.record()
    assert h.calls == 1 and "redecided" not in h.names()
    row = h.rows()[0]
    assert (row["id"], row["state"]) == (arrival_id, "payment_pending")  # claimed, not yet paid


def test_a_competing_resolution_turns_the_next_decision_into_duplicate(tmp_path):
    def resolve_earlier(attempt, other):
        with ledger.write_txn(other):  # the Reviewer approves and pays the earlier arrival
            ledger.update_arrival(
                other, 1, state="paid", amount_paid="250.00", resolution="approve"
            )

    harness = Harness(tmp_path, resolve_earlier)
    seed_review(harness.conn)
    new_id = harness.record()
    row = harness.rows()[-1]
    assert (row["id"], row["state"], row["duplicate_of"]) == (new_id, "duplicate", 1)
    assert harness.names().count("redecided") == 1
    assert harness.calls == 1  # the Duplicate re-decision makes no model call
    assert len(harness.rows()) == 2


def test_each_version_mismatch_emits_redecided_once_and_only_the_final_audit_is_stored(tmp_path):
    harness = Harness(tmp_path, lambda attempt, other: attempt < 3 and reject_competitor(other))
    new_id = harness.record()
    assert harness.calls == 3
    assert harness.names().count("redecided") == 2
    rows = harness.rows()
    assert len(rows) == 3  # two competitors plus exactly one recorded arrival
    mine = next(r for r in rows if r["id"] == new_id)
    assert json.loads(mine["record"])["decision"]["escalate_review"]["model"] == "call-3"


def test_three_consecutive_changes_are_a_processing_failure_with_no_row(tmp_path):
    harness = Harness(tmp_path, lambda attempt, other: reject_competitor(other))
    with pytest.raises(service.ProcessingFailure) as failure:
        harness.record()
    assert failure.value.stage == "ledger" and failure.value.file == "constructed.txt"
    assert (
        "identity" in failure.value.error
        and str(service.MAX_DECIDE_ATTEMPTS) in failure.value.error
    )
    assert harness.calls == service.MAX_DECIDE_ATTEMPTS == 3
    assert [r["source"] for r in harness.rows()] == ["x"] * 3  # only the competing writes
    assert harness.names().count("redecided") == 2
    assert not harness.conn.in_transaction


def test_no_lock_is_held_while_deciding(tmp_path):
    seen = []

    def unrelated_write(attempt, other):  # blocks for the busy timeout if a lock were held
        with ledger.write_txn(other):
            ledger.insert_arrival(
                other,
                arrived_at="t",
                source="u",
                vendor_key="other",
                invoice_number="INV-9",
                decision="rejected",
                state="logged_rejection",
                record="{}",
            )
        seen.append("committed")

    harness = Harness(tmp_path, unrelated_write)
    harness.record()
    assert seen == ["committed"] and harness.in_txn_while_deciding == [False]
    assert "redecided" not in harness.names()  # an unrelated identity does not move the version


def test_partial_identity_skips_the_version_check(h):
    arrival_id = h.record(make_invoice(vendor=None))
    row = next(r for r in h.rows() if r["id"] == arrival_id)
    assert (row["vendor_key"], row["state"]) == (None, "needs_review")
    assert "redecided" not in h.names()


def test_exception_while_deciding_writes_no_row(tmp_path):
    def boom(case_file):
        raise RuntimeError("model blew up")

    harness = Harness(tmp_path, review=boom)
    with pytest.raises(service.ProcessingFailure) as failure:
        harness.record()
    assert failure.value.stage == "approval" and "model blew up" in failure.value.error
    assert harness.rows() == []
