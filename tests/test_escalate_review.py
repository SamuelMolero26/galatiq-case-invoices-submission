"""Row 6 escalate-only review (REQ-ESC-1..5): characterization of the existing behavior."""

import dataclasses
import json

import pytest
from conftest import ScriptedChat, concur, tool_reply

from invoice_pipeline import catalog, service
from invoice_pipeline.approval import decide
from invoice_pipeline.critic import offline_agents, online_agents
from invoice_pipeline.llm import LLMError
from invoice_pipeline.model import Outcome


def test_concur_keeps_approved_with_rationale_and_tries(grok, make_case):
    agents = online_agents(grok, chat_fn=ScriptedChat(concur()))

    decision = decide(make_case(), agents)

    assert (decision.outcome, decision.precedence_row) == (Outcome.APPROVED, 6)
    assert decision.escalate_review.answer["rationale"] == "nothing needs a human"
    assert len(decision.escalate_review.tries) == 1


def test_escalate_answer_needs_review_no_payment(grok, make_case):
    chat = ScriptedChat(concur(verdict="escalate", rationale="odd vendor"))

    decision = decide(make_case(), online_agents(grok, chat_fn=chat))

    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.decided_by == "llm_critic"
    assert decision.reasons == ["ESCALATE_ONLY_REVIEW: odd vendor"]
    assert decision.escalate_review.answer["rationale"] == "odd vendor"


def test_raw_reject_x3_escalate_only_review_failed(grok, make_case):
    chat = ScriptedChat(*[concur(verdict="reject")] * 3)

    decision = decide(make_case(), online_agents(grok, chat_fn=chat))

    assert decision.outcome is Outcome.NEEDS_REVIEW
    assert decision.reasons[0].startswith("ESCALATE_ONLY_REVIEW_FAILED")
    call = decision.escalate_review
    assert len(call.tries) == 3 and call.answer is None and "3 tries" in call.error
    assert call.tries[0].correction and "verdict" in call.tries[0].correction


def test_timeout_failed_one_try(grok, make_case):
    chat = ScriptedChat(LLMError("timeout after 1s"))

    decision = decide(make_case(), online_agents(grok, chat_fn=chat))

    assert decision.outcome is Outcome.NEEDS_REVIEW
    assert decision.reasons == ["ESCALATE_ONLY_REVIEW_FAILED: timeout after 1s"]
    assert len(decision.escalate_review.tries) == 1


def test_tool_calls_reply_failed(grok, make_case):
    chat = ScriptedChat(tool_reply("get_reference_price", '{"sku": "WidgetA"}'))

    decision = decide(make_case(), online_agents(grok, chat_fn=chat))

    assert decision.outcome is Outcome.NEEDS_REVIEW
    assert "no tools" in decision.reasons[0] and decision.reasons[0].startswith(
        "ESCALATE_ONLY_REVIEW_FAILED"
    )
    assert len(chat.requests) == 1


def test_offline_tier_approved_error_offline_tier(make_case):
    decision = decide(make_case(), offline_agents())

    assert decision.outcome is Outcome.APPROVED and decision.precedence_row == 6
    assert decision.escalate_review.error == "offline tier"
    assert decision.escalate_review.tier == "offline"


def test_no_tools_or_verifier_called_on_row6(grok, make_case):
    calls = {"assess": 0, "verify": 0, "tools": 0}

    def counting(name):
        def call(*args, **kwargs):
            calls[name] += 1

        return call

    def factory(case_file):
        calls["tools"] += 1

    chat = ScriptedChat(concur())
    agents = online_agents(grok, factory, chat_fn=chat)
    agents = dataclasses.replace(agents, assess=counting("assess"), verify=counting("verify"))

    decision = decide(make_case(), agents)

    assert decision.outcome is Outcome.APPROVED
    assert calls == {"assess": 0, "verify": 0, "tools": 0}
    assert chat.requests[0]["tools"] is None


ROWS_1_TO_5 = {
    "row1-duplicate": dict(kind="duplicate"),
    "row2-rejection": dict(codes=["ITEM_UNKNOWN"]),
    "row3-review-trigger": dict(codes=["STOCK_SHORTAGE"]),
    "row4-heightened": dict(codes=["PRICE_DEVIATION"], quantity="50"),
    "row5-bound-failed": dict(codes=["VENDOR_UNKNOWN"]),
    "row5-full-gate": dict(codes=["PRICE_DEVIATION"]),
}


@pytest.mark.parametrize("row", ROWS_1_TO_5)
def test_escalate_never_called_rows_1_to_5(row, grok, make_case):
    agents = online_agents(grok, chat_fn=ScriptedChat(*[LLMError("unreachable")] * 8))
    called = []

    def forbidden(case_file):
        called.append(row)
        raise AssertionError("escalate_review must not run outside row 6")

    agents = dataclasses.replace(agents, escalate_review=forbidden)

    decision = decide(make_case(**ROWS_1_TO_5[row]), agents)

    assert called == [] and decision.precedence_row != 6
    assert decision.outcome is not Outcome.APPROVED


def test_service_level_zero_pay_calls_after_escalate(tmp_path, grok):
    invoice = {
        "invoice_number": "INV-9100",
        "vendor": {"name": "Precision Parts Ltd."},
        "date": "2026-01-22",
        "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250.00}],
        "subtotal": 250.00,
        "total": 250.00,
        "currency": "USD",
    }
    inventory, ledger_path = tmp_path / "inventory.db", tmp_path / "ledger.db"
    catalog.seed(inventory)
    path = tmp_path / "invoice.json"
    path.write_text(json.dumps(invoice))
    paid = []
    rt = service.Runtime(
        catalog=catalog.load_catalog(inventory),
        ledger_path=ledger_path,
        tier="grok",
        agents=online_agents(grok, chat_fn=ScriptedChat(concur(verdict="escalate"))),
        pay_fn=lambda *args: paid.append(args) or {"status": "success"},
    )

    result = service.process_path(path, rt).results[0]

    assert result.decision == Outcome.NEEDS_REVIEW and result.state == "needs_review"
    assert result.reasons[0].startswith("ESCALATE_ONLY_REVIEW:")
    assert paid == []
