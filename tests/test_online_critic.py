"""Slice 2b.1: online critic roles via ask/role_call (stub_llm only, no live calls)."""

import json

from invoice_pipeline.approval import decide
from invoice_pipeline.critic import offline_agents, online_agents
from invoice_pipeline.llm import FORMAT_TRIES, TierConfig
from invoice_pipeline.model import Outcome
from tests.factories import case_file_for, make_invoice


def tier(stub_llm) -> TierConfig:
    return TierConfig(
        tier="grok", model="m", base_url=stub_llm.url, timeout_s=5, api_key="sekret-key"
    )


def clean_case():
    """A Case File with no findings, so decide takes the row-6 escalate-only path."""
    return case_file_for(make_invoice())


CONCUR = json.dumps(
    {
        "verdict": "concur",
        "evidence": ["invoice.total"],
        "rationale": "total matches the single line total",
    }
)
ESCALATE = json.dumps(
    {
        "verdict": "escalate",
        "evidence": ["invoice.vendor"],
        "rationale": "vendor name looks odd, needs a human",
    }
)
ADVICE = json.dumps({"rationale": "blocked vendor, the rejection stands"})


class TestEscalateReview:
    def test_concur_answer_is_usable_and_approves_row_6(self, stub_llm):
        stub_llm.script(stub_llm.reply(CONCUR))
        agents = online_agents(tier(stub_llm))
        call = agents.escalate_review(clean_case())
        assert (call.role, call.tier, call.model, call.error) == (
            "escalate_review",
            "grok",
            "m",
            None,
        )
        assert call.answer["verdict"] == "concur"

    def test_concur_decision_is_approved_without_unreviewed_warnings(self, stub_llm):
        stub_llm.script(stub_llm.reply(CONCUR))
        decision = decide(clean_case(), online_agents(tier(stub_llm)))
        assert decision.outcome == Outcome.APPROVED and decision.precedence_row == 6
        assert decision.reasons == ["no findings"] and not decision.unreviewed_warnings

    def test_bad_json_is_corrected_on_the_second_try(self, stub_llm):
        stub_llm.script(stub_llm.reply("free prose"), stub_llm.reply(CONCUR))
        call = online_agents(tier(stub_llm)).escalate_review(clean_case())
        assert call.answer is not None and call.error is None
        assert len(call.tries) == 2 and call.tries[0].correction.startswith(
            "answer is not valid JSON"
        )

    def test_wrong_verdict_is_corrected(self, stub_llm):
        bad = json.dumps({"verdict": "approve", "evidence": ["invoice.total"], "rationale": "x"})
        stub_llm.script(stub_llm.reply(bad), stub_llm.reply(CONCUR))
        call = online_agents(tier(stub_llm)).escalate_review(clean_case())
        assert call.answer["verdict"] == "concur" and call.error is None
        assert "'verdict' must be" in call.tries[0].correction

    def test_unknown_evidence_path_is_corrected(self, stub_llm):
        bad = json.dumps({"verdict": "concur", "evidence": ["invoice.po"], "rationale": "x"})
        stub_llm.script(stub_llm.reply(bad), stub_llm.reply(CONCUR))
        call = online_agents(tier(stub_llm)).escalate_review(clean_case())
        assert call.answer is not None and call.error is None
        assert "is not in the Case File" in call.tries[0].correction

    def test_exhausted_answers_fail_closed(self, stub_llm):
        stub_llm.script(*(stub_llm.reply(f"bad {n}") for n in range(6)))
        agents = online_agents(tier(stub_llm))
        call = agents.escalate_review(clean_case())
        assert FORMAT_TRIES == 3 and call.answer is None
        assert "3 tries" in call.error
        decision = decide(clean_case(), agents)
        assert decision.outcome == Outcome.NEEDS_REVIEW and decision.precedence_row == 6
        assert decision.reasons[0].startswith("ESCALATE_ONLY_REVIEW_FAILED")

    def test_escalate_verdict_sends_row_6_to_review(self, stub_llm):
        stub_llm.script(stub_llm.reply(ESCALATE))
        decision = decide(clean_case(), online_agents(tier(stub_llm)))
        assert decision.outcome == Outcome.NEEDS_REVIEW and decision.precedence_row == 6
        assert decision.decided_by == "llm_critic"
        assert decision.reasons[0].startswith("ESCALATE_ONLY_REVIEW: ")


class TestAdvisory:
    def test_advisory_answer_carries_rationale(self, stub_llm):
        stub_llm.script(stub_llm.reply(ADVICE))
        agents = online_agents(tier(stub_llm))
        blocked = case_file_for(make_invoice(vendor="Fraudster LLC"))
        decision = decide(blocked, agents)
        assert decision.outcome == Outcome.REJECTED
        assert (decision.advisory.role, decision.advisory.error) == ("advisory", None)
        assert decision.advisory.answer["rationale"] == "blocked vendor, the rejection stands"

    def test_advisory_verdict_never_authorizes_payment(self, stub_llm):
        overreach = json.dumps({"verdict": "approve", "rationale": "pay it anyway"})
        stub_llm.script(stub_llm.reply(overreach))
        agents = online_agents(tier(stub_llm))
        blocked = case_file_for(make_invoice(vendor="Fraudster LLC"))
        decision = decide(blocked, agents)
        assert decision.outcome == Outcome.REJECTED  # decide never reads the advisory verdict
        assert decision.advisory.answer["verdict"] == "approve"


class TestStubsAndOffline:
    def test_assess_and_verify_are_no_answer_stubs(self, stub_llm):
        agents = online_agents(tier(stub_llm))
        assert agents.assess(clean_case()) is None
        assert agents.verify(clean_case()) is None
        assert len(stub_llm.requests) == 0  # no request is made

    def test_offline_path_still_fails_closed(self):
        call = offline_agents().escalate_review(clean_case())
        assert (call.answer, call.error) == (None, "offline tier")
