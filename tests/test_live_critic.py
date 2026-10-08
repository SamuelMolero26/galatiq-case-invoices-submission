"""Live Grok coverage of every online role (REQ-LIVE-1..3). Skipped without a configured endpoint.

Each test runs a small invoice through the real service loop with the real provider and asserts
only what must hold whatever the model answers: schemas validate (or the error is recorded),
tries and tool calls stay inside their bounds, and no outcome exceeds the role's authority.
"""

import json
import os

import pytest

from invoice_pipeline import catalog, ledger, service
from invoice_pipeline.critic import online_agents
from invoice_pipeline.llm import FORMAT_TRIES, select_tier
from invoice_pipeline.tools import MAX_TOOL_CALLS

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (os.environ.get("XAI_API_KEY") and os.environ.get("GROK_BASE_URL")),
        reason="live Grok needs XAI_API_KEY and GROK_BASE_URL",
    ),
]

BASE = {
    "vendor": {"name": "Precision Parts Ltd."},
    "date": "2026-01-22",
    "currency": "USD",
}
CLEAN = {  # row 6
    **BASE,
    "invoice_number": "LIVE-100",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250.00}],
    "subtotal": 250.00,
    "total": 250.00,
}
GATE = {  # row 5 within bound: price 20% above the reference -> Assessor and Verifier
    **CLEAN,
    "invoice_number": "LIVE-101",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 300.00}],
    "subtotal": 300.00,
    "total": 300.00,
}
SHORTAGE = {  # row 3: 40 units against a stock of 15 -> advisory
    **CLEAN,
    "invoice_number": "LIVE-103",
    "line_items": [{"item": "WidgetA", "quantity": 40, "unit_price": 250.00}],
    "subtotal": 10000.00,
    "total": 10000.00,
}
MESSY_TXT = """Vendor: Precision Parts Ltd.
Invoice: LIVE-200
Date: 2026-01-05

WidgetA  qty: 4  unit price: $250.00

Please remit the amount of 1,000.00 within 30 days.
"""


@pytest.fixture
def live(tmp_path):
    """A real Runtime over tmp files with the real grok tier; the bank is recorded, not called."""
    inventory, ledger_path = tmp_path / "inventory.db", tmp_path / "ledger.db"
    catalog.seed(inventory)
    tier = select_tier("grok", os.environ)
    paid = []
    rt = service.Runtime(
        catalog=catalog.load_catalog(inventory),
        ledger_path=ledger_path,
        tier="grok",
        agents=online_agents(tier, service.tool_factory(inventory, ledger_path)),
        pay_fn=lambda *args: paid.append(args) or {"status": "success"},
    )

    def process(name, content):
        path = tmp_path / name
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        batch = service.process_path(path, rt)
        assert not batch.failed, [str(f) for f in batch.failed]
        conn = ledger.connect(ledger_path)
        try:
            row = conn.execute(
                "SELECT record FROM arrivals WHERE id = ?", (batch.results[0].arrival_id,)
            ).fetchone()
        finally:
            conn.close()
        return batch.results[0], json.loads(row["record"])

    return process, paid


def test_live_escalate_only_review_stays_in_bounds(live):
    process, _ = live

    result, record = process("clean.json", CLEAN)

    call = record["decision"]["escalate_review"]
    assert call["role"] == "escalate_review" and call["tier"] == "grok"
    assert len(call["tries"]) <= FORMAT_TRIES
    assert call["answer"] is not None or call["error"]
    assert result.decision in ("approved", "needs_review")


def test_live_advisory_never_changes_the_outcome(live):
    process, paid = live

    result, record = process("shortage.json", SHORTAGE)

    call = record["decision"]["advisory"]
    assert len(call["tries"]) <= FORMAT_TRIES
    assert call["answer"] is not None or call["error"]
    assert result.decision == "needs_review" and paid == []


def test_live_assessor_and_verifier_respect_bounds(live):
    process, _ = live

    result, record = process("gate.json", GATE)

    attempts = record["decision"]["critic"]["attempts"]
    assert 1 <= len(attempts) <= 2
    assert sum(len(a["assessor"]["tool_calls"]) for a in attempts) <= MAX_TOOL_CALLS
    for attempt in attempts:
        assert len(attempt["assessor"]["tries"]) <= FORMAT_TRIES
        assert (
            attempt["assessor"]["accepted"]
            or attempt["assessor"]["error"]
            or (attempt["assessor"]["failures"] or attempt["assessor"]["exhausted"])
        )
    assert result.decision in ("approved", "needs_review")


def test_live_extraction_is_never_paid_without_a_human(live):
    process, paid = live

    result, record = process("invoice.txt", MESSY_TXT)

    block = record["extraction"]
    assert block is not None and len(block["call"]["tries"]) <= FORMAT_TRIES
    assert set(block["supplied"]) <= set(block["requested"])
    assert result.decision != "approved" and paid == []
