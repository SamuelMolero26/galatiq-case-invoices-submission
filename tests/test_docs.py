from pathlib import Path

ADR_DIR = Path(__file__).parent.parent / "docs" / "adr"


def test_adr_0003_is_not_created_in_slice_one():
    # ADR copies are owned by the repo owner and land in a separate commit;
    # this slice only asserts ADR-0003 is not introduced here.
    assert not list(ADR_DIR.glob("0003-*"))
