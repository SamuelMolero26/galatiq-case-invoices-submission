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

from invoice_pipeline.model import vendor_key

DEFAULT_INVENTORY_PATH = Path("inventory.db")
SCHEMA_VERSION = 1
TABLES = ("inventory", "vendors", "pricing")

_SEED_STOCK_LEVELS = {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}
_SEED_PRICES = {"WidgetA": "250", "WidgetB": "500", "GadgetX": "750"}
_SEED_TRUSTED_VENDORS = (
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
_SEED_BLOCKED_VENDORS = ("Fraudster LLC",)


class CatalogError(Exception):
    """The inventory database does not have the expected schema."""


@dataclass(frozen=True)
class Catalog:
    stock: Mapping[str, Decimal]  # canonical sku -> Stock Level
    prices: Mapping[str, Decimal]  # canonical sku -> USD reference unit price
    vendors: Mapping[str, tuple[str, str]]  # vendor_key -> (display name, "trusted" | "blocked")

    def resolve_sku(self, sku: str | None) -> str | None:
        """Canonical catalog SKU for a (case-insensitive) SKU, or None when unknown."""
        if not sku:
            return None
        folded = {s.casefold(): s for s in (*self.stock, *self.prices)}
        return folded.get(sku.casefold())


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
        conn.executemany(
            "INSERT OR IGNORE INTO inventory VALUES (?, ?)", _SEED_STOCK_LEVELS.items()
        )
        conn.executemany("INSERT OR IGNORE INTO pricing VALUES (?, ?)", _SEED_PRICES.items())
        vendors = [(n, "trusted") for n in _SEED_TRUSTED_VENDORS] + [
            (n, "blocked") for n in _SEED_BLOCKED_VENDORS
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO vendors VALUES (?, ?, ?)",
            [(vendor_key(n), n, status) for n, status in vendors],
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.close()


def load_catalog(path: Path) -> Catalog:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in TABLES:
            if table not in existing:
                raise CatalogError(f"{path}: inventory database is missing table '{table}'")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise CatalogError(
                f"{path}: unexpected user_version {version} (expected {SCHEMA_VERSION})"
            )
        stock = {sku: Decimal(level) for sku, level in conn.execute("SELECT * FROM inventory")}
        prices = {sku: Decimal(price) for sku, price in conn.execute("SELECT * FROM pricing")}
        vendors = {
            key: (name, status)
            for key, name, status in conn.execute(
                "SELECT name_key, display_name, status FROM vendors"
            )
        }
    finally:
        conn.close()
    return Catalog(stock=stock, prices=prices, vendors=vendors)
