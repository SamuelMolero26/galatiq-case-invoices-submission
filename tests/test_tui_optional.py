"""The TUI is an optional extra: without Textual, `tui` explains itself and the CLI still works."""

import json
import subprocess
import sys

import pytest
from conftest import SAMPLE_INVOICES

import invoice_pipeline
from invoice_pipeline import cli


@pytest.fixture
def without_textual(monkeypatch):
    """Make `import textual` fail as if the extra were not installed."""
    for name in ["textual", *(m for m in sys.modules if m.startswith("textual."))]:
        monkeypatch.setitem(sys.modules, name, None)  # also hides already-imported submodules
    monkeypatch.delitem(sys.modules, "invoice_pipeline.tui", raising=False)
    monkeypatch.delattr(invoice_pipeline, "tui", raising=False)  # set by an earlier import


def test_tui_without_textual_names_the_extra(batch_ledger, without_textual, capsys):
    code = cli.main(["tui", "--ledger", str(batch_ledger)])

    assert code == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "Textual" in err and "invoice-pipeline[tui]" in err and "--extra tui" in err


def test_batch_and_review_list_work_without_textual(
    tmp_path, batch_ledger, without_textual, capsys
):
    one = SAMPLE_INVOICES / "invoice_1001.txt"
    batch = ["--invoice_path", str(one), "--llm", "offline", "--json"]
    batch += ["--ledger", str(tmp_path / "b.db"), "--inventory", str(tmp_path / "inv.db")]

    assert cli.main(batch) == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(["review", "--list", "--ledger", str(batch_ledger), "--json"]) == cli.EXIT_OK
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(listed) == 8
    assert "invoice_pipeline.tui" not in sys.modules


def test_tui_refuses_a_missing_ledger(tmp_path, capsys):
    code = cli.main(["tui", "--ledger", str(tmp_path / "missing.db")])

    assert code == cli.EXIT_FAILED
    assert "ledger not found" in capsys.readouterr().err
    assert not (tmp_path / "missing.db").exists()


def test_importing_the_cli_does_not_import_textual():
    probe = "import sys, invoice_pipeline.cli; print('textual' in sys.modules)"
    root = SAMPLE_INVOICES.parent.parent
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=root, capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "False"
