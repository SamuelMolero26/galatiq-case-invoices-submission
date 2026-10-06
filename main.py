"""Entry point: `python main.py --invoice_path=<file|dir>` or `python main.py review --list`."""

import sys

from invoice_pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main())
