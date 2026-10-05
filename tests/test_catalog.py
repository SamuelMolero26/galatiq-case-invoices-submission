import sqlite3
from decimal import Decimal

import pytest

from invoice_pipeline.catalog import (
    Catalog,
    CatalogError,
    assert_schema,
    load_catalog,
    seed,
    seed_if_missing,
)


def test_missing_db_is_seeded_with_pinned_assumptions(tmp_path):
    path = tmp_path / "inventory.db"
    seed_if_missing(path)
    catalog = load_catalog(path)
    assert catalog.stock == {
        "WidgetA": Decimal("15"),
        "WidgetB": Decimal("10"),
        "GadgetX": Decimal("5"),
        "FakeItem": Decimal("0"),
    }
    assert catalog.prices == {
        "WidgetA": Decimal("250"),
        "WidgetB": Decimal("500"),
        "GadgetX": Decimal("750"),
    }
    assert catalog.vendors["fraudster llc"].status == "blocked"
    assert catalog.vendors["fraudster llc"].display_name == "Fraudster LLC"
    assert catalog.vendors["widgets inc."].status == "trusted"
    assert "noprod industries" not in catalog.vendors
    assert len(catalog.vendors) == 13
    assert all(v.status == "trusted" for k, v in catalog.vendors.items() if k != "fraudster llc")


def test_existing_db_is_untouched(tmp_path):
    path = tmp_path / "inventory.db"
    seed_if_missing(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE inventory SET stock_level = 99 WHERE sku = 'WidgetA'")
    seed_if_missing(path)
    assert load_catalog(path).stock["WidgetA"] == Decimal("99")


def test_wrong_user_version_fails_fast(tmp_path):
    path = tmp_path / "inventory.db"
    seed_if_missing(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(CatalogError, match="user_version"):
        assert_schema(path)
    with pytest.raises(CatalogError):
        load_catalog(path)


def test_missing_table_is_named(tmp_path):
    path = tmp_path / "inventory.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE inventory (sku TEXT PRIMARY KEY, stock_level INTEGER)")
        conn.execute("PRAGMA user_version = 1")
    with pytest.raises(CatalogError, match="vendors"):
        assert_schema(path)


def test_reset_restores_tampered_database(tmp_path):
    path = tmp_path / "inventory.db"
    seed_if_missing(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE inventory SET stock_level = 99")
        conn.execute("DELETE FROM vendors")
    seed(path, reset=True)
    catalog = load_catalog(path)
    assert catalog.stock["WidgetA"] == Decimal("15")
    assert len(catalog.vendors) == 13
    assert_schema(path)


def test_catalog_is_plain_and_frozen():
    catalog = Catalog(stock={"WidgetA": Decimal("1")}, prices={}, vendors={})
    with pytest.raises(TypeError):
        catalog.stock["WidgetB"] = Decimal("2")
    with pytest.raises(AttributeError):
        catalog.stock = {}
    assert catalog.resolve_sku("widgeta") == "WidgetA"
    assert catalog.resolve_sku("WidgetC") is None
    assert catalog.resolve_sku(None) is None
