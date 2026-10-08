"""The reviewer TUI New run and Processing views, driven headless through Pilot (plan 4.5-4.6)."""

import argparse
import asyncio

import pytest

pytest.importorskip("textual")

from conftest import SAMPLE_INVOICES  # noqa: E402
from test_tui import SIZE, plain  # noqa: E402
from textual.widgets import Input  # noqa: E402

from invoice_pipeline import service, tui  # noqa: E402

SAMPLE_NAMES = sorted(p.name for p in SAMPLE_INVOICES.iterdir() if p.is_file())


def drive(app, script):
    async def main():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            return await script(pilot)

    return asyncio.run(main())


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
