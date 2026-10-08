import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from invoice_pipeline import extraction
from invoice_pipeline.approval import HEIGHTENED_SCRUTINY_USD, check_assessments, label
from invoice_pipeline.catalog import Catalog
from invoice_pipeline.llm import CorrectableError, FinalError, TierConfig, ask, chat, role_call
from invoice_pipeline.model import (
    Agents,
    ArrivalSummary,
    AssessCall,
    CaseFile,
    Decision,
    Finding,
    FindingCode,
    GuardrailFailure,
    HistoryEntry,
    Invoice,
    References,
    RoleCall,
    Severity,
    ToolCall,
    UsdEquivalent,
    VerifyCall,
    VerifyCheck,
    WarningAssessment,
)
from invoice_pipeline.prompts import ASSESSOR_PROMPT, VERIFIER_PROMPT, WARNING_CHECKLIST
from invoice_pipeline.tools import TOOL_SCHEMAS, ToolRunner
from invoice_pipeline.validation import PRICE_TOLERANCE, aggregate_quantities, price_deviations

OFFLINE_TIER = "offline tier"


def build_case_file(
    invoice: Invoice,
    findings: list[Finding],
    arrival: ArrivalSummary,
    usd: UsdEquivalent | None,
    catalog: Catalog,
    history: list[HistoryEntry],
    history_total: int,
    *,
    online: bool = False,
) -> CaseFile:
    """Assemble everything a model role may read. Pure: no I/O, no model arithmetic."""
    skus = {s for item in invoice.items if (s := catalog.resolve_sku(item.sku)) is not None}
    references = References(
        reference_prices={s: catalog.prices[s] for s in skus if s in catalog.prices},
        stock_levels={s: catalog.stock[s] for s in skus if s in catalog.stock},
        aggregated_quantities=aggregate_quantities(invoice, catalog),
        price_tolerance=PRICE_TOLERANCE,
        price_deviations=price_deviations(invoice, catalog),
        usd_equivalent=usd,
        heightened_scrutiny_line=HEIGHTENED_SCRUTINY_USD,
    )
    warnings = [f for f in findings if f.severity is Severity.WARNING]
    return CaseFile(
        invoice=invoice,
        findings=findings,
        arrival=arrival,
        decision_context=_decision_context(arrival, findings) if online else [],
        checklist=_checklist(warnings) if online else {},
        references=references,
        vendor_history=history,
        vendor_history_total=history_total,
    )


def _checklist(warnings: list[Finding]) -> dict[FindingCode, tuple[str, ...]]:
    return {f.code: WARNING_CHECKLIST[f.code] for f in warnings if f.code in WARNING_CHECKLIST}


def _decision_context(arrival: ArrivalSummary, findings: list[Finding]) -> list[str]:
    """Pre-decision facts for advisory reasoning only; assessors never receive them."""
    return [f"arrival: {arrival.kind}"] + [
        f"{f.severity.value}: {label(f.code, f.line)}" for f in findings
    ]


def offline_role(role: str) -> RoleCall:
    """An absent single-call role, recorded as the offline tier (no request is made)."""
    return RoleCall(
        role=role, tier="offline", model=None, tries=[], answer=None, error=OFFLINE_TIER
    )


def offline_agents() -> Agents:
    """The permanent `Agents` bundle for the offline tier; every path fails closed, no I/O."""
    return Agents(
        assess=lambda *args, **kwargs: None,  # no usable answer
        verify=lambda *args, **kwargs: None,  # no usable answer
        escalate_review=lambda case_file: offline_role("escalate_review"),
        advise=lambda case_file, decision: offline_role("advisory"),
    )


def _is_case_file_path(case_file: BaseModel, path: str) -> bool:
    """Whether dotted `path` names a concrete value inside this Case File.

    Models resolve by field name, lists by index, dicts by key; a scalar with a
    further segment, or any unknown segment, is not in the Case File. A path that
    ends on a whole record (a model) or on a null is not evidence of anything.
    """
    node: Any = case_file
    for part in path.split("."):
        if not part:
            return False
        if isinstance(node, BaseModel):
            if part not in type(node).model_fields:
                return False
            node = getattr(node, part)
        elif isinstance(node, dict):
            if part not in node:
                return False
            node = node[part]
        elif isinstance(node, (list, tuple)):
            if not part.isdigit() or int(part) >= len(node):
                return False
            node = node[int(part)]
        else:
            return False
    return node is not None and not isinstance(node, BaseModel)


def _validate_escalate(case_file: CaseFile):
    """Validator for the escalate-only review: verdict, evidence, rationale all required."""

    def validate(content: str):
        try:
            data = json.loads(content)
        except ValueError as exc:
            return CorrectableError(f"answer is not valid JSON: {exc}")
        if not isinstance(data, dict):
            return CorrectableError("answer must be a JSON object")
        if data.get("verdict") not in ("concur", "escalate"):
            return CorrectableError("field 'verdict' must be one of 'concur', 'escalate'")
        evidence = data.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) for item in evidence)
        ):
            return CorrectableError("field 'evidence' must be a non-empty list of Case File paths")
        for path in evidence:
            if not _is_case_file_path(case_file, path):
                return CorrectableError(
                    f"evidence path {path!r} must name a non-null field of the Case File, "
                    "not a whole record"
                )
        if not isinstance(data.get("rationale"), str) or not data["rationale"].strip():
            return CorrectableError("field 'rationale' must be a non-empty string")
        return data

    return validate


def _validate_advisory(content: str):
    """Validator for the advisory role: rationale required; any verdict is never read back."""
    try:
        data = json.loads(content)
    except ValueError as exc:
        return CorrectableError(f"answer is not valid JSON: {exc}")
    if not isinstance(data, dict):
        return CorrectableError("answer must be a JSON object")
    if not isinstance(data.get("rationale"), str) or not data["rationale"].strip():
        return CorrectableError("field 'rationale' must be a non-empty string")
    return data


def _escalate_messages(case_file: CaseFile) -> list[dict]:
    return [
        {
            "role": "system",
            "content": "You are the escalate-only reviewer of a clean invoice. Reply ONLY "
            'with JSON: {"verdict": "concur"|"escalate", "evidence": '
            '["<dotted Case File path>", ...], "rationale": "..."}. Concur only when '
            "nothing in the Case File needs a human; you never approve payment.",
        },
        {"role": "user", "content": case_file.model_dump_json()},
    ]


def _advisory_messages(case_file: CaseFile, decision: Decision) -> list[dict]:
    return [
        {
            "role": "system",
            "content": "You explain an already-made decision for the audit trail. Reply ONLY "
            'with JSON: {"rationale": "..."}. You are advisory only: you never authorize '
            "payment and your verdict, if any, is not read back.",
        },
        {
            "role": "user",
            "content": case_file.model_dump_json()
            + "\n"
            + decision.model_dump_json(include={"outcome", "reasons", "precedence_row"}),
        },
    ]


def online_agents(tier: TierConfig, tool_factory=None, chat_fn=chat) -> Agents:
    """The `Agents` bundle for the grok tier.

    `tool_factory(case_file)` builds the invoice's `ToolRunner` (read-only connections); the
    Assessor gets tools only when it is supplied. The runner lives in the per-invoice `scratch`
    so both attempts share one budget; the orchestrator closes it afterwards.
    An unusable answer surfaces as an unaccepted record or None, and `decide` fails closed.
    """

    def escalate_review(case_file: CaseFile) -> RoleCall:
        asked = ask(
            tier, _escalate_messages(case_file), _validate_escalate(case_file), chat_fn=chat_fn
        )
        return role_call("escalate_review", tier, asked)

    def advise(case_file: CaseFile, decision: Decision) -> RoleCall:
        asked = ask(
            tier, _advisory_messages(case_file, decision), _validate_advisory, chat_fn=chat_fn
        )
        return role_call("advisory", tier, asked)

    def assess_role(case_file: CaseFile, attempt: int, feedback: str | None, scratch: dict):
        runner = scratch.get("runner")
        if runner is None and tool_factory is not None:
            try:
                runner = scratch["runner"] = tool_factory(case_file)
            except Exception as exc:
                error = f"tool setup failure: {type(exc).__name__}: {exc}"
                return _failed_call(AssessCall, attempt, tier, error)
        return assess(
            tier, case_file, runner=runner, attempt=attempt, feedback=feedback, chat_fn=chat_fn
        )

    def verify_role(case_file, assessments, tool_calls, attempt):
        return verify(tier, case_file, assessments, tool_calls, attempt, chat_fn=chat_fn)

    return Agents(
        assess=assess_role,
        verify=verify_role,
        escalate_review=escalate_review,
        advise=advise,
        extract=lambda raw_text, fields: extraction.extract(
            tier, raw_text, fields, chat_fn=chat_fn
        ),
    )


def _parse_entries[M: BaseModel](
    content: str, key: str, model: type[M]
) -> tuple[list[M | None], list[str]]:
    """Parse `{key: [entry, ...]}` strictly. Returns one slot per entry (None where it failed)
    and every error found, so a single bad entry never discards the valid ones."""
    try:
        data = json.loads(content)
    except ValueError as exc:
        return [], [f"answer is not valid JSON: {exc}"]
    if not isinstance(data, dict) or not isinstance(data.get(key), list) or set(data) != {key}:
        return [], [f"answer must be a JSON object with exactly one field {key!r}: a list"]
    parsed: list[M | None] = []
    errors = []
    for index, item in enumerate(data[key]):
        slot = f"{key}[{index}]"
        if not isinstance(item, dict):
            parsed.append(None)
            errors.append(f"{slot}: must be a JSON object")
            continue
        try:
            parsed.append(model.model_validate_json(json.dumps(item), strict=True))
        except ValidationError as exc:
            parsed.append(None)
            for problem in exc.errors():
                field = ".".join(str(part) for part in problem["loc"]) or "value"
                errors.append(f"{slot}: field '{field}': {problem['msg']}")
    return parsed, errors


def _duplicates(entries: list, key: str) -> list[str]:
    seen: dict[tuple, int] = {}
    errors = []
    for index, entry in enumerate(entries):
        if entry is None:
            continue
        where = (entry.code, entry.line)
        if where in seen:
            errors.append(f"{key}[{index}]: duplicate of {key}[{seen[where]}] (one per code+line)")
        seen.setdefault(where, index)
    return errors


def _correction(key: str, entries: list, errors: list[str]) -> CorrectableError:
    valid = [f"{key}[{i}] is valid" for i, e in enumerate(entries) if e is not None]
    bad = len(entries) - len(valid)
    keep = f" ({'; '.join(valid)}; resend it unchanged)" if valid and bad else ""
    return CorrectableError("; ".join(errors) + keep)


def parse_assessor(content: str) -> list[WarningAssessment] | CorrectableError:
    """Strict Assessor answer: `{"assessments": [...]}`, one per `(code, line)`, no coercion."""
    entries, errors = _parse_entries(content, "assessments", WarningAssessment)
    errors += _duplicates(entries, "assessments")
    return _correction("assessments", entries, errors) if errors else entries


def parse_verifier(
    content: str, expected: Sequence[tuple[FindingCode, int | None]]
) -> list[VerifyCheck] | CorrectableError:
    """Strict Verifier answer: exactly one check per assessed Warning in `expected`.

    A missing, stray, or duplicate check is correctable.
    """
    entries, errors = _parse_entries(content, "checks", VerifyCheck)
    errors += _duplicates(entries, "checks")
    known = {(e.code, e.line) for e in entries if e is not None}
    for index, entry in enumerate(entries):
        if entry is not None and (entry.code, entry.line) not in expected:
            errors.append(
                f"checks[{index}]: not an assessed Warning ({label(entry.code, entry.line)})"
            )
    errors += [f"missing check for {label(*want)}" for want in expected if want not in known]
    return _correction("checks", entries, errors) if errors else entries


def _guardrail_message(failures: list[GuardrailFailure]) -> str:
    listed = "; ".join(f"{f.where}: {f.message} [{f.cause.value}]" for f in failures)
    return f"The evidence guardrail refused the answer: {listed}. Resend the full JSON answer."


def _failed_call[T: BaseModel](kind: type[T], attempt: int, tier: TierConfig, error: str) -> T:
    return kind(attempt=attempt, model=tier.model, error=error)


def assess(
    tier: TierConfig,
    case_file: CaseFile,
    *,
    runner: ToolRunner | None = None,
    attempt: int = 1,
    feedback: str | None = None,
    chat_fn=chat,
) -> AssessCall:
    """One Assessor attempt. Never raises: every failure comes back as an audit record.

    Tool calls go through `runner`, whose single budget is shared by both attempts. A guardrail
    failure is a Correction Wrapper correction; only `UNEXPLAINED` is final. `accepted` means the
    answer parsed, passed the guardrail, and explains every Warning.
    """
    if tier.tier == "offline":
        return _failed_call(AssessCall, attempt, tier, OFFLINE_TIER)
    seen = len(runner.calls) if runner else 0
    state: dict[str, list] = {"assessments": [], "failures": []}

    def validate(content: str):
        parsed = parse_assessor(content)
        if isinstance(parsed, CorrectableError):
            return parsed
        state["assessments"] = parsed
        state["failures"] = check_assessments(case_file, parsed, runner.calls if runner else [])
        if not state["failures"]:
            return parsed
        message = _guardrail_message(state["failures"])
        final = any(not f.correctable for f in state["failures"])
        return FinalError(message) if final else CorrectableError(message)

    messages = [
        {"role": "system", "content": ASSESSOR_PROMPT},
        {"role": "user", "content": case_file.model_dump_json(exclude={"decision_context"})},
    ]
    if feedback:
        messages.append(
            {
                "role": "user",
                "content": f"The Verifier rejected your previous assessment: {feedback} "
                "Reassess every Warning from the facts.",
            }
        )
    try:
        asked = ask(
            tier,
            messages,
            validate,
            tools=TOOL_SCHEMAS if runner else None,
            run_tool=(lambda name, args: runner.run(attempt, name, args)) if runner else None,
            chat_fn=chat_fn,
        )
        return AssessCall(
            attempt=attempt,
            model=tier.model,
            tries=asked.tries,
            assessments=state["assessments"],
            failures=state["failures"],
            tool_calls=runner.calls[seen:] if runner else [],
            accepted=asked.value is not None,
            exhausted=asked.exhausted,
            error=asked.error,
        )
    except Exception as exc:  # the gate fails closed; it never raises into the pipeline
        return _failed_call(AssessCall, attempt, tier, f"{type(exc).__name__}: {exc}")


def verify(
    tier: TierConfig,
    case_file: CaseFile,
    assessments: list[WarningAssessment],
    tool_calls: list[ToolCall],
    attempt: int = 1,
    *,
    chat_fn=chat,
) -> VerifyCall:
    """One Verifier pass over a guardrail-accepted assessment. No tools; never raises.

    It receives every recorded tool result so it can check claims against what was looked up.
    """
    if tier.tier == "offline":
        return _failed_call(VerifyCall, attempt, tier, OFFLINE_TIER)
    expected = [(a.code, a.line) for a in assessments]
    payload = {
        "case_file": case_file.model_dump(mode="json", exclude={"decision_context"}),
        "assessments": [a.model_dump(mode="json") for a in assessments],
        "tool_results": [
            c.model_dump(mode="json", include={"index", "name", "arguments", "result", "error"})
            for c in tool_calls
        ],
    }
    messages = [
        {"role": "system", "content": VERIFIER_PROMPT},
        {"role": "user", "content": json.dumps(payload)},
    ]
    try:
        asked = ask(
            tier, messages, lambda content: parse_verifier(content, expected), chat_fn=chat_fn
        )
        return VerifyCall(
            attempt=attempt,
            model=tier.model,
            tries=asked.tries,
            checks=asked.value or [],
            accepted=asked.value is not None,
            error=asked.error,
        )
    except Exception as exc:
        return _failed_call(VerifyCall, attempt, tier, f"{type(exc).__name__}: {exc}")
