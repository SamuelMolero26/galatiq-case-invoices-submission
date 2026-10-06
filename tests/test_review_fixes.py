"""Regression tests for the PR #4 review findings."""

import io

from invoice_pipeline import cli, ledger


def _list_queue(tmp_path, ledger_path):
    out = io.StringIO()
    argv = ["review", "--list", "--ledger", str(ledger_path)]
    code = cli.main([*argv, "--inventory", str(tmp_path / "inventory.db")], out=out)
    return code, out.getvalue()


def test_review_list_on_a_missing_ledger_fails_and_creates_nothing(tmp_path):
    missing = tmp_path / "typo.db"

    code, _ = _list_queue(tmp_path, missing)

    assert code == cli.EXIT_FAILED
    assert not missing.exists()
    assert not (tmp_path / "inventory.db").exists()


def test_review_list_reads_an_existing_ledger_without_seeding_inventory(tmp_path):
    path = tmp_path / "ledger.db"
    ledger.connect(path).close()

    code, output = _list_queue(tmp_path, path)

    assert code == cli.EXIT_OK
    assert output == ""
    assert not (tmp_path / "inventory.db").exists()
