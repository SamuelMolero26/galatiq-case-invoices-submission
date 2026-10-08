"""Reference Rates: pinned USD conversion used only to classify, never to pay (plan 3.1)."""

import json
from datetime import date
from decimal import Decimal

from conftest import Harness, invoice, line, text_reply

from invoice_pipeline import ledger
from invoice_pipeline.approval import decide
from invoice_pipeline.critic import build_case_file, offline_agents
from invoice_pipeline.model import ArrivalSummary, FindingCode, Outcome, UsdEquivalent
from invoice_pipeline.rates import REFERENCE_RATES, SAFETY_BUFFER, ReferenceRate, usd_equivalent
from invoice_pipeline.validation import validate

F = FindingCode


def test_eur_rate_and_safety_buffer_are_pinned():
    assert REFERENCE_RATES["EUR"] == ReferenceRate(rate=Decimal("1.08"), as_of=date(2026, 1, 2))
    assert SAFETY_BUFFER == Decimal("0.05")
    assert "USD" not in REFERENCE_RATES  # USD is implicit


def test_eur_usd_equivalent_applies_the_rate_and_the_buffer():
    usd = usd_equivalent(Decimal("1000.00"), "EUR")

    assert usd == UsdEquivalent(
        amount=Decimal("1134.00"),
        rate=Decimal("1.08"),
        as_of=date(2026, 1, 2),
        buffer=SAFETY_BUFFER,
    )


def test_usd_is_implicit_at_par_with_no_buffer():
    usd = usd_equivalent(Decimal("10000.00"), "USD")

    assert usd is not None
    assert (usd.amount, usd.rate, usd.buffer) == (Decimal("10000.00"), Decimal(1), Decimal(0))


def test_a_currency_without_a_reference_rate_has_no_usd_equivalent():
    assert usd_equivalent(Decimal("100"), "JPY") is None


def test_eur_with_a_rate_is_a_warning_only_and_names_amount_rate_and_as_of(catalog):
    inv = invoice([line("WidgetA", "250")], currency="EUR")

    findings = validate(inv, catalog)

    assert [f.code for f in findings] == [F.CURRENCY_NON_USD]
    detail = findings[0].detail
    assert "EUR" in detail and "283.50 USD" in detail  # 250 x 1.08 x 1.05
    assert "1.08" in detail and "2026-01-02" in detail and "5%" in detail


def test_missing_rate_is_a_review_trigger_that_names_the_absence(catalog):
    inv = invoice([line("WidgetA", "250")], currency="JPY")

    findings = {f.code: f for f in validate(inv, catalog)}

    assert set(findings) == {F.CURRENCY_NON_USD, F.CURRENCY_NO_RATE}
    assert "no reference rate for JPY" in findings[F.CURRENCY_NO_RATE].detail
    assert "USD equivalent unavailable" in findings[F.CURRENCY_NON_USD].detail


def _decide(catalog, unit_price: str, currency: str):
    """VENDOR_UNKNOWN keeps rows 4-5 free of model calls: row 4 if above the limit, else 5."""
    inv = invoice([line("WidgetA", unit_price)], vendor="Unlisted Supply", currency=currency)
    case_file = build_case_file(
        inv,
        validate(inv, catalog),
        ArrivalSummary(kind="new"),
        usd_equivalent(inv.total, inv.currency),
        catalog,
        [],
        0,
    )
    return decide(case_file, offline_agents())


def test_exactly_10000_usd_is_not_above_the_critic_limit(catalog):
    assert _decide(catalog, "10000.00", "USD").precedence_row == 5
    assert _decide(catalog, "10000.01", "USD").precedence_row == 4


def test_buffered_eur_compares_against_the_limit_in_usd(catalog):
    assert _decide(catalog, "8818.34", "EUR").precedence_row == 5  # 9,999.99756 USD
    over = _decide(catalog, "8818.35", "EUR")  # 10,000.0089 USD
    assert over.precedence_row == 4 and over.outcome is Outcome.NEEDS_REVIEW


def test_the_buffer_makes_heightened_scrutiny_more_likely(catalog):
    # 9,000 EUR is 9,720 USD at the bare rate but 10,206 USD with the buffer.
    decision = _decide(catalog, "9000.00", "EUR")

    assert decision.precedence_row == 4
    assert "10206.00 USD" in decision.reasons[0]


def test_conversion_is_classification_only(catalog):
    inv = invoice([line("WidgetA", "250")], currency="EUR")
    before = inv.model_dump()

    validate(inv, catalog)
    usd_equivalent(inv.total, inv.currency)

    assert inv.model_dump() == before


EUR_GATE = {  # only CURRENCY_NON_USD: the full gate may approve it
    "invoice_number": "INV-8301",
    "vendor": {"name": "Precision Parts Ltd."},
    "date": "2026-01-22",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 300.00}],
    "subtotal": 300.00,
    "total": 300.00,
    "currency": "EUR",
}


def _currency_gate_replies(*evidence):
    assessment = {
        "code": "CURRENCY_NON_USD",
        "line": None,
        "explained": True,
        "evidence": list(evidence),
        "rationale": "the vendor invoices in EUR",
    }
    check = {"code": "CURRENCY_NON_USD", "line": None, "holds": True, "rationale": "ok"}
    return (
        text_reply(json.dumps({"assessments": [assessment]})),
        text_reply(json.dumps({"checks": [check]})),
    )


def test_payment_and_ledger_keep_the_original_amount_and_currency(tmp_path, grok):
    h = Harness(tmp_path, grok, *_currency_gate_replies("invoice.currency"))

    result = h.process("eur.json", EUR_GATE)

    assert result.decision == "approved", result.reasons
    assert h.paid == [("Precision Parts Ltd.", Decimal("300.00"), "EUR")]
    conn = ledger.connect(h.ledger_path)
    try:
        row = conn.execute("SELECT * FROM arrivals WHERE id = ?", (result.arrival_id,)).fetchone()
    finally:
        conn.close()
    assert (row["currency"], Decimal(row["total"])) == ("EUR", Decimal("300.00"))
    assert Decimal(row["amount_paid"]) == Decimal("300.00")
