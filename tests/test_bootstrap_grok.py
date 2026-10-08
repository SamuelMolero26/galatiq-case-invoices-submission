"""Slice 2b.2: bootstrap --llm grok wiring (fake env, stub only, no live calls)."""

import argparse
import json

import pytest

from invoice_pipeline import service
from tests.factories import case_file_for, make_invoice

CONCUR = json.dumps(
    {
        "verdict": "concur",
        "evidence": ["invoice.total"],
        "rationale": "total matches the single line total",
    }
)


def args_for(tmp_path, llm):
    return argparse.Namespace(
        llm=llm, ledger=tmp_path / "ledger.db", inventory=tmp_path / "inventory.db"
    )


def clear_grok_env(monkeypatch):
    for key in ("XAI_API_KEY", "GROK_BASE_URL", "XAI_MODEL"):
        monkeypatch.delenv(key, raising=False)


class TestBootstrapGrok:
    def test_grok_without_key_or_base_url_raises_bootstrap_error(
        self, tmp_path, monkeypatch
    ):
        clear_grok_env(monkeypatch)
        with pytest.raises(service.BootstrapError):
            service.bootstrap(args_for(tmp_path, "grok"))

    def test_grok_with_fake_env_builds_online_agents(self, tmp_path, monkeypatch, stub_llm):
        stub_llm.script(stub_llm.reply(CONCUR))
        monkeypatch.setenv("XAI_API_KEY", "fake-key")
        monkeypatch.setenv("GROK_BASE_URL", stub_llm.url)
        rt = service.bootstrap(args_for(tmp_path, "grok"))
        assert rt.tier == "grok"
        call = rt.agents.escalate_review(case_file_for(make_invoice()))
        assert call.answer is not None and call.error is None
        assert call.answer["verdict"] == "concur"
        assert len(stub_llm.requests) == 1

    def test_offline_default_untouched(self, tmp_path, monkeypatch):
        clear_grok_env(monkeypatch)
        rt = service.bootstrap(args_for(tmp_path, None))
        assert rt.tier == "offline"
        call = rt.agents.escalate_review(case_file_for(make_invoice()))
        assert (call.answer, call.error) == (None, "offline tier")

    def test_unknown_tier_still_bootstrap_error(self, tmp_path, monkeypatch):
        clear_grok_env(monkeypatch)
        with pytest.raises(service.BootstrapError):
            service.bootstrap(args_for(tmp_path, "bad-tier"))

    def test_grok_missing_only_key_or_only_base_url_still_raises(
        self, tmp_path, monkeypatch, stub_llm
    ):
        monkeypatch.setenv("XAI_API_KEY", "fake-key")
        monkeypatch.setenv("GROK_BASE_URL", stub_llm.url)
        monkeypatch.delenv("XAI_API_KEY")
        with pytest.raises(service.BootstrapError):
            service.bootstrap(args_for(tmp_path, "grok"))
        monkeypatch.setenv("XAI_API_KEY", "fake-key")
        monkeypatch.delenv("GROK_BASE_URL")
        with pytest.raises(service.BootstrapError):
            service.bootstrap(args_for(tmp_path, "grok"))

    def test_explicit_offline_with_grok_env_stays_offline(
        self, tmp_path, monkeypatch, stub_llm
    ):
        monkeypatch.setenv("XAI_API_KEY", "fake-key")
        monkeypatch.setenv("GROK_BASE_URL", stub_llm.url)
        rt = service.bootstrap(args_for(tmp_path, "offline"))
        assert rt.tier == "offline"
        call = rt.agents.escalate_review(case_file_for(make_invoice()))
        assert (call.answer, call.error) == (None, "offline tier")
        assert stub_llm.requests == []

    def test_auto_grok_with_both_fake_vars_set_selects_grok(
        self, tmp_path, monkeypatch, stub_llm
    ):
        monkeypatch.setenv("XAI_API_KEY", "fake-key")
        monkeypatch.setenv("GROK_BASE_URL", stub_llm.url)
        rt = service.bootstrap(args_for(tmp_path, None))
        assert rt.tier == "grok"
