import datetime as dt
from decimal import Decimal

from invoice_pipeline.critic import build_case_file, offline_agents
from invoice_pipeline.model import ArrivalSummary, CaseFile, HistoryEntry
from invoice_pipeline.validation import validate
from tests.factories import make_catalog, make_invoice, make_item

CATALOG = make_catalog()


def test_assembles_slice_one_case_file_without_io(no_network, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    invoice = make_invoice(
        [
            make_item("WidgetA", qty="4", price="300.00", note="rush order"),
            make_item("WidgetA", qty="2", price="250.00"),
            make_item("WidgetB", qty="1", price="500.00"),
        ],
        vendor="Northwind Traders",
    )
    findings = validate(invoice, CATALOG)
    history = [
        HistoryEntry(
            number="INV-9",
            total=Decimal("10.00"),
            currency="USD",
            state="paid",
            date=dt.date(2025, 1, 1),
        )
    ]
    arrival = ArrivalSummary(kind="new", amount_due=invoice.total)
    case_file = build_case_file(invoice, findings, arrival, None, CATALOG, history, 7)

    assert isinstance(case_file, CaseFile)
    assert case_file.invoice == invoice and case_file.findings == findings
    assert case_file.arrival == arrival
    refs = case_file.references
    assert refs.reference_prices == {"WidgetA": Decimal("250"), "WidgetB": Decimal("500")}
    assert refs.stock_levels == {"WidgetA": Decimal("15"), "WidgetB": Decimal("10")}
    assert refs.aggregated_quantities == {"WidgetA": Decimal("6"), "WidgetB": Decimal("1")}
    assert refs.price_tolerance == Decimal("0.15")
    assert refs.price_deviations == {0: Decimal("0.2")}
    assert refs.usd_equivalent is None
    assert refs.heightened_scrutiny_line == Decimal("10000")
    assert case_file.vendor_history == history and case_file.vendor_history_total == 7
    assert case_file.decision_context == [] and case_file.checklist == {}
    assert list(tmp_path.iterdir()) == []


def test_case_file_does_not_mutate_inputs():
    invoice = make_invoice()
    before = invoice.model_dump()
    build_case_file(invoice, [], ArrivalSummary(kind="new"), None, CATALOG, [], 0)
    assert invoice.model_dump() == before


def test_offline_agents_record_offline_tier_and_make_no_network_calls(no_network):
    agents = offline_agents()
    case_file = build_case_file(
        make_invoice(), [], ArrivalSummary(kind="new"), None, CATALOG, [], 0
    )
    assert agents.assess(case_file, [], 8, None) is None
    assert agents.verify(case_file, None, []) is None
    for role, call in [
        ("escalate_review", agents.escalate_review(case_file)),
        ("advisory", agents.advise(case_file, None)),
    ]:
        assert call.role == role and call.tier == "offline" and call.model is None
        assert call.error == "offline tier" and call.answer is None and call.tries == []
    agents.on_step("assessed", {})
