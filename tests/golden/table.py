# ruff: noqa: E501  (derivation strings quote the spec rows)
"""Golden outcome table, transcribed from the cli-runner spec (never from pipeline output).

Source: openspec/changes/invoice-pipeline-v1/specs/cli-runner/spec.md, "Golden outcome table
(offline tier)" and "Golden seed". Each `rule` restates that row's derivation from CONTEXT.md
rules; `approval_row` is the approval-decision precedence row it cites.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from invoice_pipeline.model import FindingCode as F

Slice = Literal["1a", "1b", "3"]


class Final(StrEnum):
    PAID = "Paid"
    NEEDS_REVIEW = "Needs Review"
    LOGGED_REJECTION = "Logged Rejection"
    DUPLICATE = "Duplicate"
    SUPERSEDED = "Superseded"


@dataclass(frozen=True)
class Row:
    n: int
    arrival: str
    vendor: str | None  # as written; None when the document has no vendor
    number: str  # normalized invoice number
    outcome: Final
    approval_row: int  # approval-decision precedence row cited by the derivation
    rule: str
    from_slice: Slice  # first slice that asserts this row
    paid: Decimal | None = None
    codes: frozenset[F] = frozenset()  # Findings the derivation names (must be present)
    clean: bool = False  # derivation states "no Finding"
    duplicate_of: int | None = None
    superseded_by: int | None = None


def _r(n, arrival, vendor, number, outcome, approval_row, rule, from_slice, **kw) -> Row:
    return Row(n, arrival, vendor, number, outcome, approval_row, rule, from_slice, **kw)


P, NR, LR, DUP, SUP = (
    Final.PAID,
    Final.NEEDS_REVIEW,
    Final.LOGGED_REJECTION,
    Final.DUPLICATE,
    Final.SUPERSEDED,
)

ROWS: tuple[Row, ...] = (
    _r(
        1,
        "invoice_1001.txt",
        "Widgets Inc.",
        "INV-1001",
        P,
        6,
        "approval row 6: lines tie out (2500+2500=5000), within Stock, prices at Ref, trusted vendor, no Finding",
        "1a",
        paid=Decimal("5000.00"),
        clean=True,
    ),
    _r(
        2,
        "invoice_1002.txt",
        "Gadgets Co.",
        "INV-1002",
        NR,
        3,
        "approval row 3: GadgetX 20 > non-zero Stock 5 is a Review Trigger; no Rejection Rule",
        "1a",
        codes=frozenset({F.STOCK_SHORTAGE}),
    ),
    _r(
        3,
        "invoice_1003.txt",
        "Fraudster LLC",
        "INV-1003",
        LR,
        2,
        "approval row 2: FakeItem Stock 0 and a blocked vendor are Rejection Rules",
        "1a",
        codes=frozenset({F.ITEM_ZERO_STOCK, F.VENDOR_BLOCKED}),
    ),
    _r(
        4,
        "invoice_1004.json",
        "Precision Parts Ltd.",
        "INV-1004",
        P,
        6,
        "approval row 6: 750+1000=1750, tax 140, total 1890 ties; Stock ok; no Finding",
        "1a",
        paid=Decimal("1890.00"),
        clean=True,
    ),
    _r(
        5,
        "invoice_1004_revised.json",
        "Precision Parts Ltd.",
        "INV-1004",
        NR,
        3,
        "approval row 3: row 4 is Paid and R1 is an explicit revision marker, so REVISION_PAYMENT_DELTA "
        "(Payment Delta 5940-1890 = +4050, paid 1890.00); R1 ties and GadgetX 5 <= Stock 5",
        "3",
        codes=frozenset({F.REVISION_PAYMENT_DELTA}),
    ),
    _r(
        6,
        "invoice_1005.json",
        "Global Supply Chain Partners",
        "INV-1005",
        NR,
        3,
        "approval row 3: GadgetX 8 > Stock 5 is a Review Trigger; Heightened Scrutiny only matters for Warnings",
        "1a",
        codes=frozenset({F.STOCK_SHORTAGE}),
    ),
    _r(
        7,
        "invoice_1006.csv",
        "Acme Industrial Supplies",
        "INV-1006",
        P,
        6,
        "approval row 6: 1250+1500=2750 ties; Stock not consumed by 1001/1004; no Finding",
        "1b",
        paid=Decimal("2750.00"),
        clean=True,
    ),
    _r(
        8,
        "invoice_1007.csv",
        "MegaWidgets Corp",
        "INV-1007",
        NR,
        3,
        "approval row 3: WidgetA 20 > 15 and WidgetB 15 > 10 are Review Triggers",
        "1b",
        codes=frozenset({F.STOCK_SHORTAGE}),
    ),
    _r(
        9,
        "invoice_1008.txt",
        "NoProd Industries",
        "INV-1008",
        LR,
        2,
        "approval row 2: SuperGizmo and MegaSprocket are unknown items (Rejection Rule); "
        "its VENDOR_UNKNOWN Warning is outranked",
        "1a",
        codes=frozenset({F.ITEM_UNKNOWN, F.VENDOR_UNKNOWN}),
    ),
    _r(
        10,
        "invoice_1009.json",
        None,
        "INV-1009",
        LR,
        2,
        "approval row 2: WidgetA qty -5 is QUANTITY_INVALID (Rejection Rule) and outranks the "
        "PARTIAL_IDENTITY Review Trigger (vendor missing, number present; JSON never uses the fallback)",
        "1a",
        codes=frozenset({F.QUANTITY_INVALID, F.PARTIAL_IDENTITY}),
    ),
    _r(
        11,
        "invoice_1010.txt",
        "Consolidated Materials Group",
        "INV-1010",
        NR,
        5,
        "approval row 5: WidgetA 8+4=12 <= 15, ties (6700+335+150=7185); rush line 300 vs Ref 250 = +20% "
        "> 15% is a PRICE_DEVIATION Warning within its 30% Critic Bound, total <= 10,000; offline: Unreviewed Warnings",
        "1a",
        codes=frozenset({F.PRICE_DEVIATION}),
    ),
    _r(
        12,
        "invoice_1011.pdf",
        "Summit Manufacturing Co.",
        "INV-1011",
        P,
        6,
        "approval row 6: 1500+1500=3000 ties with no subtotal or tax; no Finding",
        "1a",
        paid=Decimal("3000.00"),
        clean=True,
    ),
    _r(
        13,
        "invoice_1011.txt",
        "Summit Manufacturing Co.",
        "INV-1011",
        DUP,
        1,
        "approval row 1 (Duplicate Payment): same identity as row 12, which is Paid, and no revision marker",
        "1a",
        duplicate_of=12,
    ),
    _r(
        14,
        "invoice_1012.pdf",
        "QuickShip Distributers",
        "INV-1012",
        P,
        6,
        "approval row 6: after repair I, 3000+3500+3000=9500, tax 475, total 9975 ties; Stock within "
        "15/10/5; total <= 10,000; vendor as written is on the list",
        "1a",
        paid=Decimal("9975.00"),
        clean=True,
    ),
    _r(
        15,
        "invoice_1012.txt",
        "QuickShip Distributers",
        "INV-1012",
        DUP,
        1,
        "approval row 1 (Duplicate Payment): same identity as row 14, which is Paid, and no revision marker",
        "1a",
        duplicate_of=14,
    ),
    _r(
        16,
        "invoice_1013.json",
        "Atlas Industrial Supply",
        "INV-1013",
        SUP,
        3,
        "approval row 3: aggregated WidgetA 22 > 15, WidgetB 18 > 10, GadgetX 9 > 5 (ties 22562.80); "
        "superseded because row 17 is the latest arrival of an unfinished identity",
        "3",
        codes=frozenset({F.STOCK_SHORTAGE}),
        superseded_by=17,
    ),
    _r(
        17,
        "invoice_1013.pdf",
        "Atlas Industrial Supply",
        "INV-1013",
        NR,
        3,
        "approval row 3: row 16 is unfinished with no claimed history, so the latest arrival wins "
        "and carries the same Review Triggers",
        "3",
        codes=frozenset({F.STOCK_SHORTAGE}),
    ),
    _r(
        18,
        "invoice_1014.xml",
        "TechParts International",
        "INV-1014",
        NR,
        5,
        "approval row 5: EUR is a CURRENCY_NON_USD Warning within its Critic Bound; USD Equivalent "
        "4125.00 x 1.08 x 1.05 = 4677.75 <= 10,000; offline: Unreviewed Warnings",
        "3",
        codes=frozenset({F.CURRENCY_NON_USD}),
    ),
    _r(
        19,
        "invoice_1015.csv",
        "Reliable Components Inc.",
        "INV-1015",
        P,
        6,
        "approval row 6: 2500+2500+1500=6500 ties; Stock ok; no Finding",
        "1b",
        paid=Decimal("6500.00"),
        clean=True,
    ),
    _r(
        20,
        "invoice_1016.json",
        "Widgets Inc.",
        "INV-1016",
        LR,
        2,
        "approval row 2: WidgetC is an unknown item (Rejection Rule)",
        "1a",
        codes=frozenset({F.ITEM_UNKNOWN}),
    ),
)


def rows_for(slice_: Slice) -> list[Row]:
    """The rows first asserted by `slice_` (the table's From column)."""
    return [r for r in ROWS if r.from_slice == slice_]
