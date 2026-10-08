import datetime as dt
import http.client
import json
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from invoice_pipeline.model import LLMExchange, RoleCall, Try

GROK_TIMEOUT_S = 30  # a remote API answers quickly or not at all
XAI_MODEL_DEFAULT = "grok-4.7"
FORMAT_TRIES = 3  # Correction Wrapper tries per answer
MAX_TOKENS = 1024
MAX_TOOL_ROUNDS = 8  # tool-call rounds per try; bounds any `run_tool`, not just ToolRunner's


class ConfigError(Exception):
    """An explicitly requested tier is not fully configured (stops the command at startup)."""


class LLMError(Exception):
    """Transport, HTTP, timeout, or malformed-response failure of one request."""


class TierConfig(BaseModel, frozen=True):
    """The effective tier. The API key is never serialized, repr'd, or logged."""

    tier: Literal["grok", "offline"]
    model: str | None = None
    base_url: str | None = None
    timeout_s: float | None = None
    api_key: str | None = Field(default=None, exclude=True, repr=False)


def select_tier(requested: str | None, env: Mapping[str, str]) -> TierConfig:
    """Resolve the tier once, from the `--llm` flag and the environment. Pure: no I/O, no probing.

    Endpoint URLs are never hardcoded: the Grok base URL comes from `GROK_BASE_URL`.
    An explicit tier with incomplete configuration raises `ConfigError` (no silent fallback);
    without a flag the order is grok (key and base URL set), offline.
    """
    if requested not in (None, "grok", "offline"):
        raise ConfigError(f"unknown tier {requested!r} (choose grok or offline)")
    key = env.get("XAI_API_KEY") or None
    base_url = env.get("GROK_BASE_URL") or None
    tier = requested or ("grok" if key and base_url else "offline")
    if tier == "grok":
        if not key:
            raise ConfigError("tier grok needs XAI_API_KEY to be set")
        if not base_url:
            raise ConfigError("tier grok needs GROK_BASE_URL to be set")
        return TierConfig(
            tier="grok",
            model=env.get("XAI_MODEL") or XAI_MODEL_DEFAULT,
            base_url=base_url,
            timeout_s=GROK_TIMEOUT_S,
            api_key=key,
        )
    return TierConfig(tier="offline")


@dataclass(frozen=True)
class ToolRequest:
    id: str | None
    name: str
    arguments: str  # JSON text, as the model sent it


@dataclass(frozen=True)
class ChatReply:
    content: str | None
    tool_calls: list[ToolRequest]
    message: dict[str, Any]  # the assistant message, ready to append to the conversation
    usage: dict[str, Any] | None


def chat(tier: TierConfig, messages: list[dict], tools: list[dict] | None = None) -> ChatReply:
    """One chat-completions request. With `tools` (Assessor) function calling is offered and
    JSON mode is not; without them (every other role) the reply is requested as a JSON object."""
    if tier.tier == "offline":
        raise LLMError("offline tier")
    body: dict[str, Any] = {
        "model": tier.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
    }
    if tools:
        body["tools"] = tools
    else:
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if tier.api_key:
        headers["Authorization"] = f"Bearer {tier.api_key}"
    try:
        status, raw = _post(tier, json.dumps(body).encode(), headers)
        if status != 200:
            raise LLMError(f"HTTP {status}: {raw[:200].decode(errors='replace')}")
        payload = json.loads(raw)
        message = payload["choices"][0]["message"]
        calls = [
            ToolRequest(call.get("id"), call["function"]["name"], _arguments(call["function"]))
            for call in message.get("tool_calls") or []
        ]
    except (
        OSError,
        http.client.HTTPException,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,  # `message` or a tool call is not an object
    ) as exc:
        if isinstance(exc, TimeoutError):
            raise LLMError(f"timeout after {tier.timeout_s}s") from exc
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc
    return ChatReply(message.get("content"), calls, message, payload.get("usage"))


_conns = threading.local()  # one keep-alive connection per thread and endpoint


def _post(tier: TierConfig, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
    """POST `body` to the chat endpoint over this thread's connection, opening it when needed.

    A reused connection the server already closed fails before any reply: reconnect once. A
    timeout is never retried.
    """
    url = urllib.parse.urlsplit(f"{tier.base_url.rstrip('/')}/chat/completions")
    open_conn = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    pool = _conns.__dict__.setdefault("pool", {})
    key = (url.scheme, url.netloc, tier.timeout_s)
    for attempt in (1, 2):
        conn = pool.get(key)
        reused = conn is not None
        if conn is None:
            conn = pool[key] = open_conn(url.netloc, timeout=tier.timeout_s)
        try:
            conn.request("POST", url.path + (f"?{url.query}" if url.query else ""), body, headers)
            response = conn.getresponse()
            raw = response.read()
        except Exception as exc:
            conn.close()
            pool.pop(key, None)
            stale = isinstance(exc, ConnectionError | http.client.BadStatusLine)
            if reused and attempt == 1 and stale:
                continue
            raise
        if response.will_close:
            conn.close()
            pool.pop(key, None)
        return response.status, raw
    raise AssertionError("unreachable")


def _arguments(function: dict) -> str:
    arguments = function.get("arguments", "")
    return arguments if isinstance(arguments, str) else json.dumps(arguments)


@dataclass(frozen=True)
class CorrectableError:
    """A detectable format error: the wrapper echoes `message` to the model and asks again."""

    message: str


@dataclass(frozen=True)
class FinalError:
    """An unusable answer that must not be retried."""

    message: str


@dataclass
class Asked[T]:
    """Outcome of one wrapped answer. `value` is None when no usable answer exists."""

    value: T | None
    error: str | None
    tries: list[Try] = field(default_factory=list)
    exhausted: bool = False  # every try failed validation (as opposed to a stop at once)


class _Stop(Exception):
    """The current answer cannot continue (transport or tool failure); never retried."""


def ask[T](
    tier: TierConfig,
    messages: list[dict],
    validate: Callable[[str], T | CorrectableError | FinalError],
    *,
    tools: list[dict] | None = None,
    run_tool: Callable[[str, str], dict] | None = None,
    tries: int = FORMAT_TRIES,
    corrective_role: str = "user",
    chat_fn: Callable[..., ChatReply] = chat,
) -> Asked[T]:
    """The Correction Wrapper: validate each answer and, on a format error, reply with the exact
    failure and ask again, up to `tries` in all. Any other result (a valid value, a
    `FinalError`, a transport or tool failure) stops at once. Never raises.

    `run_tool(name, arguments_json)` answers the model's tool calls inside a try; raising
    stops the answer. Every try, request, and corrective message is recorded.
    """
    conversation = list(messages)
    done: list[Try] = []
    for number in range(1, tries + 1):
        current = Try(exchanges=[])
        done.append(current)
        try:
            reply = _converse(tier, conversation, tools, run_tool, current, chat_fn)
        except _Stop as stop:
            return Asked(None, str(stop), done)
        result = validate(reply.content or "")
        if isinstance(result, FinalError):
            return Asked(None, result.message, done)
        if not isinstance(result, CorrectableError):
            return Asked(result, None, done)
        if number == tries:
            return Asked(
                None, f"answer still invalid after {tries} tries: {result.message}", done, True
            )
        current.correction = result.message
        conversation += [
            {"role": "assistant", "content": reply.content or ""},
            {"role": corrective_role, "content": result.message},
        ]
    raise AssertionError("tries must be at least 1")


def _converse(tier, conversation, tools, run_tool, current: Try, chat_fn) -> ChatReply:
    """Request until the model answers without tool calls; each request is recorded."""
    for _ in range(MAX_TOOL_ROUNDS + 1):  # the final request must answer without tools
        called_at, started = dt.datetime.now(dt.UTC), time.monotonic()
        try:
            reply = chat_fn(tier, conversation, tools)
        except LLMError as exc:
            current.exchanges.append(_exchange(None, str(exc), called_at, started))
            raise _Stop(str(exc)) from exc
        raw = reply.content or json.dumps(reply.message.get("tool_calls"))
        current.exchanges.append(_exchange(raw, None, called_at, started))
        if not reply.tool_calls:
            return reply
        if run_tool is None:
            raise _Stop("tool failure: tool calls requested but this role has no tools")
        if any(not call.id for call in reply.tool_calls):
            raise _Stop("tool failure: tool call without an id cannot be answered")
        conversation.append(reply.message)
        for call in reply.tool_calls:
            try:
                result = run_tool(call.name, call.arguments)
            except Exception as exc:
                raise _Stop(f"tool failure: {exc}") from exc
            conversation.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
            )
    raise _Stop(f"tool failure: still calling tools after {MAX_TOOL_ROUNDS} rounds")


def _exchange(raw: str | None, error: str | None, called_at, started: float) -> LLMExchange:
    elapsed = round((time.monotonic() - started) * 1000)
    return LLMExchange(raw_answer=raw, error=error, called_at=called_at, elapsed_ms=elapsed)


def role_call(role: str, tier: TierConfig, asked: Asked) -> RoleCall:
    """Audit record of a single-call role (answer absent, with its error, when unusable)."""
    value = asked.value
    answer = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return RoleCall(
        role=role,
        tier=tier.tier,
        model=tier.model,
        tries=asked.tries,
        answer=answer,
        error=asked.error,
    )
