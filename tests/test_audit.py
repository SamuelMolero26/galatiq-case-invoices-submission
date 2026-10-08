"""Model audit persistence and the model_notes mapping (REQ-AUD-1..5, REQ-NOTES-1)."""

import json
import logging
from decimal import Decimal

import pytest
from conftest import Harness, concur, text_reply, tool_reply

from invoice_pipeline import ledger, service
from invoice_pipeline.llm import LLMError, TierConfig

SENTINEL = "sk-AUDIT-SENTINEL-4b7e"

GATE = {  # row 5 within bound: price 20% above the reference -> full gate
    "invoice_number": "INV-8101",
    "vendor": {"name": "Precision Parts Ltd."},
    "date": "2026-01-22",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 300.00}],
    "subtotal": 300.00,
    "total": 300.00,
    "currency": "USD",
}
CLEAN = {  # row 6
    **GATE,
    "invoice_number": "INV-8100",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250.00}],
    "subtotal": 250.00,
    "total": 250.00,
}
SHORTAGE = {  # row 3: 40 units against a stock of 15
    **GATE,
    "invoice_number": "INV-8103",
    "line_items": [{"item": "WidgetA", "quantity": 40, "unit_price": 250.00}],
    "subtotal": 10000.00,
    "total": 10000.00,
}
MESSY_TXT = """Vendor: Precision Parts Ltd.
Invoice: INV-8200
Date: 2026-01-05

WidgetA  qty: 4  unit price: $250.00

Please remit the amount of 1,000.00 within 30 days.
"""


def advice(rationale):
    return text_reply(json.dumps({"rationale": rationale}))


GATE_REPLIES = (
    tool_reply("get_reference_price", '{"sku": "WidgetA"}'),
    text_reply(
        json.dumps(
            {
                "assessments": [
                    {
                        "code": "PRICE_DEVIATION",
                        "line": 0,
                        "explained": True,
                        "evidence": ["invoice.items.0.unit_price", "tool.0.result.unit_price"],
                        "rationale": "price differs from the reference by a known amount",
                    }
                ]
            }
        )
    ),
    text_reply(
        json.dumps(
            {"checks": [{"code": "PRICE_DEVIATION", "line": 0, "holds": True, "rationale": "ok"}]}
        )
    ),
)


def stored(harness, arrival_id):
    conn = ledger.connect(harness.ledger_path)
    try:
        row = conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()
    finally:
        conn.close()
    return row, json.loads(row["record"])


# --- AUD-1: every audit element is persisted -------------------------------------------


def test_record_has_all_role_audit_elements(tmp_path, grok):
    h = Harness(tmp_path, grok, *GATE_REPLIES)

    result = h.process("gate.json", GATE)

    _, record = stored(h, result.arrival_id)
    attempt = record["decision"]["critic"]["attempts"][0]
    assessor, verifier = attempt["assessor"], attempt["verifier"]
    assert assessor["model"] and assessor["accepted"] and assessor["tries"]
    exchange = assessor["tries"][-1]["exchanges"][-1]
    assert exchange["raw_answer"] and exchange["called_at"] and "elapsed_ms" in exchange
    call = assessor["tool_calls"][0]
    assert call["name"] == "get_reference_price" and call["arguments"] == {"sku": "WidgetA"}
    assert call["result"] and call["called_at"]
    assert assessor["assessments"][0]["code"] == "PRICE_DEVIATION" and assessor["failures"] == []
    assert verifier["accepted"] and verifier["checks"][0]["holds"] is True
    assert record["decision"]["outcome"] == "approved"
    assert record["decision"]["bound_failures"] == []
    assert record["raw_text"] is None and record["extraction"] is None


def test_record_has_raw_text_and_extraction_block(tmp_path, grok):
    h = Harness(tmp_path, grok, text_reply(json.dumps({"total": "1000.00"})), advice("why"))

    result = h.process("invoice.txt", MESSY_TXT)

    _, record = stored(h, result.arrival_id)
    block = record["extraction"]
    assert record["raw_text"].strip() == MESSY_TXT.strip()
    assert block["requested"] == ["total"] and block["supplied"] == ["total"]
    call = block["call"]
    assert call["role"] == "extraction" and call["tier"] == "grok" and call["model"]
    assert call["answer"] == {"total": "1000.00"} and call["error"] is None
    assert call["tries"][0]["exchanges"][0]["raw_answer"]


# --- AUD-2: only the final re-decision is stored ---------------------------------------


def test_only_final_redecision_stored(tmp_path, grok, monkeypatch):
    h = Harness(tmp_path, grok, advice("first"), advice("second"))
    real, rounds = ledger.record_if_unchanged, []

    def stale_once(*args):
        rounds.append(1)
        return None if len(rounds) == 1 else real(*args)

    monkeypatch.setattr(ledger, "record_if_unchanged", stale_once)

    result = h.process("shortage.json", SHORTAGE)

    _, record = stored(h, result.arrival_id)
    assert "redecided" in h.names
    assert record["decision"]["advisory"]["answer"]["rationale"] == "second"


# --- AUD-3: failed and offline calls stay explainable ----------------------------------


def test_failed_and_offline_calls_explainable(tmp_path, grok):
    (tmp_path / "online").mkdir()
    failed = Harness(tmp_path / "online", grok, LLMError("HTTP 500"), advice("still advised"))
    result = failed.process("invoice.txt", MESSY_TXT)
    _, record = stored(failed, result.arrival_id)
    assert record["extraction"]["supplied"] == []
    assert record["extraction"]["call"]["error"] and record["extraction"]["call"]["answer"] is None

    (tmp_path / "offline").mkdir()
    offline = Harness(tmp_path / "offline", grok, tier="offline")
    offline.rt = service.Runtime(
        catalog=offline.rt.catalog,
        ledger_path=offline.ledger_path,
        pay_fn=lambda *args: {"status": "success"},
    )
    result = service.process_path(offline.write("clean.json", CLEAN), offline.rt).results[0]
    _, record = stored(offline, result.arrival_id)
    call = record["decision"]["escalate_review"]
    assert call["tier"] == "offline" and call["error"] == "offline tier"
    assert record["extraction"] is None


# --- AUD-4: the key never reaches the record, logs or repr -----------------------------


def test_sentinel_key_absent_from_record_caplog_repr(tmp_path, caplog):
    tier = TierConfig(
        tier="grok", model="m", base_url="http://llm.invalid", timeout_s=1, api_key=SENTINEL
    )
    h = Harness(tmp_path, tier, text_reply(json.dumps({"total": "1000.00"})), advice("why"))

    with caplog.at_level(logging.DEBUG):
        result = h.process("invoice.txt", MESSY_TXT)

    row, _ = stored(h, result.arrival_id)
    texts = [row["record"], caplog.text, repr(tier), str(tier), tier.model_dump_json()]
    texts += [json.dumps(h.chat.requests), json.dumps([e.detail for e in h.events], default=str)]
    assert all(SENTINEL not in text for text in texts)


def test_record_persists_the_usd_equivalent_the_decision_used(tmp_path, grok):
    h = Harness(tmp_path, grok, advice("eur"))
    result = h.process("eur.json", {**SHORTAGE, "currency": "EUR"})

    _, record = stored(h, result.arrival_id)

    usd = record["usd_equivalent"]
    assert Decimal(usd.pop("amount")) == Decimal("11340")  # 10,000 EUR x 1.08 x 1.05
    assert usd == {"currency": "EUR", "rate": "1.08", "as_of": "2026-01-02", "buffer": "0.05"}


def test_unreadable_record_has_no_usd_equivalent(tmp_path, grok):
    h = Harness(tmp_path, grok)
    result = h.process("broken.pdf", "not a pdf")

    _, record = stored(h, result.arrival_id)

    assert record["invoice"] is None and record["usd_equivalent"] is None


# --- AUD-5: records written before this change stay readable ---------------------------


def test_legacy_record_without_extraction_readable(tmp_path, grok):
    h = Harness(tmp_path, grok, advice("legacy"))
    result = h.process("shortage.json", SHORTAGE)
    conn = ledger.connect(h.ledger_path)
    try:
        row = conn.execute(
            "SELECT record FROM arrivals WHERE id = ?", (result.arrival_id,)
        ).fetchone()
        record = json.loads(row["record"])
        del record["raw_text"], record["extraction"]
        with conn:
            conn.execute(
                "UPDATE arrivals SET record = ? WHERE id = ?",
                (json.dumps(record), result.arrival_id),
            )
        legacy = service.arrival_result(conn, result.arrival_id)
    finally:
        conn.close()

    assert legacy.model_notes == "advisory"


# --- NOTES-1: one deterministic mapping ------------------------------------------------


def role(tier="grok"):
    return {"role": "x", "tier": tier}


def record(extraction=False, **decision):
    return {
        "decision": decision,
        "extraction": {"requested": ["total"], "supplied": [], "call": role()}
        if extraction
        else None,
    }


@pytest.mark.parametrize(
    "rec,expected",
    [
        (record(critic={"attempts": []}), "critic"),  # a failed critic still names the role
        (record(unreviewed_warnings=True), "offline tier"),  # offline row 5
        (record(escalate_review=role("offline")), "offline tier"),  # offline row 6
        (record(escalate_review=role()), "escalate_review"),
        (record(advisory=role()), "advisory"),
        (record(extraction=True), "extraction"),
        (record(extraction=True, advisory=role()), "extraction, advisory"),
        (record(), "none"),
    ],
)
def test_model_notes_mapping(rec, expected):
    assert service._model_notes(rec) == expected
    assert service._model_notes(rec) == service._model_notes(rec)  # deterministic


def test_extracted_collision_note():
    collision = record(extraction=True, duplicate_of=7, outcome="needs_review")

    assert service._model_notes(collision) == "extraction"


def test_online_gate_run_reports_critic(tmp_path, grok):
    h = Harness(tmp_path, grok, *GATE_REPLIES)

    assert h.process("gate.json", GATE).model_notes == "critic"


def test_online_row6_reports_escalate_review(tmp_path, grok):
    h = Harness(tmp_path, grok, concur())

    assert h.process("clean.json", CLEAN).model_notes == "escalate_review"
