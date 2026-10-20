"""``python -m synthetic_lab`` entry point (run with PYTHONPATH set to
``synthetic-lab/`` from the repo root)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
