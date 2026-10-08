"""Spike acceptance: deterministic ingestion over the 16 real invoices.

Run: pytest tests/test_ingestion_spike.py -v
PDFs are covered through their .txt mirrors (identical pipeline post-extraction).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spikes"))
from deterministic_ingestion import ingest_batch  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data" / "invoices"

# invoice_id -> (representative file, expected total, expected item count, required finding or "")
CASES = {
    "INV-1001": ("invoice_1001.txt", 5000.00, 2, ""),
    "INV-1002": ("invoice_1002.txt", 15000.00, 1, "TYPO_CLEANED"),
    "INV-1003": ("invoice_1003.txt", 100000.00, 1, "RELATIVE_DUE_DATE"),
    "INV-1004": ("invoice_1004.json", 1890.00, 2, ""),
    "INV-1004R": ("invoice_1004_revised.json", 5940.00, 3, "REVISION"),
    "INV-1005": ("invoice_1005.json", 15225.00, 3, ""),
    "INV-1006": ("invoice_1006.csv", 2750.00, 2, ""),
    "INV-1007": ("invoice_1007.csv", 15525.00, 3, ""),
    "INV-1008": ("invoice_1008.txt", 9900.00, 2, ""),
    "INV-1009": ("invoice_1009.json", -250.00, 2, "NEGATIVE_QTY"),
    "INV-1010": ("invoice_1010.txt", 7185.00, 4, ""),
    "INV-1011": ("invoice_1011.txt", 3000.00, 2, ""),
    "INV-1012": ("invoice_1012.txt", 9975.00, 3, "TYPO_CLEANED"),
    "INV-1013": ("invoice_1013.json", 22562.80, 8, "TOTAL_MISMATCH"),
    "INV-1014": ("invoice_1014.xml", 4125.00, 2, ""),
    "INV-1016": ("invoice_1016.json", 3233.00, 3, ""),
}


@pytest.mark.parametrize("case_id", list(CASES))
def test_spike_extracts_invoice(case_id):
    fname, expected_total, expected_items, required_finding = CASES[case_id]
    _, res = ingest_batch([DATA / fname])[0]
    assert res.status == "ok", f"{case_id}: {res.error}"
    assert res.total_claimed is not None, f"{case_id}: no total extracted"
    assert abs(res.total_claimed - expected_total) < 0.01, (
        f"{case_id}: total {res.total_claimed} != {expected_total}"
    )
    assert len(res.items) == expected_items, (
        f"{case_id}: items {len(res.items)} != {expected_items}: {res.items}"
    )
    if required_finding:
        assert any(required_finding in f for f in res.findings), (
            f"{case_id}: missing {required_finding} in {res.findings}"
        )


def test_spike_batch_never_throws_and_flags_duplicates():
    paths = [DATA / f for f, _, _, _ in CASES.values()]
    results = ingest_batch(paths)
    assert len(results) == len(paths)
    assert all(r.status in ("ok", "err") for _, r in results)
    by_number: dict[str, int] = {}
    for _, r in results:
        by_number[r.invoice_number] = by_number.get(r.invoice_number, 0) + 1
    assert by_number.get("INV-1004", 0) == 2
    dupes = [r for _, r in results if r.invoice_number == "INV-1004"]
    assert all(any("DUPLICATE_INVOICE_NUMBER" in f for f in r.findings) for r in dupes)
    ok_count = sum(1 for _, r in results if r.status == "ok")
    assert ok_count == len(paths), [
        (p, r.error) for p, r in results if r.status != "ok"
    ]


def test_spike_vendor_and_currency_spots():
    _, r8 = ingest_batch([DATA / "invoice_1008.txt"])[0]
    assert "NoProd" in r8.vendor
    _, r14 = ingest_batch([DATA / "invoice_1014.xml"])[0]
    assert r14.currency == "EUR"
    _, r9 = ingest_batch([DATA / "invoice_1009.json"])[0]
    assert any("MISSING_VENDOR" in f for f in r9.findings)


def test_confidence_weights_pinned():
    """T8: magic numbers become a tested table. Touch a weight, hear this test."""
    expected = {
        "invoice_1011.txt": 1.0,   # clean
        "invoice_1012.txt": 1.0,   # messy but fully cleaned: fixes cost 0
        "invoice_1002.txt": 1.0,   # typos cleaned, money ties out
        "invoice_1003.txt": 0.85,  # relative due date only
        "invoice_1013.json": 0.7,  # total mismatch only
        "invoice_1009.json": 0.0,   # negative + missing vendor/due + mismatch + negative total
    }
    for fname, want in expected.items():
        _, res = ingest_batch([DATA / fname])[0]
        assert res.confidence == want, f"{fname}: {res.confidence} != {want}"
