"""Entry point: `python main.py --invoice_path=<file|dir>`, `python main.py review --list`,
or `python main.py tui` (needs the optional `tui` extra)."""

import sys

from invoice_pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main())
