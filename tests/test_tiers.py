"""Tier selection and role wiring through the real service loop (REQ-TIER-1..6, EXT-6)."""

import dataclasses
import io
import json
from types import SimpleNamespace

import pytest
from conftest import Harness, concur, text_reply, tool_reply

from invoice_pipeline import catalog, cli, ledger, service
from invoice_pipeline.llm import LLMError, TierConfig
from invoice_pipeline.model import Outcome

CLEAN = {  # row 6: trusted vendor, catalog price, no findings
    "invoice_number": "INV-9100",
    "vendor": {"name": "Precision Parts Ltd."},
    "date": "2026-01-22",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250.00}],
    "subtotal": 250.00,
    "total": 250.00,
    "currency": "USD",
}
GATE = {  # row 5 within bound: price 20% above the reference
    **CLEAN,
    "invoice_number": "INV-9101",
    "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 300.00}],
    "subtotal": 300.00,
    "total": 300.00,
}
UNKNOWN_ITEM = {  # row 2
    **CLEAN,
    "invoice_number": "INV-9102",
    "line_items": [{"item": "UnlistedPart", "quantity": 1, "unit_price": 250.00}],
}
SHORTAGE = {  # row 3: 40 units against a stock of 15
    **CLEAN,
    "invoice_number": "INV-9103",
    "line_items": [{"item": "WidgetA", "quantity": 40, "unit_price": 250.00}],
    "subtotal": 10000.00,
    "total": 10000.00,
}
HEIGHTENED = {  # row 4: WidgetB 10 x 1,100 is above the Critic approval limit
    **CLEAN,
    "invoice_number": "INV-9104",
    "line_items": [{"item": "WidgetB", "quantity": 10, "unit_price": 1100.00}],
    "subtotal": 11000.00,
    "total": 11000.00,
}
MESSY_TXT = """Vendor: Precision Parts Ltd.
Invoice: INV-9200
Date: 2026-01-05

WidgetA  qty: 4  unit price: $250.00

Please remit the amount of 1,000.00 within 30 days.
"""

ADVICE = text_reply(json.dumps({"rationale": "explained for the reviewer"}))
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


# --- event order per row (TIER-2/3) ----------------------------------------------------

ROWS = {
    "row6": (CLEAN, [concur()], ["escalate", "decided", "payment_sent"]),
    "row2": (UNKNOWN_ITEM, [ADVICE], ["advise", "decided"]),
    "row3": (SHORTAGE, [ADVICE], ["advise", "decided"]),
    "row4": (HEIGHTENED, [ADVICE], ["advise", "decided"]),
    "full-gate": (GATE, GATE_REPLIES, ["assess", "verify", "decided", "payment_sent"]),
}


@pytest.mark.parametrize("row", ROWS)
def test_event_order_per_row(row, tmp_path, grok):
    invoice, replies, middle = ROWS[row]
    h = Harness(tmp_path, grok, *replies)

    h.process("invoice.json", invoice)

    assert h.names == ["ingested", "validated", *middle]


def test_extraction_events_precede_decision_events(tmp_path, grok):
    h = Harness(tmp_path, grok, text_reply(json.dumps({"total": "1000.00"})), ADVICE)

    result = h.process("invoice.txt", MESSY_TXT)

    assert h.names == ["ingested", "extract", "validated", "advise", "decided"]
    extract = h.events[1]
    assert extract.file == "invoice.txt" and extract.detail == {"fields": ["total"]}
    assert result.decision == Outcome.NEEDS_REVIEW and h.paid == []


def test_role_events_carry_the_file_and_attempt(tmp_path, grok):
    h = Harness(tmp_path, grok, *GATE_REPLIES)

    h.process("invoice.json", GATE)

    roles = {e.name: e for e in h.events if e.name in ("assess", "verify")}
    assert roles["assess"].file == roles["verify"].file == "invoice.json"
    assert roles["assess"].detail == {"attempt": 1}


def test_role_events_reach_the_cli_output(tmp_path, grok):
    h = Harness(tmp_path, grok, concur())
    out = io.StringIO()
    h.rt = dataclasses.replace(h.rt, on_event=cli.JsonLines(out).event)

    h.process("invoice.json", CLEAN)

    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert {"event": "escalate", "file": "invoice.json", "attempt": 1} in lines


def test_late_bound_on_event_is_used_per_file(tmp_path, grok):
    """The CLI swaps `on_event` after bootstrap, so role events must follow the runtime given."""
    h = Harness(tmp_path, grok, concur(), ADVICE)
    later = []
    rt = dataclasses.replace(h.rt, on_event=later.append)

    service.process_path(h.write("a.json", CLEAN), rt)
    service.process_path(h.write("b.json", UNKNOWN_ITEM), rt)

    assert [e.name for e in later if e.name in ("escalate", "advise")] == ["escalate", "advise"]
    assert [e.file for e in later if e.name in ("escalate", "advise")] == ["a.json", "b.json"]
    assert h.events == []


# --- extraction runs at most once, only where allowed (EXT-6/7, TIER-4/5) --------------


def test_extract_called_once_even_under_redecide(tmp_path, grok, monkeypatch):
    h = Harness(tmp_path, grok, text_reply(json.dumps({"total": "1000.00"})), ADVICE, ADVICE)
    real, rounds = ledger.record_if_unchanged, []

    def stale_once(*args):
        rounds.append(1)
        return None if len(rounds) == 1 else real(*args)

    monkeypatch.setattr(ledger, "record_if_unchanged", stale_once)

    h.process("invoice.txt", MESSY_TXT)

    assert "redecided" in h.names and h.names.count("extract") == 1
    assert len(h.chat.requests) == 3  # one extraction answer, then one advisory per decision


@pytest.mark.parametrize("name,content", [("invoice.json", CLEAN), ("invoice.csv", "a,b\n1,2\n")])
def test_structured_formats_never_call_extract(name, content, tmp_path, grok):
    h = Harness(tmp_path, grok, concur(), ADVICE)
    calls = []
    agents = dataclasses.replace(h.rt.agents, extract=lambda *a: calls.append(a))
    h.rt = dataclasses.replace(h.rt, agents=agents)

    service.process_path(h.write(name, content), h.rt)

    assert calls == [] and "extract" not in h.names


def test_offline_tier_never_calls_extract_even_if_present(tmp_path, grok):
    h = Harness(tmp_path, grok, tier="offline")
    calls = []
    h.rt = dataclasses.replace(h.rt, agents=dataclasses.replace(h.rt.agents, extract=calls.append))

    service.process_path(h.write("invoice.txt", MESSY_TXT), h.rt)

    assert calls == [] and "extract" not in h.names


def test_offline_zero_network_attempts(tmp_path, no_network):
    rt = service.Runtime(
        catalog=_seeded(tmp_path), ledger_path=tmp_path / "ledger.db", on_event=lambda e: None
    )
    path = tmp_path / "invoice.txt"
    path.write_text(MESSY_TXT)

    batch = service.process_path(path, rt)

    assert not batch.failed and no_network == []


def _seeded(tmp_path):
    inventory = tmp_path / "inventory.db"
    catalog.seed(inventory)
    return catalog.load_catalog(inventory)


# --- every role fails closed (TIER-3) --------------------------------------------------

FAIL = LLMError("timeout after 1s")
CASES = {
    "escalate": (CLEAN, [FAIL], "INV-9100", lambda r: r.reasons[0].startswith("ESCALATE_ONLY")),
    "advise": (UNKNOWN_ITEM, [FAIL], "INV-9102", lambda r: r.decision == Outcome.REJECTED),
    "assess": (GATE, [FAIL], "INV-9101", lambda r: "UNREVIEWED_WARNINGS" in r.reasons[0]),
    "extract": (MESSY_TXT, [FAIL, FAIL], None, lambda r: True),
}


@pytest.mark.parametrize("role", CASES)
def test_role_failure_fails_closed_per_role(role, tmp_path, grok):
    invoice, replies, _, check = CASES[role]
    h = Harness(tmp_path, grok, *replies)

    result = h.process("invoice.txt" if role == "extract" else "invoice.json", invoice)

    assert result.decision != Outcome.APPROVED and h.paid == []
    assert check(result)


# --- tier selection (TIER-1) -----------------------------------------------------------


def _args(tmp_path, llm):
    return SimpleNamespace(
        llm=llm, inventory=str(tmp_path / "inventory.db"), ledger=str(tmp_path / "ledger.db")
    )


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("XAI_API_KEY", "GROK_BASE_URL", "XAI_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_explicit_grok_without_key_fails_before_any_read(tmp_path, clean_env, no_fs_access):
    with no_fs_access() as attempts:
        with pytest.raises(service.BootstrapError, match="XAI_API_KEY"):
            service.bootstrap(_args(tmp_path, "grok"))

    assert attempts == []
    assert not (tmp_path / "inventory.db").exists() and not (tmp_path / "ledger.db").exists()


def test_auto_fallback_announced_once(tmp_path, clean_env):
    for n in (1, 2):
        (tmp_path / f"invoice{n}.json").write_text(json.dumps({**CLEAN, "invoice_number": f"X{n}"}))
    out = io.StringIO()

    code = cli.main(
        [
            "--invoice_path",
            str(tmp_path),
            "--ledger",
            str(tmp_path / "ledger.db"),
            "--inventory",
            str(tmp_path / "inventory.db"),
            "--json",
        ],
        out,
    )

    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    startups = [line for line in lines if line["event"] == "startup"]
    assert code == 0 and startups == [{"event": "startup", "tier": "offline"}]


# --- credentials never leave the tier config (TIER-5) ----------------------------------

SENTINEL = "sk-test-SENTINEL-9f3a"


def test_event_details_have_no_key(tmp_path):
    keyed = TierConfig(
        tier="grok", model="m", base_url="http://llm.invalid", timeout_s=1, api_key=SENTINEL
    )
    h = Harness(tmp_path, keyed, *GATE_REPLIES)

    h.process("invoice.json", GATE)

    serialized = json.dumps([[e.name, e.file, e.detail] for e in h.events], default=str)
    assert SENTINEL not in serialized and h.events
