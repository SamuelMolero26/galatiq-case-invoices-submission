import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from invoice_pipeline import cli, ledger, service

ROOT = Path(__file__).parent.parent
CORPUS = ROOT / "data" / "invoices"


class TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def flags(tmp_path):
    return [f"--ledger={tmp_path / 'ledger.db'}", f"--inventory={tmp_path / 'inventory.db'}"]


def run(argv, out=None):
    out = out or io.StringIO()
    code = cli.main(argv, out=out)
    return code, out.getvalue()


def lines(text):
    return [json.loads(line) for line in text.splitlines()]


def stage_dir(tmp_path, *names, rename=None):
    folder = tmp_path / "batch"
    folder.mkdir()
    for name in names:
        shutil.copy(CORPUS / name, folder / (rename or {}).get(name, name))
    return folder


def ledger_rows(tmp_path):
    conn = ledger.connect(tmp_path / "ledger.db")
    try:
        return [tuple(r) for r in conn.execute("SELECT * FROM arrivals ORDER BY id")]
    finally:
        conn.close()


def test_json_lines_start_with_the_tier_and_end_with_the_counts(flags):
    code, text = run([f"--invoice_path={CORPUS / 'invoice_1001.txt'}", "--llm", "offline", *flags])
    assert code == 0
    events = lines(text)  # every line parses as JSON
    assert events[0] == {"event": "startup", "tier": "offline"}
    assert events[-1] == {"event": "summary", "counts": {"paid": 1}, "failed": 0}
    [arrival] = [e for e in events if e["event"] == "arrival"]
    assert arrival["source"] == "invoice_1001.txt" and arrival["invoice_number"] == "INV-1001"
    assert (arrival["vendor"], arrival["decision"], arrival["state"]) == (
        "Widgets Inc.",
        "approved",
        "paid",
    )
    assert arrival["finding_codes"] == [] and arrival["model_notes"] == "offline tier"
    assert arrival["reasons"] == ["no findings"]
    assert {"ingested", "validated", "decided", "payment_sent"} <= {e["event"] for e in events}


def test_a_directory_runs_in_lexical_order(tmp_path, flags):
    folder = stage_dir(
        tmp_path,
        "invoice_1001.txt",
        "invoice_1003.txt",
        "invoice_1002.txt",
        rename={
            "invoice_1001.txt": "b.txt",
            "invoice_1003.txt": "a_revised.txt",
            "invoice_1002.txt": "a.txt",
        },
    )
    code, text = run([f"--invoice_path={folder}", *flags])
    assert code == 0
    order = [e["source"] for e in lines(text) if e["event"] == "arrival"]
    assert order == ["a.txt", "a_revised.txt", "b.txt"]
    assert lines(text)[-1]["counts"] == {"needs_review": 1, "logged_rejection": 1, "paid": 1}


def test_needs_review_rejected_and_duplicate_outcomes_still_exit_zero(tmp_path, flags):
    folder = stage_dir(tmp_path, "invoice_1011.pdf", "invoice_1011.txt", "invoice_1003.txt")
    code, text = run([f"--invoice_path={folder}", *flags])
    assert code == 0
    assert lines(text)[-1]["counts"] == {"logged_rejection": 1, "paid": 1, "duplicate": 1}


def test_missing_path_exits_nonzero_before_anything_is_written(tmp_path, flags, capsys):
    code, text = run([f"--invoice_path={tmp_path / 'nope.txt'}", *flags])
    assert code == cli.EXIT_FAILED and text == ""
    assert "nope.txt" in capsys.readouterr().err
    assert not (tmp_path / "ledger.db").exists() and not (tmp_path / "inventory.db").exists()


def test_bootstrap_failure_exits_nonzero_without_processing(tmp_path, flags, capsys):
    code, text = run([f"--invoice_path={CORPUS / 'invoice_1001.txt'}", "--llm", "grok", *flags])
    assert code == cli.EXIT_FAILED and text == ""
    assert "grok" in capsys.readouterr().err
    assert ledger_rows(tmp_path) == []


def test_processing_failure_is_reported_on_stderr_and_exits_nonzero(flags, capsys, monkeypatch):
    def interrupted(*args):
        raise RuntimeError("bank adapter crashed")

    monkeypatch.setattr(service.payment, "pay", interrupted)
    code, text = run([f"--invoice_path={CORPUS / 'invoice_1001.txt'}", *flags])
    assert code == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "invoice_1001.txt" in err and "payment" in err and "bank adapter crashed" in err
    assert "1 file(s) failed" in err
    assert lines(text)[-1] == {"event": "summary", "counts": {}, "failed": 1}


def test_usage_errors_exit_two(flags, capsys):
    for argv in ([], ["--bogus"], ["--llm", "bogus", "--invoice_path=x"], ["review"]):
        assert run(argv + flags)[0] == cli.EXIT_USAGE == 2
    assert cli.EXIT_USAGE not in (cli.EXIT_OK, cli.EXIT_FAILED)
    capsys.readouterr()


def test_review_list_is_read_only_and_repeatable(tmp_path, flags):
    folder = stage_dir(tmp_path, "invoice_1001.txt", "invoice_1002.txt", "invoice_1005.json")
    run([f"--invoice_path={folder}", *flags])
    before = ledger_rows(tmp_path)
    code1, first = run(["review", "--list", *flags])
    code2, second = run(["review", "--list", *flags])
    assert (code1, code2) == (0, 0) and first == second
    assert ledger_rows(tmp_path) == before
    items = lines(first)
    assert [i["source"] for i in items] == ["invoice_1002.txt", "invoice_1005.json"]
    assert items[0]["identity"] == "Gadgets Co. / INV-1002" and items[0]["state"] == "needs_review"
    assert items[0]["total"] and items[0]["currency"] == "USD" and items[0]["reasons"]


def test_review_list_marks_missing_identity_components(tmp_path, flags):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    run([f"--invoice_path={bad}", *flags])
    [item] = lines(run(["review", "--list", *flags])[1])
    assert item["identity"] == "<vendor missing> / <invoice number missing>"


def test_the_cli_exposes_no_resolution_or_settlement_command(flags):
    parser = cli.build_parser()
    commands = next(a for a in parser._actions if a.dest == "command").choices
    assert set(commands) == {"review"}
    review_options = {o for a in commands["review"]._actions for o in a.option_strings}
    assert review_options == {
        "-h",
        "--help",
        "--list",
        "--llm",
        "--ledger",
        "--inventory",
        "--json",
    }
    for argv in (["approve", "1"], ["resolve"], ["review", "--list", "--approve", "1"]):
        assert run(argv + flags)[0] == cli.EXIT_USAGE


def test_terminal_output_is_rich_not_json_lines(flags):
    code, text = run([f"--invoice_path={CORPUS / 'invoice_1001.txt'}", *flags], out=TTY())
    assert code == 0 and "offline" in text and "invoice_1001.txt" in text and "paid" in text
    assert not text.lstrip().startswith("{")


def test_json_flag_forces_json_lines_on_a_terminal(flags):
    code, text = run([f"--invoice_path={CORPUS / 'invoice_1001.txt'}", "--json", *flags], out=TTY())
    assert code == 0 and lines(text)[0]["event"] == "startup"


def test_main_py_runs_the_cli_end_to_end(tmp_path):
    done = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            f"--invoice_path={CORPUS / 'invoice_1001.txt'}",
            "--llm",
            "offline",
            f"--ledger={tmp_path / 'ledger.db'}",
            f"--inventory={tmp_path / 'inventory.db'}",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert lines(done.stdout)[-1]["counts"] == {"paid": 1}
