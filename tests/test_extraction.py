"""Extraction Fallback: gating, strict grounded parser, correction, merge (callable level)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import Harness, ScriptedChat, text_reply

from invoice_pipeline import extraction, ledger
from invoice_pipeline.critic import offline_agents, online_agents
from invoice_pipeline.ingestion.text import parse_text
from invoice_pipeline.llm import CorrectableError, LLMError
from invoice_pipeline.model import Agents, FindingCode, Ingested, Outcome, RoleCall
from invoice_pipeline.validation import validate

FIXTURES = Path(__file__).parent / "fixtures" / "extraction"


def load(name: str, fmt="txt") -> Ingested:
    return parse_text((FIXTURES / f"{name}.txt").read_text(), f"{name}.{fmt}", fmt)


def answer(**fields) -> object:
    return text_reply(json.dumps(fields))


def run(ingested: Ingested, grok, *replies):
    fields = extraction.requested(ingested)
    chat = ScriptedChat(*replies)
    return fields, chat, extraction.extract(grok, ingested.raw_text, fields, chat_fn=chat)


# --- requested(): gating ---------------------------------------------------------------


def test_requested_gating():
    assert extraction.requested(load("missing_total")) == ["total"]
    assert extraction.requested(load("missing_identity")) == ["vendor", "invoice_number", "total"]
    assert extraction.requested(load("complete")) == []
    assert extraction.requested(load("missing_total", "pdf")) == ["total"]
    structured = load("missing_total").model_copy(
        update={
            "invoice": load("missing_total").invoice.model_copy(update={"source_format": "csv"})
        }
    )
    assert extraction.requested(structured) == []
    assert extraction.requested(load("missing_total").model_copy(update={"raw_text": "  \n"})) == []
    assert extraction.requested(load("missing_total").model_copy(update={"raw_text": None})) == []
    assert extraction.requested(Ingested(invoice=None, findings=[])) == []


def test_requested_never_asks_for_items():
    ingested = load("missing_total").model_copy(update={"missing_required": ["items", "total"]})

    assert extraction.requested(ingested) == ["total"]


# --- parse_extraction(): strict and grounded -------------------------------------------

RAW = "Acme Corp invoice 2002\nGrand sum 1,000.00\n"


def parse(content, fields=("total",), raw=RAW):
    return extraction.parse_extraction(content, list(fields), raw)


def test_parser_accepts_exact_grounded_object():
    assert parse('{"total": "1000.00"}') == {"total": "1000.00"}
    assert parse('{"total": null}') == {"total": None}
    assert parse('{"vendor": "ACME  corp", "total": "1000"}', ("vendor", "total")) == {
        "vendor": "ACME  corp",
        "total": "1000",
    }


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[1]",
        '{"vendor": "Acme Corp"}',  # not requested
        '{"total": "1000.00", "vendor": "Acme Corp"}',  # extra key
        "{}",  # missing key
    ],
)
def test_parser_rejects_unknown_or_nonrequested_key(content):
    assert isinstance(parse(content), CorrectableError)


@pytest.mark.parametrize(
    "content,fields",
    [
        ('{"total": 1000.00}', ("total",)),  # number type
        ('{"total": true}', ("total",)),
        ('{"total": ""}', ("total",)),  # empty
        ('{"vendor": "Acme\\nCorp"}', ("vendor",)),  # multiline
        ('{"vendor": "%s"}' % ("A" * 121), ("vendor",)),  # too long
        ('{"total": "$1,000.00"}', ("total",)),  # not a plain decimal
        ('{"total": "1000.001"}', ("total",)),
        ('{"total": "999999.00"}', ("total",)),  # ungrounded amount
        ('{"vendor": "Globex"}', ("vendor",)),  # ungrounded text
        ('{"invoice_number": "9999"}', ("invoice_number",)),
    ],
)
def test_parser_rejects_number_type_empty_multiline_ungrounded(content, fields):
    assert isinstance(parse(content, fields), CorrectableError)


# --- extract(): correction wrapper, never raises ---------------------------------------


def test_invalid_decimal_corrected_within_3_tries(grok):
    ingested = load("missing_total")
    _, chat, call = run(
        ingested,
        grok,
        answer(total="abc"),
        answer(total="12,5x"),
        answer(total="1000.00"),
    )

    assert call.answer == {"total": "1000.00"} and call.error is None
    assert len(call.tries) == 3 and len(chat.requests) == 3
    assert call.role == "extraction" and call.tier == "grok"


def test_prompt_names_only_requested_fields_and_carries_document_as_data(grok):
    ingested = load("injection")
    _, chat, _ = run(ingested, grok, answer(total="500.00"))

    system, user = chat.requests[0]["messages"][:2]
    assert system["role"] == "system" and "total" in system["content"]
    assert "vendor" not in system["content"] and "untrusted" in system["content"]
    assert json.loads(user["content"]) == {"document": ingested.raw_text}
    assert chat.requests[0]["tools"] is None


def test_three_invalid_supplies_nothing(grok):
    ingested = load("missing_total")
    _, _, call = run(ingested, grok, answer(total="a"), answer(total="b"), answer(total="c"))

    assert call.answer is None and "still invalid after 3 tries" in call.error
    assert extraction.merge(ingested, call).invoice == ingested.invoice


def test_extract_never_raises_on_transport_error(grok):
    ingested = load("missing_total")
    _, chat, call = run(ingested, grok, LLMError("timeout after 1s"))

    assert call.answer is None and "timeout" in call.error and len(chat.requests) == 1


def test_extract_never_raises_on_unexpected_exception(grok):
    def boom(tier, messages, tools=None):
        raise RuntimeError("boom")

    call = extraction.extract(grok, "text 1,000.00", ["total"], chat_fn=boom)

    assert call.answer is None and "boom" in call.error


# --- merge() ---------------------------------------------------------------------------


def supplied(ingested, grok, **fields):
    _, _, call = run(ingested, grok, answer(**fields))
    return call, extraction.merge(ingested, call)


def test_merge_fills_only_requested(grok):
    ingested = load("missing_identity")
    call, merged = supplied(
        ingested, grok, vendor="Acme Corp", invoice_number=None, total="1000.00"
    )

    assert merged.invoice.vendor == "Acme Corp"
    assert merged.invoice.total == Decimal("1000.00")
    assert merged.invoice.invoice_number is None
    assert merged.invoice.extracted_fields == ["vendor", "total"]
    assert merged.extraction == call
    assert merged.missing_required == ingested.missing_required  # input of the audit, untouched
    assert ingested.invoice.vendor is None  # the input is not mutated


def test_merge_ignores_fields_that_were_not_requested(grok):
    ingested = load("missing_total")
    forged = RoleCall(
        role="extraction",
        tier="grok",
        model="m",
        tries=[],
        answer={"vendor": "Evil Inc", "total": "1000.00"},
    )

    merged = extraction.merge(ingested, forged)

    assert merged.invoice.vendor == "Acme Corp"
    assert merged.invoice.extracted_fields == ["total"]


def test_merge_normalizes_invoice_number(grok):
    call, merged = supplied(
        load("missing_identity"), grok, vendor=None, invoice_number="2002", total=None
    )

    assert merged.invoice.invoice_number == "INV-2002"


def test_llm_extracted_iff_supplied(grok):
    ingested = load("missing_identity")
    _, none_supplied = supplied(ingested, grok, vendor=None, invoice_number=None, total=None)
    _, some = supplied(ingested, grok, vendor="Acme Corp", invoice_number=None, total=None)

    assert none_supplied.findings == [] and none_supplied.extraction is not None
    assert none_supplied.invoice.extracted_fields == []
    assert [f.code for f in some.findings] == [FindingCode.LLM_EXTRACTED]


def test_llm_extracted_findings_name_each_supplied_field(grok):
    _, merged = supplied(
        load("missing_identity"), grok, vendor="Acme Corp", invoice_number="2002", total="1000"
    )

    assert [f.code for f in merged.findings] == [FindingCode.LLM_EXTRACTED] * 3
    assert [f.detail.split(" ")[0] for f in merged.findings] == [
        "invoice.vendor",
        "invoice.invoice_number",
        "invoice.total",
    ]


def test_exhausted_leaves_ingested_unchanged_no_finding(grok):
    ingested = load("missing_total")
    _, _, call = run(ingested, grok, LLMError("HTTP 500"))

    merged = extraction.merge(ingested, call)

    assert merged.invoice == ingested.invoice and merged.findings == []
    assert merged.extraction == call and call.error


def test_reconcile_mismatch_when_extracted_total_differs(grok, catalog):
    ingested = load("injection")  # one WidgetA at 250 x 2 -> lines sum to 500
    raw = ingested.raw_text
    call = extraction.extract(grok, raw, ["total"], chat_fn=ScriptedChat(answer(total="500.00")))
    assert extraction.merge(ingested, call).invoice.total == Decimal("500.00")

    ungrounded_but_forged = RoleCall(
        role="extraction", tier="grok", model="m", tries=[], answer={"total": "750.00"}
    )
    merged = extraction.merge(ingested, ungrounded_but_forged)
    codes = [f.code for f in validate(merged.invoice, catalog)]

    assert FindingCode.RECONCILIATION_MISMATCH in codes


def test_injection_text_cannot_widen_the_answer(grok):
    ingested = load("injection")
    _, _, call = run(ingested, grok, answer(total="999999"), answer(total="500.00"))

    assert call.answer == {"total": "500.00"}  # the injected total is not in the document


# --- wiring (EXT-7, TIER-6) ------------------------------------------------------------


def test_agents_extract_defaults_none():
    assert offline_agents().extract is None
    assert Agents.__dataclass_fields__["extract"].default is None


def test_online_agents_extract_built_offline_none(grok):
    chat = ScriptedChat(answer(total="1000.00"))
    agents = online_agents(grok, chat_fn=chat)

    call = agents.extract("Grand sum 1,000.00", ["total"])

    assert call.role == "extraction" and call.answer == {"total": "1000.00"}
    assert offline_agents().extract is None


# --- service level: gating, never paid, grounded only (EXT-6) --------------------------

ADVICE = text_reply(json.dumps({"rationale": "explained for the reviewer"}))


def txt(vendor="Precision Parts Ltd.", number="INV-2001", total=None, note=""):
    """A messy TXT invoice; a None field is left out of the document."""
    lines = [f"Vendor: {vendor}" if vendor else None, f"Invoice: {number}" if number else None]
    lines += ["Date: 2026-01-05", "", "WidgetA  qty: 4  unit price: $250.00", "", note]
    lines += [f"Total: {total}" if total else "Please remit 1,000.00 within 30 days."]
    return "\n".join(line for line in lines if line is not None) + "\n"


def reply(**fields):
    return text_reply(json.dumps(fields))


def stored(harness, arrival_id):
    conn = ledger.connect(harness.ledger_path)
    try:
        row = conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()
    finally:
        conn.close()
    return row, json.loads(row["record"])


def test_txt_online_needs_review_never_paid(tmp_path, grok):
    h = Harness(tmp_path, grok, reply(total="1000.00"), ADVICE)

    result = h.process("invoice.txt", txt())

    assert result.decision == Outcome.NEEDS_REVIEW and result.state == "needs_review"
    assert result.precedence_row == 3 and "LLM_EXTRACTED" in result.finding_codes
    assert "LLM_EXTRACTED: invoice.total supplied by the Extraction Fallback" in result.reasons
    assert h.paid == []
    row, record = stored(h, result.arrival_id)
    assert record["invoice"]["total"] == "1000.00"
    assert record["invoice"]["extracted_fields"] == ["total"]


def test_extraction_failure_keeps_deterministic_findings_unpaid(tmp_path, grok):
    h = Harness(tmp_path, grok, LLMError("HTTP 500"), ADVICE)

    result = h.process("invoice.txt", txt())

    assert result.decision != Outcome.APPROVED and h.paid == []
    assert "LLM_EXTRACTED" not in result.finding_codes
    assert "MISSING_REQUIRED_FIELD" in result.finding_codes


def test_injection_text_only_grounded_requested_fields(tmp_path, grok):
    text = (FIXTURES / "injection.txt").read_text()
    h = Harness(tmp_path, grok, reply(total="999999"), reply(total="500.00"), ADVICE)

    result = h.process("invoice.txt", text)

    assert result.decision != Outcome.APPROVED and h.paid == []
    _, record = stored(h, result.arrival_id)
    assert record["invoice"]["total"] == "500.00"
    assert record["invoice"]["extracted_fields"] == ["total"]
    assert record["invoice"]["vendor"] == "Acme Corp"  # never requested, never touched
    assert len(h.chat.requests) == 3  # one correction, then the advisory
