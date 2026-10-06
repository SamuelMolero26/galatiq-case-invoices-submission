"""Structured-document parsers. Fields are read by key: a missing key is genuinely absent."""

import json
from decimal import Decimal

from invoice_pipeline.ingestion.normalize import (
    clean_text,
    normalize_sku,
    parse_date,
    parse_money,
    parse_quantity,
)
from invoice_pipeline.model import Ingested, Invoice, LineItem, Repair, normalize_invoice_number


def _text(value) -> str | None:
    return None if value is None else clean_text(str(value))


def parse_json(text: str, source_path: str) -> Ingested:
    """Nested JSON invoice; raises ValueError on invalid JSON (the ingestion boundary catches)."""
    data = json.loads(text, parse_float=Decimal)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object at the top level")
    repairs: list[Repair] = []

    def money(key: str, *aliases: str) -> Decimal | None:
        raw = next((data[k] for k in (key, *aliases) if data.get(k) is not None), None)
        return parse_money(
            raw if raw is None or isinstance(raw, int | Decimal) else str(raw), key, repairs
        )

    items = []
    for n, row in enumerate(data.get("line_items") or []):
        sku, note = normalize_sku(_text(row.get("item")))
        notes = [x for x in (note, _text(row.get("note"))) if x]
        quantity = row.get("quantity")
        items.append(
            LineItem(
                raw_name=_text(row.get("item")) or "",
                sku=sku,
                raw_quantity=None if quantity is None else str(quantity),
                quantity=parse_quantity(
                    quantity if isinstance(quantity, int | Decimal) else _text(quantity)
                ),
                unit_price=parse_money(row.get("unit_price"), f"items[{n}].unit_price", repairs),
                line_total=parse_money(row.get("amount"), f"items[{n}].line_total", repairs),
                note="; ".join(notes) or None,
            )
        )

    vendor = data.get("vendor")
    invoice = Invoice(
        invoice_number=normalize_invoice_number(_text(data.get("invoice_number"))),
        vendor=_text(vendor.get("name") if isinstance(vendor, dict) else vendor),
        revision=_text(data.get("revision")),
        invoice_date=parse_date(_text(data.get("date")), "invoice_date", repairs),
        due_date_text=_text(data.get("due_date")),
        payment_terms=_text(data.get("payment_terms")),
        currency=(_text(data.get("currency")) or "USD").upper(),
        items=items,
        subtotal=money("subtotal"),
        tax=money("tax_amount", "tax"),
        shipping=money("shipping"),
        total=money("total"),
        notes=_text(data.get("notes")),
        po_reference=_text(data.get("po_reference") or data.get("po_number")),
        source_path=source_path,
        source_format="json",
    )
    return Ingested(invoice=invoice, findings=[], repairs=repairs)
