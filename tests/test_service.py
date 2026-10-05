import argparse
import dataclasses
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_pipeline import ledger, service
from invoice_pipeline.catalog import DEFAULT_INVENTORY_PATH
from invoice_pipeline.critic import offline_role
from invoice_pipeline.model import Agents

CORPUS = Path(__file__).parent.parent / "data" / "invoices"


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        args = argparse.Namespace(
            llm=None, ledger=tmp_path / "ledger.db", inventory=tmp_path / "inventory.db"
        )
        self.events, self.calls, self.escalations = [], [], []
        self.fail_on = None  # invoice number whose escalate-only review raises
        self.rt = dataclasses.replace(
            service.bootstrap(args),
            pay_fn=self.bank,
            on_event=self.events.append,
            agents=Agents(
                assess=lambda *a, **k: None,
                verify=lambda *a, **k: None,
                escalate_review=self.escalate,
                advise=lambda case_file, decision: offline_role("advisory"),
            ),
        )

    def bank(self, vendor, amount, currency):
        self.calls.append((vendor, amount, currency))
        return {"status": "success"}

    def escalate(self, case_file):
        self.escalations.append(case_file.invoice.invoice_number)
        if case_file.invoice.invoice_number == self.fail_on:
            raise RuntimeError("model blew up")
        return offline_role("escalate_review")

    def names(self):
        return [e.name for e in self.events]

    def rows(self):
        conn = ledger.connect(self.rt.ledger_path)
        try:
            return conn.execute("SELECT * FROM arrivals ORDER BY id").fetchall()
        finally:
            conn.close()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def corpus(*names):
    return [CORPUS / n for n in names]


def test_bootstrap_seeds_a_missing_inventory_and_creates_the_ledger(env):
    assert (env.tmp / "inventory.db").exists() and env.rt.ledger_path.exists()
    assert env.rt.tier == "offline" and env.rt.catalog.stock["WidgetA"] == Decimal(15)
    assert not Path(DEFAULT_INVENTORY_PATH).resolve().is_relative_to(env.tmp)


def test_bootstrap_failures_are_reported_not_raised_raw(tmp_path):
    def args(**kw):
        base = dict(llm=None, ledger=tmp_path / "l.db", inventory=tmp_path / "i.db")
        return argparse.Namespace(**{**base, **kw})

    with pytest.raises(service.BootstrapError, match="grok"):
        service.bootstrap(args(llm="grok"))
    bad = tmp_path / "bad.db"
    sqlite3.connect(bad).execute("PRAGMA user_version = 7").connection.close()
    with pytest.raises(service.BootstrapError, match="inventory"):
        service.bootstrap(args(inventory=bad))
    with pytest.raises(service.BootstrapError, match="user_version"):
        service.bootstrap(args(ledger=bad))


def test_one_clean_offline_invoice_reaches_paid(env):
    result = service.process_path(CORPUS / "invoice_1001.txt", env.rt)
    assert (result.state, result.decision, result.finding_codes) == ("paid", "approved", [])
    assert env.calls == [("Widgets Inc.", Decimal("5000.00"), "USD")]
    assert env.names() == ["ingested", "validated", "decided", "payment_sent"]
    assert result.model_notes == "offline tier"


def test_rejected_invoice_is_logged_without_payment(env):
    result = service.process_path(CORPUS / "invoice_1003.txt", env.rt)
    assert (result.state, result.decision) == ("logged_rejection", "rejected")
    assert "ITEM_ZERO_STOCK" in result.finding_codes and env.calls == []
    assert "payment_sent" not in env.names()


def test_review_trigger_queues_the_invoice(env):
    result = service.process_path(CORPUS / "invoice_1002.txt", env.rt)
    assert result.state == "needs_review" and env.calls == []
    assert [i.arrival_id for i in service.review_queue(env.rt)] == [result.arrival_id]


def test_unreadable_document_queues_without_validation_model_or_payment(env, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    result = service.process_path(bad, env.rt)
    assert (result.state, result.finding_codes) == ("needs_review", ["UNREADABLE_DOCUMENT"])
    assert env.names() == ["ingested", "decided"] and env.escalations == [] and env.calls == []
    assert (result.vendor, result.invoice_number) == (None, None)


def test_post_ingestion_exception_writes_no_row(env):
    env.fail_on = "INV-1001"
    with pytest.raises(service.ProcessingFailure) as failure:
        service.process_path(CORPUS / "invoice_1001.txt", env.rt)
    assert (failure.value.file, failure.value.stage) == ("invoice_1001.txt", "approval")
    assert env.rows() == [] and env.calls == []


def test_one_failed_file_does_not_stop_its_neighbors(env):
    env.fail_on = "INV-1004"
    names = ("invoice_1001.txt", "invoice_1004.json", "invoice_1011.pdf")
    batch = service.run_batch(corpus(*names), env.rt)
    assert [r.source for r in batch.results] == ["invoice_1001.txt", "invoice_1011.pdf"]
    [failure] = batch.failed
    assert (failure.file, failure.stage) == ("invoice_1004.json", "approval")
    assert "model blew up" in failure.error and len(env.calls) == 2
    assert [e.name for e in env.events if e.name == "file_failed"] == ["file_failed"]
    assert batch.counts() == {"paid": 2}
    assert [r["source"] for r in env.rows()] == ["invoice_1001.txt", "invoice_1011.pdf"]


def test_a_failure_after_the_claim_commits_keeps_the_claim(env, monkeypatch):
    def interrupted(*args):
        raise RuntimeError("process interrupted")

    monkeypatch.setattr(service.payment, "pay", interrupted)
    batch = service.run_batch(corpus("invoice_1001.txt"), env.rt)
    assert [(f.stage, f.file) for f in batch.failed] == [("payment", "invoice_1001.txt")]
    [row] = env.rows()
    assert row["state"] == "payment_pending" and row["payment_issue"] and env.calls == []


def test_a_failing_bank_leaves_payment_pending_in_the_queue(env):
    env.rt = dataclasses.replace(env.rt, pay_fn=lambda *a: {"status": "declined"})
    result = service.process_path(CORPUS / "invoice_1001.txt", env.rt)
    assert result.state == "payment_pending" and "payment_failed" in env.names()
    assert [i.state for i in service.review_queue(env.rt)] == ["payment_pending"]


def test_a_rerun_makes_clean_copies_duplicates_with_no_second_payment(env):
    first = service.process_path(CORPUS / "invoice_1001.txt", env.rt)
    second = service.process_path(CORPUS / "invoice_1001.txt", env.rt)
    assert (first.state, second.state) == ("paid", "duplicate") and len(env.calls) == 1
    assert second.reasons == [
        f"DUPLICATE_PAYMENT: already paid 5000.00 USD on arrival #{first.arrival_id}"
    ]
    assert second.model_notes == "none"


def test_offline_batch_makes_no_network_call_and_never_critic_approves_warnings(env, no_network):
    names = ("invoice_1001.txt", "invoice_1002.txt", "invoice_1003.txt", "invoice_1010.txt")
    batch = service.run_batch(corpus(*names), env.rt)
    assert batch.failed == []
    by_source = {r.source: r for r in batch.results}
    warning = by_source["invoice_1010.txt"]
    assert "PRICE_DEVIATION" in warning.finding_codes and warning.state == "needs_review"
    assert batch.counts() == {"paid": 1, "needs_review": 2, "logged_rejection": 1}


def test_collect_files_is_one_file_or_a_directory_in_lexical_order(tmp_path):
    for name in ("b.json", "a_revised.json", "a.json"):
        (tmp_path / name).write_text("{}")
    assert [p.name for p in service.collect_files(tmp_path)] == [
        "a.json",
        "a_revised.json",
        "b.json",
    ]
    assert service.collect_files(tmp_path / "b.json") == [tmp_path / "b.json"]


def test_review_queue_is_the_ledger_query_and_writes_nothing(env):
    service.run_batch(corpus("invoice_1002.txt", "invoice_1005.json"), env.rt)
    before = [tuple(r) for r in env.rows()]
    assert [i.source for i in service.review_queue(env.rt)] == [
        "invoice_1002.txt",
        "invoice_1005.json",
    ]
    assert [tuple(r) for r in env.rows()] == before
