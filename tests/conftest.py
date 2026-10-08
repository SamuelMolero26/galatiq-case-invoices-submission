"""Small shared fixtures for the selected Slice 2C closure tests."""

import builtins
import contextlib
import io
import os
import socket
from decimal import Decimal

import pytest

from invoice_pipeline.catalog import Catalog
from invoice_pipeline.critic import build_case_file
from invoice_pipeline.model import ArrivalSummary, Invoice, LineItem
from invoice_pipeline.validation import validate


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(
        stock={"WidgetA": Decimal(100), "WidgetB": Decimal(100), "GadgetX": Decimal(100)},
        prices={"WidgetA": Decimal(250), "WidgetB": Decimal(500), "GadgetX": Decimal(750)},
        vendors={"acme corp": ("Acme Corp", "trusted")},
    )


def line(sku: str, unit_price: str, quantity: str = "1") -> LineItem:
    price, qty = Decimal(unit_price), Decimal(quantity)
    return LineItem(
        raw_name=sku,
        sku=sku,
        raw_quantity=quantity,
        quantity=qty,
        unit_price=price,
        line_total=price * qty,
    )


def invoice(items: list[LineItem], vendor: str = "Acme Corp", currency: str = "USD") -> Invoice:
    total = sum((i.line_total for i in items), Decimal(0))
    return Invoice(
        invoice_number="INV-1001",
        vendor=vendor,
        invoice_date=None,
        due_date_text=None,
        payment_terms=None,
        currency=currency,
        items=items,
        subtotal=total,
        tax=None,
        shipping=None,
        total=total,
        notes=None,
        po_reference=None,
        source_path="x.txt",
        source_format="txt",
    )


@pytest.fixture
def make_case_file(catalog):
    """Build a Case File from the small catalog without external services."""

    def make(items=(("WidgetA", "300"),)):
        inv = invoice([line(sku, price) for sku, price in items])
        return build_case_file(
            inv,
            validate(inv, catalog),
            ArrivalSummary(kind="new"),
            None,
            catalog,
            [],
            0,
            online=True,
        )

    return make


class ScriptedChat:
    """A fake `chat_fn`: replays scripted replies and records every request it receives."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def __call__(self, tier, messages, tools=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def text_reply(content: str):
    from invoice_pipeline.llm import ChatReply

    return ChatReply(content, [], {"role": "assistant", "content": content}, None)


def tool_reply(name: str, arguments: str, call_id: str = "c1"):
    from invoice_pipeline.llm import ChatReply, ToolRequest

    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }
    return ChatReply(None, [ToolRequest(call_id, name, arguments)], message, None)


@pytest.fixture
def grok():
    from invoice_pipeline.llm import TierConfig

    return TierConfig(tier="grok", model="test-model", base_url="http://llm.invalid", timeout_s=1)


@pytest.fixture
def no_network(monkeypatch):
    """Fail every outbound connection attempt; returns the recorded `(kind, target)` attempts."""
    attempts: list[tuple[str, object]] = []

    def blocked(kind):
        def refuse(*args, **kwargs):
            attempts.append((kind, args[1] if kind == "connect" and len(args) > 1 else args[:1]))
            raise RuntimeError(f"network access blocked in tests: {kind}")

        return refuse

    monkeypatch.setattr(socket.socket, "connect", blocked("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", blocked("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", blocked("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("getaddrinfo"))
    return attempts


@pytest.fixture
def no_fs_access():
    """A context-manager factory: inside `with`, file opens and eval/exec are refused and recorded.

    Scoped to the `with` so pytest's own file handling is never affected.
    """

    @contextlib.contextmanager
    def guard():
        attempts: list[tuple[str, object]] = []

        def blocked(kind):
            def refuse(*args, **kwargs):
                attempts.append((kind, args[:1]))
                raise PermissionError(f"filesystem/eval access blocked in tests: {kind}")

            return refuse

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(builtins, "open", blocked("open"))
            patch.setattr(io, "open", blocked("io.open"))
            patch.setattr(os, "open", blocked("os.open"))
            patch.setattr(builtins, "eval", blocked("eval"))
            patch.setattr(builtins, "exec", blocked("exec"))
            yield attempts

    return guard


@pytest.fixture
def make_case(catalog):
    """Case File with explicit findings/arrival, for exercising a chosen precedence row."""
    from invoice_pipeline.model import FindingCode, finding

    def make(codes=(), kind="new", quantity="1", unit_price="250", vendor="Acme Corp", lines=(0,)):
        inv = invoice([line("WidgetA", unit_price, quantity)], vendor=vendor)
        findings = [finding(FindingCode(code), "scripted", ln) for code in codes for ln in lines]
        arrival = (
            ArrivalSummary(kind="duplicate", duplicate_of=1, paid_to_date=inv.total)
            if kind == "duplicate"
            else ArrivalSummary(kind="new")
        )
        return build_case_file(inv, findings, arrival, None, catalog, [], 0, online=True)

    return make


def concur(evidence=("invoice.vendor",), verdict="concur", rationale="nothing needs a human"):
    import json

    return text_reply(
        json.dumps({"verdict": verdict, "evidence": list(evidence), "rationale": rationale})
    )
