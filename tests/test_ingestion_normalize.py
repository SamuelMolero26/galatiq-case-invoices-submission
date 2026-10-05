from datetime import date
from decimal import Decimal

import pytest

from invoice_pipeline.ingestion import normalize as n
from invoice_pipeline.model import Repair


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("$3,500.00", "3500.00"),
        ("€4,125", "4125.00"),
        ("  $250 ", "250.00"),
        ("1,472.8", "1472.80"),
        ("0.005", "0.01"),  # ROUND_HALF_UP to cents
        ("-250.00", "-250.00"),
    ],
)
def test_money_cleans_symbols_and_commas_without_repair(raw, expected):
    repairs: list[Repair] = []
    value = n.parse_money(raw, "total", repairs)
    assert value == Decimal(expected)
    assert isinstance(value, Decimal)
    assert repairs == []


def test_money_repairs_letter_o_in_a_digit_position_and_records_it():
    repairs: list[Repair] = []
    assert n.parse_money("$3,500.O0", "items[1].line_total", repairs) == Decimal("3500.00")
    assert repairs == [Repair(field="items[1].line_total", raw="$3,500.O0", repaired="3500.00")]


@pytest.mark.parametrize("raw", ["", "   ", "N/A", "abc", "$", "1.2.3"])
def test_money_unparseable_is_absent_and_leaves_no_repair(raw):
    repairs: list[Repair] = []
    assert n.parse_money(raw, "total", repairs) is None
    assert repairs == []


def test_money_accepts_decimal_and_int_but_never_float():
    assert n.parse_money(Decimal("12.5"), "tax", []) == Decimal("12.50")
    assert n.parse_money(3, "tax", []) == Decimal("3.00")
    with pytest.raises(TypeError):
        n.parse_money(1.5, "tax", [])


def test_date_repairs_letter_o_and_records_it():
    repairs: list[Repair] = []
    assert n.parse_date("26-Jan-2O26", "invoice_date", repairs) == date(2026, 1, 26)
    assert repairs == [Repair(field="invoice_date", raw="26-Jan-2O26", repaired="26-Jan-2026")]


@pytest.mark.parametrize(
    "raw",
    ["2026-01-15", "Jan 15 2026", "January 15, 2026", "15-Jan-2026", "Jan 15, 2026"],
)
def test_date_formats_parse_without_repair(raw):
    repairs: list[Repair] = []
    assert n.parse_date(raw, "invoice_date", repairs) == date(2026, 1, 15)
    assert repairs == []


def test_month_names_containing_o_are_not_mangled():
    repairs: list[Repair] = []
    assert n.parse_date("30-Oct-2026", "d", repairs) == date(2026, 10, 30)
    assert n.parse_date("October 30, 2026", "d", repairs) == date(2026, 10, 30)
    assert repairs == []


@pytest.mark.parametrize("raw", ["yesterday", "immediately", "", None, "2026-13-45"])
def test_unparseable_date_is_absent(raw):
    assert n.parse_date(raw, "due", []) is None


@pytest.mark.parametrize(
    "raw, expected",
    [("10", "10"), ("-5", "-5"), ("2.5", "2.5"), (7, "7"), (Decimal("3"), "3")],
)
def test_quantity_parses_numeric_tokens_exactly(raw, expected):
    assert n.parse_quantity(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["oneO", "", None, "12x", "$5"])
def test_quantity_never_guesses(raw):
    assert n.parse_quantity(raw) is None


@pytest.mark.parametrize(
    "raw, sku, note",
    [
        ("WidgetA", "WidgetA", None),
        ("Widget A", "WidgetA", None),
        ("widget a", "WidgetA", None),
        ("  Gadget   X ", "GadgetX", None),
        ("WidgetA (rush order)", "WidgetA", "rush order"),
        ("Widget A  ( rush order ) ", "WidgetA", "rush order"),
        ("", None, None),
        ("(only a note)", None, "only a note"),
    ],
)
def test_sku_normalization_and_annotation_move(raw, sku, note):
    assert n.normalize_sku(raw) == (sku, note)


@pytest.mark.parametrize(
    "raw, expected",
    [("INV-1001", "INV-1001"), ("1002", "INV-1002"), ("INV 1012", "INV-1012"), ("", None)],
)
def test_invoice_number_spellings_converge(raw, expected):
    assert n.normalize_invoice_number(raw) == expected


def test_vendor_display_is_preserved_and_key_converges():
    assert n.clean_text("  ACME  co. ") == "ACME  co."
    assert n.clean_text("  \t ") is None
    assert n.clean_text(None) is None
    assert n.vendor_key("Acme Co.") == n.vendor_key("  ACME  co. ")
