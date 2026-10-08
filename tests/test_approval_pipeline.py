"""The full gate through the real service loop: scripted transport, real ledger and tools."""

import json

from conftest import ScriptedChat, text_reply, tool_reply

from invoice_pipeline import catalog, ledger, service
from invoice_pipeline.critic import online_agents
from invoice_pipeline.model import Outcome

INVOICE = {
    "invoice_number": "INV-9001",
    "vendor": {"name": "Precision Parts Ltd."},
    "date": "2026-01-22",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 300.00}],
    "subtotal": 300.00,
    "total": 300.00,
    "currency": "USD",
}


def run(tmp_path, grok, chat, invoice=INVOICE):
    inventory, ledger_path = tmp_path / "inventory.db", tmp_path / "ledger.db"
    catalog.seed(inventory)
    path = tmp_path / "invoice.json"
    path.write_text(json.dumps(invoice))
    agents = online_agents(grok, service.tool_factory(inventory, ledger_path), chat_fn=chat)
    rt = service.Runtime(
        catalog=catalog.load_catalog(inventory), ledger_path=ledger_path, tier="grok", agents=agents
    )
    result = service.process_path(path, rt)
    assert not result.failed
    return ledger_path, result.results[0]


def test_persisted_critic_is_reported_in_the_public_result(tmp_path, grok):
    chat = ScriptedChat(
        tool_reply("get_reference_price", '{"sku": "WidgetA"}'),
        text_reply(
            json.dumps(
                {
                    "assessments": [
                        {
                            "code": "PRICE_DEVIATION",
                            "line": 0,
                            "explained": True,
                            "evidence": ["invoice.items.0.unit_price", "tool.0.result.unit_price"],
                            "rationale": "price differs from the reference by a known amount",
                        }
                    ]
                }
            )
        ),
        text_reply(
            json.dumps(
                {
                    "checks": [
                        {"code": "PRICE_DEVIATION", "line": 0, "holds": True, "rationale": "ok"}
                    ]
                }
            )
        ),
    )
    ledger_path, result = run(tmp_path, grok, chat)
    assert result.decision == Outcome.APPROVED and result.state == "paid"
    assert result.model_notes == "critic"
    conn = ledger.connect(ledger_path)
    record = json.loads(conn.execute("SELECT record FROM arrivals").fetchone()[0])
    assert record["decision"]["critic"] is not None


def test_transport_failure_leaves_the_invoice_in_review_unpaid(tmp_path, grok):
    from invoice_pipeline.llm import LLMError

    _, result = run(tmp_path, grok, ScriptedChat(LLMError("timeout after 30s")))
    assert result.decision == Outcome.NEEDS_REVIEW and result.state == "needs_review"
    assert "UNREVIEWED_WARNINGS" in result.reasons[0]


def test_escalate_review_model_notes_are_preserved(tmp_path, grok):
    clean = {
        **INVOICE,
        "line_items": [{**INVOICE["line_items"][0], "unit_price": 250.00}],
        "subtotal": 250.00,
        "total": 250.00,
    }
    reply = text_reply(
        json.dumps(
            {
                "verdict": "concur",
                "evidence": ["invoice.total"],
                "rationale": "the invoice matches the catalog",
            }
        )
    )

    _, result = run(tmp_path, grok, ScriptedChat(reply), clean)

    assert result.decision == Outcome.APPROVED
    assert result.model_notes == "escalate_review"


def test_advisory_model_notes_are_preserved(tmp_path, grok):
    rejected = {
        **INVOICE,
        "line_items": [{**INVOICE["line_items"][0], "item": "UnlistedPart"}],
    }
    reply = text_reply(json.dumps({"rationale": "the item is not in the catalog"}))

    _, result = run(tmp_path, grok, ScriptedChat(reply), rejected)

    assert result.decision == Outcome.REJECTED
    assert result.model_notes == "advisory"
