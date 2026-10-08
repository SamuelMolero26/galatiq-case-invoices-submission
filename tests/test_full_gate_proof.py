"""Slice 2b.3: full-gate proof on the record_arrival path with online agents.

Stub_llm only (loopback), fake env, no live calls, no secrets.
"""

import argparse
import json

from invoice_pipeline import ledger, service
from invoice_pipeline.llm import FORMAT_TRIES
from invoice_pipeline.model import Ingested
from tests.factories import make_invoice

CONCUR = json.dumps(
    {
        "verdict": "concur",
        "evidence": ["invoice.total"],
        "rationale": "total matches the single line total",
    }
)


def args_for(tmp_path, llm):
    return argparse.Namespace(
        llm=llm, ledger=tmp_path / "ledger.db", inventory=tmp_path / "inventory.db"
    )


def bootstrap_auto_grok(tmp_path, monkeypatch, stub_llm):
    monkeypatch.setenv("XAI_API_KEY", "fake-key")
    monkeypatch.setenv("GROK_BASE_URL", stub_llm.url)
    return service.bootstrap(args_for(tmp_path, None))


def recorded(conn, arrival_id):
    row = conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()
    return service.arrival_result(conn, arrival_id), json.loads(row["record"])["decision"]


class TestFullGateProof:
    def test_concur_on_clean_invoice_approves_row_6(self, tmp_path, monkeypatch, stub_llm):
        stub_llm.script(stub_llm.reply(CONCUR))
        rt = bootstrap_auto_grok(tmp_path, monkeypatch, stub_llm)
        assert rt.tier == "grok"
        conn = ledger.connect(rt.ledger_path)
        try:
            arrival_id = service.record_arrival(
                conn, Ingested(invoice=make_invoice(), findings=[]), "proof.txt", rt
            )
            result, decision = recorded(conn, arrival_id)
        finally:
            conn.close()
        assert (result.decision, result.precedence_row) == ("approved", 6)
        assert result.reasons == ["no findings"]
        assert "UNREVIEWED_WARNINGS" not in " ".join(result.reasons)
        assert decision["unreviewed_warnings"] is False
        assert decision["escalate_review"]["answer"]["verdict"] == "concur"
        assert result.model_notes == "escalate_review"
        assert result.state == "payment_pending"
        assert len(stub_llm.requests) == 1

    def test_exhausted_critic_fails_closed_never_approved(
        self, tmp_path, monkeypatch, stub_llm
    ):
        stub_llm.script(*(stub_llm.reply(f"bad {n}") for n in range(FORMAT_TRIES)))
        rt = bootstrap_auto_grok(tmp_path, monkeypatch, stub_llm)
        assert rt.tier == "grok"
        conn = ledger.connect(rt.ledger_path)
        try:
            arrival_id = service.record_arrival(
                conn, Ingested(invoice=make_invoice(), findings=[]), "proof.txt", rt
            )
            result, decision = recorded(conn, arrival_id)
        finally:
            conn.close()
        assert (result.decision, result.precedence_row) == ("needs_review", 6)
        assert result.reasons[0].startswith("ESCALATE_ONLY_REVIEW_FAILED")
        assert result.state == "needs_review"
        assert decision["unreviewed_warnings"] is False
        assert decision["escalate_review"]["answer"] is None
        assert len(stub_llm.requests) == FORMAT_TRIES
