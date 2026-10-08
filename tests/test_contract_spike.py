"""Contract spike: identical behavior cases against both implementations.

Run: pytest tests/test_contract_spike.py -v
Documents parity AND honest differences (finding suffixes, coercion).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spikes"))
import invoice_contract_dataclass as dc  # noqa: E402
import invoice_contract_pydantic as pd  # noqa: E402
from pydantic import ValidationError  # noqa: E402


def payload(**over):
    base = {
        "invoice_number": "INV-1011",
        "source_file": "invoice_1011.txt",
        "source_format": "txt",
        "vendor_name": "Summit Manufacturing Co.",
        "date_raw": "2026-01-20",
        "due_raw": "2026-02-20",
        "items": [{"sku_raw": "WidgetA", "sku": "WidgetA", "quantity": 6, "unit_price": 250.0}],
        "total_claimed": 3000.0,
        "total_computed": 1500.0,
        "confidence": 1.0,
        "findings": [],
    }
    base.update(over)
    return base


def test_valid_invoice_parity():
    a, b = dc.Invoice(**payload()), pd.Invoice(**payload())
    assert a.invoice_number == b.invoice_number == "INV-1011"
    assert a.items[0].quantity == b.items[0].quantity == 6


def test_empty_vendor_allowed_both():
    assert dc.Invoice(**payload(vendor_name="")).vendor_name == ""
    assert pd.Invoice(**payload(vendor_name="")).vendor_name == ""


def test_negative_qty_allowed_both_but_flagged():
    p = payload(
        items=[{"sku_raw": "WidgetA", "sku": "WidgetA", "quantity": -5, "unit_price": 250.0}]
    )
    assert dc.Invoice(**p).items[0].quantity == -5
    assert pd.Invoice(**p).items[0].quantity == -5


def test_bad_number_rejected_both():
    with pytest.raises(ValueError):
        dc.Invoice(**payload(invoice_number="XYZ"))
    with pytest.raises(ValidationError):
        pd.Invoice(**payload(invoice_number="XYZ"))


def test_confidence_range_rejected_both():
    with pytest.raises(ValueError):
        dc.Invoice(**payload(confidence=1.5))
    with pytest.raises(ValidationError):
        pd.Invoice(**payload(confidence=1.5))


def test_unknown_finding_rejected_both():
    with pytest.raises(ValueError):
        dc.Invoice(**payload(findings=["MADE_UP_CODE"]))
    with pytest.raises(ValidationError):
        pd.Invoice(**payload(findings=["MADE_UP_CODE"]))


def test_finding_suffix_difference():
    """Real differentiator: dataclass tolerates 'CODE: detail', Pydantic Literal does not."""
    assert dc.Invoice(**payload(findings=["REVISION: R1 duplicate"])).findings
    with pytest.raises(ValidationError):
        pd.Invoice(**payload(findings=["REVISION: R1 duplicate"]))


def test_qty_string_coercion_difference():
    """Pydantic lax coerces '6'->6; dataclass rejects. Strictness is a choice."""
    with pytest.raises(ValueError):
        dc.Invoice(
            **payload(items=[{"sku_raw": "W", "sku": "W", "quantity": "six", "unit_price": 1.0}])
        )
    ok = pd.Invoice(
        **payload(items=[{"sku_raw": "W", "sku": "W", "quantity": "6", "unit_price": 1.0}])
    )
    assert ok.items[0].quantity == 6


def test_json_roundtrip_both():
    a = dc.Invoice.from_json(dc.Invoice(**payload()).to_json())
    assert a.invoice_number == "INV-1011" and a.items[0].sku == "WidgetA"
    b = pd.Invoice.model_validate_json(pd.Invoice(**payload()).model_dump_json())
    assert b.invoice_number == "INV-1011" and b.items[0].sku == "WidgetA"
