#!/usr/bin/env python3
"""Run the loopback gateway. Supported entry point: python server.py"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from nexus.app import app, main  # noqa: E402

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()
