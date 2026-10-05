"""Typed contract shared by every stage. Severity is defined once, in `SEVERITY`."""

from enum import StrEnum, auto

from pydantic import BaseModel


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
