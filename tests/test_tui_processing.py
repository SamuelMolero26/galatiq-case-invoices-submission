"""The reviewer TUI New run and Processing views, driven headless through Pilot (plan 4.5-4.6)."""

import argparse
import threading
from pathlib import Path

import pytest

pytest.importorskip("textual")

from conftest import SAMPLE_INVOICES  # noqa: E402
from test_tui import drive, plain  # noqa: E402
from textual.widgets import Input  # noqa: E402

from invoice_pipeline import service, tui, view  # noqa: E402

SAMPLE_NAMES = sorted(p.name for p in SAMPLE_INVOICES.iterdir() if p.is_file())


async def type_source(pilot, path) -> None:
    pilot.app.query_one("#source", Input).value = str(path)
    await pilot.pause()


def found(app) -> str:
    return plain(app.query_one(tui.FoundPane).body.content)


def test_discover_groups_the_sample_folder_by_type():
    discovery = service.discover(SAMPLE_INVOICES)

    assert discovery.problem is None
    assert [p.name for p in discovery.paths] == SAMPLE_NAMES
    assert discovery.paths == tuple(service.collect_files(SAMPLE_INVOICES))  # what a run takes
    assert discovery.types == {"txt": 7, "json": 6, "csv": 3, "pdf": 3, "xml": 1}


@pytest.mark.parametrize(
    "source, problem",
    [
        ("  ", "type a local folder path"),
        ("{tmp}/missing-folder", "not found: {tmp}/missing-folder"),
        ("{tmp}", "no files in {tmp}"),
    ],
)
def test_discover_explains_what_cannot_run(tmp_path, source, problem):
    discovery = service.discover(source.format(tmp=tmp_path))

    assert discovery.paths == () and discovery.types == {}
    assert discovery.problem == problem.format(tmp=tmp_path)


def test_new_run_lists_the_found_files_grouped_by_type(batch_ledger):
    async def script(pilot):
        await type_source(pilot, SAMPLE_INVOICES)
        return pilot.app.mode, found(pilot.app)

    mode, text = drive(tui.InvoiceApp(batch_ledger, start="new_run"), script)

    assert mode == "new_run"
    assert "20 files · 7 txt · 6 json · 3 csv · 3 pdf · 1 xml" in text
    assert [name for name in SAMPLE_NAMES if name in text] == SAMPLE_NAMES


def test_new_run_shows_why_a_source_cannot_run(batch_ledger, tmp_path):
    async def script(pilot):
        await type_source(pilot, tmp_path / "nowhere")
        return found(pilot.app)

    text = drive(tui.InvoiceApp(batch_ledger, start="new_run"), script)

    assert "not found:" in text and "nowhere" in text


def test_new_run_is_the_default_and_results_stay_reachable(batch_ledger):
    async def script(pilot):
        seen = [pilot.app.mode]
        await pilot.press("tab")
        seen.append((pilot.app.mode, pilot.app.active_view))
        await pilot.press("n")
        seen.append(pilot.app.mode)
        await pilot.press("shift+tab")
        seen.append((pilot.app.mode, pilot.app.active_view))
        return seen

    seen = drive(tui.InvoiceApp(batch_ledger, start="new_run"), script)

    assert seen == ["new_run", ("results", "all"), "new_run", ("results", "all")]


def test_results_footer_offers_a_new_run(batch_ledger):
    async def script(pilot):
        return pilot.app.mode, plain(pilot.app.query_one("#keys").content, width=150)

    mode, keys = drive(tui.InvoiceApp(batch_ledger), script)

    assert mode == "results" and "n new run" in keys


def test_the_tui_command_opens_on_new_run(batch_ledger, monkeypatch):
    opened = []
    monkeypatch.setattr(tui.InvoiceApp, "run", lambda app: opened.append(app))

    tui.run(batch_ledger, argparse.Namespace(llm="offline"))

    assert [app.start for app in opened] == ["new_run"]


# Processing: a run streams its pipeline events into the view, then lands on the Results.


def files_panel(app) -> list[str]:
    return [line.strip() for line in plain(app.query_one(tui.RunFiles).content).splitlines()]


def log_panel(app) -> list[str]:
    body = app.query_one(tui.AgentLog).body.content
    return [" ".join(line.split()) for line in plain(body).splitlines() if line.strip()]


def progress_panel(app) -> str:
    return plain(app.query_one("#progress").content, width=150)


def test_scripted_events_update_stages_progress_and_the_agent_log(batch_ledger):
    discovery = service.Discovery(
        (Path("a.csv"), Path("b.json"), Path("c.txt")), {"csv": 1, "json": 1, "txt": 1}
    )
    app = tui.InvoiceApp(batch_ledger, start="new_run")

    async def script(pilot):
        pilot.app._show_processing("inbox/", discovery)
        pilot.app._file_started("a.csv")
        for name, file, detail in [
            ("ingested", "a.csv", {"unreadable": False}),
            ("validated", "a.csv", {"findings": []}),
            ("assess", "a.csv", {"attempt": 1}),
            ("decided", "a.csv", {"outcome": "approved", "row": 7}),
            ("payment_sent", "a.csv", {"arrival_id": 1}),
        ]:
            pilot.app._pipeline_event(name, file, detail)
        pilot.app._file_done("a.csv", ("paid",), 0)
        pilot.app._file_started("b.json")
        pilot.app._pipeline_event("ingested", "b.json", {"unreadable": False})
        pilot.app._pipeline_event("validated", "b.json", {"findings": ["PRICE_MISMATCH"]})
        await pilot.pause()
        return (
            pilot.app.mode,
            progress_panel(pilot.app),
            files_panel(pilot.app),
            log_panel(pilot.app),
        )

    mode, progress, files, log = drive(app, script)

    assert mode == "processing"
    assert "processing inbox/" in progress and "1/3 done" in progress and "53%" in progress
    assert [line.split()[1:] for line in files if line] == [
        ["a.csv", "██████████", "approved"],
        ["b.json", "██████░░░░", "approval"],
        ["c.txt", "░░░░░░░░░░", "queued"],
    ]
    assert log == [
        "ingestion a.csv parsed",
        "validation a.csv checked against inventory",
        "approval a.csv assess · attempt 1",
        "approval a.csv decision → approved",
        "payment a.csv payment sent",
        "ingestion b.json parsed",
        "validation b.json checked against inventory · 1 finding",
    ]


def offline_runtime(tmp_path, pay_fn) -> service.Runtime:
    from invoice_pipeline import catalog, ledger
    from invoice_pipeline.critic import offline_agents

    inventory, ledger_path = tmp_path / "inventory.db", tmp_path / "ledger.db"
    catalog.seed(inventory)
    ledger.connect(ledger_path).close()
    return service.Runtime(
        catalog=catalog.load_catalog(inventory),
        ledger_path=ledger_path,
        agents=offline_agents(),
        pay_fn=pay_fn,
    )


async def until(pilot, condition, timeout=20.0):
    for _ in range(int(timeout / 0.05)):
        if condition():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not reached in time")


def test_a_run_streams_into_processing_then_lands_on_matching_results(tmp_path):
    reached, gate = threading.Event(), threading.Event()

    def bank(*args):
        reached.set()
        gate.wait(timeout=20)  # hold the run mid-batch, inside the worker thread
        return {"status": "success"}

    rt = offline_runtime(tmp_path, bank)
    app = tui.InvoiceApp(rt.ledger_path, runtime=rt, start="new_run")

    async def script(pilot):
        await type_source(pilot, SAMPLE_INVOICES)
        await pilot.press("enter")
        await until(pilot, reached.is_set)
        await pilot.pause()
        app = pilot.app
        running = [w.name for w in app.workers if w.group == "run" and w.is_running]
        during = app.mode, running, files_panel(app), log_panel(app)
        await pilot.press("tab")  # handled while the worker is still blocked mid-run
        peek = app.mode, app._available()
        await pilot.press("n")
        back = app.mode
        gate.set()
        await app.workers.wait_for_complete()
        await until(pilot, lambda: app.mode == "results")
        tabs = plain(app.query_one(tui.TabBar).content)
        return during, peek, back, tabs, files_panel(app), app.query_one(tui.FileList).option_count

    (mode, running, files, log), peek, back, tabs, files_after, rows = drive(app, script)

    assert mode == "processing" and len(running) == 1
    assert any("payment" in line for line in files) and any("queued" in line for line in files)
    assert "approval invoice_1001.txt decision → approved" in log
    assert peek == ("results", ()) and back == "processing"  # no actions while a run writes
    results = view.results(rt.ledger_path)
    assert results.count("all") == rows == 20
    assert f"all {results.count('all')}" in tabs
    assert f"approved {results.count('approved')}" in tabs
    assert f"needs review {len(service.review_queue(rt.ledger_path))}" in tabs
    assert f"rejected {results.count('rejected')}" in tabs
    assert sum("queued" in line for line in files_after) == 0


def test_enter_runs_nothing_when_the_source_has_no_files(tmp_path):
    rt = offline_runtime(tmp_path, lambda *args: {"status": "success"})

    async def script(pilot):
        await type_source(pilot, tmp_path / "nowhere")
        await pilot.press("enter")
        status = plain(pilot.app.query_one("#source-status").content)
        return pilot.app.mode, list(pilot.app.workers), status

    mode, workers, status = drive(
        tui.InvoiceApp(rt.ledger_path, runtime=rt, start="new_run"), script
    )

    assert (mode, workers) == ("new_run", [])
    assert "not found:" in status


# Live ingestion pane: what each ingested file parsed into, fed by scripted pipeline events.

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
        ["a.json", "b.json", "c.txt", "many.csv"],
        [
            ("ingested", "a.json", PARSED),
            ("extract", "a.json", {"fields": ["vendor"]}),
            (
                "ingested",
                "b.json",
                {"unreadable": True, "reason": "parse: bad json", "findings": []},
            ),
            ("ingested", "many.csv", PARSED),
            ("ingested", "many.csv", PARSED | {"invoice_number": "INV-8"}),
        ],
    )

    assert "a.json" in text and "Acme Corp · INV-7 · 125.50 USD · 2 items" in text
    assert "MISSING_PO" in text and "extracted: vendor" in text
    assert "b.json" in text and "unreadable: parse: bad json" in text
    assert "c.txt" not in text  # not ingested yet
    assert text.count("many.csv") == 2 and "INV-8" in text  # one block per invoice
