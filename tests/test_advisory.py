"""Advisory role (REQ-ADV-1..5): characterization of the existing behavior."""

import dataclasses
import json

import pytest
from conftest import ScriptedChat, text_reply

from invoice_pipeline.approval import decide, decide_unreadable
from invoice_pipeline.critic import offline_agents, offline_role, online_agents
from invoice_pipeline.model import FindingCode, Outcome, finding

ADVISED = {
    "row2": dict(codes=["ITEM_UNKNOWN"]),
    "row3": dict(codes=["STOCK_SHORTAGE"]),
    "row4": dict(codes=["PRICE_DEVIATION"], quantity="50"),
    "row5-bound-failed": dict(codes=["VENDOR_UNKNOWN"]),
}
NOT_ADVISED = {
    "row1-duplicate": dict(kind="duplicate"),
    "row5-full-gate": dict(codes=["PRICE_DEVIATION"]),
    "row6": dict(),
}


def counting_agents(counter):
    def advise(case_file, decision):
        counter.append(decision.precedence_row)
        return offline_role("advisory")

    return dataclasses.replace(offline_agents(), advise=advise)


@pytest.mark.parametrize("row", ADVISED)
def test_advise_called_once_rows_2_3_4_bound5(row, make_case):
    seen = []

    decision = decide(make_case(**ADVISED[row]), counting_agents(seen))

    assert len(seen) == 1 and seen == [decision.precedence_row]
    assert decision.advisory is not None


@pytest.mark.parametrize("row", NOT_ADVISED)
def test_advise_never_on_dup_fullgate_row6(row, make_case):
    seen = []

    decide(make_case(**NOT_ADVISED[row]), counting_agents(seen))

    assert seen == []


def test_advise_never_on_unreadable():
    decision = decide_unreadable([finding(FindingCode.UNREADABLE_DOCUMENT, "no text")])

    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.advisory is None


def advice(**fields):
    return text_reply(json.dumps({"rationale": "explained", **fields}))


@pytest.mark.parametrize("row", ADVISED)
@pytest.mark.parametrize("verdict", ["approve", "reject", "investigate"])
def test_decision_byte_equal_excluding_advisory(row, verdict, grok, make_case):
    chat = ScriptedChat(advice(verdict=verdict, approved=verdict == "approve"))
    online = decide(make_case(**ADVISED[row]), online_agents(grok, chat_fn=chat))
    plain = decide(make_case(**ADVISED[row]), offline_agents())

    assert online.advisory.answer["rationale"] == "explained"
    assert online.model_dump_json(exclude={"advisory"}) == plain.model_dump_json(
        exclude={"advisory"}
    )


def test_exhausted_advice_absent_tries_error_recorded(grok, make_case):
    chat = ScriptedChat(*[text_reply("not json")] * 3)
    online = decide(make_case(**ADVISED["row3"]), online_agents(grok, chat_fn=chat))
    plain = decide(make_case(**ADVISED["row3"]), offline_agents())

    assert online.advisory.answer is None
    assert len(online.advisory.tries) == 3 and "3 tries" in online.advisory.error
    assert online.outcome is plain.outcome and online.reasons == plain.reasons
