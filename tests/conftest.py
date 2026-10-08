"""Small shared fixtures for the selected Slice 2C closure tests."""

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
