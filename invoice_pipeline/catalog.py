"""Reference data: Stock Levels, the vendor master, and the USD price list.

The seed values are stated business assumptions (the README gives none): every corpus
vendor is trusted except Fraudster LLC (blocked), NoProd Industries is absent, and the
reference prices are WidgetA 250, WidgetB 500, GadgetX 750.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from invoice_pipeline.model import vendor_key

DEFAULT_INVENTORY_PATH = Path("inventory.db")
TABLES = ("inventory", "vendors", "pricing")

STOCK_LEVELS = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}
PRICES = {"WidgetA": "250", "WidgetB": "500", "GadgetX": "750"}
TRUSTED_VENDORS = (
    "Widgets Inc.",
    "Gadgets Co.",
    "Precision Parts Ltd.",
    "Global Supply Chain Partners",
    "Acme Industrial Supplies",
    "MegaWidgets Corp",
    "QuickShip Distributers",
    "Summit Manufacturing Co.",
    "Consolidated Materials Group",
    "Atlas Industrial Supply",
    "TechParts International",
    "Reliable Components Inc.",
)
BLOCKED_VENDORS = ("Fraudster LLC",)


class KnownVendor(NamedTuple):
    display_name: str
    status: str  # "trusted" | "blocked"


@dataclass(frozen=True)
class Catalog:
    stock: Mapping[str, Decimal]  # canonical sku -> Stock Level
    prices: Mapping[str, Decimal]  # canonical sku -> USD reference unit price
    vendors: Mapping[str, KnownVendor]  # vendor_key -> known vendor

    def resolve_sku(self, sku: str | None) -> str | None:
        """Canonical catalog SKU for a (case-insensitive) SKU, or None when unknown."""
        if not sku:
            return None
        return next(
            (s for s in (*self.stock, *self.prices) if s.casefold() == sku.casefold()), None
        )


def seed(path: Path, reset: bool = False) -> None:
    """Create the schema and pinned data. `reset=True` drops existing tables first."""
    with sqlite3.connect(path) as conn:
        if reset:
            for table in TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                sku TEXT PRIMARY KEY, stock_level INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS vendors (
                name_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('trusted', 'blocked', 'unknown')));
            CREATE TABLE IF NOT EXISTS pricing (sku TEXT PRIMARY KEY, unit_price TEXT NOT NULL);
            """
        )
        conn.executemany("INSERT OR IGNORE INTO inventory VALUES (?, ?)", STOCK_LEVELS.items())
        conn.executemany("INSERT OR IGNORE INTO pricing VALUES (?, ?)", PRICES.items())
        vendors = [(n, "trusted") for n in TRUSTED_VENDORS] + [
            (n, "blocked") for n in BLOCKED_VENDORS
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO vendors VALUES (?, ?, ?)",
            [(vendor_key(n), n, status) for n, status in vendors],
        )
    conn.close()


def load_catalog(path: Path) -> Catalog:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        stock = {sku: Decimal(level) for sku, level in conn.execute("SELECT * FROM inventory")}
        prices = {sku: Decimal(price) for sku, price in conn.execute("SELECT * FROM pricing")}
        vendors = {
            key: KnownVendor(name, status)
            for key, name, status in conn.execute(
                "SELECT name_key, display_name, status FROM vendors"
            )
        }
    finally:
        conn.close()
    return Catalog(stock=stock, prices=prices, vendors=vendors)
