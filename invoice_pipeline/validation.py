"""Validation: checks an Invoice against the Catalog and emits Findings. It never decides."""

from collections import defaultdict
from decimal import Decimal

from invoice_pipeline.catalog import Catalog
from invoice_pipeline.model import Finding, FindingCode, Invoice, finding, vendor_key


def validate(invoice: Invoice, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    findings += _identity(invoice)
    findings += _vendor(invoice, catalog)
    findings += _items(invoice, catalog)
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
    return [finding(FindingCode.VENDOR_UNKNOWN, f"vendor '{invoice.vendor}' is not on the list")]


def _valid_quantity(quantity: Decimal | None) -> bool:
    return quantity is not None and quantity > 0 and quantity == quantity.to_integral_value()


def _items(invoice: Invoice, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    aggregated: dict[str, Decimal] = defaultdict(Decimal)
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
        sku = catalog.resolve_sku(item.sku)
        if sku is None:
            name = item.sku or item.raw_name
            if name not in unknown:
                unknown.add(name)
                findings.append(
                    finding(FindingCode.ITEM_UNKNOWN, f"item '{name}' is not in inventory", index)
                )
        elif _valid_quantity(item.quantity):
            aggregated[sku] += item.quantity

    zero_stock = {sku for sku, level in catalog.stock.items() if level == 0}
    reported_zero: set[str] = set()
    for index, item in enumerate(invoice.items):
        sku = catalog.resolve_sku(item.sku)
        if sku in zero_stock and sku not in reported_zero:
            reported_zero.add(sku)
            findings.append(
                finding(FindingCode.ITEM_ZERO_STOCK, f"item '{sku}' has Stock Level 0", index)
            )
    for sku, quantity in aggregated.items():
        level = catalog.stock.get(sku)
        if level is not None and level > 0 and quantity > level:
            findings.append(
                finding(
                    FindingCode.STOCK_SHORTAGE,
                    f"item '{sku}': aggregate quantity {quantity} exceeds Stock Level {level}",
                )
            )
    return findings
