"""The service API used by presenters: process files, list the queue, resolve entries.

Orchestration only: every rule lives in ingestion, validation, approval, ledger or payment.
"""

import json
import logging
import os
import sqlite3
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from invoice_pipeline import catalog, extraction, ingestion, ledger, payment
from invoice_pipeline.approval import decide, decide_unreadable
from invoice_pipeline.catalog import Catalog, CatalogError
from invoice_pipeline.critic import build_case_file, offline_agents, online_agents
from invoice_pipeline.llm import ConfigError, select_tier
from invoice_pipeline.model import (
    Agents,
    CriticAttempt,
    Decision,
    Event,
    Finding,
    FindingCode,
    Ingested,
    Invoice,
    PaymentIssue,
    QueueItem,
    RoleCall,
    Severity,
    vendor_key,
)
from invoice_pipeline.rates import usd_equivalent
from invoice_pipeline.tools import ToolRunner, open_readonly
from invoice_pipeline.validation import validate

MAX_DECIDE_ATTEMPTS = 3  # read-decide-write rounds before a processing failure

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Runtime:
    catalog: Catalog
    ledger_path: Path
    tier: str = "offline"
    agents: Agents = field(default_factory=offline_agents)
    pay_fn: payment.PayFn = payment.mock_payment
    on_event: Callable[[Event], None] = lambda event: None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


class ProcessingFailure(Exception):
    """An invoice of a file failed after ingestion, at the named stage."""

    def __init__(self, file: str, stage: str, error: str):
        super().__init__(f"{file}: {stage}: {error}")
        self.file, self.stage, self.error = file, stage, error


def _notify(rt: Runtime, event: Event) -> None:
    """Deliver an event; an observer error is logged and never changes what was processed."""
    try:
        rt.on_event(event)
    except Exception:
        log.warning("event observer failed on %s for %s", event.name, event.file, exc_info=True)


def _bind_events(rt: Runtime, source: str) -> Runtime:
    """Route the model roles' step events to this runtime's observer, tagged with the file.

    Bound per file, not at bootstrap: the CLI swaps `on_event` after bootstrap.
    """
    previous = rt.agents.on_step

    def on_step(name: str, detail: dict) -> None:
        _notify(rt, Event(name, source, dict(detail)))
        previous(name, detail)

    return replace(rt, agents=replace(rt.agents, on_step=on_step))


def _maybe_extract(ingested: Ingested, source: str, rt: Runtime) -> Ingested:
    """Run the Extraction Fallback once for an online readable TXT/PDF invoice with gaps."""
    fields = extraction.requested(ingested)
    if rt.tier == "offline" or rt.agents.extract is None or not fields:
        return ingested
    _notify(rt, Event("extract", source, {"fields": fields}))
    with _stage(source, "extraction"):
        return extraction.merge(ingested, rt.agents.extract(ingested.raw_text, fields))


@contextmanager
def _stage(file: str, stage: str):
    """Report any exception inside as a ProcessingFailure naming the file and stage."""
    try:
        yield
    except ProcessingFailure:
        raise
    except Exception as exc:
        raise ProcessingFailure(file, stage, f"{type(exc).__name__}: {exc}") from exc


def record_arrival(
    conn, ingested: Ingested, source: str, rt: Runtime, replacing: tuple[int, int] | None = None
) -> int:
    """Validate, then read-decide-write until the identity's version holds; returns the arrival id.

    No database lock is held while deciding. A changed identity discards the Decision and decides
    again on the fresh history; `MAX_DECIDE_ATTEMPTS` changes in a row is a processing failure.
    `replacing` is `(arrival_id, seq)` of an existing arrival whose Decision a retry replaces in
    place (`ledger.replace_if_unchanged`) instead of recording a new arrival.
    """
    exclude = replacing[0] if replacing else None
    invoice = ingested.invoice
    with _stage(source, "validation"):
        findings = [*ingested.findings, *validate(invoice, rt.catalog)]
    # classification only: the Reviewer and Heightened Scrutiny read it; payment never does
    usd = usd_equivalent(invoice.total, invoice.currency) if invoice.total is not None else None
    _notify(rt, Event("validated", source, {"findings": [f.code.value for f in findings]}))
    identity, key = invoice.identity(), vendor_key(invoice.vendor)
    for attempt in range(1, MAX_DECIDE_ATTEMPTS + 1):
        with _stage(source, "ledger"):
            rows, version = ledger.read_identity(conn, identity) if identity else ([], 0)
            ctx = ledger.classify(rows, invoice)
            history, total = ledger.vendor_history(conn, key, exclude) if key else ([], 0)
        all_findings = [*findings, *ctx.findings]
        with _stage(source, "approval"):
            case_file = build_case_file(
                invoice,
                all_findings,
                ctx.arrival,
                usd,
                rt.catalog,
                history,
                total,
                online=rt.tier != "offline",
            )
            decision = decide(case_file, rt.agents)
        new = ledger.Arrival(
            source, rt.now(), ingested, all_findings, decision, ctx.arrival.amount_due, usd
        )
        with _stage(source, "ledger"):
            if replacing is not None:
                arrival_id = ledger.replace_if_unchanged(conn, *replacing, identity, version, new)
            elif identity is None:  # Partial/Incomplete Identity: nothing to version-check
                return ledger.record(conn, new)
            else:
                arrival_id = ledger.record_if_unchanged(conn, identity, version, new)
        if arrival_id is not None:
            return arrival_id
        if attempt < MAX_DECIDE_ATTEMPTS:
            _notify(rt, Event("redecided", source, {"attempt": attempt}))
    raise ProcessingFailure(
        source,
        "ledger",
        f"identity changed {MAX_DECIDE_ATTEMPTS} times while deciding; not recorded",
    )


@dataclass(frozen=True)
class ArrivalResult:
    """One recorded arrival as presenters see it."""

    arrival_id: int
    source: str
    invoice_number: str | None
    vendor: str | None
    decision: str
    precedence_row: int
    state: str
    finding_codes: list[str]
    reasons: list[str]
    model_notes: str  # "offline tier", a role name, or "none" (no model role ran)


def _model_notes(record: dict) -> str:
    """Which model roles touched the decision, derived only from the persisted record."""
    decision = record["decision"]
    notes = []
    if record.get("extraction") is not None:
        notes.append("extraction")
    if decision.get("critic") is not None:
        notes.append("critic")  # also when every attempt failed
    elif decision.get("unreviewed_warnings"):
        notes.append("offline tier")  # row 5 without a critic: no online role ever ran
    else:
        for role in ("escalate_review", "advisory"):
            if call := decision.get(role):
                notes.append("offline tier" if call["tier"] == "offline" else role)
                break
    return ", ".join(notes) or "none"


def arrival_result(conn, arrival_id: int) -> ArrivalResult:
    row = conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()
    record = json.loads(row["record"])
    decision = record["decision"]
    notes = _model_notes(record)
    return ArrivalResult(
        arrival_id=arrival_id,
        source=row["source"],
        invoice_number=row["invoice_number"],
        vendor=row["vendor_name"],
        decision=row["decision"],
        precedence_row=decision["precedence_row"],
        state=row["state"],
        finding_codes=[f["code"] for f in record["findings"]],
        reasons=decision["reasons"],
        model_notes=notes,
    )


def _paid_if_claimed(conn, arrival_id: int, rt: Runtime, source: str) -> ArrivalResult:
    """Pay an arrival its Decision just claimed (Approved -> Payment Pending), exactly once."""
    result = arrival_result(conn, arrival_id)
    if result.state == "payment_pending":
        pay_claimed(conn, arrival_id, rt, source)
        result = arrival_result(conn, arrival_id)
    return result


def pay_claimed(conn, arrival_id: int, rt: Runtime, source: str) -> None:
    """Call the bank for a committed claim and emit the outcome; the claim stays on any failure."""
    with _stage(source, "payment"):
        issue = payment.pay(conn, arrival_id, rt.pay_fn, rt.now)
    if issue is None:
        _notify(rt, Event("payment_sent", source, {"arrival_id": arrival_id}))
    else:
        _notify(rt, Event("payment_failed", source, {"issue": issue.model_dump(mode="json")}))


class ResolutionRefused(Exception):
    """A Resolution was refused; nothing changed."""


_REJECT_ONLY = {
    FindingCode.UNREADABLE_DOCUMENT: "it is an unreadable document",
    FindingCode.PARTIAL_IDENTITY: "its identity is incomplete, so the duplicate check cannot run",
    FindingCode.MISSING_REQUIRED_FIELD: "a required amount is missing",
    FindingCode.NONPOSITIVE_TOTAL: "its total is not positive",
}


def _state_refusal(row) -> str | None:
    """Why no reviewer action applies to this arrival in its state, or None."""
    refused_by_state = {
        "payment_pending": "payment pending: v1 has no settlement action, check the bank outside",
        "superseded": f"replaced by its later arrival #{row['superseded_by']}; it needs no action",
        "duplicate": f"a Duplicate of arrival #{row['duplicate_of']}, which is already claimed",
        "logged_rejection": (
            "rejected: only a new arrival of a corrected invoice can lead to payment"
        ),
        "paid": "already paid",
    }
    if row["state"] in refused_by_state:
        return f"arrival #{row['id']} is {refused_by_state[row['state']]}"
    if row["resolution"]:
        return f"arrival #{row['id']} already has a Resolution ({row['resolution']})"
    return None


def _refusal(row, action: str) -> str | None:
    """Why this Resolution must be refused, or None when it may proceed."""
    if why := _state_refusal(row):
        return why
    if action == "reject":
        return None
    codes = {FindingCode(f["code"]) for f in json.loads(row["record"])["findings"]}
    for code, why in _REJECT_ONLY.items():
        if code in codes:
            return f"cannot approve arrival #{row['id']}: {why}; reject it instead"
    if row["vendor_key"] is None or row["invoice_number"] is None:
        return f"cannot approve arrival #{row['id']}: its identity is incomplete; reject it instead"
    if row["amount_due"] is None or Decimal(row["amount_due"]) <= 0:
        return f"cannot approve arrival #{row['id']}: no known positive payable amount; reject it"
    return None


def resolve(rt: Runtime, arrival_id: int, action: str, reason: str) -> ArrivalResult:
    """Approve or reject one Needs Review arrival, with a mandatory reason (no model, no editing).

    One short write transaction re-reads the row, so a stale view is refused; a refusal (including
    a payment-cap failure) rolls back and leaves the entry intact. Approval pays after the commit.
    """
    reason = (reason or "").strip()
    if action not in ("approve", "reject"):
        raise ResolutionRefused("a Resolution is either approve or reject")
    if not reason:
        raise ResolutionRefused("a Resolution needs a reason")
    conn = ledger.connect(rt.ledger_path)
    try:
        with ledger.write_txn(conn):
            row = conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()
            if row is None:
                raise ResolutionRefused(f"no arrival #{arrival_id}")
            if why := _refusal(row, action):
                raise ResolutionRefused(why)
            now = rt.now()
            ruling = dict(resolution=action, resolution_reason=reason, resolved_at=now.isoformat())
            if action == "reject":
                ledger.update_arrival(conn, arrival_id, state="logged_rejection", **ruling)
            else:
                pending = conn.execute(
                    "SELECT id FROM arrivals WHERE vendor_key = ? AND invoice_number = ?"
                    " AND state = 'payment_pending'",
                    (row["vendor_key"], row["invoice_number"]),
                ).fetchone()
                if pending:
                    raise ResolutionRefused(
                        f"cannot approve arrival #{arrival_id}: payment pending on arrival "
                        f"#{pending['id']}; v1 has no settlement action, check the bank outside"
                    )
                try:
                    ledger.claim(conn, arrival_id, now, **ruling)
                except ledger.CapExceeded as exc:
                    raise ResolutionRefused(str(exc)) from exc
        if action == "approve":
            pay_claimed(conn, arrival_id, rt, row["source"])
        return arrival_result(conn, arrival_id)
    finally:
        conn.close()


class RetryRefused(Exception):
    """A retry was refused, or could not complete; the arrival is unchanged."""


def _failed_model_call(record: dict) -> str | None:
    """The model role whose failed or unavailable call left this arrival undecided, or None.

    Extraction and the escalate-only review count only when an online call got no usable
    answer; Unreviewed Warnings (row 5) is by definition no usable Critic answer, offline too.
    """
    extraction, decision = record.get("extraction"), record["decision"]
    if extraction and extraction["call"]["answer"] is None:
        return "extraction"
    if decision.get("unreviewed_warnings"):
        return "critic"
    escalate = decision.get("escalate_review")
    if (
        decision["decided_by"] == "rule_engine"
        and escalate
        and escalate["tier"] != "offline"
        and escalate["answer"] is None
    ):
        return "escalate_review"
    return None


def _retry_refusal(row, online: bool) -> str | None:
    """Why a retry must be refused, or None when it may run (the fail-closed retry gate)."""
    arrival = f"arrival #{row['id']}"
    if not online:
        return f"offline tier: a retry of {arrival} would make no model call, so nothing changes"
    if why := _state_refusal(row):
        return why
    record = json.loads(row["record"])
    if record["invoice"] is None:
        return f"{arrival} is an unreadable document: no model call can change that"
    if _failed_model_call(record) is not None:
        return None
    decision = record["decision"]
    if decision["decided_by"] == "llm_critic" or decision.get("critic") is not None:
        return f"{arrival} has a definitive model verdict; a reviewer resolves it"
    return (
        f"{arrival} was decided by the rules (row {decision['precedence_row']}), not by a "
        "failed model call; a retry cannot change it"
    )


def _stored_ingested(record: dict, rerun_extraction: bool) -> Ingested:
    """The Ingested a readable arrival was decided from, rebuilt from its stored record.

    Ingestion raises no Finding for a readable invoice; only the Extraction Fallback adds one
    (LLM_EXTRACTED). Validation and Ledger Findings are derived again when deciding.
    """
    extraction = record.get("extraction")
    return Ingested(
        invoice=Invoice.model_validate(record["invoice"]),
        findings=[
            Finding.model_validate(f)
            for f in record["findings"]
            if f["code"] == FindingCode.LLM_EXTRACTED
        ],
        repairs=record["repairs"],
        raw_text=record["raw_text"],
        missing_required=extraction["requested"] if extraction else [],
        extraction=None
        if extraction is None or rerun_extraction
        else RoleCall.model_validate(extraction["call"]),
    )


def retry(rt: Runtime, arrival_id: int) -> ArrivalResult:
    """Decide one Needs Review arrival again after its model call failed (online tier only).

    The SAME arrival goes through `record_arrival` again, so every rule, Critic Bound,
    guardrail and the payment cap apply unchanged, and its Decision is replaced in place (no
    new arrival). An Approved retry is claimed and paid exactly once; one that fails again
    stays Needs Review. A refusal or failure leaves the arrival unchanged.
    """
    conn = ledger.connect(rt.ledger_path)
    try:
        row = conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()
        if row is None:
            raise RetryRefused(f"no arrival #{arrival_id}")
        if why := _retry_refusal(row, online=rt.tier != "offline"):
            raise RetryRefused(why)
        record, source = json.loads(row["record"]), row["source"]
        rt = _bind_events(rt, source)
        extraction_failed = _failed_model_call(record) == "extraction"
        try:
            ingested = _stored_ingested(record, rerun_extraction=extraction_failed)
            if extraction_failed:
                ingested = _maybe_extract(ingested, source, rt)
            record_arrival(conn, ingested, source, rt, replacing=(arrival_id, row["seq"]))
        except ProcessingFailure as exc:
            raise RetryRefused(
                f"retry of arrival #{arrival_id} did not complete ({exc.stage}: {exc.error}); "
                "it is unchanged"
            ) from exc
        _notify(rt, Event("retried", source, {"arrival_id": arrival_id}))
        return _paid_if_claimed(conn, arrival_id, rt, source)
    finally:
        conn.close()


class BootstrapError(Exception):
    """The run cannot start: unsupported tier, or a database with the wrong schema."""


def ledger_path_of(args) -> Path:
    return Path(args.ledger or ledger.DEFAULT_LEDGER_PATH)


def tool_factory(inventory: Path, ledger_path: Path):
    """Build the Assessor's per-invoice `ToolRunner` over fresh read-only connections."""

    def make(case_file) -> ToolRunner:
        stock = open_readonly(inventory)
        try:
            return ToolRunner(stock, open_readonly(ledger_path), case_file.invoice)
        except Exception:
            stock.close()
            raise

    return make


def bootstrap(args) -> Runtime:
    """Seed a missing inventory, check both schemas, and select the tier via select_tier."""
    try:
        tier_cfg = select_tier(getattr(args, "llm", None), os.environ)
    except ConfigError as exc:
        raise BootstrapError(str(exc)) from exc
    inventory = Path(args.inventory or catalog.DEFAULT_INVENTORY_PATH)
    ledger_path = ledger_path_of(args)
    try:
        if not inventory.exists():
            catalog.seed(inventory)
        loaded = catalog.load_catalog(inventory)
        ledger.connect(ledger_path).close()
    except (CatalogError, ledger.LedgerError, sqlite3.Error, OSError) as exc:
        raise BootstrapError(str(exc)) from exc
    agents = (
        online_agents(tier_cfg, tool_factory(inventory, ledger_path))
        if tier_cfg.tier == "grok"
        else offline_agents()
    )
    return Runtime(catalog=loaded, ledger_path=ledger_path, tier=tier_cfg.tier, agents=agents)


def collect_files(path: Path | str) -> list[Path]:
    """One file, or every file directly in a directory in ascending filename order."""
    path = Path(path)
    return sorted(p for p in path.iterdir() if p.is_file()) if path.is_dir() else [path]


@dataclass(frozen=True)
class Discovery:
    """The files a run over a local source would process, in processing order."""

    paths: tuple[Path, ...]
    types: dict[str, int]  # file type (suffix) -> count, in first-seen order
    problem: str | None = None  # why nothing can run from this source


def discover(source: Path | str) -> Discovery:
    """What `collect_files` finds at a local folder (or file) path; never reads the files."""
    if not str(source).strip():
        return Discovery((), {}, "type a local folder path")
    path = Path(source).expanduser()
    if not path.exists():
        return Discovery((), {}, f"not found: {source}")
    try:
        paths = tuple(collect_files(path))
    except OSError as exc:
        return Discovery((), {}, f"cannot read {source}: {exc.strerror or exc}")
    if not paths:
        return Discovery((), {}, f"no files in {source}")
    types = Counter(p.suffix.lower().lstrip(".") or "no type" for p in paths)
    return Discovery(paths, dict(types))


def process_path(path: Path | str, rt: Runtime) -> "BatchResult":
    """Ingest one file, then decide, record and (when Approved) pay each invoice in it
    independently; a failing invoice is reported and the rest of the file continues."""
    path = Path(path)
    results, failed = [], []
    conn = ledger.connect(rt.ledger_path)
    try:
        for ingested in ingestion.ingest(path):
            try:
                results.append(_process_one(conn, ingested, path.name, rt))
            except Exception as exc:
                failed.append(_failure(rt, path, exc))
    finally:
        conn.close()
    return BatchResult(results, failed)


def _process_one(conn, ingested: Ingested, source: str, rt: Runtime) -> ArrivalResult:
    rt = _bind_events(rt, source)
    _notify(rt, Event("ingested", source, {"unreadable": ingested.invoice is None}))
    if ingested.invoice is None:
        with _stage(source, "approval"):
            decision = decide_unreadable(ingested.findings)
        new = ledger.Arrival(source, rt.now(), ingested, ingested.findings, decision, None)
        with _stage(source, "ledger"):
            arrival_id = ledger.record(conn, new)
    else:
        ingested = _maybe_extract(ingested, source, rt)
        arrival_id = record_arrival(conn, ingested, source, rt)
    result = arrival_result(conn, arrival_id)
    _notify(
        rt, Event("decided", source, {"outcome": result.decision, "row": result.precedence_row})
    )
    return _paid_if_claimed(conn, arrival_id, rt, source)


@dataclass(frozen=True)
class BatchResult:
    results: list[ArrivalResult]
    failed: list[ProcessingFailure]

    def counts(self) -> dict[str, int]:
        return dict(Counter(r.state for r in self.results))


def _failure(rt: Runtime, path: Path, exc: Exception) -> ProcessingFailure:
    """Report an exception as a ProcessingFailure (emitting `file_failed`) and return it."""
    failure = (
        exc
        if isinstance(exc, ProcessingFailure)
        else ProcessingFailure(Path(path).name, "processing", f"{type(exc).__name__}: {exc}")
    )
    _notify(
        rt, Event("file_failed", failure.file, {"stage": failure.stage, "error": failure.error})
    )
    return failure


def run_batch(paths: list[Path], rt: Runtime) -> BatchResult:
    """Process files in the given order; a processing failure is reported and the rest continue.

    Every committed invoice is in `results`, also when another invoice of its file failed.
    """
    results, failed = [], []
    for path in paths:
        try:
            batch = process_path(path, rt)
        except Exception as exc:  # the file itself could not be processed (e.g. the Ledger)
            failed.append(_failure(rt, path, exc))
            continue
        results.extend(batch.results)
        failed.extend(batch.failed)
    return BatchResult(results, failed)


@contextmanager
def _read_only(ledger_path: Path):
    """An existing Ledger opened read-only; it is never created, migrated or seeded."""
    if not ledger_path.exists():
        raise BootstrapError(f"ledger not found: {ledger_path}")
    try:
        conn = ledger.connect(ledger_path, read_only=True)
    except (ledger.LedgerError, sqlite3.Error) as exc:
        raise BootstrapError(str(exc)) from exc
    try:
        yield conn
    finally:
        conn.close()


def review_queue(ledger_path: Path) -> list[QueueItem]:
    """The Review Queue: the same Ledger query every presenter uses (read-only)."""
    with _read_only(ledger_path) as conn:
        return ledger.review_queue(conn)


# Results read models: what the reviewer TUI shows, built only from the stored Ledger record.
# Presentation mapping only: no rule runs and no Decision is re-computed here.

VIEWS = ("all", "approved", "needs_review", "rejected")
# Ledger state -> Results view. "needs_review" is exactly the Review Queue (Needs Review and
# Payment Pending), so its count matches `review --list`. Duplicate and Superseded arrivals are
# not rejections (their invoice is already claimed or replaced), so only "all" shows them.
_VIEW_OF_STATE = {
    "paid": "approved",
    "needs_review": "needs_review",
    "payment_pending": "needs_review",
    "logged_rejection": "rejected",
}


@dataclass(frozen=True)
class ResultRow:
    arrival_id: int
    source: str
    state: str
    view: str | None  # None: shown only under "all"


@dataclass(frozen=True)
class Results:
    rows: list[ResultRow]  # every arrival, in arrival order
    files: int  # distinct source files
    funnel: dict[str, int]  # ingest -> validate -> approve -> paid

    def in_view(self, view: str) -> list[ResultRow]:
        return [r for r in self.rows if view == "all" or r.view == view]

    def count(self, view: str) -> int:
        return len(self.in_view(view))


def results(ledger_path: Path) -> Results:
    """Every arrival with its view, plus the batch funnel (read-only)."""
    with _read_only(ledger_path) as conn:
        rows = conn.execute(
            "SELECT id, source, state, json_extract(record, '$.invoice') IS NOT NULL AS readable"
            " FROM arrivals ORDER BY id"
        ).fetchall()
    funnel = {
        "ingest": len(rows),
        "validate": sum(r["readable"] for r in rows),  # validation ran on a readable invoice
        "approve": sum(r["state"] in ("paid", "payment_pending") for r in rows),  # claimed
        "paid": sum(r["state"] == "paid" for r in rows),
    }
    return Results(
        rows=[
            ResultRow(r["id"], r["source"], r["state"], _VIEW_OF_STATE.get(r["state"]))
            for r in rows
        ],
        files=len({r["source"] for r in rows}),
        funnel=funnel,
    )


@dataclass(frozen=True)
class Stage:
    name: str  # ingestion, validation, approval, payment
    status: str  # ok, warn, fail, held, logged, skipped
    summary: str


@dataclass(frozen=True)
class Note:
    role: str
    text: str
    model: bool  # the text is a model's output (or its failure), not a rule's or a person's


@dataclass(frozen=True)
class UsdEvidence:
    total: Decimal
    currency: str
    amount: Decimal  # buffered USD Equivalent
    rate: Decimal  # USD per unit of `currency`
    as_of: date
    buffer_pct: int


@dataclass(frozen=True)
class ArrivalDetail:
    arrival_id: int
    source: str
    state: str
    view: str | None
    scrutiny: str  # standard or heightened
    stages: list[Stage]
    notes: list[Note]
    finding_codes: list[str]  # unique, in recorded order
    usd: UsdEvidence | None
    actions: tuple[str, ...]  # reviewer actions it allows now: approve, reject, retry


def arrival_detail(ledger_path: Path, arrival_id: int, *, online: bool = False) -> ArrivalDetail:
    """One arrival as the Results detail pane shows it (read-only).

    `actions` applies the same refusals `resolve` and `retry` enforce; `online` is whether the
    presenter's runtime can call a model (retry is never offered offline).
    """
    with _read_only(ledger_path) as conn:
        row = conn.execute("SELECT * FROM arrivals WHERE id = ?", (arrival_id,)).fetchone()
    if row is None:
        raise LookupError(f"no arrival #{arrival_id}")
    record = json.loads(row["record"])
    decision = Decision.model_validate(record["decision"])
    findings = [Finding.model_validate(f) for f in record["findings"]]
    heightened = any(r.startswith("HEIGHTENED_SCRUTINY") for r in decision.reasons)
    return ArrivalDetail(
        arrival_id=arrival_id,
        source=row["source"],
        state=row["state"],
        view=_VIEW_OF_STATE.get(row["state"]),
        scrutiny="heightened" if heightened else "standard",
        stages=[
            _ingestion_stage(record),
            _validation_stage(record, findings),
            _approval_stage(decision),
            _payment_stage(row),
        ],
        notes=_notes(record, decision, row),
        finding_codes=list(dict.fromkeys(f.code.value for f in findings)),
        usd=_usd_evidence(row, record),
        actions=tuple(
            action
            for action, refusal in (
                ("approve", _refusal(row, "approve")),
                ("reject", _refusal(row, "reject")),
                ("retry", _retry_refusal(row, online)),
            )
            if refusal is None
        ),
    )


def _ingestion_stage(record: dict) -> Stage:
    if record["invoice"] is None:
        return Stage("ingestion", "fail", f"unreadable: {record['unreadable_reason']}")
    if (extracted := record.get("extraction")) is not None:
        supplied = ", ".join(extracted["supplied"]) or "nothing"
        return Stage("ingestion", "warn", f"model extracted {supplied}")
    if record["repairs"]:
        return Stage(
            "ingestion", "warn", "repaired " + ", ".join(r["field"] for r in record["repairs"])
        )
    return Stage("ingestion", "ok", f"parsed {record['invoice']['source_format']}")


def _validation_stage(record: dict, findings: list[Finding]) -> Stage:
    if record["invoice"] is None:
        return Stage("validation", "skipped", "not run: unreadable document")
    if not findings:
        return Stage("validation", "ok", "no findings")
    status = "fail" if any(f.severity is Severity.REJECTION_RULE for f in findings) else "warn"
    return Stage("validation", status, ", ".join(dict.fromkeys(f.code.value for f in findings)))


_APPROVAL_STATUS = {
    "approved": "ok",
    "needs_review": "warn",
    "rejected": "fail",
    "duplicate": "fail",
}


def _approval_stage(decision: Decision) -> Stage:
    outcome, by = decision.outcome.value.replace("_", " "), decision.decided_by.replace("_", " ")
    summary = f"{outcome} by {by}, row {decision.precedence_row}"
    return Stage("approval", _APPROVAL_STATUS[decision.outcome.value], summary)


def _payment_stage(row) -> Stage:
    match row["state"]:
        case "paid":
            return Stage("payment", "ok", f"paid {row['amount_paid']} {row['currency']}")
        case "payment_pending":
            issue = PaymentIssue.model_validate_json(row["payment_issue"])
            return Stage("payment", "warn", f"pending: {issue.what}")
        case "needs_review":
            return Stage("payment", "held", "held for review")
        case "logged_rejection":
            by_reviewer = row["resolution"] == "reject"
            return Stage(
                "payment", "logged", "rejected by reviewer" if by_reviewer else "rejection logged"
            )
        case "duplicate":
            return Stage("payment", "logged", f"not paid: duplicate of #{row['duplicate_of']}")
        case _:  # superseded
            return Stage("payment", "logged", f"not paid: superseded by #{row['superseded_by']}")


def _notes(record: dict, decision: Decision, row) -> list[Note]:
    """Who said what about this arrival: rules, model roles (labeled), and the reviewer."""
    notes = []
    if (extracted := record.get("extraction")) is not None:
        notes.append(_role_note(RoleCall.model_validate(extracted["call"])))
    notes.append(Note("rule engine", "; ".join(decision.reasons), model=False))
    if decision.bound_failures:
        notes.append(Note("critic bound", "; ".join(decision.bound_failures), model=False))
    for attempt in decision.critic.attempts if decision.critic else []:
        notes.extend(_attempt_notes(attempt))
    for call in (decision.escalate_review, decision.advisory):
        if call is not None:
            notes.append(_role_note(call))
    if row["resolution"]:
        text = f"{row['resolution']}: {row['resolution_reason']}"
        notes.append(Note("reviewer", text, model=False))
    return notes


def _role_note(call: RoleCall) -> Note:
    role = call.role.replace("_", "-")
    if call.tier == "offline":
        return Note(role, f"{call.error or 'offline tier'}: no model call made", model=False)
    if call.answer is None:
        return Note(role, f"no usable answer: {call.error}", model=True)
    if call.role == "extraction":
        return Note(role, "answered " + ", ".join(sorted(call.answer)), model=True)
    rationale, verdict = call.answer.get("rationale", ""), call.answer.get("verdict")
    return Note(role, f"{verdict}: {rationale}" if verdict else rationale, model=True)


def _attempt_notes(attempt: CriticAttempt) -> list[Note]:
    n, assessor, verifier = attempt.attempt, attempt.assessor, attempt.verifier
    notes = []
    if assessor.assessments:
        text = "; ".join(
            f"{_where(a.code, a.line)} {'explained' if a.explained else 'unexplained'}: "
            f"{a.rationale}"
            for a in assessor.assessments
        )
    else:
        text = f"no usable answer: {assessor.error}"
    notes.append(Note(f"assessor #{n}", text, model=True))
    if assessor.failures:  # the evidence guardrail is a rule, not the model
        text = "; ".join(f"{f.where}: {f.message}" for f in assessor.failures)
        notes.append(Note(f"guardrail #{n}", text, model=False))
    if verifier is not None:
        if verifier.checks:
            text = "; ".join(
                f"{_where(c.code, c.line)} {'holds' if c.holds else 'does not hold'}: {c.rationale}"
                for c in verifier.checks
            )
        else:
            text = f"no usable answer: {verifier.error}"
        notes.append(Note(f"verifier #{n}", text, model=True))
    return notes


def _where(code: FindingCode, line: int | None) -> str:
    return code.value if line is None else f"{code.value} line {line}"


def _usd_evidence(row, record: dict) -> UsdEvidence | None:
    """The USD Equivalent persisted with the decision, read back as stored (never recomputed).

    None for a USD invoice (at par, nothing to show), a currency without a Reference Rate, and
    records written before the field existed.
    """
    usd = record.get("usd_equivalent")
    if usd is None or usd["currency"] == "USD":
        return None
    return UsdEvidence(
        total=Decimal(row["total"]),
        currency=usd["currency"],
        amount=Decimal(usd["amount"]),
        rate=Decimal(usd["rate"]),
        as_of=date.fromisoformat(usd["as_of"]),
        buffer_pct=int(Decimal(usd["buffer"]) * 100),
    )
