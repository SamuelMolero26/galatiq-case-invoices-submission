import json

import pytest

from invoice_pipeline.llm import (
    FORMAT_TRIES,
    CorrectableError,
    FinalError,
    TierConfig,
    ask,
    role_call,
)

MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "case"}]
KNOWN_PATHS = {"invoice.notes"}


def tier(stub, timeout=5) -> TierConfig:
    return TierConfig(
        tier="grok", model="m", base_url=stub.url, timeout_s=timeout, api_key="sekret-key"
    )


def validate(content: str):
    """A role-like validator: parse, type, and citation checks may all ask for a correction."""
    try:
        data = json.loads(content)
    except ValueError as exc:
        return CorrectableError(f"answer is not valid JSON: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("explained"), bool):
        return CorrectableError("field 'explained' must be a boolean")
    if data.get("evidence") not in KNOWN_PATHS:
        return CorrectableError(f"evidence path {data.get('evidence')!r} is not in the Case File")
    return data


GOOD = json.dumps({"explained": True, "evidence": "invoice.notes"})


def test_valid_first_answer_stops_after_one_request(stub_llm):
    stub_llm.script(stub_llm.reply(GOOD))
    asked = ask(tier(stub_llm), MESSAGES, validate)
    assert asked.value == {"explained": True, "evidence": "invoice.notes"}
    assert (asked.error, asked.exhausted, len(stub_llm.requests)) == (None, False, 1)
    (only,) = asked.tries
    assert only.correction is None
    (exchange,) = only.exchanges
    assert exchange.raw_answer == GOOD and exchange.error is None
    assert exchange.called_at.tzinfo is not None and exchange.elapsed_ms >= 0


def test_format_error_is_corrected_on_the_second_try(stub_llm):
    stub_llm.script(stub_llm.reply("free prose"), stub_llm.reply(GOOD))
    asked = ask(tier(stub_llm), MESSAGES, validate)
    assert asked.value is not None and len(stub_llm.requests) == 2
    first, second = asked.tries
    assert first.correction.startswith("answer is not valid JSON")
    assert second.correction is None
    sent = stub_llm.requests[1]["body"]["messages"]
    assert sent[:2] == MESSAGES
    assert sent[2] == {"role": "assistant", "content": "free prose"}
    assert sent[3] == {"role": "user", "content": first.correction}


@pytest.mark.parametrize(
    ("answer", "failure"),
    [
        ("free prose", "not valid JSON"),
        (json.dumps({"explained": "true", "evidence": "invoice.notes"}), "must be a boolean"),
        (json.dumps({"explained": True, "evidence": "invoice.po"}), "'invoice.po' is not in"),
    ],
)
def test_parse_type_and_citation_failures_are_echoed_exactly(stub_llm, answer, failure):
    stub_llm.script(stub_llm.reply(answer), stub_llm.reply(GOOD))
    asked = ask(tier(stub_llm), MESSAGES, validate)
    correction = stub_llm.requests[1]["body"]["messages"][-1]["content"]
    assert failure in correction and correction == asked.tries[0].correction


def test_three_bad_answers_stop_and_fail_closed(stub_llm):
    stub_llm.script(*(stub_llm.reply(f"bad {n}") for n in range(5)))
    asked = ask(tier(stub_llm), MESSAGES, validate)
    assert FORMAT_TRIES == 3 and len(stub_llm.requests) == 3
    assert asked.value is None and asked.exhausted
    assert "3 tries" in asked.error and "not valid JSON" in asked.error
    assert [t.correction is None for t in asked.tries] == [False, False, True]


def test_explicit_corrective_role_override_is_honored(stub_llm):
    stub_llm.script(stub_llm.reply("prose"), stub_llm.reply(GOOD))
    ask(tier(stub_llm), MESSAGES, validate, corrective_role="user")
    assert stub_llm.requests[1]["body"]["messages"][-1]["role"] == "user"


def test_http_error_after_a_correction_stops_at_once(stub_llm):
    stub_llm.script(
        stub_llm.reply("free prose"),
        ("status", 400, "messages refused"),
        stub_llm.reply(GOOD),
    )
    asked = ask(tier(stub_llm), MESSAGES, validate)
    assert asked.value is None and not asked.exhausted and "HTTP 400" in asked.error
    assert len(stub_llm.requests) == 2 and len(asked.tries) == 2
    corrected = stub_llm.requests[1]["body"]["messages"][-1]
    assert corrected == {"role": "user", "content": asked.tries[0].correction}
    (failed,) = asked.tries[1].exchanges
    assert failed.raw_answer is None and "HTTP 400" in failed.error


def test_timeout_after_a_correction_is_not_retried(stub_llm):
    stub_llm.script(stub_llm.reply("free prose"), ("hang", 0.8), stub_llm.reply(GOOD))
    asked = ask(tier(stub_llm, timeout=0.2), MESSAGES, validate)
    assert asked.value is None and not asked.exhausted and "timeout" in asked.error
    assert len(stub_llm.requests) == 2 and len(asked.tries) == 2
    assert [len(t.exchanges) for t in asked.tries] == [1, 1]


def test_every_correction_is_a_user_message_and_tries_stay_bounded(stub_llm):
    stub_llm.script(
        stub_llm.reply("bad one"),
        stub_llm.reply("bad two"),
        stub_llm.reply("bad three"),
        stub_llm.reply(GOOD),
    )
    asked = ask(tier(stub_llm), MESSAGES, validate)
    assert FORMAT_TRIES == 3 and len(asked.tries) == 3
    assert len(stub_llm.requests) == 3
    assert asked.value is None and asked.exhausted
    assert [len(t.exchanges) for t in asked.tries] == [1, 1, 1]
    for request, previous in zip(stub_llm.requests[1:], asked.tries[:2], strict=True):
        assert request["body"]["messages"][-1] == {
            "role": "user",
            "content": previous.correction,
        }


def test_final_error_stops_at_once_without_a_retry(stub_llm):
    stub_llm.script(stub_llm.reply("anything"), stub_llm.reply(GOOD))
    asked = ask(tier(stub_llm), MESSAGES, lambda content: FinalError("not retryable"))
    assert (asked.value, asked.error, asked.exhausted) == (None, "not retryable", False)
    assert len(stub_llm.requests) == 1 and asked.tries[0].correction is None


def test_http_error_is_one_request_and_recorded(stub_llm):
    stub_llm.script(("status", 500, "boom"), stub_llm.reply(GOOD))
    asked = ask(tier(stub_llm), MESSAGES, validate)
    assert asked.value is None and not asked.exhausted and "HTTP 500" in asked.error
    assert len(stub_llm.requests) == 1
    (exchange,) = asked.tries[0].exchanges
    assert exchange.raw_answer is None and "HTTP 500" in exchange.error


def test_timeout_is_one_request_and_recorded(stub_llm):
    stub_llm.script(("hang", 0.8), stub_llm.reply(GOOD))
    asked = ask(tier(stub_llm, timeout=0.2), MESSAGES, validate)
    assert asked.value is None and "timeout" in asked.error and len(stub_llm.requests) == 1
    assert asked.tries[0].exchanges[0].raw_answer is None


def test_tool_loop_is_one_try_with_each_request_recorded(stub_llm):
    stub_llm.script(
        stub_llm.reply(None, tool_calls=[("get_stock_level", '{"sku": "WidgetA"}')]),
        stub_llm.reply(GOOD),
    )
    calls = []

    def run_tool(name, arguments):
        calls.append((name, arguments))
        return {"found": True}

    asked = ask(tier(stub_llm), MESSAGES, validate, tools=[{"type": "function"}], run_tool=run_tool)
    assert asked.value is not None and calls == [("get_stock_level", '{"sku": "WidgetA"}')]
    (only,) = asked.tries
    assert len(only.exchanges) == 2 and "get_stock_level" in only.exchanges[0].raw_answer
    sent = stub_llm.requests[1]["body"]["messages"]
    assert sent[-2]["tool_calls"][0]["id"] == "call_0"
    assert sent[-1] == {"role": "tool", "tool_call_id": "call_0", "content": '{"found": true}'}


def test_a_failing_tool_stops_the_answer_at_once(stub_llm):
    stub_llm.script(
        stub_llm.reply(None, tool_calls=[("search_vendors", "{}")]), stub_llm.reply(GOOD)
    )

    def run_tool(name, arguments):
        raise RuntimeError(f"unknown tool {name}")

    asked = ask(tier(stub_llm), MESSAGES, validate, tools=[{"type": "function"}], run_tool=run_tool)
    assert asked.value is None and not asked.exhausted
    assert asked.error == "tool failure: unknown tool search_vendors"
    assert len(stub_llm.requests) == 1 and len(asked.tries[0].exchanges) == 1


def test_tool_calls_from_a_role_without_tools_stop_at_once(stub_llm):
    stub_llm.script(stub_llm.reply(None, tool_calls=[("get_stock_level", "{}")]))
    asked = ask(tier(stub_llm), MESSAGES, validate)
    assert asked.value is None and "tool" in asked.error and len(stub_llm.requests) == 1


class TestRoleCall:
    def test_a_valid_answer_is_recorded_with_every_try(self, stub_llm):
        stub_llm.script(stub_llm.reply("prose"), stub_llm.reply(GOOD))
        config = tier(stub_llm)
        call = role_call("advisory", config, ask(config, MESSAGES, validate))
        assert (call.role, call.tier, call.model, call.error) == ("advisory", "grok", "m", None)
        assert call.answer == {"explained": True, "evidence": "invoice.notes"}
        assert len(call.tries) == 2 and call.tries[0].correction

    def test_exhausted_tries_leave_the_answer_absent_with_the_error(self, stub_llm):
        stub_llm.script(*(stub_llm.reply("bad") for _ in range(3)))
        config = tier(stub_llm)
        call = role_call("escalate_review", config, ask(config, MESSAGES, validate))
        assert call.answer is None and "3 tries" in call.error and len(call.tries) == 3
