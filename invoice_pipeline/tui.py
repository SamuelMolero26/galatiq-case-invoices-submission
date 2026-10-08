"""Reviewer TUI: New run (a local folder and what it holds), the batch Results views
(all / approved / needs review / rejected) and the reviewer actions (approve & pay, reject, retry).

Thin presentation over the service read models: widgets receive plain dataclasses and never
touch the Ledger or any model role; only `InvoiceApp` calls `service.*`, and which actions an
arrival allows comes from `ArrivalDetail.actions`. Needs the optional `tui` extra (Textual);
`python main.py tui` imports this module lazily.
"""

import argparse
from pathlib import Path

from rich.box import SQUARE
from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Input, OptionList, Static
from textual.widgets.option_list import Option

from invoice_pipeline import service
from invoice_pipeline.service import (
    ArrivalDetail,
    ArrivalResult,
    Discovery,
    Note,
    ResultRow,
    Results,
    Stage,
    UsdEvidence,
)

# Palette from the owner's Paper mockups
BG, TEXT = "#141411", "#D6D2C6"
MUTED, DIM, SOFT, RULE = "#69655B", "#898270", "#A59E8F", "#3A382F"
GREEN, AMBER, AMBER_DIM, RED = "#89B775", "#C6AC6E", "#9A8656", "#C38A76"

TABS = (  # (view, label, colour), in `service.VIEWS` order
    ("all", "all", TEXT),
    ("approved", "approved", GREEN),
    ("needs_review", "needs review", AMBER),
    ("rejected", "rejected", RED),
)
VIEW_COLOUR = {view: colour for view, _, colour in TABS} | {None: SOFT}
STATE_MARK = {  # Ledger state -> (glyph, colour, badge)
    "paid": ("✓", GREEN, "APPROVED"),
    "needs_review": ("?", AMBER, "NEEDS REVIEW"),
    "payment_pending": ("!", AMBER, "PAYMENT PENDING"),
    "logged_rejection": ("✗", RED, "REJECTED"),
    "duplicate": ("=", SOFT, "DUPLICATE"),
    "superseded": ("~", SOFT, "SUPERSEDED"),
}
STAGE_MARK = {  # Stage status -> (glyph, glyph colour, summary colour)
    "ok": ("✓", GREEN, TEXT),
    "warn": ("!", AMBER, AMBER),
    "fail": ("✗", RED, RED),
    "held": ("○", AMBER, AMBER),
    "logged": ("■", RED, TEXT),
    "skipped": ("○", MUTED, MUTED),
}
KEY_HINTS = (
    ("tab", "next view"),
    ("shift+tab", "prev"),
    ("↑↓ / j k", "select"),
    ("1-4", "jump view"),
    ("n", "new run"),
    ("q", "quit"),
)
NEW_RUN_HINTS = (("tab", "results"),)
DEFAULT_SOURCE = "data/invoices/"
FUNNEL = ("ingest", "validate", "approve", "paid")
ACTIONS = (  # (action, key, label, colour), in `ArrivalDetail.actions` order
    ("approve", "a", "approve & pay", GREEN),
    ("reject", "x", "reject", RED),
    ("retry", "r", "retry", AMBER),
)
ACTION_KEY = {action: (key, label) for action, key, label, _ in ACTIONS}
RESOLVED = {"approve": "approved", "reject": "rejected"}
VIEW_STATUS = {"approved": GREEN, "rejected": RED, "needs_review": AMBER}


def render_detail(detail: ArrivalDetail) -> RenderableType:
    """The detail pane for one arrival: badge, scrutiny, pipeline, amount, notes, findings."""
    parts = _summary(detail)
    if (chips := _chips(detail)) is not None:
        parts += [Text(), chips]
    return Group(*parts)


def _summary(detail: ArrivalDetail) -> list[RenderableType]:
    """Everything above the action buttons: badge, scrutiny, pipeline, amount, agent notes."""
    glyph, colour, badge = STATE_MARK[detail.state]
    head = Table.grid(expand=True)
    head.add_column()
    head.add_column(justify="right")
    head.add_row(Text(detail.source, SOFT), Text(f"{glyph} {badge}", f"bold {colour}"))
    scrutiny = AMBER if detail.scrutiny == "heightened" else TEXT
    parts: list[RenderableType] = [
        head,
        Rule(style=RULE),
        Text.assemble(("scrutiny ", MUTED), (detail.scrutiny, f"bold {scrutiny}")),
        Text(),
        Text("pipeline", MUTED),
        _stages(detail.stages),
    ]
    if detail.usd is not None:
        parts += [Text(), Text("amount", MUTED), _usd(detail.usd)]
    parts += [Text(), Text("agent notes", MUTED), _notes(detail.notes)]
    return parts


def _chips(detail: ArrivalDetail) -> RenderableType | None:
    """One chip per Finding code, in the colour of the arrival's view."""
    if not detail.finding_codes:
        return None
    chip = VIEW_COLOUR[detail.view]
    return Columns(
        [
            Panel(Text(code, chip), box=SQUARE, border_style=chip, expand=False)
            for code in detail.finding_codes
        ]
    )


def _stages(stages: list[Stage]) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(width=1)
    table.add_column(width=11)
    table.add_column(ratio=1)
    for stage in stages:
        glyph, glyph_colour, colour = STAGE_MARK[stage.status]
        table.add_row(
            Text(glyph, glyph_colour), Text(stage.name, f"bold {TEXT}"), Text(stage.summary, colour)
        )
    return table


def _usd(usd: UsdEvidence) -> Text:
    return Text(
        f"{usd.total:,.2f} {usd.currency} → {usd.amount:,.2f} USD at {usd.rate} USD/{usd.currency},"
        f" as of {usd.as_of.isoformat()}, +{usd.buffer_pct}% buffer",
        TEXT,
    )


def _notes(notes: list[Note]) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(width=15, no_wrap=True)
    table.add_column(width=5)
    table.add_column(ratio=1)
    for note in notes:  # a model's words carry a "model" tag; rules and people do not
        tag = Text("model", f"italic {AMBER_DIM}") if note.model else Text()
        table.add_row(Text(note.role, MUTED), tag, Text(note.text, TEXT))
    return table


def _file_prompt(row: ResultRow, marked: bool) -> Table:
    glyph, colour, _ = STATE_MARK[row.state]
    path = Path(row.source)
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=1)
    grid.add_column(width=1)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)
    name = Text(path.stem, f"bold {TEXT}" if marked else TEXT)
    grid.add_row(
        Text("▸" if marked else " ", TEXT), Text(glyph, colour), name, Text(path.suffix, MUTED)
    )
    return grid


class RunHeader(Static):
    """`invoice-flow · batch run · N files` and the ingest -> paid funnel."""

    def show(self, results: Results) -> None:
        self.funnel(results.files, results.funnel)

    def funnel(self, files: int, counts: dict[str, int], colour: str = GREEN) -> None:
        """The header for `files` files; a funnel stage without a count shows its name only."""
        left = Text.assemble(
            ("invoice-flow", f"bold {TEXT}"), (f" · batch run · {files} files", MUTED)
        )
        right = Text()
        for i, stage in enumerate(FUNNEL):
            if i:
                right.append(" → ", MUTED)
            right.append(stage, f"bold {TEXT}")
            if stage in counts:
                right.append(f" {counts[stage]}", colour)
        grid = Table.grid(expand=True)
        grid.add_column()
        grid.add_column(justify="right")
        grid.add_row(left, right)
        self.update(grid)


class TabBar(Static):
    """One tab per view with its count; the active tab is filled with its colour."""

    def show(self, results: Results, active: str) -> None:
        text = Text()
        for view, label, colour in TABS:
            count = results.count(view)
            if view == active:
                text.append(f" {label} {count} ", f"bold {BG} on {colour}")
            else:
                text.append(f" {label} ", colour)
                text.append(f"{count} ", DIM)
            text.append("  ")
        self.update(text)


class FileList(OptionList):
    """The arrivals of the active view; ▸ marks the selection."""

    BINDINGS = [Binding("j", "cursor_down", show=False), Binding("k", "cursor_up", show=False)]

    def show(self, rows: list[ResultRow], index: int = 0) -> None:
        self.rows, self.marked = rows, None
        self.clear_options()
        self.add_options(Option(_file_prompt(row, False), id=str(row.arrival_id)) for row in rows)
        if rows:
            self.highlighted = min(max(index, 0), len(rows) - 1)

    def mark(self, index: int) -> None:
        if self.marked is not None and self.marked < len(self.rows):
            self.replace_option_prompt_at_index(
                self.marked, _file_prompt(self.rows[self.marked], False)
            )
        self.replace_option_prompt_at_index(index, _file_prompt(self.rows[index], True))
        self.marked = index


class ActionBar(Horizontal):
    """The reviewer action buttons; only the actions the selected arrival allows are shown."""

    def compose(self) -> ComposeResult:
        for action, key, label, _ in ACTIONS:
            button = Button(Text(f"[{key}] {label}"), id=action)
            button.can_focus = False  # the file list keeps the keyboard
            yield button

    def show(self, actions: tuple[str, ...]) -> None:
        for button in self.query(Button):
            button.display = button.id in actions
        self.display = bool(actions)


class DetailPane(VerticalScroll):
    """The selected arrival's detail, its action buttons and the last action's outcome."""

    detail: ArrivalDetail | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="detail-body")
        yield ActionBar(id="actions")
        yield Static(id="detail-status")
        yield Static(id="detail-chips")

    @property
    def body(self) -> Static:
        return self.query_one("#detail-body", Static)

    @property
    def status(self) -> Static:
        return self.query_one("#detail-status", Static)

    def show(self, detail: ArrivalDetail | None) -> None:
        self.detail = detail
        self.body.update(
            Group(*_summary(detail)) if detail else Text("no arrivals in this view", MUTED)
        )
        chips = _chips(detail) if detail else None
        self.query_one("#detail-chips", Static).update(chips or "")
        self.scroll_home(animate=False)

    def say(self, message: str | None, colour: str = TEXT) -> None:
        self.status.update(Text(message or "", colour))
        self.status.display = bool(message)


class ReasonScreen(ModalScreen[str | None]):
    """Asks for the mandatory reason of a Resolution: Enter confirms, Escape cancels."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, action: str, source: str):
        super().__init__()
        self.action_name, self.source = action, source

    def compose(self) -> ComposeResult:
        key, label = ACTION_KEY[self.action_name]
        colour = next(c for a, _, _, c in ACTIONS if a == self.action_name)
        with Vertical(id="reason-box"):
            yield Static(Text.assemble((label, f"bold {colour}"), (f"  {self.source}", SOFT)))
            yield Input(placeholder="reason (required)", id="reason-input")
            yield Static(id="reason-error")
            yield Static(_hints((("enter", "confirm"), ("esc", "cancel"))))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        reason = event.value.strip()
        if not reason:
            self.query_one("#reason-error", Static).update(Text("a reason is required", RED))
            return
        self.dismiss(reason)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _hints(pairs) -> Text:
    text = Text()
    for key, label in pairs:
        text.append(key, f"bold {TEXT}")
        text.append(f" {label}   ", MUTED)
    return text


def _key_hints(actions: tuple[str, ...] = ()) -> Text:
    """The footer: navigation keys, then the available action keys, then quit."""
    return _hints([*KEY_HINTS[:-1], *(ACTION_KEY[a] for a in actions), KEY_HINTS[-1]])


class SourcePane(Vertical):
    """`new run · source`: the local folder a new run reads."""

    def compose(self) -> ComposeResult:
        yield Static(Text("invoice directory", SOFT))
        with Horizontal(id="source-row"):
            yield Static(Text("❯", f"bold {GREEN}"), id="source-prompt")
            yield Input(DEFAULT_SOURCE, placeholder=DEFAULT_SOURCE, id="source")
        yield Static(
            Text("Local folder path. Reads pdf, txt, csv, json and xml.", MUTED), id="source-help"
        )
        yield Static(id="source-status")

    def say(self, message: str | None, colour: str = TEXT) -> None:
        self.query_one("#source-status", Static).update(Text(message or "", colour))


class FoundPane(VerticalScroll):
    """`found`: the files a run over the source would process, grouped by type."""

    def compose(self) -> ComposeResult:
        yield Static(id="found-body")

    @property
    def body(self) -> Static:
        return self.query_one("#found-body", Static)

    def show(self, discovery: Discovery) -> None:
        if discovery.problem:
            self.body.update(Text(discovery.problem, AMBER))
            return
        summary = Text(f"{len(discovery.paths)} files", TEXT)
        for kind, count in discovery.types.items():
            summary.append(f" · {count} {kind}", SOFT)
        names = Text("\n".join(p.name for p in discovery.paths), TEXT)
        self.body.update(Group(summary, Rule(style=RULE), names))


class InvoiceApp(App):
    """New run, the Results views and the reviewer actions. The only class that calls the service.

    `start` is the mode it opens on: "results" or "new_run". Without a `runtime` (and no
    `bootstrap` arguments to build one) the results are read-only.
    """

    CSS_PATH = "tui.tcss"
    TITLE = "invoice-flow"
    BINDINGS = [
        Binding("tab", "cycle_view(1)", "next view", priority=True, show=False),
        Binding("shift+tab", "cycle_view(-1)", "prev view", priority=True, show=False),
        *(Binding(str(n), f"jump_view({n - 1})", show=False) for n in range(1, len(TABS) + 1)),
        *(Binding(key, f"act('{action}')", show=False) for action, key, _, _ in ACTIONS),
        Binding("n", "new_run", show=False),
        Binding("q", "quit", "quit", show=False),
    ]

    def __init__(
        self,
        ledger_path: Path | str,
        runtime: service.Runtime | None = None,
        bootstrap: argparse.Namespace | None = None,
        start: str = "results",
    ):
        super().__init__()
        self.ledger_path = Path(ledger_path)
        self.runtime, self.bootstrap_args = runtime, bootstrap
        self.start, self.mode = start, start
        self.discovery: Discovery | None = None
        self.active_view = "all"
        self.results: Results | None = None
        self.retrying = False  # a retry worker is running: no other action until it ends
        self.keep_status = False  # the next selection change keeps the outcome just shown

    def compose(self) -> ComposeResult:
        yield RunHeader(id="header")
        with ContentSwitcher(initial=self.start, id="modes"):
            with Vertical(id="results"):
                yield TabBar(id="tabs")
                with Horizontal(id="body"):
                    yield FileList(id="files")
                    yield DetailPane(id="detail")
            with Horizontal(id="new_run"):
                yield SourcePane(id="source-pane")
                yield FoundPane(id="found")
        yield Static(_key_hints(), id="keys")

    def on_mount(self) -> None:
        self.query_one(FileList).border_title = "files"
        self.query_one(SourcePane).border_title = "new run · source"
        self.query_one(FoundPane).border_title = "found"
        self.query_one(DetailPane).say(None)
        try:
            if self.runtime is None and self.bootstrap_args is not None:
                self.runtime = service.bootstrap(self.bootstrap_args)
            self.results = service.results(self.ledger_path)
        except service.BootstrapError as exc:
            self.exit(return_code=1, message=f"cannot start: {exc}")
            return
        self._show_view("all")
        self._set_mode(self.start)

    def _set_mode(self, mode: str) -> None:
        """Show New run or the Results views, with their header, focus and keys."""
        self.mode = mode
        self.query_one(ContentSwitcher).current = mode
        if mode == "new_run":
            source = self.query_one("#source", Input)
            self._discover(source.value)
            self.query_one("#keys", Static).update(_hints(NEW_RUN_HINTS))
            source.focus()
        else:
            self.query_one(RunHeader).show(self.results)
            self._show_actions()
            self.query_one(FileList).focus()

    def action_new_run(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self._set_mode("new_run")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "source":
            self._discover(event.value)

    def _discover(self, source: str) -> None:
        self.discovery = service.discover(source)
        self.query_one(FoundPane).show(self.discovery)
        if self.mode == "new_run":
            self.query_one(RunHeader).funnel(len(self.discovery.paths), {})

    def action_cycle_view(self, step: int) -> None:
        if isinstance(self.screen, ModalScreen):  # tab is a priority key: not under a dialog
            return
        if self.mode != "results":  # tab leaves New run for the view shown last
            self._set_mode("results")
            return
        index = service.VIEWS.index(self.active_view)
        self._show_view(service.VIEWS[(index + step) % len(service.VIEWS)])

    def action_jump_view(self, index: int) -> None:
        if self.mode != "results":
            self._set_mode("results")
        self._show_view(service.VIEWS[index])

    def _show_view(self, view: str, keep: int | None = None) -> None:
        """Show a view; `keep` re-selects that arrival when it is still in the view."""
        self.active_view = view
        self.query_one(TabBar).show(self.results, view)
        rows = self.results.in_view(view)
        files = self.query_one(FileList)
        index = 0
        if keep is not None:
            ids = [row.arrival_id for row in rows]
            index = ids.index(keep) if keep in ids else (files.highlighted or 0)
        files.show(rows, index)
        if not rows:
            self.keep_status = False
            self.query_one(DetailPane).show(None)
            self._show_actions()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.query_one(FileList).mark(event.option_index)
        pane = self.query_one(DetailPane)
        if self.keep_status:
            self.keep_status = False
        else:
            pane.say(None)
        online = self.runtime is not None and self.runtime.tier != "offline"
        pane.show(service.arrival_detail(self.ledger_path, int(event.option.id), online=online))
        self._show_actions()

    def _available(self) -> tuple[str, ...]:
        detail = self.query_one(DetailPane).detail
        if self.runtime is None or detail is None or self.retrying or self.mode != "results":
            return ()
        return detail.actions

    def _show_actions(self) -> None:
        available = self._available()
        self.query_one(ActionBar).show(available)
        if self.mode == "results":
            self.query_one("#keys", Static).update(_key_hints(available))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_act(event.button.id)

    def action_act(self, action: str) -> None:
        """Start an action the selected arrival allows; anything else is ignored."""
        detail = self.query_one(DetailPane).detail
        if action not in self._available():
            return
        if action == "retry":
            self.retrying = True
            self._show_actions()
            self.query_one(DetailPane).say(
                f"retrying arrival #{detail.arrival_id}: asking the model again…", AMBER
            )
            self._retry(detail.arrival_id)
            return
        self.push_screen(
            ReasonScreen(action, detail.source),
            lambda reason: self._resolve(detail.arrival_id, action, reason),
        )

    def _resolve(self, arrival_id: int, action: str, reason: str | None) -> None:
        if reason is None:  # cancelled
            return
        try:
            result = service.resolve(self.runtime, arrival_id, action, reason)
        except service.ResolutionRefused as exc:
            self._refresh(arrival_id, f"refused: {exc}", RED)
        except Exception as exc:  # shown, never a crash
            self._refresh(arrival_id, f"{action} failed: {exc}", RED)
        else:
            self._refresh(arrival_id, *_outcome(RESOLVED[action], result))

    @work(thread=True, exclusive=True, group="retry")
    def _retry(self, arrival_id: int) -> None:
        """Retry in a worker thread (a model call can take a while); the UI never blocks."""
        try:
            result = service.retry(self.runtime, arrival_id)
        except service.RetryRefused as exc:
            outcome = (f"retry refused: {exc}", RED)
        except Exception as exc:  # shown, never a crash
            outcome = (f"retry failed: {exc}", RED)
        else:
            outcome = _outcome(f"retried ({result.decision.replace('_', ' ')})", result)
        self.call_from_thread(self._retried, arrival_id, *outcome)

    def _retried(self, arrival_id: int, message: str, colour: str) -> None:
        self.retrying = False
        self._refresh(arrival_id, message, colour)

    def _refresh(self, arrival_id: int, message: str, colour: str) -> None:
        """Reload the results from the Ledger, keep the view, and show the action's outcome."""
        self.results = service.results(self.ledger_path)
        self.query_one(RunHeader).show(self.results)
        self.keep_status = True
        self._show_view(self.active_view, keep=arrival_id)
        self.query_one(DetailPane).say(message, colour)
        self._show_actions()


def _outcome(verb: str, result: ArrivalResult) -> tuple[str, str]:
    state = result.state.replace("_", " ")
    view = {"paid": "approved", "logged_rejection": "rejected"}.get(result.state, "needs_review")
    return f"{verb} arrival #{result.arrival_id} · now {state}", VIEW_STATUS[view]


def run(ledger_path: Path, args: argparse.Namespace | None = None) -> int:
    """Open the reviewer TUI on New run; its runtime (LLM tier, inventory) is bootstrapped from
    `args`, so a new run uses the same `--llm` tier as the batch CLI."""
    flags = {"llm": None, "inventory": None, **vars(args or argparse.Namespace())}
    flags["ledger"] = str(ledger_path)
    app = InvoiceApp(ledger_path, bootstrap=argparse.Namespace(**flags), start="new_run")
    app.run()
    return app.return_code or 0
