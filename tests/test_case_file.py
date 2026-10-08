"""Full-gate Case File: checklist, decision context, vendor history, strict contracts."""

import json

import pytest
from pydantic import ValidationError

from invoice_pipeline.model import (
    SEVERITY,
    CaseFile,
    FindingCode,
    HistoryEntry,
    Severity,
    VerifyCheck,
    WarningAssessment,
)
from invoice_pipeline.prompts import ASSESSOR_PROMPT, VERIFIER_PROMPT, WARNING_CHECKLIST

WARNINGS = {code for code, severity in SEVERITY.items() if severity is Severity.WARNING}


def test_checklist_covers_exactly_the_warning_codes():
    assert set(WARNING_CHECKLIST) == WARNINGS
    assert all(questions for questions in WARNING_CHECKLIST.values())


def test_online_case_file_carries_checklist_for_present_warnings_only(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "300")], vendor="Zeta Corp")
    present = {f.code for f in case_file.findings if f.severity is Severity.WARNING}
    assert present == {FindingCode.PRICE_DEVIATION, FindingCode.VENDOR_UNKNOWN}
    assert set(case_file.checklist) == present
    assert case_file.checklist[FindingCode.PRICE_DEVIATION] == tuple(
        WARNING_CHECKLIST[FindingCode.PRICE_DEVIATION]
    )


def test_a_clean_invoice_has_an_empty_checklist(make_case_file):
    assert make_case_file(items=[("WidgetA", "250")]).checklist == {}


def test_offline_case_file_stays_slice_one_shaped(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "300")], online=False)
    assert case_file.checklist == {} and case_file.decision_context == []


def test_online_decision_context_is_deterministic_and_advisory(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "300")])
    assert case_file.decision_context == make_case_file(items=[("WidgetA", "300")]).decision_context
    text = " ".join(case_file.decision_context)
    assert "arrival: new" in text and "PRICE_DEVIATION" in text


def test_vendor_history_is_limited_with_its_total(make_case_file):
    history = [
        HistoryEntry(number=f"INV-{n}", total=None, currency="USD", state="paid", date=None)
        for n in range(10)
    ]
    case_file = make_case_file(history=history, history_total=25)
    assert len(case_file.vendor_history) == 10 and case_file.vendor_history_total == 25


def test_computed_deviation_is_addressable_without_model_arithmetic(make_case_file):
    case_file = make_case_file(items=[("WidgetA", "300")])
    assert str(case_file.references.price_deviations[0]) == "0.2"
    assert str(case_file.references.reference_prices["WidgetA"]) == "250"


def test_slice_one_case_file_json_still_deserializes(make_case_file):
    data = json.loads(make_case_file(online=False).model_dump_json())
    data.pop("checklist"), data.pop("decision_context")
    restored = CaseFile.model_validate(data)
    assert restored.checklist == {} and restored.decision_context == []


def test_no_checklist_version_or_hash_field_exists():
    assert not {n for n in CaseFile.model_fields if "version" in n or "hash" in n}


def test_prompts_name_the_roles_authority():
    assert "never approve" in ASSESSOR_PROMPT.lower()
    assert "holds" in VERIFIER_PROMPT


@pytest.mark.parametrize("explained", ["true", 1, None])
def test_assessment_explained_is_a_strict_bool(explained):
    with pytest.raises(ValidationError):
        WarningAssessment(
            code=FindingCode.PRICE_DEVIATION,
            line=0,
            explained=explained,
            evidence=[],
            rationale="r",
        )


@pytest.mark.parametrize("holds", ["yes", 0, None])
def test_verifier_check_holds_is_a_strict_bool(holds):
    with pytest.raises(ValidationError):
        VerifyCheck(code=FindingCode.PRICE_DEVIATION, line=0, holds=holds, rationale="r")


def test_assessment_code_must_be_a_known_finding_code():
    with pytest.raises(ValidationError):
        WarningAssessment(code="NOPE", line=None, explained=True, evidence=[], rationale="r")
