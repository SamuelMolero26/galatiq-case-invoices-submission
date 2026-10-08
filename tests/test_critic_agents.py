"""critic.assess / critic.verify against a scripted transport (zero network)."""

import json

import pytest
from conftest import ScriptedChat, text_reply, tool_reply

from invoice_pipeline.critic import assess, offline_agents, online_agents, verify
from invoice_pipeline.llm import LLMError
from invoice_pipeline.model import FindingCode, GuardrailCause, WarningAssessment
from invoice_pipeline.tools import TOOL_SCHEMAS

PRICE = FindingCode.PRICE_DEVIATION
GOOD = ["invoice.items.0.unit_price", "references.reference_prices.WidgetA"]


def answer(explained=True, evidence=None, code="PRICE_DEVIATION", line=0):
    entry = {
        "code": code,
        "line": line,
        "explained": explained,
        "evidence": GOOD if evidence is None else evidence,
        "rationale": "because",
    }
    return json.dumps({"assessments": [entry]})


def checks(holds=True):
    return json.dumps(
        {"checks": [{"code": "PRICE_DEVIATION", "line": 0, "holds": holds, "rationale": "ok"}]}
    )


@pytest.fixture
def case_file(make_case_file):
    return make_case_file(items=[("WidgetA", "300")])


def test_a_grounded_answer_is_accepted_without_tools_when_there_is_no_runner(grok, case_file):
    chat = ScriptedChat(text_reply(answer()))
    call = assess(grok, case_file, attempt=1, chat_fn=chat)
    assert call.accepted and call.error is None and call.model == "test-model"
    assert call.assessments[0].code is PRICE and len(call.tries) == 1
    assert chat.requests[0]["tools"] is None
    user = chat.requests[0]["messages"][1]["content"]
    assert "decision_context" not in user and "PRICE_DEVIATION" in user


def test_tool_calls_are_run_recorded_and_citable_as_evidence(grok, case_file, tool_runner):
    runner = tool_runner()
    chat = ScriptedChat(
        tool_reply("get_reference_price", '{"sku": "WidgetA"}'),
        text_reply(answer(evidence=["tool.0.result.unit_price"])),
    )
    call = assess(grok, case_file, runner=runner, attempt=1, chat_fn=chat)
    assert call.accepted
    assert chat.requests[0]["tools"] == TOOL_SCHEMAS
    assert [c.name for c in call.tool_calls] == ["get_reference_price"]
    assert call.tool_calls[0].result["found"] is True
    assert chat.requests[1]["messages"][-1]["role"] == "tool"


def test_a_guardrail_failure_drives_a_wrapper_correction_naming_the_cause(grok, case_file):
    chat = ScriptedChat(text_reply(answer(evidence=["invoice.items.9.sku"])), text_reply(answer()))
    call = assess(grok, case_file, attempt=1, chat_fn=chat)
    assert call.accepted and len(call.tries) == 2
    correction = chat.requests[1]["messages"][-1]["content"]
    assert "unresolved_evidence" in correction and "PRICE_DEVIATION line 0" in correction
    assert call.tries[0].correction == correction


def test_exhausted_guardrail_corrections_are_not_accepted_and_keep_the_failures(grok, case_file):
    bad = text_reply(answer(evidence=["invoice.items.9.sku"]))
    call = assess(grok, case_file, attempt=1, chat_fn=ScriptedChat(bad, bad, bad))
    assert not call.accepted and call.exhausted and len(call.tries) == 3
    assert [f.cause for f in call.failures] == [GuardrailCause.UNRESOLVED_EVIDENCE]
    assert call.error and "3 tries" in call.error


def test_unexplained_finality_stops_at_once_without_correction(grok, case_file):
    chat = ScriptedChat(text_reply(answer(explained=False, evidence=[])))
    call = assess(grok, case_file, attempt=1, chat_fn=chat)
    assert not call.accepted and not call.exhausted and len(call.tries) == 1
    assert call.failures[0].cause is GuardrailCause.UNEXPLAINED
    assert call.assessments and not call.assessments[0].explained


def test_the_tool_budget_is_shared_across_attempts_and_the_failing_call_is_kept(
    grok, case_file, tool_runner
):
    runner = tool_runner(budget=1)
    first = ScriptedChat(
        tool_reply("get_stock_level", '{"sku": "WidgetA"}'),
        text_reply(answer()),
    )
    assert assess(grok, case_file, runner=runner, attempt=1, chat_fn=first).accepted
    second = ScriptedChat(tool_reply("get_stock_level", '{"sku": "WidgetA"}'))
    call = assess(grok, case_file, runner=runner, attempt=2, chat_fn=second)
    assert not call.accepted and "budget" in call.error
    assert len(runner.calls) == 2 and runner.calls[1].error and runner.calls[1].attempt == 2
    assert [c.index for c in call.tool_calls] == [1]


def test_an_unknown_tool_is_a_recorded_tool_failure_not_an_exception(grok, case_file, tool_runner):
    runner = tool_runner()
    chat = ScriptedChat(tool_reply("drop_table", "{}"))
    call = assess(grok, case_file, runner=runner, attempt=1, chat_fn=chat)
    assert not call.accepted and "tool failure" in call.error
    assert runner.calls[0].error


def test_attempt_two_receives_the_verifier_feedback(grok, case_file):
    chat = ScriptedChat(text_reply(answer()))
    call = assess(grok, case_file, attempt=2, feedback="line 0 claim is false", chat_fn=chat)
    assert call.accepted and call.attempt == 2
    assert any("line 0 claim is false" in m["content"] for m in chat.requests[0]["messages"])


def test_offline_makes_no_request_and_records_the_tier(case_file):
    from invoice_pipeline.llm import TierConfig

    def boom(*args, **kwargs):
        raise AssertionError("no request offline")

    call = assess(TierConfig(tier="offline"), case_file, attempt=1, chat_fn=boom)
    assert call.error == "offline tier" and not call.accepted and call.tries == []


def test_transport_and_unexpected_failures_return_audit_records(grok, case_file):
    chat = ScriptedChat(LLMError("timeout after 1s"))
    call = assess(grok, case_file, attempt=1, chat_fn=chat)
    assert not call.accepted and call.error == "timeout after 1s" and len(call.tries) == 1
    broken = assess(grok, case_file, attempt=1, chat_fn=ScriptedChat(RuntimeError("boom")))
    assert not broken.accepted and "boom" in broken.error


# --- verify ---------------------------------------------------------------------------------


def accepted_assessments():
    return [
        WarningAssessment(code=PRICE, line=0, explained=True, evidence=GOOD, rationale="because")
    ]


def test_verifier_gets_assessments_and_recorded_tool_results_but_no_tools(
    grok, case_file, tool_runner
):
    runner = tool_runner()
    runner.run(1, "get_reference_price", '{"sku": "WidgetA"}')
    chat = ScriptedChat(text_reply(checks()))
    call = verify(grok, case_file, accepted_assessments(), runner.calls, attempt=1, chat_fn=chat)
    assert call.accepted and call.checks[0].holds is True and call.error is None
    request = chat.requests[0]
    assert request["tools"] is None
    sent = request["messages"][1]["content"]
    assert "get_reference_price" in sent and "because" in sent


def test_a_missing_verifier_check_is_corrected_inside_the_wrapper(grok, case_file):
    chat = ScriptedChat(text_reply('{"checks": []}'), text_reply(checks()))
    call = verify(grok, case_file, accepted_assessments(), [], attempt=1, chat_fn=chat)
    assert call.accepted and len(call.tries) == 2
    assert "missing check for PRICE_DEVIATION line 0" in chat.requests[1]["messages"][-1]["content"]


def test_verifier_failures_return_audit_records(grok, case_file):
    from invoice_pipeline.llm import TierConfig

    call = verify(
        grok, case_file, accepted_assessments(), [], 1, chat_fn=ScriptedChat(LLMError("down"))
    )
    assert not call.accepted and call.error == "down"
    offline = verify(TierConfig(tier="offline"), case_file, accepted_assessments(), [], 1)
    assert offline.error == "offline tier" and offline.tries == []
    junk = ScriptedChat(*[text_reply("nope")] * 3)
    exhausted = verify(grok, case_file, accepted_assessments(), [], 1, chat_fn=junk)
    assert not exhausted.accepted and "3 tries" in exhausted.error


# --- Agents wiring --------------------------------------------------------------------------


def test_online_agents_run_the_gate_roles_and_register_tool_cleanup(grok, case_file, tool_runner):
    closed = []
    runner = tool_runner()
    runner.close = lambda: closed.append(True)
    chat = ScriptedChat(text_reply(answer()), text_reply(checks()))
    agents = online_agents(grok, tool_factory=lambda cf: runner, chat_fn=chat)
    scratch: dict = {}
    assessed = agents.assess(case_file, 1, None, scratch)
    assert assessed.accepted and chat.requests[0]["tools"] == TOOL_SCHEMAS
    verified = agents.verify(case_file, assessed.assessments, runner.calls, 1)
    assert verified.accepted
    for cleanup in scratch["cleanup"]:
        cleanup()
    assert closed == [True]


def test_a_failing_tool_factory_is_a_recorded_failure(grok, case_file):
    def factory(cf):
        raise OSError("ledger missing")

    agents = online_agents(grok, tool_factory=factory, chat_fn=ScriptedChat())
    call = agents.assess(case_file, 1, None, {})
    assert not call.accepted and "ledger missing" in call.error


def test_offline_agents_give_no_gate_answer(case_file):
    agents = offline_agents()
    assert agents.assess(case_file, 1, None, {}) is None
    assert agents.verify(case_file, [], [], 1) is None
