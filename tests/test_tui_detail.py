"""Results read models for the reviewer TUI, built only from the Ledger record (plan 4.2-4.3)."""

import json
from datetime import date
from decimal import Decimal

import pytest
from conftest import Harness, concur, ledger_states, text_reply

from invoice_pipeline import ledger, service, view

EUR_GATE = {  # only CURRENCY_NON_USD: the full gate runs on it
    "invoice_number": "INV-8401",
    "vendor": {"name": "Precision Parts Ltd."},
    "date": "2026-01-22",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 300.00}],
    "subtotal": 300.00,
    "total": 300.00,
    "currency": "EUR",
}
EUR_SHORTAGE = {  # row 3: 20 units against a stock of 15, advisory role runs
    **EUR_GATE,
    "invoice_number": "INV-8403",
    "line_items": [{"item": "WidgetA", "quantity": 20, "unit_price": 250.00}],
    "subtotal": 5000.00,
    "total": 5000.00,
}
EUR_HEIGHTENED = {  # 9,000 EUR: 10,206 USD with the buffer
    **EUR_GATE,
    "invoice_number": "INV-8402",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 9000.00}],
    "subtotal": 9000.00,
    "total": 9000.00,
}
CLEAN_USD = {  # no findings: row 6, the escalate-only review runs
    **EUR_GATE,
    "invoice_number": "INV-8405",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250.00}],
    "subtotal": 250.00,
    "total": 250.00,
    "currency": "USD",
}
ADVICE = text_reply(json.dumps({"rationale": "explained for the reviewer"}))


def _arrival_id(ledger_path, source) -> int:
    rows = view.results(ledger_path).rows
    return next(r.arrival_id for r in rows if r.source == source)


def _detail(ledger_path, source) -> view.ArrivalDetail:
    return view.arrival_detail(ledger_path, _arrival_id(ledger_path, source))


def _notes(detail) -> dict[str, view.Note]:
    return {note.role: note for note in detail.notes}


def test_views_follow_ledger_states_and_the_review_queue(batch_ledger):
    results = view.results(batch_ledger)
    states = ledger_states(batch_ledger)

    assert results.count("all") == sum(states.values()) == 20
    assert results.count("approved") == states["paid"]
    assert results.count("needs_review") == len(service.review_queue(batch_ledger))
    assert results.count("rejected") == states["logged_rejection"]
    duplicates = [r for r in results.rows if r.state == "duplicate"]
    assert len(duplicates) == 2 and all(r.view is None for r in duplicates)  # only under "all"
    assert [r.arrival_id for r in results.in_view("all")] == sorted(
        r.arrival_id for r in results.rows
    )


def test_funnel_counts_come_from_the_ledger(batch_ledger):
    results = view.results(batch_ledger)
    states = ledger_states(batch_ledger)

    assert results.files == 20
    assert results.funnel["ingest"] == 20
    assert results.funnel["paid"] == states["paid"]
    assert results.funnel["approve"] == states["paid"] + states["payment_pending"]
    assert results.funnel["paid"] <= results.funnel["approve"] <= results.funnel["validate"]


def test_eur_detail_carries_the_stored_usd_equivalent_rate_and_as_of(batch_ledger):
    detail = _detail(batch_ledger, "invoice_1014.xml")

    assert detail.view == "needs_review"
    usd = detail.usd
    assert (usd.total, usd.currency) == (Decimal("4125.00"), "EUR")
    assert usd.amount == Decimal("4677.75")
    assert usd.rate == Decimal("1.08") and usd.buffer_pct == 5
    assert usd.as_of == date(2026, 1, 2)
    assert detail.finding_codes == ["CURRENCY_NON_USD"]


def test_usd_invoice_has_no_usd_evidence(batch_ledger):
    assert _detail(batch_ledger, "invoice_1001.txt").usd is None


def _rewrite_record(ledger_path, arrival_id, change) -> None:
    """Edit one stored record in place, as a different writer (or an older one) would have."""
    conn = ledger.connect(ledger_path)
    try:
        row = conn.execute("SELECT record FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()
        record = json.loads(row["record"])
        change(record)
        with ledger.write_txn(conn):
            conn.execute(
                "UPDATE arrivals SET record = ? WHERE id = ?", (json.dumps(record), arrival_id)
            )
    finally:
        conn.close()


def test_usd_evidence_does_not_depend_on_the_finding_wording(batch_ledger):
    arrival_id = _arrival_id(batch_ledger, "invoice_1014.xml")

    def reword(record):
        for f in record["findings"]:
            f["detail"] = "non-USD invoice (wording changed)"

    _rewrite_record(batch_ledger, arrival_id, reword)
    usd = view.arrival_detail(batch_ledger, arrival_id).usd

    assert (usd.total, usd.currency, usd.amount) == (Decimal("4125.00"), "EUR", Decimal("4677.75"))
    assert (usd.rate, usd.as_of, usd.buffer_pct) == (Decimal("1.08"), date(2026, 1, 2), 5)


def test_legacy_record_without_usd_equivalent_has_no_usd_evidence(batch_ledger):
    arrival_id = _arrival_id(batch_ledger, "invoice_1014.xml")

    _rewrite_record(batch_ledger, arrival_id, lambda record: record.pop("usd_equivalent", None))
    detail = view.arrival_detail(batch_ledger, arrival_id)

    assert detail.usd is None  # no fallback to parsing the finding detail
    assert detail.finding_codes == ["CURRENCY_NON_USD"]


def test_rejected_detail_shows_findings_and_failed_stages(batch_ledger):
    detail = _detail(batch_ledger, "invoice_1009.json")

    assert (detail.view, detail.state, detail.scrutiny) == (
        "rejected",
        "logged_rejection",
        "standard",
    )
    assert detail.finding_codes == [  # unique codes, in recorded order
        "PARTIAL_IDENTITY",
        "QUANTITY_INVALID",
        "NONPOSITIVE_TOTAL",
        "RECONCILIATION_MISMATCH",
    ]
    stages = {s.name: s for s in detail.stages}
    assert [s.name for s in detail.stages] == ["ingestion", "validation", "approval", "payment"]
    assert (
        stages["validation"].status == "fail" and "QUANTITY_INVALID" in stages["validation"].summary
    )
    assert stages["approval"].status == "fail" and "row 2" in stages["approval"].summary
    assert (stages["payment"].status, stages["payment"].summary) == ("logged", "rejection logged")


def test_paid_and_duplicate_payment_stages(batch_ledger):
    paid = {s.name: s for s in _detail(batch_ledger, "invoice_1011.pdf").stages}
    duplicate = _detail(batch_ledger, "invoice_1011.txt")

    assert paid["payment"].status == "ok" and "3000.00 USD" in paid["payment"].summary
    assert duplicate.view is None
    assert {s.name: s for s in duplicate.stages}["payment"].summary.startswith(
        "not paid: duplicate"
    )


def test_offline_role_calls_are_not_labeled_model_output(batch_ledger):
    notes = _notes(_detail(batch_ledger, "invoice_1001.txt"))

    assert notes["rule engine"].text == "no findings" and not notes["rule engine"].model
    assert "offline tier" in notes["escalate-review"].text
    assert not notes["escalate-review"].model


def test_model_answers_are_labeled_model_output(tmp_path, grok):
    h = Harness(tmp_path, grok, ADVICE)
    result = h.process("eur.json", EUR_SHORTAGE)

    notes = _notes(view.arrival_detail(h.ledger_path, result.arrival_id))

    assert notes["advisory"].model and notes["advisory"].text == "explained for the reviewer"
    assert not notes["rule engine"].model


def test_escalate_review_verdict_is_model_output(tmp_path, grok):
    h = Harness(tmp_path, grok, concur())
    result = h.process("usd.json", CLEAN_USD)

    note = _notes(view.arrival_detail(h.ledger_path, result.arrival_id))["escalate-review"]

    assert note.model and note.text == "concur: nothing needs a human"


def test_full_gate_attempts_become_assessor_and_verifier_notes(tmp_path, grok):
    assessment = {
        "code": "CURRENCY_NON_USD",
        "line": None,
        "explained": True,
        "evidence": ["invoice.currency", "references.usd_equivalent.amount"],
        "rationale": "the converted amount is well under the limit",
    }
    check = {"code": "CURRENCY_NON_USD", "line": None, "holds": True, "rationale": "ok"}
    h = Harness(
        tmp_path,
        grok,
        text_reply(json.dumps({"assessments": [assessment]})),
        text_reply(json.dumps({"checks": [check]})),
    )
    result = h.process("eur.json", EUR_GATE)

    detail = view.arrival_detail(h.ledger_path, result.arrival_id)
    notes = _notes(detail)

    assert detail.view == "approved"
    assert notes["assessor #1"].model and "well under the limit" in notes["assessor #1"].text
    assert notes["verifier #1"].model and "CURRENCY_NON_USD holds" in notes["verifier #1"].text


def test_heightened_scrutiny_is_read_from_the_decision(tmp_path, grok):
    h = Harness(tmp_path, grok, ADVICE)
    result = h.process("eur.json", EUR_HEIGHTENED)

    assert view.arrival_detail(h.ledger_path, result.arrival_id).scrutiny == "heightened"


def test_unknown_arrival_is_refused(batch_ledger):
    with pytest.raises(LookupError, match="no arrival #999"):
        view.arrival_detail(batch_ledger, 999)
