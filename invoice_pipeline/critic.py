import json
from typing import Any

from pydantic import BaseModel

from invoice_pipeline.approval import HEIGHTENED_SCRUTINY_USD
from invoice_pipeline.catalog import Catalog
from invoice_pipeline.llm import CorrectableError, TierConfig, ask, role_call
from invoice_pipeline.model import (
    Agents,
    ArrivalSummary,
    CaseFile,
    Decision,
    Finding,
    FindingCode,
    HistoryEntry,
    Invoice,
    References,
    RoleCall,
    Severity,
    UsdEquivalent,
)
from invoice_pipeline.prompts import WARNING_CHECKLIST
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

    def where(f: Finding) -> str:
        return f.code.value + (f" line {f.line}" if f.line is not None else "")

    return [f"arrival: {arrival.kind}"] + [f"{f.severity.value}: {where(f)}" for f in findings]


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


def online_agents(tier: TierConfig) -> Agents:
    """The `Agents` bundle for the grok tier: escalate-only + advisory via `ask`/`role_call`.

    assess/verify stay no-answer stubs until slice 2c. An unusable answer (malformed,
    exhausted, FinalError) surfaces as answer None with error, and `decide` fails closed.
    """

    def escalate_review(case_file: CaseFile) -> RoleCall:
        asked = ask(tier, _escalate_messages(case_file), _validate_escalate(case_file))
        return role_call("escalate_review", tier, asked)

    def advise(case_file: CaseFile, decision: Decision) -> RoleCall:
        asked = ask(tier, _advisory_messages(case_file, decision), _validate_advisory)
        return role_call("advisory", tier, asked)

    return Agents(
        assess=lambda *args, **kwargs: None,  # slice 2c: full-gate assessor
        verify=lambda *args, **kwargs: None,  # slice 2c: full-gate verifier
        escalate_review=escalate_review,
        advise=advise,
    )
