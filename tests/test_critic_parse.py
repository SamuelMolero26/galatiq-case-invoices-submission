"""Strict Assessor and Verifier answer parsers: typed values or deterministic errors."""

import json

import pytest

from invoice_pipeline.critic import parse_assessor, parse_verifier
from invoice_pipeline.llm import CorrectableError
from invoice_pipeline.model import FindingCode, VerifyCheck, WarningAssessment

PRICE = FindingCode.PRICE_DEVIATION


def entry(**changes):
    base = {
        "code": "PRICE_DEVIATION",
        "line": 0,
        "explained": True,
        "evidence": ["invoice.items.0.unit_price"],
        "rationale": "because",
    }
    return {**base, **changes}


def assessor(*entries):
    return json.dumps({"assessments": list(entries)})


def check(**changes):
    return {**{"code": "PRICE_DEVIATION", "line": 0, "holds": True, "rationale": "ok"}, **changes}


def verifier(*checks):
    return json.dumps({"checks": list(checks)})


def test_a_valid_assessor_answer_parses_to_typed_assessments():
    (a,) = parse_assessor(assessor(entry()))
    assert a == WarningAssessment(
        code=PRICE,
        line=0,
        explained=True,
        evidence=["invoice.items.0.unit_price"],
        rationale="because",
    )


def test_a_whole_invoice_assessment_may_have_a_null_line():
    (a,) = parse_assessor(assessor(entry(code="VENDOR_UNKNOWN", line=None)))
    assert a.line is None


@pytest.mark.parametrize("content", ["", "not json", "[]", '"x"', "{}", '{"assessments": 3}'])
def test_a_wrong_envelope_is_correctable(content):
    assert isinstance(parse_assessor(content), CorrectableError)


@pytest.mark.parametrize(
    "bad",
    [
        {"explained": "true"},
        {"explained": 1},
        {"explained": None},
        {"line": "0"},
        {"line": 0.0},
        {"line": True},
        {"code": "NOT_A_CODE"},
        {"code": 7},
        {"evidence": "invoice.items.0.unit_price"},
        {"evidence": [3]},
        {"rationale": None},
        {"surprise": "field"},
    ],
)
def test_strict_types_and_unknown_fields_are_rejected_without_coercion(bad):
    result = parse_assessor(assessor(entry(**bad)))
    assert isinstance(result, CorrectableError) and "assessments[0]" in result.message


def test_a_missing_field_is_named():
    broken = entry()
    del broken["explained"]
    result = parse_assessor(assessor(broken))
    assert isinstance(result, CorrectableError) and "explained" in result.message


def test_every_bad_assessment_is_reported_and_the_valid_ones_are_kept_untouched():
    result = parse_assessor(
        assessor(entry(), entry(code="VENDOR_UNKNOWN", line=None, explained="yes"), entry(line="x"))
    )
    assert isinstance(result, CorrectableError)
    assert "assessments[1]" in result.message and "assessments[2]" in result.message
    assert "assessments[0] is valid" in result.message


def test_two_assessments_for_one_code_and_line_are_refused():
    result = parse_assessor(assessor(entry(), entry(rationale="again")))
    assert isinstance(result, CorrectableError) and "duplicate" in result.message


def test_parsers_never_raise_on_hostile_input():
    for content in ("\x00", "{" * 5000, json.dumps({"assessments": [None, 3, "x", []]})):
        assert isinstance(parse_assessor(content), CorrectableError)
        assert isinstance(parse_verifier(content, [(PRICE, 0)]), CorrectableError)


def test_a_valid_verifier_answer_parses_to_typed_checks():
    (c,) = parse_verifier(verifier(check()), [(PRICE, 0)])
    assert c == VerifyCheck(code=PRICE, line=0, holds=True, rationale="ok")


@pytest.mark.parametrize("bad", [{"holds": "true"}, {"holds": 1}, {"holds": None}, {"x": 1}])
def test_verifier_checks_are_strict(bad):
    result = parse_verifier(verifier(check(**bad)), [(PRICE, 0)])
    assert isinstance(result, CorrectableError) and "checks[0]" in result.message


def test_a_missing_verifier_check_is_correctable_and_named():
    result = parse_verifier(verifier(check()), [(PRICE, 0), (FindingCode.VENDOR_UNKNOWN, None)])
    assert isinstance(result, CorrectableError)
    assert "VENDOR_UNKNOWN" in result.message and "missing" in result.message


def test_an_empty_verifier_answer_misses_every_check():
    assert isinstance(parse_verifier(verifier(), [(PRICE, 0)]), CorrectableError)


def test_a_stray_or_duplicate_verifier_check_is_refused():
    stray = parse_verifier(verifier(check(), check(code="VENDOR_UNKNOWN", line=None)), [(PRICE, 0)])
    assert isinstance(stray, CorrectableError) and "not an assessed Warning" in stray.message
    twice = parse_verifier(verifier(check(), check(holds=False)), [(PRICE, 0)])
    assert isinstance(twice, CorrectableError) and "duplicate" in twice.message
