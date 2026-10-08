"""The Processing view's live ingestion pane, fed by scripted pipeline events."""

from pathlib import Path

import pytest

pytest.importorskip("textual")

from test_tui import plain  # noqa: E402
from test_tui_processing import drive  # noqa: E402

from invoice_pipeline import service, tui  # noqa: E402

PARSED = {
    "unreadable": False,
    "vendor": "Acme Corp",
    "invoice_number": "INV-7",
    "total": "125.50",
    "currency": "USD",
    "items": 2,
    "findings": ["MISSING_PO"],
}


def pane_text(app) -> str:
    return " ".join(plain(app.query_one(tui.IngestionPane).body.content, width=60).split())


def run_events(batch_ledger, names, events):
    discovery = service.Discovery(tuple(Path(n) for n in names), {})

    async def script(pilot):
        await pilot.pause()
        pilot.app._show_processing("inbox/", discovery)
        for name in names:
            pilot.app._file_started(name)
        for event in events:
            pilot.app._pipeline_event(*event)
        await pilot.pause()
        return pane_text(pilot.app)

    return drive(tui.InvoiceApp(batch_ledger, start="new_run"), script)


def test_the_pane_lists_what_each_ingested_file_parsed_into(batch_ledger):
    text = run_events(
        batch_ledger,
        ["a.json", "b.json", "c.txt"],
        [
            ("ingested", "a.json", PARSED),
            ("extract", "a.json", {"fields": ["vendor"]}),
            (
                "ingested",
                "b.json",
                {"unreadable": True, "reason": "parse: bad json", "findings": []},
            ),
        ],
    )

    assert "a.json" in text and "Acme Corp · INV-7 · 125.50 USD · 2 items" in text
    assert "MISSING_PO" in text and "extracted: vendor" in text
    assert "b.json" in text and "unreadable: parse: bad json" in text
    assert "c.txt" not in text  # not ingested yet


def test_a_csv_with_several_invoices_gets_a_block_for_each(batch_ledger):
    second = PARSED | {"invoice_number": "INV-8"}
    text = run_events(
        batch_ledger,
        ["many.csv"],
        [("ingested", "many.csv", PARSED), ("ingested", "many.csv", second)],
    )

    assert text.count("many.csv") == 2
    assert "INV-7" in text and "INV-8" in text
