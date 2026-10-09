"""The reviewer TUI Results views, driven headless through Textual's Pilot (plan 4.1-4.3)."""

import ast
import asyncio
import dataclasses
import io
import json
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("textual")

from conftest import Harness, text_reply  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.text import Text  # noqa: E402

from invoice_pipeline import ledger, service, tui, view  # noqa: E402

SIZE = (150, 40)


def drive(app, script):
    """Run `script(pilot)` against the app headless and return what it returned."""

    async def main():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            return await script(pilot)

    return asyncio.run(main())


def plain(renderable, width=140) -> str:
    console = Console(file=io.StringIO(), width=width, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def render_detail(detail) -> Group:
    chips = tui._chips(detail)
    return Group(*tui._summary(detail), *([Text(), chips] if chips is not None else []))


def _states(ledger_path) -> Counter:
    conn = ledger.connect(ledger_path, read_only=True)
    try:
        return Counter(r["state"] for r in conn.execute("SELECT state FROM arrivals"))
    finally:
        conn.close()


def _view(app):
    files, detail = app.query_one(tui.FileList), app.query_one(tui.DetailPane)
    shown = detail.detail.source if detail.detail else None
    return app.active_view, files.option_count, files.highlighted, shown


def test_tabs_show_counts_matching_ledger_states(batch_ledger):
    states = _states(batch_ledger)
    queued = len(service.review_queue(batch_ledger))

    async def script(pilot):
        return plain(pilot.app.query_one(tui.TabBar).content), _view(pilot.app)

    tabs, (view, rows, highlighted, shown) = drive(tui.InvoiceApp(batch_ledger), script)

    assert f"all {sum(states.values())}" in tabs
    assert f"approved {states['paid']}" in tabs
    assert f"needs review {queued}" in tabs
    assert f"rejected {states['logged_rejection']}" in tabs
    assert (view, rows, highlighted, shown) == ("all", 20, 0, "invoice_1001.txt")


def test_header_shows_files_and_funnel(batch_ledger):
    async def script(pilot):
        return plain(pilot.app.query_one(tui.RunHeader).content, width=150)

    header = drive(tui.InvoiceApp(batch_ledger), script)

    assert "invoice-flow · batch run · 20 files" in header
    assert "ingest 20 → validate 20 → approve 6 → paid 6" in header


def test_tab_keys_cycle_and_number_keys_jump(batch_ledger):
    async def script(pilot):
        seen = []
        for key in ("tab", "tab", "tab", "tab", "shift+tab", "3", "1", "4", "2"):
            await pilot.press(key)
            seen.append(_view(pilot.app)[:2])
        return seen

    seen = drive(tui.InvoiceApp(batch_ledger), script)

    assert seen == [
        ("approved", 6),
        ("needs_review", 8),
        ("rejected", 4),
        ("all", 20),
        ("rejected", 4),
        ("needs_review", 8),
        ("all", 20),
        ("rejected", 4),
        ("approved", 6),
    ]


def test_arrow_and_vim_keys_move_the_selection_and_the_detail(batch_ledger):
    async def script(pilot):
        seen = []
        for key in ("j", "j", "down", "k", "up"):
            await pilot.press(key)
            seen.append(_view(pilot.app)[2:])
        return seen

    seen = drive(tui.InvoiceApp(batch_ledger), script)

    assert seen == [
        (1, "invoice_1002.txt"),
        (2, "invoice_1003.txt"),
        (3, "invoice_1004.json"),
        (2, "invoice_1003.txt"),
        (1, "invoice_1002.txt"),
    ]


def test_needs_review_detail_shows_the_eur_usd_evidence(batch_ledger):
    async def script(pilot):
        await pilot.press("3")
        while pilot.app.query_one(tui.DetailPane).detail.source != "invoice_1014.xml":
            await pilot.press("j")
        return plain(pilot.app.query_one(tui.DetailPane).body.content)

    detail = drive(tui.InvoiceApp(batch_ledger), script)

    assert "? NEEDS REVIEW" in detail
    assert "4,125.00 EUR → 4,677.75 USD at 1.08 USD/EUR, as of 2026-01-02, +5% buffer" in detail
    assert "CURRENCY_NON_USD" in detail


def test_rejected_detail_shows_stages_and_finding_chips(batch_ledger):
    detail = view.arrival_detail(batch_ledger, 10)  # invoice_1009.json

    text = plain(render_detail(detail))

    assert detail.source == "invoice_1009.json" and "✗ REJECTED" in text
    assert "scrutiny standard" in text
    for name in ("ingestion", "validation", "approval", "payment"):
        assert name in text
    assert "rejection logged" in text
    for code in detail.finding_codes:
        assert code in text


def test_model_notes_are_labeled_and_rule_notes_are_not(tmp_path, grok):
    from test_tui_detail import EUR_SHORTAGE

    advice = text_reply(json.dumps({"rationale": "explained for the reviewer"}))
    h = Harness(tmp_path, grok, advice)
    result = h.process("eur.json", EUR_SHORTAGE)

    lines = plain(render_detail(view.arrival_detail(h.ledger_path, result.arrival_id)))
    lines = lines.splitlines()

    advisory = next(line for line in lines if "explained for the reviewer" in line)
    rules = next(line for line in lines if line.lstrip().startswith("rule engine"))
    assert "model" in advisory.split("explained")[0]
    assert "model" not in rules


def test_empty_view_shows_no_detail(tmp_path, batch_ledger):
    conn = ledger.connect(batch_ledger)
    with ledger.write_txn(conn):
        conn.execute("DELETE FROM arrivals WHERE state = 'logged_rejection'")
    conn.close()

    async def script(pilot):
        await pilot.press("4")
        return _view(pilot.app), plain(pilot.app.query_one(tui.DetailPane).body.content)

    (view, rows, _, shown), text = drive(tui.InvoiceApp(batch_ledger), script)

    assert (view, rows, shown) == ("rejected", 0, None)
    assert "no arrivals in this view" in text


def test_widgets_never_reach_past_the_service_read_models():
    tree = ast.parse(Path(tui.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("invoice_pipeline") for a in node.names)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("invoice_pipeline"):
            if node.module == "invoice_pipeline":
                assert [a.name for a in node.names] == ["service", "view"]
            else:  # only plain read-model dataclasses come from the service and view modules
                module = {"invoice_pipeline.service": service, "invoice_pipeline.view": view}
                assert node.module in module
                for alias in node.names:
                    owner = module[node.module]
                    assert dataclasses.is_dataclass(getattr(owner, alias.name)), alias.name
    app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "InvoiceApp")
    inside = {id(n) for n in ast.walk(app)}
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in ("service", "view")]
    assert uses and all(id(n) in inside for n in uses)
