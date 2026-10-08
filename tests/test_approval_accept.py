"""accept_verdict: the one deterministic gate between model answers and an approval."""

import pytest

from invoice_pipeline.approval import accept_verdict
from invoice_pipeline.model import (
    AssessCall,
    CriticAttempt,
    FindingCode,
    GuardrailCause,
    GuardrailFailure,
    Severity,
    VerifyCall,
    VerifyCheck,
    WarningAssessment,
)


def evidence_for(finding):
    if finding.line is None:
        return ["invoice.vendor"]
    return [f"invoice.items.{finding.line}.unit_price"]


def warnings_of(case_file):
    return [f for f in case_file.findings if f.severity is Severity.WARNING]


def assessments(case_file, **changes):
    return [
        WarningAssessment(
            **{
                "code": f.code,
                "line": f.line,
                "explained": True,
                "evidence": evidence_for(f),
                "rationale": "because",
                **changes,
            }
        )
        for f in warnings_of(case_file)
    ]


def checks(case_file, holds=True):
    return [
        VerifyCheck(code=f.code, line=f.line, holds=holds, rationale="checked")
        for f in warnings_of(case_file)
    ]


def attempt(case_file, *, assessed=None, checked=None, number=1, accepted=True):
    assessed = assessments(case_file) if assessed is None else assessed
    checked = checks(case_file) if checked is None else checked
    return CriticAttempt(
        attempt=number,
        assessor=AssessCall(attempt=number, assessments=assessed, accepted=accepted),
        verifier=VerifyCall(attempt=number, checks=checked, accepted=True),
    )


@pytest.fixture
def case_file(make_case_file):
    # two within-bound price Warnings, trusted vendor
    return make_case_file(items=[("WidgetA", "300"), ("WidgetB", "600")])


def test_every_warning_explained_grounded_and_holding_is_approved(case_file):
    verdict = accept_verdict(case_file, attempt(case_file), [])
    assert verdict.approved and verdict.usable and not verdict.retry and verdict.reasons == []


def test_a_false_claim_is_rejected_with_feedback_and_may_be_corrected(case_file):
    checked = checks(case_file)
    checked[0] = checked[0].model_copy(update={"holds": False, "rationale": "price is wrong"})
    verdict = accept_verdict(case_file, attempt(case_file, checked=checked), [])
    assert not verdict.approved and verdict.usable and verdict.retry
    assert "price is wrong" in verdict.feedback and "PRICE_DEVIATION line 0" in verdict.reasons[0]


def test_an_unaccepted_assessment_without_failures_is_no_usable_answer(case_file):
    call = CriticAttempt(
        attempt=1, assessor=AssessCall(attempt=1, error="timeout after 30s"), verifier=None
    )
    verdict = accept_verdict(case_file, call, [])
    assert not verdict.approved and not verdict.usable and not verdict.retry
    assert "timeout after 30s" in verdict.reasons[0]


def test_unexplained_finality_is_usable_final_and_not_correctable(case_file):
    failure = GuardrailFailure(
        cause=GuardrailCause.UNEXPLAINED,
        where="PRICE_DEVIATION line 0",
        message="not explained",
        correctable=False,
    )
    call = CriticAttempt(attempt=1, assessor=AssessCall(attempt=1, failures=[failure]))
    verdict = accept_verdict(case_file, call, [])
    assert not verdict.approved and verdict.usable and not verdict.retry
    assert "unexplained" in verdict.reasons[0].lower()


def test_the_guardrail_is_rerun_even_when_the_record_claims_acceptance(case_file):
    bad = assessments(case_file, evidence=["invoice.items.9.sku"])
    verdict = accept_verdict(case_file, attempt(case_file, assessed=bad), [])
    assert not verdict.approved and not verdict.retry
    assert "unresolved_evidence" in " ".join(verdict.reasons)


def test_an_assessment_marked_unexplained_cannot_be_approved(case_file):
    verdict = accept_verdict(
        case_file, attempt(case_file, assessed=assessments(case_file, explained=False)), []
    )
    assert not verdict.approved and not verdict.retry


def test_a_warning_outside_its_bound_is_never_approved(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "350")])  # 40% deviation
    verdict = accept_verdict(case_file, attempt(case_file), [])
    assert not verdict.approved and "Critic Bound" in " ".join(verdict.reasons)


def test_vendor_unknown_is_never_clearable_by_the_gate(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "250")], vendor="Zeta Corp")
    verdict = accept_verdict(case_file, attempt(case_file), [])
    assert not verdict.approved and "never" in " ".join(verdict.reasons)


def test_a_missing_verifier_check_is_no_usable_answer(case_file):
    verdict = accept_verdict(case_file, attempt(case_file, checked=checks(case_file)[:1]), [])
    assert not verdict.approved and not verdict.usable and not verdict.retry
    assert "missing" in " ".join(verdict.reasons)


def test_a_stray_verifier_pass_cannot_stand_in_for_a_real_check(case_file):
    stray = VerifyCheck(code=FindingCode.CURRENCY_NON_USD, line=None, holds=True, rationale="x")
    checked = [*checks(case_file)[:1], stray]
    verdict = accept_verdict(case_file, attempt(case_file, checked=checked), [])
    assert not verdict.approved and not verdict.usable


def test_an_extra_check_beside_a_full_set_is_refused(case_file):
    stray = VerifyCheck(code=FindingCode.CURRENCY_NON_USD, line=None, holds=True, rationale="x")
    verdict = accept_verdict(case_file, attempt(case_file, checked=[*checks(case_file), stray]), [])
    assert not verdict.approved and not verdict.usable


def test_a_duplicate_check_is_refused(case_file):
    checked = [*checks(case_file), *checks(case_file)[:1]]
    verdict = accept_verdict(case_file, attempt(case_file, checked=checked), [])
    assert not verdict.approved and not verdict.usable


def test_no_verifier_answer_is_no_usable_answer(case_file):
    call = CriticAttempt(
        attempt=1, assessor=AssessCall(attempt=1, assessments=assessments(case_file), accepted=True)
    )
    verdict = accept_verdict(case_file, call, [])
    assert not verdict.approved and not verdict.usable
