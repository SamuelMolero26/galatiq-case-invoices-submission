"""The Rule Engine: one Decision per arrival, from the Case File and injected model roles."""

from decimal import Decimal

from invoice_pipeline.model import (
    Agents,
    CaseFile,
    Decision,
    Finding,
    FindingCode,
    Outcome,
    Severity,
)
from invoice_pipeline.validation import PRICE_TOLERANCE

HEIGHTENED_SCRUTINY_USD = Decimal("10000")  # LLM Critic approval limit; "above" is strictly greater
CRITIC_PRICE_CEILING = 2 * PRICE_TOLERANCE  # row 5: a larger PRICE_DEVIATION never reaches agents


def _of(case_file: CaseFile, severity: Severity) -> list[Finding]:
    return [f for f in case_file.findings if f.severity is severity]


def _where(finding: Finding) -> str:
    return f"{finding.code}" + (f" line {finding.line}" if finding.line is not None else "")


def check_bounds(case_file: CaseFile) -> list[str]:
    """One failure per Warning outside its Critic Bound (pure; before any model call)."""
    failures = []
    for f in _of(case_file, Severity.WARNING):
        if f.code == FindingCode.VENDOR_UNKNOWN:
            failures.append(f"{_where(f)}: the LLM Critic may never clear it (bound: never)")
        elif f.code == FindingCode.PRICE_DEVIATION:
            deviation = case_file.references.price_deviations.get(f.line)
            if deviation is not None and deviation > CRITIC_PRICE_CEILING:
                failures.append(
                    f"{_where(f)}: deviation {deviation * 100:.2f}% exceeds the "
                    f"{CRITIC_PRICE_CEILING * 100:.0f}% Critic Bound"
                )
    return failures


def _decision(outcome, reasons, row, decided_by="rule_engine", **extra) -> Decision:
    return Decision(
        outcome=outcome, reasons=reasons, precedence_row=row, decided_by=decided_by, **extra
    )


def decide(case_file: CaseFile, agents: Agents) -> Decision:
    """Normative precedence, first matching row wins. Only this function builds a Decision."""
    warnings = _of(case_file, Severity.WARNING)
    if (
        case_file.arrival.kind == "duplicate"
        or _of(case_file, Severity.REJECTION_RULE)
        or _of(case_file, Severity.REVIEW_TRIGGER)
    ):
        raise NotImplementedError("precedence rows 1-3 land with task 1.13")
    if warnings:
        return _decide_warnings(case_file, agents, warnings)
    return _decide_clean(case_file, agents)


def _decide_warnings(case_file: CaseFile, agents: Agents, warnings: list[Finding]) -> Decision:
    names = ", ".join(_where(f) for f in warnings)
    usd = case_file.references.usd_equivalent
    amount = usd.amount if usd is not None else case_file.invoice.total
    if amount is not None and amount > HEIGHTENED_SCRUTINY_USD:
        currency = "USD" if usd is not None else case_file.invoice.currency
        decision = _decision(
            Outcome.NEEDS_REVIEW,
            [
                f"HEIGHTENED_SCRUTINY: {amount:.2f} {currency} is above the "
                f"${HEIGHTENED_SCRUTINY_USD:,.0f} Critic approval limit ({names})"
            ],
            4,
        )
    elif failures := check_bounds(case_file):
        decision = _decision(Outcome.NEEDS_REVIEW, failures, 5, bound_failures=failures)
    else:
        # Slice 1 has no online roles: within-bound Warnings never get a usable Critic answer.
        return _decision(
            Outcome.NEEDS_REVIEW,
            [f"UNREVIEWED_WARNINGS: no usable Critic answer for {names}"],
            5,
            unreviewed_warnings=True,
        )
    decision.advisory = agents.advise(case_file, decision)
    return decision


def _decide_clean(case_file: CaseFile, agents: Agents) -> Decision:
    """Row 6: Approved unless the escalate-only review escalates; it can never add an approval."""
    call = agents.escalate_review(case_file)
    answer = call.answer or {}
    if call.tier == "offline" or answer.get("verdict") == "concur":
        return _decision(Outcome.APPROVED, ["no findings"], 6, escalate_review=call)
    if answer.get("verdict") == "escalate":
        reason = f"ESCALATE_ONLY_REVIEW: {answer.get('rationale', 'escalated')}"
        return _decision(
            Outcome.NEEDS_REVIEW, [reason], 6, decided_by="llm_critic", escalate_review=call
        )
    reason = f"ESCALATE_ONLY_REVIEW_FAILED: {call.error or 'no usable answer'}"
    return _decision(Outcome.NEEDS_REVIEW, [reason], 6, escalate_review=call)
