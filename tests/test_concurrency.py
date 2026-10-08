"""Concurrent batches decide exactly as a sequential run does; the model transport reuses its
connection. A revision must never overtake the invoice it revises, so files that share an
identity (or may still gain one) stay in one lane, in file order."""

import re
import threading

from conftest import SAMPLE_INVOICES

from invoice_pipeline import catalog, service
from invoice_pipeline.critic import offline_agents


def _runtime(tmp_path, name):
    inventory = tmp_path / f"{name}-inventory.db"
    catalog.seed(inventory)
    return service.Runtime(
        catalog=catalog.load_catalog(inventory),
        ledger_path=tmp_path / f"{name}-ledger.db",
        agents=offline_agents(),
        pay_fn=lambda *args: {"status": "success"},
    )


def _outcomes(batch):
    """What was decided; arrival numbers named in reasons depend on how lanes interleave."""
    return [
        (r.source, r.state, r.precedence_row, [re.sub(r"#\d+", "#N", x) for x in r.reasons])
        for r in batch.results
    ]


def test_concurrent_batch_matches_sequential(tmp_path):
    paths = service.collect_files(SAMPLE_INVOICES)
    sequential = service.run_batch(paths, _runtime(tmp_path, "seq"))
    concurrent = service.run_batch(paths, _runtime(tmp_path, "par"), workers=4)
    assert not concurrent.failed
    assert _outcomes(concurrent) == _outcomes(sequential)


def test_concurrent_batch_reports_each_file_once(tmp_path):
    paths = service.collect_files(SAMPLE_INVOICES)
    started, done, lock = [], [], threading.Lock()

    def on_start(name):
        with lock:
            started.append(name)

    def on_done(name, batch):
        with lock:
            done.append(name)

    service.run_batch(
        paths, _runtime(tmp_path, "cb"), workers=4, on_start=on_start, on_done=on_done
    )
    names = sorted(p.name for p in paths)
    assert sorted(started) == names
    assert sorted(done) == names


def test_default_workers_reads_the_environment(monkeypatch):
    monkeypatch.delenv("INVOICE_WORKERS", raising=False)
    assert service.default_workers() == 4
    monkeypatch.setenv("INVOICE_WORKERS", "8")
    assert service.default_workers() == 8
    for bad in ("0", "-2", "many", ""):
        monkeypatch.setenv("INVOICE_WORKERS", bad)
        assert service.default_workers() == 4
