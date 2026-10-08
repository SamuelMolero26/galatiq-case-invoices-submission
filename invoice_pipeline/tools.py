"""The Assessor's four read-only exact-lookup tools.

Each tool is a bound-parameter lookup on an indexed key over a read-only connection. There is no
free-text or fuzzy search and nothing writes, so the Assessor can neither reinterpret facts nor
argue that an unknown vendor "is really" a known one.
"""

import datetime as dt
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from invoice_pipeline import ledger
from invoice_pipeline.model import Invoice, ToolCall

MAX_TOOL_CALLS = 8  # per invoice, shared by both Assessor attempts and all tries

_ARGUMENT_TYPES = {"get_reference_price": ("sku", str), "get_stock_level": ("sku", str)}
_ARGUMENT_TYPES |= {"get_vendor_history": ("vendor_key", str), "get_invoice_line": ("n", int)}
_DESCRIPTIONS = {
    "get_reference_price": "USD reference unit price of a SKU (exact key).",
    "get_stock_level": "Stock Level of a SKU (exact key).",
    "get_vendor_history": "The vendor's 10 most recent prior Ledger arrivals (newest first) "
    "and the total count of its prior arrivals (exact vendor key).",
    "get_invoice_line": "Line item n (0-based) of the invoice being assessed.",
}
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": _DESCRIPTIONS[name],
            "parameters": {
                "type": "object",
                "properties": {key: {"type": "integer" if kind is int else "string"}},
                "required": [key],
                "additionalProperties": False,
            },
        },
    }
    for name, (key, kind) in _ARGUMENT_TYPES.items()
]


class ToolError(Exception):
    """A tool call that cannot be answered: unknown name, bad arguments, failure, or budget."""


def open_readonly(path: Path | str) -> sqlite3.Connection:
    """A connection that cannot write: SQLite refuses any change through it."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class ToolRunner:
    """Runs tool calls for one invoice: one shared budget, every call recorded in order.

    `inventory` and `ledger_conn` are read-only connections (`open_readonly`). `run` raises
    `ToolError` for a failed call after recording it; earlier calls stay recorded.
    """

    def __init__(
        self,
        inventory: sqlite3.Connection,
        ledger_conn: sqlite3.Connection,
        invoice: Invoice,
        budget: int = MAX_TOOL_CALLS,
    ):
        self.inventory, self.ledger_conn = inventory, ledger_conn
        self.invoice, self.budget = invoice, budget
        self.calls: list[ToolCall] = []

    def run(self, attempt: int, name: str, arguments: str) -> dict[str, Any]:
        called_at, started = dt.datetime.now(dt.UTC), time.monotonic()
        parsed: dict[str, Any] = {"raw": arguments}
        result, error = None, None
        try:
            if len(self.calls) >= self.budget:
                raise ToolError(f"tool budget of {self.budget} calls exhausted")
            parsed = _parse(name, arguments)
            result = self._lookup(name, **parsed)
        except Exception as exc:
            error = str(exc) if isinstance(exc, ToolError) else f"{type(exc).__name__}: {exc}"
        self.calls.append(
            ToolCall(
                index=len(self.calls),
                attempt=attempt,
                name=name,
                arguments=parsed,
                result=result,
                error=error,
                called_at=called_at,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        )
        if error:
            raise ToolError(error)
        return result

    def _lookup(self, name: str, **args) -> dict[str, Any]:
        if name == "get_reference_price":
            row = self.inventory.execute(
                "SELECT unit_price FROM pricing WHERE sku = ?", (args["sku"],)
            ).fetchone()
            return {"sku": args["sku"], "found": row is not None, "unit_price": row and row[0]}
        if name == "get_stock_level":
            row = self.inventory.execute(
                "SELECT stock_level FROM inventory WHERE sku = ?", (args["sku"],)
            ).fetchone()
            return {
                "sku": args["sku"],
                "found": row is not None,
                "stock_level": row and str(row[0]),
            }
        if name == "get_vendor_history":
            entries, total = ledger.vendor_history(self.ledger_conn, args["vendor_key"])
            return {
                "vendor_key": args["vendor_key"],
                "entries": [e.model_dump(mode="json") for e in entries],
                "total": total,
            }
        n, items = args["n"], self.invoice.items
        found = 0 <= n < len(items)
        return {"n": n, "found": found, "item": items[n].model_dump(mode="json") if found else None}


def _parse(name: str, arguments: str) -> dict[str, Any]:
    if name not in _ARGUMENT_TYPES:
        raise ToolError(f"unknown tool {name!r}")
    key, kind = _ARGUMENT_TYPES[name]
    try:
        parsed = json.loads(arguments)
    except ValueError:
        parsed = None
    value = parsed.get(key) if isinstance(parsed, dict) else None
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {key}
        or not isinstance(value, kind)
        or isinstance(value, bool)
    ):
        raise ToolError(f"malformed arguments for {name}: expected {{{key!r}: {kind.__name__}}}")
    return parsed
