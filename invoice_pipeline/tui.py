"""Reviewer TUI: the batch Results views (all / approved / needs review / rejected).

Thin presentation over the service read models: widgets receive plain dataclasses and never
touch the Ledger or any model role; only `InvoiceApp` calls `service.*`. Needs the optional
`tui` extra (Textual); `python main.py tui` imports this module lazily.
"""

from pathlib import Path

from rich.box import SQUARE
from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from invoice_pipeline import service
from invoice_pipeline.service import ArrivalDetail, Note, ResultRow, Results, Stage, UsdEvidence

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
    ("q", "quit"),
)


def render_detail(detail: ArrivalDetail) -> RenderableType:
    """The detail pane for one arrival: badge, scrutiny, pipeline, amount, notes, findings."""
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
    if detail.finding_codes:
        chip = VIEW_COLOUR[detail.view]
        chips = [
            Panel(Text(code, chip), box=SQUARE, border_style=chip, expand=False)
            for code in detail.finding_codes
        ]
        parts += [Text(), Columns(chips)]
    return Group(*parts)


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
        left = Text.assemble(
            ("invoice-flow", f"bold {TEXT}"), (f" · batch run · {results.files} files", MUTED)
        )
        right = Text()
        for i, (stage, count) in enumerate(results.funnel.items()):
            if i:
                right.append(" → ", MUTED)
            right.append(f"{stage} ", f"bold {TEXT}")
            right.append(str(count), GREEN)
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

    def show(self, rows: list[ResultRow]) -> None:
        self.rows, self.marked = rows, None
        self.clear_options()
        self.add_options(Option(_file_prompt(row, False), id=str(row.arrival_id)) for row in rows)
        if rows:
            self.highlighted = 0

    def mark(self, index: int) -> None:
        if self.marked is not None and self.marked < len(self.rows):
            self.replace_option_prompt_at_index(
                self.marked, _file_prompt(self.rows[self.marked], False)
            )
        self.replace_option_prompt_at_index(index, _file_prompt(self.rows[index], True))
        self.marked = index


class DetailPane(VerticalScroll):
    """The selected arrival's detail, or a placeholder for an empty view."""

    detail: ArrivalDetail | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="detail-body")

    @property
    def body(self) -> Static:
        return self.query_one("#detail-body", Static)

    def show(self, detail: ArrivalDetail | None) -> None:
        self.detail = detail
        self.body.update(
            render_detail(detail) if detail else Text("no arrivals in this view", MUTED)
        )
        self.scroll_home(animate=False)


def _key_hints() -> Text:
    text = Text()
    for key, label in KEY_HINTS:
        text.append(key, f"bold {TEXT}")
        text.append(f" {label}   ", MUTED)
    return text


class InvoiceApp(App):
    """The Results screen. The only class that calls the service."""

    CSS_PATH = "tui.tcss"
    TITLE = "invoice-flow"
    BINDINGS = [
        Binding("tab", "cycle_view(1)", "next view", priority=True, show=False),
        Binding("shift+tab", "cycle_view(-1)", "prev view", priority=True, show=False),
        *(Binding(str(n), f"jump_view({n - 1})", show=False) for n in range(1, len(TABS) + 1)),
        Binding("q", "quit", "quit", show=False),
    ]

    def __init__(self, ledger_path: Path | str):
        super().__init__()
        self.ledger_path = Path(ledger_path)
        self.active_view = "all"
        self.results: Results | None = None

    def compose(self) -> ComposeResult:
        yield RunHeader(id="header")
        yield TabBar(id="tabs")
        with Horizontal(id="body"):
            yield FileList(id="files")
            yield DetailPane(id="detail")
        yield Static(_key_hints(), id="keys")

    def on_mount(self) -> None:
        self.query_one(FileList).border_title = "files"
        try:
            self.results = service.results(self.ledger_path)
        except service.BootstrapError as exc:
            self.exit(return_code=1, message=f"cannot start: {exc}")
            return
        self.query_one(RunHeader).show(self.results)
        self._show_view("all")
        self.query_one(FileList).focus()

    def action_cycle_view(self, step: int) -> None:
        index = service.VIEWS.index(self.active_view)
        self._show_view(service.VIEWS[(index + step) % len(service.VIEWS)])

    def action_jump_view(self, index: int) -> None:
        self._show_view(service.VIEWS[index])

    def _show_view(self, view: str) -> None:
        self.active_view = view
        self.query_one(TabBar).show(self.results, view)
        rows = self.results.in_view(view)
        self.query_one(FileList).show(rows)
        if not rows:
            self.query_one(DetailPane).show(None)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.query_one(FileList).mark(event.option_index)
        detail = service.arrival_detail(self.ledger_path, int(event.option.id))
        self.query_one(DetailPane).show(detail)


def run(ledger_path: Path) -> int:
    app = InvoiceApp(ledger_path)
    app.run()
    return app.return_code or 0
