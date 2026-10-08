"""The `ingested` pipeline event carries what the parse produced, for live presentation."""

from conftest import SAMPLE_INVOICES

from invoice_pipeline import catalog, service
from invoice_pipeline.critic import offline_agents


def _events(tmp_path, path):
    inventory = tmp_path / "inventory.db"
    catalog.seed(inventory)
    events = []
    rt = service.Runtime(
        catalog=catalog.load_catalog(inventory),
        ledger_path=tmp_path / "ledger.db",
        agents=offline_agents(),
        pay_fn=lambda *args: {"status": "success"},
        on_event=events.append,
    )
    service.process_path(path, rt)
    return events


def test_ingested_event_summarizes_the_parse(tmp_path):
    events = _events(tmp_path, SAMPLE_INVOICES / "invoice_1001.txt")

    detail = next(e.detail for e in events if e.name == "ingested")

    assert detail["unreadable"] is False
    assert detail["vendor"] and detail["invoice_number"]
    assert isinstance(detail["total"], str) and detail["currency"]
    assert detail["items"] >= 1
    assert isinstance(detail["findings"], list)


def test_ingested_event_explains_an_unreadable_document(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")

    detail = next(e.detail for e in _events(tmp_path, bad) if e.name == "ingested")

    assert detail["unreadable"] is True
    assert detail["reason"]
    assert detail["findings"]
