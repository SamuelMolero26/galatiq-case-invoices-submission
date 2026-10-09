"""Read models for presenters (the TUI), built only from the stored Ledger record."""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from invoice_pipeline.approval import label
from invoice_pipeline.model import (
    CriticAttempt,
    Decision,
    Finding,
    PaymentIssue,
    RoleCall,
    Severity,
)
from invoice_pipeline.service import read_only, resolve_refusal, retry_refusal

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
    with read_only(ledger_path) as conn:
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
    with read_only(ledger_path) as conn:
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
                ("approve", resolve_refusal(row, "approve")),
                ("reject", resolve_refusal(row, "reject")),
                ("retry", retry_refusal(row, online)),
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
            f"{label(a.code, a.line)} {'explained' if a.explained else 'unexplained'}: "
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
                f"{label(c.code, c.line)} {'holds' if c.holds else 'does not hold'}: {c.rationale}"
                for c in verifier.checks
            )
        else:
            text = f"no usable answer: {verifier.error}"
        notes.append(Note(f"verifier #{n}", text, model=True))
    return notes


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
