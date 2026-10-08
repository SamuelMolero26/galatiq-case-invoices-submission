import json
import socket
import time

import pytest

from invoice_pipeline.llm import (
    GROK_TIMEOUT_S,
    XAI_MODEL_DEFAULT,
    ConfigError,
    LLMError,
    TierConfig,
    chat,
    select_tier,
)

MESSAGES = [{"role": "user", "content": "hi"}]
BASE_URL = "https://grok.example/v1"
GROK_ENV = {"XAI_API_KEY": "k", "GROK_BASE_URL": BASE_URL}
TOOLS = [{"type": "function", "function": {"name": "get_stock_level", "parameters": {}}}]


def grok(stub, api_key: str | None = "sekret-key", timeout_s: float = 5) -> TierConfig:
    return TierConfig(
        tier="grok", model="grok-x", base_url=stub.url, timeout_s=timeout_s, api_key=api_key
    )


class TestSelectTier:
    def test_nothing_configured_is_offline(self):
        assert select_tier(None, {}).tier == "offline"

    def test_explicit_grok_without_key_names_the_variable(self):
        with pytest.raises(ConfigError, match="XAI_API_KEY"):
            select_tier("grok", {"GROK_BASE_URL": BASE_URL})

    def test_explicit_grok_without_base_url_names_the_variable(self):
        with pytest.raises(ConfigError, match="GROK_BASE_URL"):
            select_tier("grok", {"XAI_API_KEY": "k"})

    def test_unknown_tier_is_a_config_error(self):
        with pytest.raises(ConfigError, match="gemini"):
            select_tier("gemini", {})

    def test_automatic_order_is_grok_then_offline(self):
        assert select_tier(None, GROK_ENV).tier == "grok"
        assert select_tier(None, {"XAI_API_KEY": "k"}).tier == "offline"
        assert select_tier(None, {"GROK_BASE_URL": BASE_URL}).tier == "offline"
        assert select_tier(None, {"XAI_API_KEY": "", "GROK_BASE_URL": BASE_URL}).tier == "offline"

    def test_explicit_offline_ignores_configuration(self):
        assert select_tier("offline", GROK_ENV).tier == "offline"

    def test_grok_defaults_and_overrides(self):
        tier = select_tier("grok", GROK_ENV)
        assert (tier.base_url, tier.model, tier.timeout_s) == (
            BASE_URL,
            XAI_MODEL_DEFAULT,
            GROK_TIMEOUT_S,
        )
        assert XAI_MODEL_DEFAULT == "grok-4.7"
        assert select_tier("grok", {**GROK_ENV, "XAI_MODEL": "g2"}).model == "g2"

    def test_serialization_has_tier_model_base_timeout_and_no_key(self):
        tier = select_tier("grok", {**GROK_ENV, "XAI_API_KEY": "sekret-key"})
        dumped = tier.model_dump_json()
        assert json.loads(dumped) == {
            "tier": "grok",
            "model": XAI_MODEL_DEFAULT,
            "base_url": BASE_URL,
            "timeout_s": GROK_TIMEOUT_S,
        }
        assert "sekret-key" not in dumped + repr(tier) + str(tier)


class TestChat:
    def test_posts_model_messages_and_bounds_to_the_configured_base(self, stub_llm):
        stub_llm.script(stub_llm.reply('{"ok": true}', usage={"total_tokens": 7}))
        reply = chat(grok(stub_llm), MESSAGES)
        request = stub_llm.requests[0]
        assert request["path"] == "/v1/chat/completions"
        assert request["body"]["model"] == "grok-x"
        assert request["body"]["messages"] == MESSAGES
        assert request["body"]["temperature"] == 0
        assert request["body"]["max_tokens"] > 0
        assert (reply.content, reply.tool_calls, reply.usage) == (
            '{"ok": true}',
            [],
            {"total_tokens": 7},
        )

    def test_json_only_role_asks_for_json_and_sends_no_tools(self, stub_llm):
        stub_llm.script(stub_llm.reply("{}"))
        chat(grok(stub_llm), MESSAGES)
        body = stub_llm.requests[0]["body"]
        assert body["response_format"] == {"type": "json_object"}
        assert "tools" not in body

    def test_assessor_request_carries_tools_and_no_response_format(self, stub_llm):
        stub_llm.script(stub_llm.reply("{}"))
        chat(grok(stub_llm), MESSAGES, tools=TOOLS)
        body = stub_llm.requests[0]["body"]
        assert body["tools"] == TOOLS
        assert "response_format" not in body

    def test_tool_calls_are_parsed(self, stub_llm):
        stub_llm.script(
            stub_llm.reply(None, tool_calls=[("get_stock_level", '{"sku": "WidgetA"}')])
        )
        reply = chat(grok(stub_llm), MESSAGES, tools=TOOLS)
        (call,) = reply.tool_calls
        assert (call.id, call.name, call.arguments) == (
            "call_0",
            "get_stock_level",
            '{"sku": "WidgetA"}',
        )
        assert reply.message["tool_calls"][0]["id"] == "call_0"

    def test_object_arguments_are_normalized_to_json_text(self, stub_llm):
        body = stub_llm.reply(None, tool_calls=[("get_stock_level", "")])
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = {"sku": "A"}
        stub_llm.script(body)
        (call,) = chat(grok(stub_llm), MESSAGES, tools=TOOLS).tool_calls
        assert json.loads(call.arguments) == {"sku": "A"}

    def test_bearer_header_is_sent_only_when_a_key_is_set(self, stub_llm):
        stub_llm.script(stub_llm.reply("{}"), stub_llm.reply("{}"))
        chat(grok(stub_llm), MESSAGES)
        chat(grok(stub_llm, api_key=None), MESSAGES)
        keyed_headers, keyless_headers = (r["headers"] for r in stub_llm.requests)
        assert keyed_headers["authorization"] == "Bearer sekret-key"
        assert "authorization" not in keyless_headers

    def test_offline_tier_makes_no_request(self, no_network):
        with pytest.raises(LLMError, match="offline tier"):
            chat(TierConfig(tier="offline"), MESSAGES)

    def test_http_failure_raises_once_without_retry(self, stub_llm):
        stub_llm.script(("status", 500, "boom"), stub_llm.reply("{}"))
        with pytest.raises(LLMError, match="HTTP 500"):
            chat(grok(stub_llm), MESSAGES)
        assert len(stub_llm.requests) == 1

    def test_malformed_body_is_an_llm_error(self, stub_llm):
        stub_llm.script({"unexpected": True})
        with pytest.raises(LLMError):
            chat(grok(stub_llm), MESSAGES)

    def test_null_message_is_an_llm_error(self, stub_llm):
        stub_llm.script({"choices": [{"message": None}]})
        with pytest.raises(LLMError):
            chat(grok(stub_llm), MESSAGES)

    def test_timeout_raises_within_the_tier_timeout_and_is_not_retried(self, stub_llm):
        stub_llm.script(("hang", 0.8))
        tier = grok(stub_llm, timeout_s=0.3)
        started = time.monotonic()
        with pytest.raises(LLMError, match="timeout"):
            chat(tier, MESSAGES)
        assert time.monotonic() - started < 0.75
        assert len(stub_llm.requests) == 1

    def test_refused_connection_is_an_llm_error(self, loopback_only):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        tier = TierConfig(
            tier="grok", model="m", base_url=f"http://127.0.0.1:{port}/v1", timeout_s=2
        )
        with pytest.raises(LLMError):
            chat(tier, MESSAGES)
