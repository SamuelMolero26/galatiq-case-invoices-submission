from decimal import Decimal

import pytest

from invoice_pipeline.approval import (
    CRITIC_PRICE_CEILING,
    HEIGHTENED_SCRUTINY_USD,
    check_bounds,
    decide,
)
from invoice_pipeline.critic import offline_agents
from invoice_pipeline.model import Outcome
from tests.factories import (
    CountingAgents,
    case_file_for,
    make_catalog,
    make_invoice,
    make_item,
    online_role,
)

BIG = make_catalog(stock={"WidgetA": Decimal("1000"), "WidgetB": Decimal("1000")})


def over_limit_invoice(total_tax="0.01", **kw):
    """40 x 250.00 = 10000.00, plus tax: a priced-at-reference invoice above the line."""
    return make_invoice([make_item("WidgetA", qty="40", price="250.00")], tax=total_tax, **kw)


def test_constants():
    assert HEIGHTENED_SCRUTINY_USD == Decimal("10000")
    assert CRITIC_PRICE_CEILING == Decimal("0.30")


# --- row 6 / clean --------------------------------------------------------------


def test_clean_offline_invoice_is_approved_with_offline_review_recorded(no_network):
    decision = decide(case_file_for(make_invoice()), offline_agents())
    assert decision.outcome is Outcome.APPROVED and decision.precedence_row == 6
    assert decision.decided_by == "rule_engine"
    assert decision.escalate_review.error == "offline tier"
    assert decision.advisory is None


def test_clean_invoice_above_the_limit_still_follows_row_six():
    case_file = case_file_for(over_limit_invoice(), BIG)
    assert case_file.findings == []
    assert decide(case_file, offline_agents()).precedence_row == 6


def test_row_six_concur_keeps_approval_and_escalate_or_failure_fails_closed():
    case_file = case_file_for(make_invoice())
    concur = CountingAgents(escalate=online_role("escalate_review", {"verdict": "concur"}))
    assert decide(case_file, concur.agents).outcome is Outcome.APPROVED
    escalate = CountingAgents(
        escalate=online_role("escalate_review", {"verdict": "escalate", "rationale": "odd notes"})
    )
    decision = decide(case_file, escalate.agents)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.decided_by == "llm_critic"
    assert any("odd notes" in r for r in decision.reasons)
    broken = CountingAgents(escalate=online_role("escalate_review", None, "timeout"))
    decision = decide(case_file, broken.agents)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.decided_by == "rule_engine"


# --- row 4: Heightened Scrutiny ---------------------------------------------------


def test_warning_strictly_above_the_limit_is_row_four_without_the_gate():
    stub = CountingAgents()
    case_file = case_file_for(over_limit_invoice(vendor="Northwind Traders"), BIG)
    decision = decide(case_file, stub.agents)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.precedence_row == 4
    assert decision.decided_by == "rule_engine" and not decision.unreviewed_warnings
    assert "$10,000" in " ".join(decision.reasons) and "10000.01" in " ".join(decision.reasons)
    assert stub.calls["assess"] == stub.calls["verify"] == stub.calls["escalate_review"] == 0
    assert stub.calls["advise"] == 1 and decision.advisory is not None


def test_warning_exactly_at_the_limit_is_within_it():
    case_file = case_file_for(over_limit_invoice(total_tax="0", vendor="Northwind Traders"), BIG)
    assert case_file.invoice.total == Decimal("10000.00")
    decision = decide(case_file, offline_agents())
    assert decision.precedence_row == 5


# --- row 5: bounds and Unreviewed Warnings ---------------------------------------


def priced(price, **kw):
    return make_invoice([make_item("WidgetA", qty="1", price=price, note="rush order")], **kw)


def test_check_bounds_price_ceiling_is_inclusive_at_thirty_percent():
    assert check_bounds(case_file_for(priced("325.00"))) == []
    failures = check_bounds(case_file_for(priced("325.10")))
    assert len(failures) == 1 and "PRICE_DEVIATION" in failures[0] and "30%" in failures[0]
    assert "40.00%" in check_bounds(case_file_for(priced("350.00")))[0]


def test_check_bounds_vendor_unknown_always_fails_alone_or_mixed():
    unknown = case_file_for(make_invoice(vendor="Northwind Traders"))
    assert len(check_bounds(unknown)) == 1 and "VENDOR_UNKNOWN" in check_bounds(unknown)[0]
    mixed = case_file_for(priced("350.00", vendor="Northwind Traders"))
    failures = check_bounds(mixed)
    assert (
        len(failures) == 2 and "VENDOR_UNKNOWN" in failures[0] and "PRICE_DEVIATION" in failures[1]
    )


def test_within_bound_warning_offline_is_unreviewed_warnings(no_network):
    stub = CountingAgents()
    decision = decide(case_file_for(priced("300.00")), stub.agents)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.precedence_row == 5
    assert decision.unreviewed_warnings is True and decision.bound_failures == []
    assert decision.decided_by == "rule_engine"
    assert "PRICE_DEVIATION" in " ".join(decision.reasons)
    assert stub.calls["advise"] == 0


def test_bound_failure_is_not_unreviewed_warnings_and_gets_advisory():
    stub = CountingAgents()
    decision = decide(case_file_for(priced("350.00")), stub.agents)
    assert decision.outcome is Outcome.NEEDS_REVIEW and decision.precedence_row == 5
    assert decision.unreviewed_warnings is False
    assert len(decision.bound_failures) == 1 and "30%" in decision.bound_failures[0]
    assert stub.calls["advise"] == 1 and stub.calls["assess"] == 0


@pytest.mark.parametrize("vendor", ["Northwind Traders"])
def test_unknown_vendor_never_reaches_the_assessor(vendor):
    stub = CountingAgents()
    decision = decide(case_file_for(make_invoice(vendor=vendor)), stub.agents)
    assert decision.outcome is Outcome.NEEDS_REVIEW
    assert stub.calls["assess"] == 0 and decision.bound_failures
