"""approval.orchestrate / decide row 5: at most two attempts, only the final one may approve."""

import pytest
from test_approval_accept import assessments, checks, warnings_of

from invoice_pipeline.approval import decide
from invoice_pipeline.critic import offline_agents, offline_role
from invoice_pipeline.model import (
    Agents,
    AssessCall,
    FindingCode,
    GuardrailCause,
    GuardrailFailure,
    Outcome,
    VerifyCall,
)


class Script:
    """Scripted gate roles that record call order, feedback, and cleanup."""

    def __init__(self, case_file, assess=(), verify=()):
        self.case_file = case_file
        self.assess_plan, self.verify_plan = list(assess), list(verify)
        self.events, self.feedback, self.cleaned = [], [], []

    def agents(self) -> Agents:
        def assess(case_file, attempt, feedback, scratch):
            self.feedback.append(feedback)
            scratch.setdefault("cleanup", []).append(lambda: self.cleaned.append(attempt))
            step = self.assess_plan.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        def verify(case_file, assessed, tool_calls, attempt):
            return self.verify_plan.pop(0)

        return Agents(
            assess=assess,
            verify=verify,
            escalate_review=lambda cf: pytest.fail("row 6 role in row 5"),
            advise=lambda cf, d: offline_role("advisory"),
            on_step=lambda event, detail: self.events.append((event, detail["attempt"])),
        )


def good_assess(case_file, number=1, **changes):
    return AssessCall(attempt=number, assessments=assessments(case_file, **changes), accepted=True)


def verified(case_file, number=1, holds=True):
    return VerifyCall(attempt=number, checks=checks(case_file, holds), accepted=True)


@pytest.fixture
def case_file(make_case_file):
    return make_case_file(items=[("WidgetA", "300")])  # one within-bound PRICE_DEVIATION


def run(case_file, script):
    return decide(case_file, script.agents())


def test_two_good_answers_approve_with_the_audit_and_events(case_file):
    script = Script(case_file, [good_assess(case_file)], [verified(case_file)])
    decision = run(case_file, script)
    assert decision.outcome is Outcome.APPROVED and decision.precedence_row == 5
    assert decision.decided_by == "llm_critic" and not decision.unreviewed_warnings
    assert decision.advisory is None and decision.bound_failures == []
    assert [a.attempt for a in decision.critic.attempts] == [1]
    assert script.events == [("assess", 1), ("verify", 1)]
    assert script.cleaned == [1]


def test_offline_agents_stay_unreviewed_warnings(case_file):
    decision = decide(case_file, offline_agents())
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.unreviewed_warnings
    assert decision.critic is None and "UNREVIEWED_WARNINGS" in decision.reasons[0]


def test_exhausted_tries_are_unreviewed_warnings_and_skip_the_verifier(case_file):
    exhausted = AssessCall(attempt=1, exhausted=True, error="answer still invalid after 3 tries")
    script = Script(case_file, [exhausted], [])
    decision = run(case_file, script)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.unreviewed_warnings
    assert script.events == [("assess", 1)]
    assert decision.critic.attempts[0].verifier is None


def test_an_agent_that_raises_fails_closed(case_file):
    script = Script(case_file, [RuntimeError("boom")], [])
    decision = run(case_file, script)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.unreviewed_warnings
    assert script.cleaned == [1]


def test_unexplained_finality_is_needs_review_without_a_second_attempt(case_file):
    failure = GuardrailFailure(
        cause=GuardrailCause.UNEXPLAINED,
        where="PRICE_DEVIATION line 0",
        message="m",
        correctable=False,
    )
    script = Script(case_file, [AssessCall(attempt=1, failures=[failure])], [])
    decision = run(case_file, script)
    assert decision.outcome is Outcome.NEEDS_REVIEW and not decision.unreviewed_warnings
    assert "unexplained" in decision.reasons[0].lower()
    assert script.events == [("assess", 1)]


def test_a_false_claim_gets_one_correction_through_the_identical_gate(case_file):
    script = Script(
        case_file,
        [good_assess(case_file), good_assess(case_file, 2)],
        [verified(case_file, holds=False), verified(case_file, 2)],
    )
    decision = run(case_file, script)
    assert decision.outcome is Outcome.APPROVED
    assert [a.attempt for a in decision.critic.attempts] == [1, 2]
    assert script.feedback[0] is None and "checked" in script.feedback[1]
    assert decision.critic.attempts[1].feedback == script.feedback[1]
    assert script.events == [
        ("assess", 1),
        ("verify", 1),
        ("correct", 2),
        ("assess", 2),
        ("verify", 2),
    ]
    assert script.cleaned == [1, 2]


def test_a_corrected_answer_with_bad_evidence_is_not_approved(case_file):
    bad = good_assess(case_file, 2, evidence=["invoice.items.9.sku"])
    script = Script(
        case_file,
        [good_assess(case_file), bad],
        [verified(case_file, holds=False), verified(case_file, 2)],
    )
    decision = run(case_file, script)
    assert decision.outcome is Outcome.NEEDS_REVIEW
    assert "unresolved_evidence" in " ".join(decision.reasons)


def test_a_repeated_false_claim_is_needs_review_and_there_is_no_third_attempt(case_file):
    script = Script(
        case_file,
        [good_assess(case_file), good_assess(case_file, 2)],
        [verified(case_file, holds=False), verified(case_file, 2, holds=False)],
    )
    decision = run(case_file, script)
    assert decision.outcome is Outcome.NEEDS_REVIEW and not decision.unreviewed_warnings
    assert len(decision.critic.attempts) == 2 and script.assess_plan == []


def test_a_failed_correction_attempt_cannot_fall_back_to_the_first(case_file):
    script = Script(
        case_file,
        [good_assess(case_file), AssessCall(attempt=2, error="timeout after 30s")],
        [verified(case_file, holds=False)],
    )
    decision = run(case_file, script)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.unreviewed_warnings


def test_a_missing_verifier_check_never_approves(case_file):
    short = VerifyCall(attempt=1, checks=[], accepted=True)
    decision = run(case_file, Script(case_file, [good_assess(case_file)], [short]))
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.unreviewed_warnings


def test_only_a_guardrail_clean_assessment_reaches_the_verifier(case_file):
    bad = good_assess(case_file, evidence=["invoice.items.9.sku"])
    script = Script(case_file, [bad], [])
    decision = run(case_file, script)
    assert decision.outcome is Outcome.NEEDS_REVIEW
    assert script.events == [("assess", 1)]


def test_bound_failures_never_reach_the_gate(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "350")])  # 40% deviation
    decision = decide(case_file, Script(case_file).agents())
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.bound_failures
    assert decision.critic is None and not decision.unreviewed_warnings


def test_heightened_scrutiny_never_reaches_the_gate(make_case_file):
    case_file = make_case_file(items=[("GadgetX", "900")] * 15)  # above $10,000, 20% over
    decision = decide(case_file, Script(case_file).agents())
    assert decision.precedence_row == 4 and decision.critic is None


def test_vendor_unknown_is_needs_review_even_with_perfect_answers(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "250")], vendor="Zeta Corp")
    assert [f.code for f in warnings_of(case_file)] == [FindingCode.VENDOR_UNKNOWN]
    script = Script(case_file, [good_assess(case_file)], [verified(case_file)])
    decision = run(case_file, script)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.bound_failures
