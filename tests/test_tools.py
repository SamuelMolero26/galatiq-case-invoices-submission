import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from invoice_pipeline import catalog, ledger
from invoice_pipeline.model import Outcome
from invoice_pipeline.tools import (
    MAX_TOOL_CALLS,
    TOOL_SCHEMAS,
    ToolError,
    ToolRunner,
    open_readonly,
)
from tests.factories import NOW, make_arrival, make_invoice, make_item

INVOICE = make_invoice(
    items=[make_item(), make_item("WidgetB", price="500", note="rush order")], vendor="Acme Co."
)


@pytest.fixture
def paths(tmp_path):
    catalog.seed(tmp_path / "inventory.db")
    with ledger.connect(tmp_path / "ledger.db") as conn:
        for minute in range(12):
            invoice = make_invoice(vendor="Acme Co.", number=f"INV-{minute}", currency="EUR")
            when = NOW + timedelta(minutes=minute)
            ledger.record(conn, make_arrival(Outcome.NEEDS_REVIEW, invoice, arrived_at=when))
    conn.close()
    return tmp_path / "inventory.db", tmp_path / "ledger.db"


@pytest.fixture
def runner(paths):
    inventory, ledger_path = (open_readonly(p) for p in paths)
    yield ToolRunner(inventory, ledger_path, INVOICE)
    inventory.close()
    ledger_path.close()


def run(runner, name, **arguments):
    return runner.run(1, name, json.dumps(arguments))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exactly_four_read_only_exact_lookup_tools():
    names = [schema["function"]["name"] for schema in TOOL_SCHEMAS]
    assert names == [
        "get_reference_price",
        "get_stock_level",
        "get_vendor_history",
        "get_invoice_line",
    ]
    assert MAX_TOOL_CALLS == 8


def test_exact_key_lookups_return_the_stored_values(runner):
    assert run(runner, "get_reference_price", sku="WidgetA") == {
        "sku": "WidgetA",
        "found": True,
        "unit_price": "250",
    }
    assert run(runner, "get_stock_level", sku="GadgetX") == {
        "sku": "GadgetX",
        "found": True,
        "stock_level": "5",
    }
    line = run(runner, "get_invoice_line", n=1)
    assert line["found"] and line["item"]["note"] == "rush order" and line["n"] == 1


@pytest.mark.parametrize(
    ("name", "arguments", "result"),
    [
        ("get_reference_price", {"sku": "Nope"}, {"sku": "Nope", "found": False}),
        ("get_stock_level", {"sku": "widgeta"}, {"sku": "widgeta", "found": False}),
        ("get_invoice_line", {"n": 5}, {"n": 5, "found": False}),
        ("get_invoice_line", {"n": -1}, {"n": -1, "found": False}),
    ],
)
def test_no_match_is_a_recorded_not_found_result_not_an_error(runner, name, arguments, result):
    got = run(runner, name, **arguments)
    assert {k: v for k, v in got.items() if k in result} == result
    assert got.get("unit_price") is None and got.get("stock_level") is None
    assert runner.calls[-1].error is None and runner.calls[-1].result == got


def test_vendor_history_is_ten_newest_first_plus_the_total(runner):
    result = run(runner, "get_vendor_history", vendor_key="acme co.")
    assert result["total"] == 12 and len(result["entries"]) == 10
    assert [e["number"] for e in result["entries"]][:2] == ["INV-11", "INV-10"]
    assert result["entries"][0]["currency"] == "EUR"
    assert run(runner, "get_vendor_history", vendor_key="nobody") == {
        "vendor_key": "nobody",
        "entries": [],
        "total": 0,
    }


def test_arguments_are_bound_parameters_never_sql(runner, paths):
    before = digest(paths[0])
    hostile = "WidgetA' OR '1'='1"
    assert run(runner, "get_reference_price", sku=hostile)["found"] is False
    assert run(runner, "get_stock_level", sku="x'; DROP TABLE inventory;--")["found"] is False
    assert run(runner, "get_vendor_history", vendor_key="acme co.' OR '1'='1")["total"] == 0
    assert digest(paths[0]) == before


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("search_vendors", '{"q": "acme"}', "unknown tool"),
        ("get_stock_level", "not json", "malformed arguments"),
        ("get_stock_level", "[1]", "malformed arguments"),
        ("get_stock_level", "{}", "malformed arguments"),
        ("get_stock_level", '{"sku": 5}', "malformed arguments"),
        ("get_stock_level", '{"sku": "A", "extra": 1}', "malformed arguments"),
        ("get_invoice_line", '{"n": "0"}', "malformed arguments"),
        ("get_invoice_line", '{"n": true}', "malformed arguments"),
    ],
)
def test_unknown_names_and_malformed_arguments_fail_safely_and_are_recorded(
    runner, name, arguments, message
):
    run(runner, "get_stock_level", sku="WidgetA")  # a prior good call stays recorded
    with pytest.raises(ToolError, match=message):
        runner.run(1, name, arguments)
    first, failed = runner.calls
    assert first.error is None and first.index == 0
    assert (failed.index, failed.name, failed.result) == (1, name, None)
    assert message in failed.error


def test_the_ninth_call_fails_and_the_eight_executed_calls_stay_recorded(runner):
    for _ in range(MAX_TOOL_CALLS):
        run(runner, "get_stock_level", sku="WidgetA")
    with pytest.raises(ToolError, match="budget"):
        run(runner, "get_stock_level", sku="WidgetA")
    assert len(runner.calls) == MAX_TOOL_CALLS + 1
    assert all(c.error is None for c in runner.calls[:MAX_TOOL_CALLS])
    assert "budget" in runner.calls[-1].error


def test_one_budget_is_shared_across_attempts(runner):
    for attempt in (1, 2):
        for _ in range(MAX_TOOL_CALLS // 2):
            runner.run(attempt, "get_stock_level", '{"sku": "WidgetA"}')
    with pytest.raises(ToolError, match="budget"):
        runner.run(2, "get_stock_level", '{"sku": "WidgetA"}')
    assert [c.attempt for c in runner.calls[:8]] == [1] * 4 + [2] * 4


def test_calls_are_numbered_and_timed(runner):
    run(runner, "get_stock_level", sku="WidgetA")
    run(runner, "get_invoice_line", n=0)
    assert [c.index for c in runner.calls] == [0, 1]
    assert runner.calls[0].arguments == {"sku": "WidgetA"}
    assert runner.calls[0].called_at.tzinfo is not None and runner.calls[0].elapsed_ms >= 0


def test_a_tool_that_raises_is_recorded_as_an_error(paths):
    inventory = open_readonly(paths[0])
    runner = ToolRunner(inventory, open_readonly(paths[1]), INVOICE)
    inventory.close()
    with pytest.raises(ToolError, match="ProgrammingError"):
        run(runner, "get_stock_level", sku="WidgetA")
    assert runner.calls[0].result is None and "ProgrammingError" in runner.calls[0].error


def test_connections_are_read_only_so_a_write_fails_and_changes_nothing(paths):
    before = [digest(p) for p in paths]
    for path, statement in zip(
        paths,
        ("UPDATE inventory SET stock_level = 0", "DELETE FROM arrivals"),
        strict=True,
    ):
        conn = open_readonly(path)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(statement)
        conn.close()
    assert [digest(p) for p in paths] == before


def test_there_is_no_search_or_write_tool():
    for schema in TOOL_SCHEMAS:
        properties = schema["function"]["parameters"]["properties"]
        assert set(properties) <= {"sku", "vendor_key", "n"}
