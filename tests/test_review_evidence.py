"""USD Equivalent as Reviewer evidence: Case File, Review Queue, and payment (plan 3.2)."""

import json
from datetime import date
from decimal import Decimal

from conftest import Harness, text_reply

from invoice_pipeline import service

EUR_GATE = {  # only CURRENCY_NON_USD: the full gate may approve it
    "invoice_number": "INV-8401",
    "vendor": {"name": "Precision Parts Ltd."},
    "date": "2026-01-22",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 300.00}],
    "subtotal": 300.00,
    "total": 300.00,
    "currency": "EUR",
}
EUR_HEIGHTENED = {  # 9,000 EUR: 9,720 USD at the bare rate, 10,206 USD with the buffer
    **EUR_GATE,
    "invoice_number": "INV-8402",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 9000.00}],
    "subtotal": 9000.00,
    "total": 9000.00,
}
EUR_SHORTAGE = {  # row 3: 20 units against a stock of 15
    **EUR_GATE,
    "invoice_number": "INV-8403",
    "line_items": [{"item": "WidgetA", "quantity": 20, "unit_price": 250.00}],
    "subtotal": 5000.00,
    "total": 5000.00,
}
JPY_SHORTAGE = {**EUR_SHORTAGE, "invoice_number": "INV-8404", "currency": "JPY"}
ADVICE = text_reply(json.dumps({"rationale": "explained for the reviewer"}))


def _currency_gate_replies(*evidence):
    assessment = {
        "code": "CURRENCY_NON_USD",
        "line": None,
        "explained": True,
        "evidence": list(evidence),
        "rationale": "the converted amount is well under the limit",
    }
    check = {"code": "CURRENCY_NON_USD", "line": None, "holds": True, "rationale": "ok"}
    return (
        text_reply(json.dumps({"assessments": [assessment]})),
        text_reply(json.dumps({"checks": [check]})),
    )


def _case_file(request) -> dict:
    """The Case File a model role received (the JSON user message carrying `references`)."""
    for message in request["messages"]:
        content = message.get("content") or ""
        if message["role"] == "user" and content.startswith("{") and '"references"' in content:
            return json.JSONDecoder().raw_decode(content)[0]
    raise AssertionError("no Case File in the request")


def _queued(h, arrival_id):
    return next(q for q in service.review_queue(h.ledger_path) if q.arrival_id == arrival_id)


def test_non_usd_case_file_carries_amount_rate_and_as_of(tmp_path, grok):
    replies = _currency_gate_replies("invoice.currency", "references.usd_equivalent.amount")
    h = Harness(tmp_path, grok, *replies)

    result = h.process("eur.json", EUR_GATE)

    usd = _case_file(h.chat.requests[0])["references"]["usd_equivalent"]
    assert Decimal(usd["amount"]) == Decimal("340.20")  # 300 x 1.08 x 1.05
    assert Decimal(usd["rate"]) == Decimal("1.08") and Decimal(usd["buffer"]) == Decimal("0.05")
    assert date.fromisoformat(usd["as_of"]) == date(2026, 1, 2)
    # the evidence path names a non-null value now, so the gate may approve on it
    assert result.decision == "approved", result.reasons


def test_payment_arguments_never_carry_a_converted_amount(tmp_path, grok):
    replies = _currency_gate_replies("invoice.currency", "references.usd_equivalent.amount")
    h = Harness(tmp_path, grok, *replies)

    h.process("eur.json", EUR_GATE)

    assert h.paid == [("Precision Parts Ltd.", Decimal("300.00"), "EUR")]


def test_heightened_scrutiny_and_reviewer_use_the_same_usd_equivalent(tmp_path, grok):
    h = Harness(tmp_path, grok, ADVICE)

    result = h.process("eur.json", EUR_HEIGHTENED)

    assert result.decision == "needs_review" and result.precedence_row == 4
    assert result.reasons[0].startswith("HEIGHTENED_SCRUTINY: 10206.00 USD")
    advised = _case_file(h.chat.requests[0])["references"]["usd_equivalent"]
    assert Decimal(advised["amount"]) == Decimal("10206.00")
    item = _queued(h, result.arrival_id)
    assert (item.total, item.currency) == (Decimal("9000.00"), "EUR")
    assert any("10206.00 USD" in reason for reason in item.reasons)
    assert h.paid == []


def test_review_queue_detail_shows_amount_rate_and_as_of(tmp_path, grok):
    h = Harness(tmp_path, grok, ADVICE)

    result = h.process("eur.json", EUR_SHORTAGE)

    item = _queued(h, result.arrival_id)
    currency = next(r for r in item.reasons if r.startswith("CURRENCY_NON_USD"))
    assert "5670.00 USD" in currency  # 5,000 x 1.08 x 1.05
    assert "1.08" in currency and "2026-01-02" in currency
    assert (item.total, item.currency) == (Decimal("5000.00"), "EUR")


def test_missing_rate_detail_names_the_absence(tmp_path, grok):
    h = Harness(tmp_path, grok, ADVICE)

    result = h.process("jpy.json", JPY_SHORTAGE)

    assert "CURRENCY_NO_RATE" in result.finding_codes
    item = _queued(h, result.arrival_id)
    assert "CURRENCY_NO_RATE: no reference rate for JPY" in item.reasons
    assert any("JPY; USD equivalent unavailable" in reason for reason in item.reasons)
    assert _case_file(h.chat.requests[0])["references"]["usd_equivalent"] is None
