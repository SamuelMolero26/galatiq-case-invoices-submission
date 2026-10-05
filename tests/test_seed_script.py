import importlib.util
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from invoice_pipeline.catalog import load_catalog, seed_if_missing

SCRIPT = Path(__file__).parent.parent / "scripts" / "seed_inventory.py"


def load_script():
    spec = importlib.util.spec_from_file_location("seed_inventory", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_exits_zero():
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--inventory" in result.stdout


def test_reset_restores_inventory_and_leaves_ledger_untouched(tmp_path):
    inventory = tmp_path / "inventory.db"
    ledger = tmp_path / "ledger.db"
    ledger.write_bytes(b"payment history that must never be touched")
    before = ledger.read_bytes()
    seed_if_missing(inventory)
    with sqlite3.connect(inventory) as conn:
        conn.execute("UPDATE inventory SET stock_level = 1")
        conn.execute("DELETE FROM pricing")
    assert load_script().main(["--inventory", str(inventory)]) == 0
    catalog = load_catalog(inventory)
    assert catalog.stock["WidgetA"] == Decimal("15")
    assert catalog.prices["GadgetX"] == Decimal("750")
    assert ledger.read_bytes() == before


def test_reset_delegates_to_catalog_seed(tmp_path, monkeypatch):
    calls = []
    module = load_script()
    monkeypatch.setattr(
        module.catalog, "seed", lambda path, reset=False: calls.append((path, reset))
    )
    module.main(["--inventory", str(tmp_path / "inventory.db")])
    assert calls == [(tmp_path / "inventory.db", True)]
