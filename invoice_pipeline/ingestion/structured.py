"""Structured-document parsers. Fields are read by key: a missing key is genuinely absent."""

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
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
    vendor = data.get("vendor")
    if isinstance(vendor, dict):
        data = {**data, "vendor": vendor.get("name")}
    return _build(data, data.get("line_items") or [], source_path, "json")


def _build(data: dict, rows: list[dict], source_path: str, source_format: str) -> Ingested:
    """Map a format-neutral field dict (JSON key names) and line rows to a typed Invoice."""
    repairs: list[Repair] = []

    def money(key: str, *aliases: str) -> Decimal | None:
        raw = next((data[k] for k in (key, *aliases) if data.get(k) is not None), None)
        return parse_money(
            raw if raw is None or isinstance(raw, int | Decimal) else str(raw), key, repairs
        )

    items = []
    for n, row in enumerate(rows):
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

    invoice = Invoice(
        invoice_number=normalize_invoice_number(_text(data.get("invoice_number"))),
        vendor=_text(data.get("vendor")),
        revision=_text(data.get("revision")),
        invoice_date=parse_date(_us_date(_text(data.get("date"))), "invoice_date", repairs),
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
        source_format=source_format,
    )
    return Ingested(invoice=invoice, findings=[], repairs=repairs)


def _us_date(text: str | None) -> str | None:
    """MM/DD/YYYY (the CSV spelling) as ISO; any other text is left for parse_date."""
    if text and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", text):
        try:
            return datetime.strptime(text, "%m/%d/%Y").date().isoformat()
        except ValueError:
            return text
    return text


_ROW_COLUMNS = {
    "invoice number": "invoice_number",
    "vendor": "vendor",
    "date": "date",
    "due date": "due_date",
    "item": "item",
    "qty": "quantity",
    "unit price": "unit_price",
    "line total": "amount",
}


def parse_csv(text: str, source_path: str) -> Ingested:
    """CSV in two shapes: field/value rows (repeated item groups) or one row per line item."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("no CSV rows")
    header = [c.strip().lower() for c in rows[0]]
    data: dict = {}
    lines: list[dict] = []
    if header[:2] == ["field", "value"]:
        for row in rows[1:]:
            key, value = row[0].strip().lower(), row[1] if len(row) > 1 else ""
            if key == "item":
                lines.append({"item": value})
            elif key in ("quantity", "unit_price") and lines:
                lines[-1][key] = value
            else:
                data[key] = value
    else:
        cols = {_ROW_COLUMNS[h]: i for i, h in enumerate(header) if h in _ROW_COLUMNS}
        for row in rows[1:]:
            cells = {k: row[i].strip() if i < len(row) else "" for k, i in cols.items()}
            if cells.get("invoice_number"):
                for key in ("invoice_number", "vendor", "date", "due_date"):
                    data.setdefault(key, cells.get(key))
                lines.append(cells)
            else:  # footer row: label in the Unit Price column, amount in Line Total
                label = re.sub(r"\(.*?\)|:", "", cells.get("unit_price", "")).strip().lower()
                if label in ("subtotal", "tax", "total"):
                    data[label] = cells.get("amount")
    return _build(data, lines, source_path, "csv")


def parse_xml(text: str, source_path: str) -> Ingested:
    """Nested XML invoice (header, line_items, totals). Stdlib expat does not fetch external
    entities; a malformed document raises ValueError for the ingestion boundary to catch."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML: {exc}") from exc
    data: dict = {}
    for section in (root, root.find("header"), root.find("totals")):
        for child in section if section is not None else ():
            if len(child) == 0:
                data[child.tag] = child.text
    lines = [
        {
            "item": item.findtext("name"),
            "quantity": item.findtext("quantity"),
            "unit_price": item.findtext("unit_price"),
            "amount": item.findtext("amount"),
            "note": item.findtext("note"),
        }
        for item in root.findall("line_items/item")
    ]
    return _build(data, lines, source_path, "xml")
