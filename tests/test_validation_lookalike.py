import pytest

from invoice_pipeline.catalog import KnownVendor
from invoice_pipeline.model import FindingCode, Severity, vendor_key
from invoice_pipeline.validation import VENDOR_LOOKALIKE_THRESHOLD, validate, vendor_lookalike
from tests.factories import codes, make_catalog, make_invoice


def catalog_with(*vendors):
    return make_catalog(vendors={vendor_key(n): KnownVendor(n, s) for n, s in vendors})


CATALOG = catalog_with(
    ("Acme Supplies", "trusted"),
    ("Gadgets Co.", "trusted"),
    ("Fraudster LLC", "blocked"),
    ("Summit Manufacturers", "trusted"),
)


def lookalike(name, catalog=CATALOG):
    return vendor_lookalike(name, catalog)


def test_threshold_constant():
    assert VENDOR_LOOKALIKE_THRESHOLD == 0.85


def test_typo_names_the_known_vendor_and_score():
    found = lookalike("Acme Suppiles")
    assert found.code == FindingCode.VENDOR_LOOKALIKE
    assert found.severity is Severity.REVIEW_TRIGGER
    assert "Acme Supplies" in found.detail and "0.92" in found.detail


def test_exact_threshold_fires_and_just_below_does_not():
    at = lookalike("Summit Manufacturing Co.")  # normalizes to a 0.85 score
    assert at is not None and "0.85" in at.detail and "Summit Manufacturers" in at.detail
    assert lookalike("Summit Manufactrng") is None  # 0.842


def test_clearly_different_vendor_is_not_a_lookalike():
    assert lookalike("Northwind Traders") is None


def test_exact_known_vendor_is_not_compared():
    assert lookalike("ACME  Supplies") is None
    assert lookalike("Fraudster LLC") is None


def test_punctuation_variant_of_blocked_vendor_scores_one_but_is_not_blocked():
    findings = validate(make_invoice(vendor="Fraudster L.L.C"), CATALOG)
    assert set(codes(findings)) == {"VENDOR_UNKNOWN", "VENDOR_LOOKALIKE"}
    detail = next(f.detail for f in findings if f.code == FindingCode.VENDOR_LOOKALIKE)
    assert "Fraudster LLC" in detail and "1.00" in detail


def test_unknown_near_match_adds_lookalike_beside_unknown():
    findings = validate(make_invoice(vendor="Acme Suppiles"), CATALOG)
    assert codes(findings) == ["VENDOR_UNKNOWN", "VENDOR_LOOKALIKE"]


def test_short_and_suffix_only_names_are_safe():
    assert lookalike("Co.") is None
    assert lookalike("Ac") is None
    assert lookalike("   ") is None


def test_suffix_normalization_ignores_company_forms():
    catalog = catalog_with(("Northwind Traders Incorporated", "trusted"))
    assert lookalike("Northwind Traders Ltd", catalog).detail.endswith("(score 1.00)")


def test_deterministic_tie_breaks_by_name():
    catalog = catalog_with(("Beta Parts", "trusted"), ("Alfa Parts", "trusted"))
    first = lookalike("Gamma Parts", catalog)
    again = lookalike("Gamma Parts", catalog)
    assert first == again
    equal = catalog_with(("Zed Tools", "trusted"), ("Zeb Tools", "trusted"))
    assert "Zeb Tools" in lookalike("Zef Tools", equal).detail


def test_only_trusted_or_blocked_vendors_are_compared():
    catalog = catalog_with(("Acme Supplies", "unknown"))
    assert lookalike("Acme Suppiles", catalog) is None


@pytest.mark.parametrize("vendor", ["Acme Supplies", "Gadgets Co."])
def test_exact_known_vendor_adds_no_vendor_finding(vendor):
    assert validate(make_invoice(vendor=vendor), CATALOG) == []


def test_missing_vendor_never_runs_the_comparison():
    assert vendor_lookalike(None, CATALOG) is None
