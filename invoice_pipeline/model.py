"""Typed contract shared by every stage. Severity is defined once, in `SEVERITY`."""

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum, auto
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator


class Severity(StrEnum):
    REJECTION_RULE = "rejection_rule"
    REVIEW_TRIGGER = "review_trigger"
    WARNING = "warning"


class FindingCode(StrEnum):
    """Stable Finding codes. Values equal names (e.g. "VENDOR_BLOCKED")."""

    @staticmethod
    def _generate_next_value_(name, start, count, last_values):
        return name

    # Rejection Rules
    VENDOR_BLOCKED = auto()
    ITEM_ZERO_STOCK = auto()
    ITEM_UNKNOWN = auto()
    QUANTITY_INVALID = auto()
    INCOMPLETE_IDENTITY = auto()
    # Review Triggers
    STOCK_SHORTAGE = auto()
    RECONCILIATION_MISMATCH = auto()
    MISSING_REQUIRED_FIELD = auto()
    NONPOSITIVE_TOTAL = auto()
    VENDOR_LOOKALIKE = auto()
    CURRENCY_NO_RATE = auto()
    PARTIAL_IDENTITY = auto()
    REVISION_PAYMENT_DELTA = auto()
    UNREADABLE_DOCUMENT = auto()
    LLM_EXTRACTED = auto()
    # Warnings
    VENDOR_UNKNOWN = auto()
    PRICE_DEVIATION = auto()
    CURRENCY_NON_USD = auto()


# Duplicate Payment is an arrival classification (Decision Duplicate), not a Finding.

_R, _T, _W = Severity.REJECTION_RULE, Severity.REVIEW_TRIGGER, Severity.WARNING
SEVERITY: dict[FindingCode, Severity] = {
    FindingCode.VENDOR_BLOCKED: _R,
    FindingCode.ITEM_ZERO_STOCK: _R,
    FindingCode.ITEM_UNKNOWN: _R,
    FindingCode.QUANTITY_INVALID: _R,
    FindingCode.INCOMPLETE_IDENTITY: _R,
    FindingCode.STOCK_SHORTAGE: _T,
    FindingCode.RECONCILIATION_MISMATCH: _T,
    FindingCode.MISSING_REQUIRED_FIELD: _T,
    FindingCode.NONPOSITIVE_TOTAL: _T,
    FindingCode.VENDOR_LOOKALIKE: _T,
    FindingCode.CURRENCY_NO_RATE: _T,
    FindingCode.PARTIAL_IDENTITY: _T,
    FindingCode.REVISION_PAYMENT_DELTA: _T,
    FindingCode.UNREADABLE_DOCUMENT: _T,
    FindingCode.LLM_EXTRACTED: _T,
    FindingCode.VENDOR_UNKNOWN: _W,
    FindingCode.PRICE_DEVIATION: _W,
    FindingCode.CURRENCY_NON_USD: _W,
}


class Finding(BaseModel, frozen=True):
    code: FindingCode
    severity: Severity
    detail: str
    line: int | None = None  # line-item index when item-scoped


def finding(code: FindingCode, detail: str, line: int | None = None) -> Finding:
    """The only Finding constructor: severity always comes from `SEVERITY`."""
    return Finding(code=code, severity=SEVERITY[code], detail=detail, line=line)


def _no_float(value):
    if isinstance(value, float):
        raise ValueError("money and quantities must be Decimal or str, never float")
    return value


Money = Annotated[Decimal, BeforeValidator(_no_float)]


def vendor_key(name: str | None) -> str | None:
    """Comparison key: trimmed, case-folded, whitespace-collapsed. None when blank."""
    key = " ".join((name or "").split()).casefold()
    return key or None


def normalize_invoice_number(raw: str | None) -> str | None:
    """Canonical `INV-<digits>` when digits are present; otherwise the upper-cased text."""
    text = (raw or "").strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return f"INV-{digits}" if digits else text.upper()


class LineItem(BaseModel):
    raw_name: str
    sku: str | None  # normalized; None when not recognizable
    raw_quantity: str | None  # original token, retained even when non-numeric
    quantity: Money | None  # None/zero/negative/fractional -> QUANTITY_INVALID
    unit_price: Money | None
    line_total: Money | None
    note: str | None = None  # context, never an exemption


class Invoice(BaseModel):
    invoice_number: str | None
    vendor: str | None
    revision: str | None = None
    invoice_date: date | None
    due_date_text: str | None  # raw context only (no deadline is derived)
    payment_terms: str | None  # raw context only
    currency: str  # ISO-4217 upper-case
    items: list[LineItem]
    subtotal: Money | None
    tax: Money | None
    shipping: Money | None
    total: Money | None
    notes: str | None
    po_reference: str | None
    source_path: str
    source_format: Literal["txt", "json", "csv", "xml", "pdf"]
    extracted_fields: list[str] = []  # fields supplied by the Extraction Fallback

    def identity(self) -> tuple[str, str] | None:
        """(vendor_key, normalized number); None unless both parts are present."""
        key, number = vendor_key(self.vendor), normalize_invoice_number(self.invoice_number)
        return (key, number) if key and number else None


class Repair(BaseModel):
    field: str  # "invoice_date", "items[2].line_total"
    raw: str  # "2O26"
    repaired: str  # "2026"


class Ingested(BaseModel):
    invoice: Invoice | None  # None -> Unreadable Document
    findings: list[Finding]
    repairs: list[Repair] = []  # Reviewer evidence only; never in the Case File
    unreadable_reason: str | None = None  # "<step>: <ErrorType>: <message>" when invoice is None
    raw_text: str | None = None  # TXT / PDF text layer; input of the Extraction Fallback
    missing_required: list[Literal["vendor", "invoice_number", "total", "items"]] = []
