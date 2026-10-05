import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

from invoice_pipeline.model import (
    Agents,
    Decision,
    Event,
    LLMExchange,
    Outcome,
    PaymentIssue,
    QueueItem,
    RoleCall,
    Try,
)

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def test_only_four_decisions_and_payment_pending_is_not_one():
    assert {o.value for o in Outcome} == {"approved", "needs_review", "rejected", "duplicate"}
    assert "payment_pending" not in {o.value for o in Outcome}


def offline_role(role: str) -> RoleCall:
    return RoleCall(
        role=role,
        tier="offline",
        model=None,
        tries=[
            Try(
                exchanges=[
                    LLMExchange(
                        raw_answer=None, error="offline tier", called_at=NOW, elapsed_ms=None
                    )
                ]
            )
        ],
        answer=None,
        error="offline tier",
    )


def test_decision_round_trips_with_offline_audit_and_links():
    decision = Decision(
        outcome=Outcome.DUPLICATE,
        reasons=["DUPLICATE_PAYMENT: already paid 3000.00 USD on arrival #12"],
        precedence_row=1,
        decided_by="rule_engine",
        duplicate_of=12,
        escalate_review=offline_role("escalate_review"),
        advisory=offline_role("advisory"),
        bound_failures=["PRICE_DEVIATION line 0: 40.00% > 30% bound"],
    )
    restored = Decision.model_validate_json(decision.model_dump_json())
    assert restored == decision
    assert restored.duplicate_of == 12
    assert restored.precedence_row == 1
    assert restored.advisory.error == "offline tier"
    assert restored.advisory.tries[0].exchanges[0].error == "offline tier"
    assert restored.unreviewed_warnings is False


def test_payment_issue_round_trips_exactly():
    issue = PaymentIssue(
        what="attempt initiated; bank outcome unconfirmed", when=NOW, bank_response=None
    )
    assert PaymentIssue.model_validate_json(issue.model_dump_json()) == issue


def test_queue_item_and_event_carry_plain_data():
    item = QueueItem(
        arrival_id=3,
        vendor="Acme",
        invoice_number=None,
        source="data/invoices/invoice_1009.json",
        total=Decimal("100.10"),
        currency="USD",
        state="needs_review",
        reasons=["PARTIAL_IDENTITY"],
    )
    assert QueueItem.model_validate_json(item.model_dump_json()) == item
    assert item.payment_issue is None
    event = Event(name="decided", file="a.txt", detail={"outcome": "approved"})
    assert event.name == "decided"


def test_agents_bundle_has_the_permanent_four_roles():
    names = [f.name for f in dataclasses.fields(Agents)]
    assert names == ["assess", "verify", "escalate_review", "advise", "on_step"]
    assert Agents.__dataclass_params__.frozen
