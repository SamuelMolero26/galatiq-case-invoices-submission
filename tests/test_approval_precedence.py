from decimal import Decimal

import pytest

from invoice_pipeline.approval import decide, decide_unreadable
from invoice_pipeline.critic import offline_agents
from invoice_pipeline.model import (
    SEVERITY,
    ArrivalSummary,
    FindingCode,
    Outcome,
    Severity,
    finding,
)
from tests.factories import (
    CountingAgents,
    case_file_for,
    make_catalog,
    make_invoice,
    make_item,
    online_role,
)

DUPLICATE = ArrivalSummary(kind="duplicate", duplicate_of=12, paid_to_date=Decimal("3000.00"))
ALWAYS_CONCUR = dict(
    escalate=online_role("escalate_review", {"verdict": "concur"}),
    advise=online_role("advisory", {"recommendation": "approve", "explanation": "fine"}),
)


def stub():
    return CountingAgents(**ALWAYS_CONCUR)


def total_calls(counting: CountingAgents) -> int:
    return sum(counting.calls.values())


# --- row 1 ---------------------------------------------------------------------


def test_duplicate_is_row_one_with_reason_link_and_no_model_call():
    counting = stub()
    case_file = case_file_for(make_invoice(), arrival=DUPLICATE)
    decision = decide(case_file, counting.agents)
    assert decision.outcome is Outcome.DUPLICATE and decision.precedence_row == 1
    assert decision.decided_by == "rule_engine" and decision.duplicate_of == 12
    assert decision.reasons == ["DUPLICATE_PAYMENT: already paid 3000.00 USD on arrival #12"]
    assert total_calls(counting) == 0
    assert decision.advisory is None and decision.escalate_review is None


def test_duplicate_of_a_pending_payment_says_pending():
    pending = DUPLICATE.model_copy(update={"claimed_state": "payment_pending"})
    decision = decide(case_file_for(make_invoice(), arrival=pending), offline_agents())
    assert decision.reasons == ["DUPLICATE_PAYMENT: payment of 3000.00 USD pending on arrival #12"]


def test_duplicate_outranks_every_finding_and_keeps_them_in_the_case_file():
    invoice = make_invoice([make_item("WidgetC")], vendor="Fraudster LLC", currency="EUR")
    case_file = case_file_for(invoice, arrival=DUPLICATE)
    severities = {f.severity for f in case_file.findings}
    assert severities == set(Severity)
    counting = stub()
    decision = decide(case_file, counting.agents)
    assert decision.outcome is Outcome.DUPLICATE and total_calls(counting) == 0


# --- row 2 ---------------------------------------------------------------------


def test_rejection_rule_beats_review_triggers_and_warnings_and_lists_all_findings():
    invoice = make_invoice([make_item("WidgetC"), make_item("GadgetX", qty="20", price="750")])
    invoice = invoice.model_copy(update={"vendor": "Northwind Traders"})
    case_file = case_file_for(invoice)
    counting = stub()
    decision = decide(case_file, counting.agents)
    assert decision.outcome is Outcome.REJECTED and decision.precedence_row == 2
    reasons = " ".join(decision.reasons)
    assert "ITEM_UNKNOWN" in reasons and "STOCK_SHORTAGE" in reasons and "VENDOR_UNKNOWN" in reasons
    assert counting.calls["advise"] == 1 and total_calls(counting) == 1


def test_rejection_rule_outranks_partial_identity_and_both_are_listed():
    invoice = make_invoice([make_item(qty="-5")], vendor=None)
    decision = decide(case_file_for(invoice), stub().agents)
    assert decision.outcome is Outcome.REJECTED
    reasons = " ".join(decision.reasons)
    assert "PARTIAL_IDENTITY" in reasons and "QUANTITY_INVALID" in reasons


def test_incomplete_identity_rejects():
    decision = decide(case_file_for(make_invoice(vendor=None, number=None)), offline_agents())
    assert decision.outcome is Outcome.REJECTED and "INCOMPLETE_IDENTITY" in decision.reasons[0]


def test_advisory_recommendation_never_changes_a_rejection():
    advise = online_role("advisory", {"recommendation": "approve", "explanation": "looks ok"})
    case_file = case_file_for(make_invoice([make_item("FakeItem", qty="1", price="1")]))
    with_advice = decide(case_file, CountingAgents(advise=advise).agents)
    without = decide(case_file, offline_agents())
    assert with_advice.advisory.answer["recommendation"] == "approve"
    for field in ("outcome", "reasons", "precedence_row", "decided_by"):
        assert getattr(with_advice, field) == getattr(without, field)


# --- row 3 ---------------------------------------------------------------------


def test_review_trigger_is_row_three_and_no_agent_judges_it():
    counting = stub()
    invoice = make_invoice([make_item("GadgetX", qty="20", price="750")])
    decision = decide(case_file_for(invoice), counting.agents)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.precedence_row == 3
    assert counting.calls["assess"] == counting.calls["verify"] == 0
    assert counting.calls["escalate_review"] == 0 and counting.calls["advise"] == 1


def test_review_trigger_outranks_heightened_scrutiny_and_warnings():
    big = make_catalog(stock={"WidgetA": Decimal("1000")})
    invoice = make_invoice(
        [make_item("WidgetA", qty="40", price="250.00")], tax="0.01", currency="EUR"
    )
    decision = decide(case_file_for(invoice, big), offline_agents())
    assert decision.precedence_row == 3


def test_lookalike_vendor_stays_with_the_reviewer_even_if_every_role_concurs():
    counting = stub()
    decision = decide(case_file_for(make_invoice(vendor="Acme Suppiles")), counting.agents)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.precedence_row == 3
    assert counting.calls["escalate_review"] == 0
    assert decision.advisory.answer["recommendation"] == "approve"


def test_non_usd_reaches_needs_review():
    decision = decide(case_file_for(make_invoice(currency="EUR")), offline_agents())
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.precedence_row == 3


# --- unreadable document ---------------------------------------------------------


def test_unreadable_document_enters_row_three_without_validation_or_models():
    unreadable = finding(FindingCode.UNREADABLE_DOCUMENT, "read: ValueError: PDF has no text layer")
    decision = decide_unreadable([unreadable])
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.precedence_row == 3
    assert decision.decided_by == "rule_engine"
    assert "UNREADABLE_DOCUMENT" in decision.reasons[0] and "no text layer" in decision.reasons[0]
    assert decision.advisory is None and decision.escalate_review is None


# --- rows 4-6 stay in order ---------------------------------------------------------


def test_remaining_rows_keep_their_order():
    unknown = decide(case_file_for(make_invoice(vendor="Northwind Traders")), offline_agents())
    assert unknown.precedence_row == 5
    clean = decide(case_file_for(make_invoice()), offline_agents())
    assert clean.precedence_row == 6


# --- authority ----------------------------------------------------------------------


@pytest.mark.parametrize("code", list(FindingCode))
def test_only_a_rejection_rule_can_produce_rejected(code):
    base = case_file_for(make_invoice())
    case_file = base.model_copy(update={"findings": [finding(code, "constructed")]})
    decision = decide(case_file, stub().agents)
    assert (decision.outcome is Outcome.REJECTED) == (SEVERITY[code] is Severity.REJECTION_RULE)
    assert decision.outcome is not Outcome.DUPLICATE
    if SEVERITY[code] is not Severity.WARNING:
        assert decision.outcome is not Outcome.APPROVED


def test_agents_that_always_concur_cannot_approve_findings():
    for invoice in (
        make_invoice(vendor="Northwind Traders"),
        make_invoice([make_item("GadgetX", qty="20", price="750")]),
        make_invoice([make_item("WidgetC")]),
    ):
        assert decide(case_file_for(invoice), stub().agents).outcome is not Outcome.APPROVED
