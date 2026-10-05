"""Validation: checks an Invoice against the Catalog and emits Findings. It never decides."""

import re
from collections import defaultdict
from decimal import Decimal
from difflib import SequenceMatcher

from invoice_pipeline.catalog import Catalog
from invoice_pipeline.model import Finding, FindingCode, Invoice, finding, vendor_key

PRICE_TOLERANCE = Decimal("0.15")  # the single price-tolerance constant
VENDOR_LOOKALIKE_THRESHOLD = 0.85  # SequenceMatcher ratio at or above this is a lookalike
_COMPANY_SUFFIXES = {
    "inc", "incorporated", "co", "company", "corp", "corporation", "llc", "ltd", "limited",
}  # fmt: skip


def validate(invoice: Invoice, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    findings += _identity(invoice)
    findings += _vendor(invoice, catalog)
    findings += _items(invoice, catalog)
    findings += _payable(invoice)
    findings += reconcile(invoice)[0]
    findings += _prices(invoice, catalog)
    findings += _currency(invoice)
    return findings


def _identity(invoice: Invoice) -> list[Finding]:
    has_vendor = vendor_key(invoice.vendor) is not None
    has_number = bool((invoice.invoice_number or "").strip())
    if not has_vendor and not has_number:
        return [finding(FindingCode.INCOMPLETE_IDENTITY, "vendor and invoice number missing")]
    if not has_vendor:
        return [finding(FindingCode.PARTIAL_IDENTITY, "vendor missing")]
    if not has_number:
        return [finding(FindingCode.PARTIAL_IDENTITY, "invoice number missing")]
    return []


def _vendor(invoice: Invoice, catalog: Catalog) -> list[Finding]:
    key = vendor_key(invoice.vendor)
    if key is None:
        return []  # a missing vendor is an identity Finding, never VENDOR_UNKNOWN
    known = catalog.vendors.get(key)
    if known is not None and known.status == "blocked":
        return [finding(FindingCode.VENDOR_BLOCKED, f"vendor '{invoice.vendor}' is blocked")]
    if known is not None and known.status == "trusted":
        return []
    findings = [
        finding(FindingCode.VENDOR_UNKNOWN, f"vendor '{invoice.vendor}' is not on the list")
    ]
    if (lookalike := vendor_lookalike(invoice.vendor, catalog)) is not None:
        findings.append(lookalike)
    return findings


def _comparison_name(name: str) -> str:
    """Lower-cased, punctuation removed, whitespace collapsed, trailing company suffixes dropped."""
    tokens = re.sub(r"[^\w\s]", "", name.casefold()).split()
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def vendor_lookalike(vendor: str | None, catalog: Catalog) -> Finding | None:
    """Review Trigger when a not-exactly-known vendor resembles a trusted or blocked one."""
    key = vendor_key(vendor)
    if key is None or key in catalog.vendors and catalog.vendors[key].status != "unknown":
        return None
    name = _comparison_name(vendor)
    if not name:
        return None
    scored = sorted(
        (
            -SequenceMatcher(None, name, _comparison_name(known.display_name)).ratio(),
            known.display_name,
        )
        for known in catalog.vendors.values()
        if known.status in ("trusted", "blocked") and _comparison_name(known.display_name)
    )
    if not scored or -scored[0][0] < VENDOR_LOOKALIKE_THRESHOLD:
        return None
    return finding(
        FindingCode.VENDOR_LOOKALIKE,
        f"resembles known vendor '{scored[0][1]}' (score {-scored[0][0]:.2f})",
    )


def _valid_quantity(quantity: Decimal | None) -> bool:
    return quantity is not None and quantity > 0 and quantity == quantity.to_integral_value()


def aggregate_quantities(invoice: Invoice, catalog: Catalog) -> dict[str, Decimal]:
    """Valid quantities summed per canonical SKU; invalid quantities are never invented."""
    aggregated: dict[str, Decimal] = defaultdict(Decimal)
    for item in invoice.items:
        sku = catalog.resolve_sku(item.sku)
        if sku is not None and _valid_quantity(item.quantity):
            aggregated[sku] += item.quantity
    return dict(aggregated)


def _items(invoice: Invoice, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    unknown: set[str] = set()
    for index, item in enumerate(invoice.items):
        if not _valid_quantity(item.quantity):
            token = item.raw_quantity if item.raw_quantity is not None else "missing"
            findings.append(
                finding(
                    FindingCode.QUANTITY_INVALID,
                    f"quantity '{token}' is not a positive whole number",
                    line=index,
                )
            )
        if catalog.resolve_sku(item.sku) is None:
            name = item.sku or item.raw_name
            if name not in unknown:
                unknown.add(name)
                findings.append(
                    finding(FindingCode.ITEM_UNKNOWN, f"item '{name}' is not in inventory", index)
                )

    reported_zero: set[str] = set()
    for index, item in enumerate(invoice.items):
        sku = catalog.resolve_sku(item.sku)
        if sku is not None and catalog.stock.get(sku) == 0 and sku not in reported_zero:
            reported_zero.add(sku)
            findings.append(
                finding(FindingCode.ITEM_ZERO_STOCK, f"item '{sku}' has Stock Level 0", index)
            )
    for sku, quantity in aggregate_quantities(invoice, catalog).items():
        level = catalog.stock.get(sku)
        if level is not None and level > 0 and quantity > level:
            findings.append(
                finding(
                    FindingCode.STOCK_SHORTAGE,
                    f"item '{sku}': aggregate quantity {quantity} exceeds Stock Level {level}",
                )
            )
    return findings


def _m(amount: Decimal) -> str:
    return f"{amount:.2f}"


def _payable(invoice: Invoice) -> list[Finding]:
    """Missing or nonpositive payable amounts fail closed; nothing is derived or guessed."""
    findings: list[Finding] = []
    if invoice.total is None:
        findings.append(finding(FindingCode.MISSING_REQUIRED_FIELD, "invoice total missing"))
    elif invoice.total <= 0:
        findings.append(
            finding(
                FindingCode.NONPOSITIVE_TOTAL, f"stated total {_m(invoice.total)} cannot be paid"
            )
        )
    for index, item in enumerate(invoice.items):
        if item.unit_price is None:
            findings.append(
                finding(FindingCode.MISSING_REQUIRED_FIELD, "unit price missing", line=index)
            )
    return findings


def reconcile(invoice: Invoice) -> tuple[list[Finding], list[str]]:
    """Exact Reconciliation: (findings, notes). Notes record derived or unverifiable amounts."""
    findings: list[Finding] = []
    notes: list[str] = []
    amounts: list[Decimal] = []
    derived: list[str] = []
    verifiable = True
    for index, item in enumerate(invoice.items):
        if item.quantity is None or item.unit_price is None:
            verifiable = False
            notes.append(f"line {index}: amount cannot be known; tie-outs not verifiable")
            continue
        expected = item.quantity * item.unit_price
        if item.line_total is None:
            derived.append(f"line {index} amount derived: {_m(expected)}")
            notes.append(derived[-1])
            amounts.append(expected)
            continue
        amounts.append(item.line_total)
        if item.line_total != expected:
            findings.append(
                finding(
                    FindingCode.RECONCILIATION_MISMATCH,
                    f"line {index}: {item.quantity} x {_m(item.unit_price)} = {_m(expected)} "
                    f"but stated line amount is {_m(item.line_total)}",
                    line=index,
                )
            )
    if not verifiable:
        return findings, notes
    suffix = f" ({'; '.join(derived)})" if derived else ""
    lines_sum = sum(amounts, Decimal(0))
    if invoice.subtotal is not None and lines_sum != invoice.subtotal:
        findings.append(
            finding(
                FindingCode.RECONCILIATION_MISMATCH,
                f"subtotal: lines sum to {_m(lines_sum)} but stated subtotal is "
                f"{_m(invoice.subtotal)}{suffix}",
            )
        )
    if invoice.total is not None:
        base = invoice.subtotal if invoice.subtotal is not None else lines_sum
        tax, shipping = invoice.tax or Decimal(0), invoice.shipping or Decimal(0)
        expected_total = base + tax + shipping
        if expected_total != invoice.total:
            findings.append(
                finding(
                    FindingCode.RECONCILIATION_MISMATCH,
                    f"total: {_m(base)} + tax {_m(tax)} + shipping {_m(shipping)} = "
                    f"{_m(expected_total)} but stated total is {_m(invoice.total)}{suffix}",
                )
            )
    return findings, notes


def _signed_deviations(invoice: Invoice, catalog: Catalog) -> dict[int, tuple[Decimal, Decimal]]:
    """line index -> (signed deviation, reference price) for USD lines with a reference price."""
    if invoice.currency != "USD":
        return {}  # no conversion is ever performed, so non-USD lines are not compared
    result = {}
    for index, item in enumerate(invoice.items):
        sku = catalog.resolve_sku(item.sku)
        reference = catalog.prices.get(sku) if sku else None
        if reference and item.unit_price is not None:
            result[index] = ((item.unit_price - reference) / reference, reference)
    return result


def price_deviations(invoice: Invoice, catalog: Catalog) -> dict[int, Decimal]:
    """line index -> absolute deviation for lines beyond tolerance (Case File references)."""
    return {
        index: abs(deviation)
        for index, (deviation, _) in _signed_deviations(invoice, catalog).items()
        if abs(deviation) > PRICE_TOLERANCE
    }


def _prices(invoice: Invoice, catalog: Catalog) -> list[Finding]:
    signed = _signed_deviations(invoice, catalog)
    return [
        finding(
            FindingCode.PRICE_DEVIATION,
            f"unit price {_m(invoice.items[index].unit_price)} "
            f"vs reference {_m(signed[index][1])}: "
            f"{signed[index][0] * 100:+.2f}%, tolerance {PRICE_TOLERANCE * 100:.0f}%",
            line=index,
        )
        for index in price_deviations(invoice, catalog)
    ]


def _currency(invoice: Invoice) -> list[Finding]:
    """Slice 1 fails closed on any non-USD invoice; Reference Rates arrive in slice 3."""
    if invoice.currency == "USD":
        return []
    return [
        finding(FindingCode.CURRENCY_NON_USD, f"invoice currency is {invoice.currency}"),
        finding(
            FindingCode.CURRENCY_NO_RATE,
            f"currency not supported yet: no reference rate for {invoice.currency}",
        ),
    ]
