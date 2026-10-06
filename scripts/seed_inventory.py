"""Explicitly reset the inventory database to the pinned seed data.

Only the inventory database is touched; the payment Ledger is a separate file.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_pipeline import catalog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=catalog.DEFAULT_INVENTORY_PATH,
        help="inventory database to reset (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    catalog.seed(args.inventory, reset=True)
    print(f"inventory reset to seed data: {args.inventory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
