"""Reviewer actions in the service: the action matrix and the gated retry of a failed model review.

A retry re-decides the SAME arrival through the ordinary decide path, so every rule, bound,
guardrail and the payment cap still apply; it is offered only when a failed or unavailable model
call (never a rule, never a definitive model verdict) left the arrival in Needs Review.
"""

import dataclasses
import json

import pytest
from conftest import Harness, concur
from test_extraction import ADVICE, reply, txt

from invoice_pipeline import catalog, ledger, service
from invoice_pipeline.critic import offline_agents
from invoice_pipeline.llm import LLMError

CLEAN = {  # a trusted vendor, a known item, no findings: row 6 (escalate-only review)
    "invoice_number": "INV-3001",
    "vendor": {"name": "Precision Parts Ltd."},
    "date": "2026-01-02",
    "line_items": [{"item": "WidgetA", "quantity": 4, "unit_price": 250.00}],
    "subtotal": 1000.00,
    "total": 1000.00,
    "currency": "USD",
}


def _offline(h: Harness) -> service.Runtime:
    """The same Ledger and bank, deciding offline (no model call is ever made)."""
    return dataclasses.replace(h.rt, tier="offline", agents=offline_agents())


def _arrivals(h: Harness) -> int:
    conn = ledger.connect(h.ledger_path, read_only=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM arrivals").fetchone()[0]
    finally:
        conn.close()


def _record(h: Harness, arrival_id: int) -> dict:
    conn = ledger.connect(h.ledger_path, read_only=True)
    try:
        row = conn.execute("SELECT record FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()
        return json.loads(row["record"])
    finally:
        conn.close()


def _actions(ledger_path, arrival_id, online=False) -> tuple[str, ...]:
    return service.arrival_detail(ledger_path, arrival_id, online=online).actions


def _failed_review(h: Harness, name="a.json", content=CLEAN) -> service.ArrivalResult:
    """Process an invoice whose escalate-only review got no usable answer (Needs Review)."""
    result = h.process(name, content)
    assert result.state == "needs_review"
    assert result.reasons[0].startswith("ESCALATE_ONLY_REVIEW_FAILED")
    return result


# --- the action matrix (read model) ------------------------------------------------------------


def _by_source(ledger_path) -> dict[str, int]:
    return {r.source: r.arrival_id for r in service.results(ledger_path).rows}


@pytest.mark.parametrize(
    ("source", "offline", "online"),
    [
        # approvable Needs Review decided by a rule (row 3): no retry, whatever the tier
        ("invoice_1002.txt", ("approve", "reject"), ("approve", "reject")),
        # Unreviewed Warnings (row 5, no usable Critic answer): retry only on an online tier
        ("invoice_1014.xml", ("approve", "reject"), ("approve", "reject", "retry")),
        # revision of a paid invoice: payable is the remaining delta, reviewer may approve
        ("invoice_1004_revised.json", ("approve", "reject"), ("approve", "reject")),
        ("invoice_1001.txt", (), ()),  # paid
        ("invoice_1003.txt", (), ()),  # logged rejection
        ("invoice_1011.txt", (), ()),  # duplicate
    ],
)
def test_actions_follow_the_ledger_state_and_the_tier(batch_ledger, source, offline, online):
    arrival_id = _by_source(batch_ledger)[source]

    assert _actions(batch_ledger, arrival_id) == offline
    assert _actions(batch_ledger, arrival_id, online=True) == online


def test_payment_pending_offers_no_action(batch_ledger):
    arrival_id = _by_source(batch_ledger)["invoice_1002.txt"]
    rt = service.Runtime(
        catalog=catalog.load_catalog(batch_ledger.parent / "inventory.db"),
        ledger_path=batch_ledger,
        pay_fn=lambda *args: {"status": "declined"},
    )

    assert service.resolve(rt, arrival_id, "approve", "stock confirmed").state == "payment_pending"
    assert _actions(batch_ledger, arrival_id, online=True) == ()


# --- the retry gate ----------------------------------------------------------------------------


def test_failed_model_review_offers_retry_only_online(tmp_path, grok):
    h = Harness(tmp_path, grok, LLMError("timeout after 1s"))
    first = _failed_review(h)

    assert _actions(h.ledger_path, first.arrival_id, online=True) == ("approve", "reject", "retry")
    assert _actions(h.ledger_path, first.arrival_id) == ("approve", "reject")


def test_offline_tier_refuses_retry_and_changes_nothing(tmp_path, grok):
    h = Harness(tmp_path, grok, LLMError("timeout after 1s"))
    first = _failed_review(h)
    before = _record(h, first.arrival_id)

    with pytest.raises(service.RetryRefused, match="offline"):
        service.retry(_offline(h), first.arrival_id)

    assert _record(h, first.arrival_id) == before and h.paid == []


@pytest.mark.parametrize(
    ("name", "content", "why"),
    [
        (  # Review Trigger (row 3): the stated total disagrees with the lines
            "mismatch.json",
            {**CLEAN, "total": 1050.00},
            "decided by the rules",
        ),
        (  # Heightened Scrutiny (row 4): above the Critic approval limit
            "large.json",
            {
                **CLEAN,
                "currency": "EUR",
                "line_items": [{"item": "WidgetA", "quantity": 50, "unit_price": 250.00}],
                "subtotal": 12500.00,
                "total": 12500.00,
            },
            "decided by the rules",
        ),
        ("unreadable.json", "{ not json", "unreadable"),
        (  # Rejection Rule (row 2): already a Logged Rejection
            "blocked.json",
            {**CLEAN, "vendor": {"name": "Fraudster LLC"}},
            "rejected",
        ),
    ],
)
def test_rule_decided_arrivals_refuse_retry(tmp_path, grok, name, content, why):
    h = Harness(tmp_path, grok)  # online, but no reply scripted: a model call would fail the test
    result = service.process_path(h.write(name, content), _offline(h)).results[0]

    with pytest.raises(service.RetryRefused, match=why):
        service.retry(h.rt, result.arrival_id)

    assert h.chat.requests == [] and "retry" not in _actions(h.ledger_path, result.arrival_id, True)


def test_definitive_model_verdict_refuses_retry(tmp_path, grok):
    h = Harness(tmp_path, grok, concur(verdict="escalate", rationale="an odd remittance note"))
    result = h.process("a.json", CLEAN)
    assert result.reasons == ["ESCALATE_ONLY_REVIEW: an odd remittance note"]

    with pytest.raises(service.RetryRefused, match="definitive"):
        service.retry(h.rt, result.arrival_id)

    assert len(h.chat.requests) == 1


# --- retrying ----------------------------------------------------------------------------------


def test_retry_that_now_approves_pays_exactly_once_on_the_same_arrival(tmp_path, grok):
    h = Harness(tmp_path, grok, LLMError("timeout after 1s"), concur())
    first = _failed_review(h)
    arrivals = _arrivals(h)

    result = service.retry(h.rt, first.arrival_id)

    assert result.arrival_id == first.arrival_id and _arrivals(h) == arrivals
    assert (result.decision, result.state, result.precedence_row) == ("approved", "paid", 6)
    assert len(h.paid) == 1
    record = _record(h, first.arrival_id)
    assert record["decision"]["reasons"] == ["no findings"]
    assert record["prior_decisions"][0]["reasons"][0].startswith("ESCALATE_ONLY_REVIEW_FAILED")
    with pytest.raises(service.RetryRefused, match="paid"):
        service.retry(h.rt, first.arrival_id)
    assert len(h.paid) == 1


def test_retry_that_fails_again_stays_in_review_and_unpaid(tmp_path, grok):
    h = Harness(tmp_path, grok, LLMError("timeout after 1s"), LLMError("timeout again"))
    first = _failed_review(h)

    result = service.retry(h.rt, first.arrival_id)

    assert (result.arrival_id, result.state) == (first.arrival_id, "needs_review")
    assert result.reasons == ["ESCALATE_ONLY_REVIEW_FAILED: timeout again"]
    assert h.paid == [] and _arrivals(h) == 1
    assert "retry" in _actions(h.ledger_path, first.arrival_id, online=True)
    assert first.arrival_id in [q.arrival_id for q in service.review_queue(h.ledger_path)]


def test_retry_still_applies_the_rules_a_paid_twin_makes_it_a_duplicate(tmp_path, grok):
    h = Harness(tmp_path, grok, LLMError("timeout after 1s"))  # the retry makes no model call
    first = _failed_review(h)
    twin = service.process_path(h.write("b.json", CLEAN), _offline(h)).results[0]
    assert twin.state == "paid" and len(h.paid) == 1

    result = service.retry(h.rt, first.arrival_id)

    assert (result.arrival_id, result.decision, result.state) == (
        first.arrival_id,
        "duplicate",
        "duplicate",
    )
    assert f"on arrival #{twin.arrival_id}" in result.reasons[0]
    assert len(h.paid) == 1 and _arrivals(h) == 2


def test_retry_reruns_a_failed_extraction(tmp_path, grok):
    h = Harness(
        tmp_path,
        grok,
        LLMError("timeout after 1s"),  # extraction: no usable answer
        LLMError("timeout after 1s"),  # advisory
        reply(total="1000.00"),  # extraction on retry
        ADVICE,
    )
    first = h.process("invoice.txt", txt())
    assert first.state == "needs_review" and "LLM_EXTRACTED" not in first.finding_codes
    assert _actions(h.ledger_path, first.arrival_id, online=True) == ("reject", "retry")

    result = service.retry(h.rt, first.arrival_id)

    # an extracted field is a Review Trigger: still Needs Review, now with a payable amount
    assert (result.arrival_id, result.state) == (first.arrival_id, "needs_review")
    assert "LLM_EXTRACTED" in result.finding_codes and h.paid == []
    assert _actions(h.ledger_path, first.arrival_id, online=True) == ("approve", "reject")


def test_retry_refused_when_a_reviewer_resolved_the_arrival_meanwhile(tmp_path, grok):
    h = Harness(tmp_path, grok, LLMError("timeout after 1s"), concur())
    first = _failed_review(h)

    def reject_meanwhile(event):
        if event.name == "escalate":  # while the model is being asked
            service.resolve(h.rt, first.arrival_id, "reject", "a reviewer got there first")

    with pytest.raises(service.RetryRefused, match="changed"):
        service.retry(dataclasses.replace(h.rt, on_event=reject_meanwhile), first.arrival_id)

    record = _record(h, first.arrival_id)
    assert record["decision"]["reasons"][0].startswith("ESCALATE_ONLY_REVIEW_FAILED")
    assert service.arrival_detail(h.ledger_path, first.arrival_id).state == "logged_rejection"
    assert h.paid == []
