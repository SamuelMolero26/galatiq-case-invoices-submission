"""The Rule Engine: one Decision per arrival, from the Case File and injected model roles."""

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from invoice_pipeline.model import (
    Agents,
    CaseFile,
    CriticAttempt,
    CriticRecord,
    Decision,
    Finding,
    FindingCode,
    GuardrailCause,
    GuardrailFailure,
    Outcome,
    Severity,
    ToolCall,
    WarningAssessment,
    vendor_key,
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


def _reasons(findings: list[Finding]) -> list[str]:
    ordered = sorted(findings, key=lambda f: list(Severity).index(f.severity))
    return [f"{_where(f)}: {f.detail}" for f in ordered]


def _duplicate(case_file: CaseFile) -> Decision:
    arrival, currency = case_file.arrival, case_file.invoice.currency
    claimed = (
        f"already paid {arrival.paid_to_date:.2f} {currency}"
        if arrival.claimed_state == "paid"
        else f"payment of {arrival.paid_to_date:.2f} {currency} pending"
    )
    return _decision(
        Outcome.DUPLICATE,
        [f"DUPLICATE_PAYMENT: {claimed} on arrival #{arrival.duplicate_of}"],
        1,
        duplicate_of=arrival.duplicate_of,
    )


def decide_unreadable(findings: list[Finding]) -> Decision:
    """An Unreadable Document has no invoice: row 3, with no Validation and no model call."""
    return _decision(Outcome.NEEDS_REVIEW, _reasons(findings), 3)


def decide(case_file: CaseFile, agents: Agents) -> Decision:
    """Normative precedence, first matching row wins. Only this function builds a Decision."""
    if case_file.arrival.kind == "duplicate":  # row 1: no model call of any kind
        return _duplicate(case_file)
    if _of(case_file, Severity.REJECTION_RULE):  # row 2
        decision = _decision(Outcome.REJECTED, _reasons(case_file.findings), 2)
    elif _of(case_file, Severity.REVIEW_TRIGGER):  # row 3: human-only
        decision = _decision(Outcome.NEEDS_REVIEW, _reasons(case_file.findings), 3)
    elif warnings := _of(case_file, Severity.WARNING):  # rows 4-5
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
        else:  # the full gate: its Decision is final and carries no advisory
            return orchestrate(case_file, agents)
    else:  # row 6: Approved unless the escalate-only review escalates; never adds an approval
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
    decision.advisory = agents.advise(case_file, decision)  # explains; never read back
    return decision


# --- evidence guardrail ----------------------------------------------------------------------

_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_SELF_ROOTS = {"findings", "checklist", "decision_context"}
_SKU_TABLES = {"reference_prices", "stock_levels", "aggregated_quantities"}
_LINE_ITEM_EVIDENCE_FIELDS = {
    "raw_name",
    "sku",
    "raw_quantity",
    "quantity",
    "unit_price",
    "line_total",
}


class PathError(ValueError):
    """An evidence path that cannot be used as evidence."""


class MalformedPath(PathError):
    """The path is not in the dot/index grammar."""


class UnresolvedPath(PathError):
    """A well-formed path that names no concrete value."""


def resolve_path(root: dict, tool_calls: list[ToolCall], path: str) -> Any:
    """Resolve a dotted path over in-memory JSON (the Case File dump, or `tool.<n>.<path>`).

    The grammar is segments of `[A-Za-z0-9_-]` joined by dots; digits index lists. Nothing is
    evaluated and no file is touched. The value must be concrete: not null, not a whole
    record (object), not an empty list.
    """
    parts = path.split(".")
    if not all(_SEGMENT.fullmatch(part) for part in parts):
        raise MalformedPath(f"{path!r} is not a dotted path of names and list indexes")
    node: Any = root
    if parts[0] == "tool":
        index = parts[1] if len(parts) > 1 else ""
        if not index.isdigit() or int(index) >= len(tool_calls) or tool_calls[int(index)].error:
            raise UnresolvedPath(f"{path!r} names no successful tool call")
        call = tool_calls[int(index)]
        node, parts = (
            call.model_dump(mode="json", include={"name", "arguments", "result"}),
            parts[2:],
        )
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise UnresolvedPath(f"{path!r} names no value in the Case File or tool results")
    if node is None or isinstance(node, dict) or node == []:
        raise UnresolvedPath(f"{path!r} must name a concrete non-null value, not a whole record")
    return node


def _failure(cause: GuardrailCause, where: str, message: str, correctable: bool = True):
    return GuardrailFailure(cause=cause, where=where, message=message, correctable=correctable)


class _Evidence:
    """Judges whether one resolvable evidence path belongs to one assessed Warning."""

    def __init__(self, case_file: CaseFile, tool_calls: list[ToolCall]):
        self.case_file, self.tool_calls = case_file, tool_calls
        self.invoice = case_file.invoice
        self.currency = case_file.invoice.currency
        self.vendor = vendor_key(case_file.invoice.vendor)

    def line_sku(self, line: int | None) -> str | None:
        items = self.invoice.items
        sku = items[line].sku if line is not None and 0 <= line < len(items) else None
        return sku.casefold() if sku else None

    def classify(self, code: FindingCode, line: int | None, path: str) -> str | GuardrailCause:
        """`"relevant"`, `"self"`, `"neutral"`, or the GuardrailCause that refuses the path."""
        parts = path.split(".")
        if parts[0] in _SELF_ROOTS:
            return "self"
        if parts[0] == "tool":
            return self._tool(code, line, self.tool_calls[int(parts[1])], parts[2:])
        if parts[0] == "vendor_history":
            return self._history(parts[1:], self.case_file.vendor_history)
        if parts[0] == "vendor_history_total":
            return "relevant"
        if parts[:2] == ["invoice", "items"]:
            field = parts[3] if len(parts) == 4 else None
            if field not in _LINE_ITEM_EVIDENCE_FIELDS:
                return "neutral"
            index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            if index is None or line is None:
                return "neutral"
            return "relevant" if index == line else GuardrailCause.CROSS_LINE_EVIDENCE
        if parts[:2] == ["invoice", "vendor"]:
            return "relevant" if code is FindingCode.VENDOR_UNKNOWN else "neutral"
        if parts[:2] == ["invoice", "currency"]:
            return "relevant" if code is FindingCode.CURRENCY_NON_USD else "neutral"
        if parts[0] == "references" and len(parts) > 1:
            return self._reference(code, line, parts[1:])
        return "neutral"

    def _reference(self, code, line, parts) -> str | GuardrailCause:
        table, key = parts[0], parts[1] if len(parts) > 1 else None
        if table in _SKU_TABLES and key is not None:
            if line is None:
                return "neutral"
            same = key.casefold() == self.line_sku(line)
            return "relevant" if same else GuardrailCause.CROSS_LINE_EVIDENCE
        if table == "price_deviations" and key is not None:
            if line is None:
                return "neutral"
            return "relevant" if key == str(line) else GuardrailCause.CROSS_LINE_EVIDENCE
        if table == "price_tolerance":
            return "relevant" if code is FindingCode.PRICE_DEVIATION else "neutral"
        if table == "usd_equivalent":
            return "relevant" if code is FindingCode.CURRENCY_NON_USD else "neutral"
        return "neutral"

    def _history(self, parts, entries) -> str | GuardrailCause:
        if parts and parts[0].isdigit() and int(parts[0]) < len(entries):
            if entries[int(parts[0])].currency != self.currency:
                return GuardrailCause.WRONG_HISTORY_CURRENCY
        return "relevant"

    def _tool(self, code, line, call: ToolCall, parts) -> str | GuardrailCause:
        args = call.arguments
        if call.name == "get_vendor_history":
            if vendor_key(str(args.get("vendor_key", ""))) != self.vendor:
                return GuardrailCause.CROSS_VENDOR_EVIDENCE
            if parts[:2] == ["result", "entries"] and len(parts) > 2 and parts[2].isdigit():
                entries = (call.result or {}).get("entries") or []
                index = int(parts[2])
                if index < len(entries) and entries[index].get("currency") != self.currency:
                    return GuardrailCause.WRONG_HISTORY_CURRENCY
            return "relevant"
        if line is None:
            return "neutral"
        if call.name == "get_invoice_line":
            return "relevant" if args.get("n") == line else GuardrailCause.CROSS_LINE_EVIDENCE
        same = str(args.get("sku", "")).casefold() == self.line_sku(line)
        return "relevant" if same else GuardrailCause.CROSS_LINE_EVIDENCE


def check_assessments(
    case_file: CaseFile, assessments: list[WarningAssessment], tool_calls: list[ToolCall]
) -> list[GuardrailFailure]:
    """The pure evidence guardrail: one grounded, explained assessment per Warning, or named causes.

    Only `UNEXPLAINED` (the Assessor says the Warning is not explained) is final; every other
    cause is correctable. Order is deterministic: wrong, missing, then per-assessment causes.
    """
    wanted = list(dict.fromkeys((f.code, f.line) for f in _of(case_file, Severity.WARNING)))
    failures, by_key = [], {}
    for a in assessments:
        key = (a.code, a.line)
        if key not in wanted or key in by_key:
            why = "duplicate" if key in by_key else "not a Warning of this invoice"
            failures.append(
                _failure(GuardrailCause.WRONG_ASSESSMENT, _label(*key), f"assessment is {why}")
            )
        else:
            by_key[key] = a
    for key in wanted:
        if key not in by_key:
            failures.append(
                _failure(GuardrailCause.MISSING_ASSESSMENT, _label(*key), "no assessment given")
            )
    root = case_file.model_dump(mode="json")
    judge = _Evidence(case_file, tool_calls)
    for key in wanted:
        if key in by_key:
            failures += _check_one(judge, root, tool_calls, by_key[key])
    return failures


def _label(code: FindingCode, line: int | None) -> str:
    return f"{code}" + (f" line {line}" if line is not None else "")


def _check_one(judge: _Evidence, root: dict, tool_calls, a: WarningAssessment):
    where = _label(a.code, a.line)
    if not a.explained:
        return [
            _failure(
                GuardrailCause.UNEXPLAINED,
                where,
                "the Assessor could not explain this Warning",
                correctable=False,
            )
        ]
    if not a.evidence:
        return [
            _failure(GuardrailCause.EMPTY_EVIDENCE, where, "an explained Warning needs evidence")
        ]
    failures, relevant, selfish = [], 0, 0
    for path in a.evidence:
        try:
            resolve_path(root, tool_calls, path)
        except MalformedPath as exc:
            failures.append(_failure(GuardrailCause.MALFORMED_PATH, where, str(exc)))
            continue
        except UnresolvedPath as exc:
            failures.append(_failure(GuardrailCause.UNRESOLVED_EVIDENCE, where, str(exc)))
            continue
        verdict = judge.classify(a.code, a.line, path)
        if isinstance(verdict, GuardrailCause):
            failures.append(_failure(verdict, where, f"evidence {path!r} does not belong here"))
        elif verdict == "relevant":
            relevant += 1
        elif verdict == "self":
            selfish += 1
    if not failures and not relevant:
        cause = GuardrailCause.SELF_EVIDENCE if selfish else GuardrailCause.IRRELEVANT_EVIDENCE
        failures.append(_failure(cause, where, "no evidence supports this Warning's explanation"))
    return failures


@dataclass(frozen=True)
class Verdict:
    """What one critic attempt amounts to under the gate."""

    approved: bool
    usable: bool  # False: no usable answer, which fails closed to Unreviewed Warnings
    retry: bool  # True: a correction attempt may follow
    reasons: list[str]
    feedback: str | None = None  # what attempt 2 is told


def accept_verdict(
    case_file: CaseFile, attempt: CriticAttempt, tool_calls: list[ToolCall]
) -> Verdict:
    """The deterministic gate between model answers and an approval (pure).

    Approved only when every Warning is within its bound, has a guardrail-accepted explained
    assessment (re-checked here, whatever the record claims), and has exactly one Verifier check
    with `holds=True`. A false claim is usable and correctable; unexplained finality is usable
    and final; a malformed or absent answer is no usable answer.
    """
    assessor, verifier = attempt.assessor, attempt.verifier
    if not assessor.accepted:
        if final := [f for f in assessor.failures if not f.correctable]:
            return Verdict(False, True, False, [_refusal(f) for f in final])
        return Verdict(False, False, False, [assessor.error or "no usable assessment"])
    if failures := check_bounds(case_file):
        return Verdict(False, True, False, failures)
    if guardrail := check_assessments(case_file, assessor.assessments, tool_calls):
        return Verdict(False, True, False, [_refusal(f) for f in guardrail])
    if verifier is None or not verifier.accepted:
        error = verifier.error if verifier else None
        return Verdict(False, False, False, [error or "no usable Verifier answer"])
    wanted = list(dict.fromkeys((f.code, f.line) for f in _of(case_file, Severity.WARNING)))
    counts = Counter((c.code, c.line) for c in verifier.checks)
    malformed = (
        [f"Verifier check missing for {_label(*key)}" for key in wanted if key not in counts]
        + [
            f"Verifier check for {_label(*key)} is not an assessed Warning"
            for key in counts
            if key not in wanted
        ]
        + [f"Verifier gave {n} checks for {_label(*key)}" for key, n in counts.items() if n > 1]
    )
    if malformed:
        return Verdict(False, False, False, malformed)
    false = [c for c in verifier.checks if not c.holds]
    if false:
        reasons = [
            f"{_label(c.code, c.line)}: the Verifier rejected the explanation: {c.rationale}"
            for c in false
        ]
        return Verdict(False, True, True, reasons, feedback=" ".join(reasons))
    return Verdict(True, True, False, [])


def _refusal(failure: GuardrailFailure) -> str:
    return f"{failure.where}: {failure.message} [{failure.cause.value}]"


def _step(agents: Agents, event: str, attempt: int) -> None:
    try:
        agents.on_step(event, {"attempt": attempt})
    except Exception:  # an observer never changes a decision
        pass


def _attempt(case_file: CaseFile, agents: Agents, number: int, feedback, scratch, calls):
    """Run one Assessor + Verifier attempt. Returns the record, or None when a role gave nothing."""
    _step(agents, "assess", number)
    assessed = agents.assess(case_file, number, feedback, scratch)
    if assessed is None:
        return None
    calls.extend(assessed.tool_calls)
    verified = None
    if assessed.accepted and not check_assessments(case_file, assessed.assessments, calls):
        _step(agents, "verify", number)  # only guardrail-accepted output reaches the Verifier
        verified = agents.verify(case_file, assessed.assessments, list(calls), number)
    return CriticAttempt(attempt=number, feedback=feedback, assessor=assessed, verifier=verified)


def orchestrate(case_file: CaseFile, agents: Agents) -> Decision:
    """Row 5 within bound: at most two Assessor+Verifier attempts; only the final one can approve.

    Never raises. No usable answer (offline, transport, exhausted tries, a raising role, a
    malformed Verifier answer) is Unreviewed Warnings; a usable refusal is Needs Review.
    """
    names = ", ".join(_where(f) for f in _of(case_file, Severity.WARNING))
    scratch: dict = {}
    attempts: list[CriticAttempt] = []
    calls: list[ToolCall] = []
    verdict = Verdict(False, False, False, ["no usable Critic answer"])
    try:
        feedback = None
        for number in (1, 2):
            if number == 2:
                _step(agents, "correct", number)
            try:
                record = _attempt(case_file, agents, number, feedback, scratch, calls)
            except Exception as exc:  # a role that raises is no answer
                verdict = Verdict(False, False, False, [f"{type(exc).__name__}: {exc}"])
                break
            if record is None:
                verdict = Verdict(False, False, False, ["no usable Critic answer"])
                break
            attempts.append(record)
            verdict = accept_verdict(case_file, record, calls)
            if verdict.approved or not verdict.retry:
                break
            feedback = verdict.feedback
    finally:
        for cleanup in scratch.get("cleanup", []):
            try:
                cleanup()
            except Exception:
                pass
    critic = CriticRecord(attempts=attempts) if attempts else None
    if verdict.approved:
        reasons = [f"LLM_CRITIC: {names} explained by evidence and verified"]
        return _decision(Outcome.APPROVED, reasons, 5, decided_by="llm_critic", critic=critic)
    if not verdict.usable:
        return _decision(
            Outcome.NEEDS_REVIEW,
            [f"UNREVIEWED_WARNINGS: no usable Critic answer for {names}"],
            5,
            unreviewed_warnings=True,
            critic=critic,
        )
    reasons = [f"CRITIC_NOT_ACCEPTED: {reason}" for reason in verdict.reasons]
    return _decision(Outcome.NEEDS_REVIEW, reasons, 5, critic=critic)
