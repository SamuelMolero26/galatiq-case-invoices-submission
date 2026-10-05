"""Shape checks for the source-derived golden table (no pipeline is run here)."""

from decimal import Decimal
from pathlib import Path

from tests.golden import ROWS, Final, rows_for

CORPUS = Path(__file__).parent.parent / "data" / "invoices"

# Stated independently in cli-runner "Final Ledger state"; the table must agree with them.
SPEC_FINAL = {
    Final.PAID: [1, 4, 7, 12, 14, 19],
    Final.LOGGED_REJECTION: [3, 9, 10, 20],
    Final.DUPLICATE: [13, 15],
    Final.NEEDS_REVIEW: [2, 5, 6, 8, 11, 17, 18],
    Final.SUPERSEDED: [16],
}
# approval-decision precedence rows that can produce each final state
ROW_FOR = {
    Final.PAID: {6},
    Final.LOGGED_REJECTION: {2},
    Final.DUPLICATE: {1},
    Final.NEEDS_REVIEW: {3, 5},
    Final.SUPERSEDED: {3},  # decided Needs Review, then a later arrival supersedes it
}


def of(outcome: Final) -> list[int]:
    return [r.n for r in ROWS if r.outcome is outcome]


def test_twenty_unique_rows_in_corpus_arrival_order():
    assert [r.n for r in ROWS] == list(range(1, 21))
    assert len({r.arrival for r in ROWS}) == 20
    assert [r.arrival for r in ROWS] == sorted(p.name for p in CORPUS.iterdir())


def test_final_counts_match_the_spec_table():
    for outcome, arrivals in SPEC_FINAL.items():
        assert of(outcome) == arrivals, outcome
    assert len(ROWS) == sum(len(v) for v in SPEC_FINAL.values()) == 20
    assert sum(1 for r in ROWS if r.outcome is Final.PAID) == 6
    assert sum(1 for r in ROWS if r.outcome is Final.NEEDS_REVIEW) == 7


def test_four_logged_rejections_and_two_duplicates():
    assert of(Final.LOGGED_REJECTION) == [3, 9, 10, 20]
    assert of(Final.DUPLICATE) == [13, 15]


def test_every_row_cites_a_source_rule_consistent_with_its_outcome():
    for r in ROWS:
        assert f"row {r.approval_row}" in r.rule, r.n
        assert len(r.rule) > 20, r.n
        assert r.approval_row in ROW_FOR[r.outcome], r.n


def test_paid_rows_carry_their_amount_and_nothing_else_does():
    paid = {r.n: r.paid for r in ROWS if r.paid is not None}
    assert paid == {
        1: Decimal("5000.00"),
        4: Decimal("1890.00"),
        7: Decimal("2750.00"),
        12: Decimal("3000.00"),
        14: Decimal("9975.00"),
        19: Decimal("6500.00"),
    }
    assert set(paid) == set(of(Final.PAID))


def test_duplicates_and_supersession_link_rows_of_one_identity():
    by_n = {r.n: r for r in ROWS}
    for r in ROWS:
        for target in (r.duplicate_of, r.superseded_by):
            if target is not None:
                assert (by_n[target].vendor, by_n[target].number) == (r.vendor, r.number)
    assert {r.n: r.duplicate_of for r in ROWS if r.duplicate_of} == {13: 12, 15: 14}
    assert all(by_n[r.duplicate_of].outcome is Final.PAID for r in ROWS if r.duplicate_of)
    assert {r.n: r.superseded_by for r in ROWS if r.superseded_by} == {16: 17}


def test_staged_ownership_follows_the_from_column():
    assert [r.n for r in rows_for("1a")] == [1, 2, 3, 4, 6, 9, 10, 11, 12, 13, 14, 15, 20]
    assert [r.n for r in rows_for("1b")] == [7, 8, 19]
    assert [r.n for r in rows_for("3")] == [5, 16, 17, 18]
    for r in ROWS:
        csv_or_xml = r.arrival.endswith((".csv", ".xml"))
        assert not (csv_or_xml and r.from_slice == "1a"), r.n


def test_clean_rows_name_no_findings_and_the_pinned_codes_are_unique_facts():
    assert [r.n for r in ROWS if r.clean] == [1, 4, 7, 12, 14, 19]
    assert all(not r.codes for r in ROWS if r.clean)
    by_n = {r.n: {c.value for c in r.codes} for r in ROWS}
    assert by_n[2] == {"STOCK_SHORTAGE"} and by_n[6] == {"STOCK_SHORTAGE"}
    assert by_n[3] == {"ITEM_ZERO_STOCK", "VENDOR_BLOCKED"}
    assert by_n[9] == {"ITEM_UNKNOWN", "VENDOR_UNKNOWN"} and by_n[20] == {"ITEM_UNKNOWN"}
    assert by_n[10] == {"QUANTITY_INVALID", "PARTIAL_IDENTITY"}
    assert by_n[11] == {"PRICE_DEVIATION"}
