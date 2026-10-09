"""Reviewer actions in the TUI: approve & pay, reject, retry (plan 4.4), driven through Pilot.

The TUI only shows what `ArrivalDetail.actions` allows and delegates to `service.resolve` and
`service.retry`; every refusal is shown as text, never as a crash.
"""

import dataclasses
import io
import json
import threading

import pytest

pytest.importorskip("textual")

from conftest import Harness, concur  # noqa: E402
from test_service_retry import CLEAN  # noqa: E402
from test_tui import drive, plain  # noqa: E402
from textual.widgets import Button, Input, Static  # noqa: E402

from invoice_pipeline import catalog, cli, service, tui, view  # noqa: E402
from invoice_pipeline.llm import LLMError  # noqa: E402


@pytest.fixture
def paid() -> list:
    return []


@pytest.fixture
def rt(batch_ledger, paid) -> service.Runtime:
    """An offline runtime over the batch Ledger whose bank records every payment."""
    return service.Runtime(
        catalog=catalog.load_catalog(batch_ledger.parent / "inventory.db"),
        ledger_path=batch_ledger,
        pay_fn=lambda *args: paid.append(args) or {"status": "success"},
    )


async def select(pilot, view_key: str, source: str) -> None:
    await pilot.press(view_key)
    pane = pilot.app.query_one(tui.DetailPane)
    for _ in range(25):
        if pane.detail is not None and pane.detail.source == source:
            return
        await pilot.press("j")
    raise AssertionError(f"{source} not found in view {view_key}")


def buttons(app) -> list[str]:
    return [b.id for b in app.query_one(tui.ActionBar).query(Button) if b.display]


def hints(app) -> str:
    return plain(app.query_one("#keys", Static).content, width=150)


def status(app) -> str:
    return plain(app.query_one(tui.DetailPane).status.content)


def tabs(app) -> str:
    return plain(app.query_one(tui.TabBar).content)


async def give_reason(pilot, reason: str) -> None:
    pilot.app.screen.query_one(Input).value = reason
    await pilot.press("enter")
    await pilot.pause()


def test_buttons_and_hints_show_only_the_available_actions(rt):
    async def script(pilot):
        seen = {}
        await select(pilot, "3", "invoice_1002.txt")  # approvable Needs Review (rule decided)
        seen["approvable"] = buttons(pilot.app), hints(pilot.app)
        await select(pilot, "3", "invoice_1004_revised.json")  # revision: delta payable
        seen["reject_only"] = buttons(pilot.app), hints(pilot.app)
        await select(pilot, "2", "invoice_1001.txt")  # paid
        seen["paid"] = buttons(pilot.app), hints(pilot.app)
        await pilot.press("a", "x", "r")  # unavailable: nothing opens, nothing runs
        seen["screen"] = type(pilot.app.screen)
        return seen

    seen = drive(tui.InvoiceApp(rt.ledger_path, rt), script)

    approvable_buttons, approvable_hints = seen["approvable"]
    assert approvable_buttons == ["approve", "reject"]
    assert "a approve & pay" in approvable_hints and "x reject" in approvable_hints
    assert "r retry" not in approvable_hints
    reject_buttons, reject_hints = seen["reject_only"]
    assert reject_buttons == ["approve", "reject"] and "a approve & pay" in reject_hints
    paid_buttons, paid_hints = seen["paid"]
    assert paid_buttons == [] and "reject" not in paid_hints
    assert seen["screen"] is not tui.ReasonScreen


def test_retry_is_offered_online_for_unreviewed_warnings(rt):
    online = dataclasses.replace(rt, tier="grok")  # display only: nothing is retried here

    async def script(pilot):
        await select(pilot, "3", "invoice_1014.xml")
        return buttons(pilot.app), hints(pilot.app)

    shown, keys = drive(tui.InvoiceApp(rt.ledger_path, online), script)

    assert shown == ["approve", "reject", "retry"] and "r retry" in keys


def test_without_a_runtime_the_results_are_read_only(rt):
    async def script(pilot):
        await select(pilot, "3", "invoice_1002.txt")
        return buttons(pilot.app), hints(pilot.app)

    shown, keys = drive(tui.InvoiceApp(rt.ledger_path), script)

    assert shown == [] and "approve" not in keys


def test_approve_requires_a_reason_and_delegates_to_resolve_unchanged(rt, paid, monkeypatch):
    calls, resolve = [], service.resolve

    def spy(*args):
        calls.append(args)
        return resolve(*args)

    monkeypatch.setattr(service, "resolve", spy)
    arrival_id = {r.source: r.arrival_id for r in view.results(rt.ledger_path).rows}[
        "invoice_1002.txt"
    ]

    async def script(pilot):
        seen = {}
        await select(pilot, "3", "invoice_1002.txt")
        await pilot.press("x")
        await pilot.press("escape")  # cancelled: nothing is resolved
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        seen["modal"] = isinstance(pilot.app.screen, tui.ReasonScreen)
        await give_reason(pilot, "   ")
        seen["empty"] = (
            isinstance(pilot.app.screen, tui.ReasonScreen),
            plain(pilot.app.screen.query_one("#reason-error", Static).content),
        )
        await give_reason(pilot, "stock confirmed with the warehouse")
        seen["after"] = type(pilot.app.screen), status(pilot.app), tabs(pilot.app)
        return seen

    seen = drive(tui.InvoiceApp(rt.ledger_path, rt), script)

    assert seen["modal"]
    still_open, error = seen["empty"]
    assert still_open and "a reason is required" in error
    screen, shown, tab_text = seen["after"]
    assert screen is not tui.ReasonScreen
    assert calls == [(rt, arrival_id, "approve", "stock confirmed with the warehouse")]
    assert len(paid) == 1 and "paid" in shown
    assert "approved 7" in tab_text  # 6 paid by the batch, plus this one: refreshed
    assert view.arrival_detail(rt.ledger_path, arrival_id).state == "paid"


def test_refusal_text_is_shown_and_nothing_changes(rt, paid):
    async def script(pilot):
        await select(pilot, "3", "invoice_1013.json")
        await pilot.press("a")
        await give_reason(pilot, "stock confirmed")
        await select(pilot, "3", "invoice_1013.pdf")  # the same invoice, already paid above
        await pilot.press("a")
        await give_reason(pilot, "pay the pdf copy too")
        return status(pilot.app), pilot.app.query_one(tui.DetailPane).detail.state

    shown, state = drive(tui.InvoiceApp(rt.ledger_path, rt), script)

    assert "payment cap" in shown and state == "needs_review"
    assert len(paid) == 1


def test_reject_is_reflected_by_review_list(rt, paid):
    async def script(pilot):
        await select(pilot, "3", "invoice_1004_revised.json")
        arrival_id = pilot.app.query_one(tui.DetailPane).detail.arrival_id
        await pilot.press("x")
        await give_reason(pilot, "a revision is handled outside v1")
        return arrival_id, tabs(pilot.app), status(pilot.app)

    arrival_id, tab_text, shown = drive(tui.InvoiceApp(rt.ledger_path, rt), script)

    out = io.StringIO()
    assert cli.main(["review", "--list", "--ledger", str(rt.ledger_path)], out=out) == 0
    queued = [json.loads(line)["arrival_id"] for line in out.getvalue().splitlines()]
    assert arrival_id not in queued and "rejected" in shown
    assert "rejected 5" in tab_text and paid == []


def test_retry_runs_in_a_worker_with_progress_then_the_result(tmp_path, grok):
    h = Harness(tmp_path, grok, LLMError("timeout after 1s"), concur())
    first = h.process("a.json", CLEAN)
    assert first.state == "needs_review"
    model_called = threading.Event()
    release = threading.Event()

    def hold_the_model(event):
        if event.name == "escalate":  # the worker waits here, as on a slow model call
            model_called.set()
            release.wait(5)

    rt = dataclasses.replace(h.rt, on_event=hold_the_model)

    async def script(pilot):
        await select(pilot, "3", "a.json")
        await pilot.press("r")
        assert model_called.wait(5)
        await pilot.pause()
        during = status(pilot.app), buttons(pilot.app)
        release.set()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        return during, status(pilot.app), tabs(pilot.app)

    (progress, while_running), result, tab_text = drive(tui.InvoiceApp(h.ledger_path, rt), script)

    assert "retrying" in progress and while_running == []
    assert "approved" in result and "paid" in result
    assert len(h.paid) == 1 and "approved 1" in tab_text
    assert view.arrival_detail(h.ledger_path, first.arrival_id).state == "paid"
